# mcp_server/agents/erp_agent.py
"""
ERP Agent — Closed Won → Odoo Sales Order 자동 생성 전담
─────────────────────────────────────────────────────────────────
설계 (v2 refactor, 2026-05-18):
  · Odoo 연결/호출 로직은 services/odoo_service.py 로 이관됨.
  · MCP 노출 read-only 도구는 tools/erp_tools.py.
  · 이 파일은 ontology dispatch 가 호출하는 policy-driven action 만 보유.

호출 흐름:
  ontology rule (opp_won_vip / opp_won_standard)
    └─> erp_agent.create_sales_order(policy, context)        ← 이 파일
         └─> odoo_service.find_or_create_partner(...)        ← services/
              └─> XML-RPC (asyncio.to_thread 로 래핑)

⚠️ 이전 버전과의 차이:
  · _odoo_session_cache, _odoo_connect, _odoo_call, _find_or_create_partner,
    _find_or_create_product 가 odoo_service 로 이동.
  · 모든 동기 XML-RPC 호출은 asyncio.to_thread 로 래핑 — event loop blocking 해소.
  · API key 하드코딩 default 제거 — 환경변수 없으면 plan-only 모드.
"""
import sys
import asyncio
import logging
from typing import Dict, Any, Optional

from .base_agent import BaseAgent
from ..services import odoo_service

logger = logging.getLogger(__name__)


class ERPAgent(BaseAgent):
    """BC2 Closed Won → Odoo Sales Order 자동 생성 전담 Agent (정책 액션 only)"""

    def __init__(self, llm_config: dict, service_manager=None):
        super().__init__(
            name="ERP Agent",
            description=(
                "Odoo ERP 의 Sales Order / Partner / Product 를 전담합니다. "
                "BC2 Closed Won 분기 (rule: opp_won_vip/opp_won_standard) 에서 "
                "ontology dispatch 로 호출되어 SFDC Opportunity 의 정보를 Odoo SO 로 "
                "자동 생성합니다. 멱등성 보장 — client_order_ref 로 중복 push 차단."
            ),
            llm_config=llm_config,
        )
        self.service_manager = service_manager

    # ═══════════════════════════════════════════════════════════════
    # Tool & Action registration
    # ═══════════════════════════════════════════════════════════════
    def register_tools_from_services(self, user_id: str = None):
        """
        agent 의 내부 tool 은 LLM(think) 이 호출하는 도구만 등록.
        MCP 표면에 노출되는 도구는 tools/erp_tools.py 에서 별도 등록한다.
        """

        async def get_odoo_status() -> Dict[str, Any]:
            """Odoo 연결 상태 확인 (agent 내부 think() 용)"""
            return await asyncio.to_thread(lambda: (
                odoo_service.is_available(),
                odoo_service.get_service_status(),
            )[1])

        async def find_existing_sales_order(opp_name: str) -> Optional[Dict]:
            """SFDC Opp 이름으로 기존 Odoo SO 조회 (멱등성 체크)"""
            try:
                return await asyncio.to_thread(
                    odoo_service.find_existing_sales_order, opp_name
                )
            except RuntimeError:
                return None

        self.register_tool(
            'get_odoo_status', get_odoo_status,
            'Odoo 연결 상태를 확인합니다'
        )
        self.register_tool(
            'find_existing_sales_order', find_existing_sales_order,
            'SFDC Opp 이름으로 Odoo 에 이미 push 된 SO 가 있는지 조회 (opp_name)'
        )

        # ─── Policy-driven actions (Ontology dispatch 용) ───
        self._register_policy_actions(user_id)

        print(
            f"[ERP Agent] {len(self._tools)} tools, "
            f"{len(self._action_handlers)} actions registered for user: {user_id}",
            file=sys.stderr,
        )

    # ═══════════════════════════════════════════════════════════════
    # Policy-driven Actions (Ontology dispatch 용)
    # ═══════════════════════════════════════════════════════════════
    def _register_policy_actions(self, user_id: str = None):
        """ontology.yaml 의 delegate_to 가 호출하는 정책 기반 액션."""

        # ─────────────────────────────────────────────────────────
        # create_sales_order — Closed Won → Odoo SO 자동 생성 (멱등)
        # Type 1: Pure code — LLM 0회.
        # rule: opp_won_vip / opp_won_standard (BC2)
        # ─────────────────────────────────────────────────────────
        async def create_sales_order(policy: dict, context: dict) -> dict:
            opp = context.get("opportunity") or {}
            tier = policy.get("tier") or opp.get("tier") or "Standard"
            account_name = (
                opp.get("account_name")
                or context.get("account_name")
                or (context.get("customer") or {}).get("name")
                or ""
            )
            opp_name = opp.get("name") or f"{account_name} - Module X"
            amount = opp.get("amount")

            if not account_name:
                return {
                    "action": "create_sales_order",
                    "success": False,
                    "error": "account_name 누락 — Closed Won context 에 Account 정보 없음",
                }
            if amount is None:
                amount = float(policy.get("default_amount", 500))

            # ─ Odoo 미설정 환경 — plan-only 반환 (정책 분기 검증용)
            available = await asyncio.to_thread(odoo_service.is_available)
            if not available:
                status = await asyncio.to_thread(odoo_service.get_service_status)
                return {
                    "action": "create_sales_order",
                    "success": False,
                    "error": f"Odoo 미사용 가능: {status.get('reason', 'unknown')}",
                    "intended_plan": {
                        "operation": "create_odoo_sales_order",
                        "tier": tier,
                        "account_name": account_name,
                        "opp_name": opp_name,
                        "amount": amount,
                        "target_state": policy.get("target_state", "sale"),
                    },
                    "note": "Odoo 미연결 — 정책 분기 검증용 plan 만 반환",
                }

            try:
                # 1) 멱등성 체크
                existing = await asyncio.to_thread(
                    odoo_service.find_existing_sales_order, opp_name
                )
                if existing:
                    return {
                        "action": "create_sales_order",
                        "success": True,
                        "skipped": True,
                        "reason": "이미 Odoo 에 동일 client_order_ref 의 SO 존재 (멱등성)",
                        "existing_order": existing,
                    }

                # 2) Partner 준비
                partner_id = await asyncio.to_thread(
                    odoo_service.find_or_create_partner,
                    account_name, tier,
                    f"BC2 ontology dispatch 자동 생성",
                )
                if not partner_id:
                    return {
                        "action": "create_sales_order",
                        "success": False,
                        "error": "Partner 생성/조회 실패",
                    }

                # 3) Product / order_line 준비
                payload_products = (context.get("payload") or {}).get("products") or []
                if not payload_products and policy.get("product_type_override_from_payload"):
                    payload_products = (opp.get("products") or [])
                default_product_type = policy.get("default_product_type", "service")

                order_lines = []
                product_summary = []

                if payload_products:
                    for p in payload_products:
                        p_name = p.get("name") or "Module X"
                        p_type = p.get("type") or default_product_type
                        p_price = float(p.get("price", amount or 500))
                        p_qty = float(p.get("qty", 1))
                        product_id = await asyncio.to_thread(
                            odoo_service.find_or_create_product,
                            p_name, p_price, p_type,
                        )
                        order_lines.append((0, 0, {
                            "product_id": product_id,
                            "product_uom_qty": p_qty,
                            "price_unit": p_price,
                            "name": p_name,
                        }))
                        product_summary.append({
                            "name": p_name, "type": p_type,
                            "qty": p_qty, "price": p_price,
                        })
                else:
                    product_id = await asyncio.to_thread(
                        odoo_service.find_or_create_product,
                        "Module X", 500, default_product_type,
                    )
                    order_lines.append((0, 0, {
                        "product_id": product_id,
                        "product_uom_qty": 1,
                        "price_unit": amount,
                        "name": opp_name,
                    }))
                    product_summary.append({
                        "name": "Module X", "type": default_product_type,
                        "qty": 1, "price": amount,
                    })

                # 4) Currency 결정
                currency_name = (
                    (context.get("payload") or {}).get("currency")
                    or policy.get("currency")
                    or "USD"
                )
                currency_id = await asyncio.to_thread(
                    odoo_service.find_currency_id, currency_name
                )

                # 5) Sales Order 생성 + (옵션) 확정
                note = (
                    f"BC2 ontology dispatch 자동 생성 (tier: {tier}, "
                    f"product_types: {[p['type'] for p in product_summary]})"
                )
                confirm = (
                    policy.get("confirm_immediately", True)
                    and policy.get("target_state", "sale") == "sale"
                )

                so_result = await asyncio.to_thread(
                    odoo_service.create_sales_order,
                    partner_id, order_lines, opp_name, note, currency_id, confirm,
                )

                return {
                    "action": "create_sales_order",
                    "success": True,
                    "order_id": so_result["order_id"],
                    "order": so_result["order"],
                    "url": so_result["url"],
                    "products": product_summary,
                    "policy_applied": {
                        "tier": tier,
                        "target_state": policy.get("target_state", "sale"),
                        "confirm_immediately": policy.get("confirm_immediately", True),
                        "default_product_type": default_product_type,
                        "product_type_override_from_payload":
                            policy.get("product_type_override_from_payload", False),
                        "currency": currency_name,
                    },
                    "note": (
                        "BC3 (재고/배송) 의 진입점 — 이 SO 가 Confirmed 되면 "
                        "다음 분기 (Delivery Order, VIP 우선 할당) 가 활성화됨. "
                        "storable type 라인이 들어가면 자동 Delivery Order 생성됨."
                    ),
                }
            except RuntimeError as e:
                return {
                    "action": "create_sales_order",
                    "success": False,
                    "error": f"Odoo service error: {e}",
                }
            except Exception as e:
                logger.exception(f"[erp_agent.create_sales_order] 예상치 못한 오류")
                return {
                    "action": "create_sales_order",
                    "success": False,
                    "error": str(e),
                }

        self.register_action(
            'create_sales_order', create_sales_order,
            'BC2: Closed Won → Odoo Sales Order 자동 생성 (멱등성 보장, client_order_ref 로 중복 차단)'
        )
