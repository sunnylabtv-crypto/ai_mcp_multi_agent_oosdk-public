# mcp_server/agents/inventory_agent.py
"""
Inventory Agent — BC3: SO Confirmed → Allocation → Shipping 전담
─────────────────────────────────────────────────────────────────
설계 (2026-05-19):
  · Odoo 의 stock.picking / stock.move / stock.quant 를 다루는 "정책 어댑터".
  · ERP (Odoo) 가 transaction (실제 수량 차감, picking validate) 을 한다.
    그 위에 ontology 정책 (VIP 선점, target_delivery 매칭) 이 얹힌다.
  · Odoo 미연결 환경에서도 plan-only 모드로 흐름 검증 가능.

핵심 액션 (rule 매핑):
  rule                                    action
  ───────────────────────────────────────────────────────────────
  order_split_by_line_type        →  split_fulfillment_path
  inventory_allocate_vip_preempt  →  allocate_with_preemption
  inventory_allocate_standard     →  allocate_fifo
  stock_received_replenish        →  replenish_priority_queue
  delivery_ready_to_ship_vip      →  dispatch_shipment

선점 모드 (soft preempt — 옵션 1 채택):
  · Odoo stock.move.state 가 "assigned" 인 Standard 라인만 회수 가능.
  · "partially_available" / "done" 은 회수 안 함 (현장 picking 시작/완료).
  · 회수 후 VIP 가 reserve, Standard 는 Waiting 큐로 강등.

소비할 incoming 매칭:
  · target_delivery_date 까지 도착 예정 PO 만 매칭 (get_pending_receipts).
  · 부족분이 incoming 으로 메워지면 "Reserved_Against_Incoming" 상태로 표시.

플로우 (예: VIP SO confirmed):
  ┌─ split_fulfillment_path
  │     · service 라인 → email_agent.send_license_activation (별도 spawn)
  │     · storable 라인 → list_pickings_for_order → spawn delivery_ready_check
  ├─ allocate_with_preemption (or _standard, depending on tier)
  │     · 가용재고 부족 시 Standard 'assigned' move 회수 → VIP 에 reserve
  ├─ replenish_priority_queue (입고 발생 시)
  │     · 미할당 큐를 tier+date 로 정렬해 reserve_move 호출
  └─ dispatch_shipment
        · 전 라인 reserved → validate_picking → state 'done'
"""
import sys
import json
import math
import asyncio
import logging
import xmlrpc.client
from typing import Dict, Any, List, Optional, Tuple, Type

from .base_agent import BaseAgent
from ..services import odoo_service

logger = logging.getLogger(__name__)

# BC3 MED #M3 — odoo_service 호출에서 의도적으로 잡는 예외들.
#   · xmlrpc.client.Fault     : Odoo 가 보낸 RPC fault (도메인/권한/제약 오류).
#   · xmlrpc.client.ProtocolError : HTTP layer.
#   · ConnectionError / OSError   : 네트워크, DNS.
#   · TimeoutError                : socket timeout.
#   · RuntimeError                : odoo_service 가 reraise 하는 인증 실패 등.
# 잡은 예외 클래스명을 로그에 함께 남겨 추후 silent fail 추적 용이.
_ODOO_RPC_ERRORS: Tuple[Type[BaseException], ...] = (
    xmlrpc.client.Fault,
    xmlrpc.client.ProtocolError,
    ConnectionError,
    TimeoutError,
    OSError,
    RuntimeError,
)


class InventoryAgent(BaseAgent):
    """BC3 — Inventory Allocation / Shipping 전담 Agent (정책 액션 only)."""

    def __init__(self, llm_config: dict, service_manager=None):
        super().__init__(
            name="Inventory Agent",
            description=(
                "Odoo ERP 의 stock.picking / stock.move / stock.quant 를 전담합니다. "
                "BC3 SO Confirmed → Allocation → Shipping 분기에서 ontology dispatch 로 "
                "호출됩니다. VIP 우선 정책 (soft preempt), 입고 시 backorder 우선 충족, "
                "target_delivery_date 매칭을 정책 레이어에서 표현. 실제 차감/검증은 Odoo 위임."
            ),
            llm_config=llm_config,
        )
        self.service_manager = service_manager

    # ═══════════════════════════════════════════════════════════════
    # Tool & Action registration
    # ═══════════════════════════════════════════════════════════════
    def register_tools_from_services(self, user_id: str = None):
        """
        Agent 의 내부 tool 은 LLM(think) 이 호출하는 도구만 등록.
        MCP 표면에 노출되는 read-only 조회 도구는 추후 tools/inventory_tools.py 분리.
        """

        async def get_inventory_state(product_id: int) -> Dict[str, Any]:
            """제품의 현재 가용재고 상태 조회"""
            try:
                return await asyncio.to_thread(odoo_service.get_inventory_state, product_id)
            except RuntimeError as e:
                return {"product_id": product_id, "error": str(e)}

        async def get_picking_status(picking_id: int) -> Dict[str, Any]:
            """단일 picking 의 현재 상태 + 라인별 reservation 조회"""
            try:
                picking = await asyncio.to_thread(odoo_service.get_picking, picking_id)
                moves = await asyncio.to_thread(odoo_service.get_picking_moves, picking_id)
                return {"picking": picking, "moves": moves}
            except RuntimeError as e:
                return {"picking_id": picking_id, "error": str(e)}

        self.register_tool(
            'get_inventory_state', get_inventory_state,
            '제품의 현재 가용재고 (on_hand / reserved / available) 조회'
        )
        self.register_tool(
            'get_picking_status', get_picking_status,
            'Delivery Order (stock.picking) 의 상태와 라인별 reservation 조회'
        )

        self._register_policy_actions(user_id)

        print(
            f"[Inventory Agent] {len(self._tools)} tools, "
            f"{len(self._action_handlers)} actions registered for user: {user_id}",
            file=sys.stderr,
        )

    # ═══════════════════════════════════════════════════════════════
    # Helpers — Odoo 미사용 시 plan-only 모드 분기
    # ═══════════════════════════════════════════════════════════════
    @staticmethod
    async def _odoo_available() -> bool:
        return await asyncio.to_thread(odoo_service.is_available)

    @staticmethod
    async def _odoo_status() -> Dict[str, Any]:
        return await asyncio.to_thread(odoo_service.get_service_status)

    @staticmethod
    def _tier_priority(tier: str, table: Dict[str, int]) -> int:
        """tier_priority 정책 표에서 점수 lookup (기본값 0)."""
        return int(table.get(tier or "Standard", 0))

    # ─── BC4 S0: Priority Override (Case A) helpers ───
    # 결정론적 override. LLM 미개입 — 사람이 명시한 so_ids 의 정렬 _score 만 boost.
    @staticmethod
    def _resolve_override(policy: Optional[Dict], context: Optional[Dict]) -> Optional[Dict]:
        """런타임 override 객체 해석 + 만료(auto_expire_hours) 검사.

        우선순위: policy["priority_override_runtime"] (server dispatch 가 주입) →
        context["receipt"]["priority_override"] (payload 통과분, 폴백).
        만료됐거나 so_ids 없으면 None → 정렬 boost 미적용(= 기존 동작).
        """
        ov = (policy or {}).get("priority_override_runtime")
        if not ov:
            rec = (context or {}).get("receipt") or {}
            ov = rec.get("priority_override") if isinstance(rec, dict) else None
        if not ov or not ov.get("so_ids"):
            return None
        exp = ov.get("expires_at")
        if exp:
            # 동일 포맷(UTC isoformat + 'Z') 문자열 비교 — 소비 시점 soft-gate.
            from datetime import datetime as _dt
            if (_dt.utcnow().isoformat() + "Z") > exp:
                return None
        return ov

    @staticmethod
    def _apply_override_score(base: int, override: Dict, tier_table: Dict[str, int]) -> int:
        """override SO 의 정렬 점수 계산.

        · equal_vip (기본): VIP 와 동급. 단 base 가 이미 더 높으면(예: 그 SO 가 VIP)
          그대로 유지 → max(base, VIP).  VIP 를 뒤로 밀지 않음.
        · above_vip: VIP 점수 + boost_score → 모든 VIP 보다 앞.
        """
        cfg = (override or {}).get("cfg") or {}
        mode = cfg.get("mode", "equal_vip")
        vip = int((tier_table or {}).get("VIP", 100))
        if mode == "above_vip":
            return vip + int(cfg.get("boost_score", 1000))
        return max(int(base or 0), vip)

    # ─── BC4 S1: Partial Shipment Advisor (지능 부분) ───
    # "가용분 먼저 부분출하(split) vs 전량 채워질 때까지 대기(wait)" — 정답 없는 판단.
    # LLM 이 추천만 하고, 실제 출하 실행은 결정론 코드(confirm tool)가 한다.
    # 안전: 모든 LLM 실패(타임아웃/파싱오류/키없음/invalid)는 rule_baseline 으로 폴백.
    @staticmethod
    def _parse_advisor_json(raw: Optional[str]) -> Dict[str, Any]:
        """LLM 응답에서 JSON 추출 (base_agent.think 의 코드펜스 스트립 패턴 재사용)."""
        if not raw:
            raise ValueError("advisor LLM 응답 없음(None)")
        cleaned = raw.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        return json.loads(cleaned.strip())

    async def _partial_shipment_advisor(
        self, policy: dict, context: dict, shortage: dict,
    ) -> Dict[str, Any]:
        """split / wait 추천. 실패 시 rule_baseline 폴백 (fail-safe)."""
        picking = context.get("picking") or {}
        rule_default = policy.get("rule_baseline", "split")   # API 실패 시 추천

        # 현재 delivery_ready_check 컨텍스트가 제공하는 사실만 LLM 에 전달.
        # (incoming_eta / 고객 이력은 아직 link enrichment 가 없어 제외 — 추후 확장 시 추가)
        facts = {
            "tier": picking.get("tier"),
            "demand": shortage.get("demand"),
            "reserved": shortage.get("reserved"),
            "shortage": shortage.get("shortage"),
            "target_delivery_date": picking.get("scheduled_date"),
        }
        system = (
            "You are a fulfillment advisor for a B2B warehouse. Given a partially "
            "reservable order, recommend exactly one of: 'split' (ship the available "
            "quantity now and backorder the rest) or 'wait' (hold the whole order until "
            "fully reservable). Weigh customer tier, shortage size, delivery urgency, and "
            "incoming ETA. You ADVISE ONLY — a human approves before anything ships. "
            'Respond with ONLY a JSON object: '
            '{"recommendation":"split|wait","rationale":"<1-2 short sentences>",'
            '"confidence":<0.0-1.0>}'
        )
        user = json.dumps(facts, ensure_ascii=False)
        try:
            from ..services.openai_service import generate_text_with_system
            raw = await asyncio.to_thread(
                generate_text_with_system,
                system_prompt=system, user_prompt=user,
                temperature=0.0, max_tokens=300,    # temp=0 → 재현성 최대
            )
            advice = self._parse_advisor_json(raw)
            rec = advice.get("recommendation")
            if rec not in ("split", "wait"):
                raise ValueError(f"invalid recommendation: {rec!r}")
            return {
                "recommendation": rec,
                "rationale": str(advice.get("rationale", ""))[:400],
                "confidence": advice.get("confidence"),
                "source": "llm",
            }
        except Exception as e:
            logger.warning(
                f"[partial_advisor] LLM 실패 → rule baseline({rule_default}) 폴백: {e}"
            )
            return {
                "recommendation": rule_default,
                "rationale": "LLM 미가용 — 정책 baseline 으로 안전 폴백",
                "confidence": None,
                "source": "fallback_rule",
            }

    # ─── BC5: Replenishment Qty Advisor (지능 부분 ①) ───
    # "충족 불가 shortage 가 났는데, 얼마나 발주할까?" — 정답 없는 판단.
    #   · 부족분만? 안전재고까지? 대기 backorder 합산 수요까지?
    # LLM 이 recommended_qty + urgency 를 추천. 실제 발주(picking 생성)는 결정론 코드.
    # 안전: 모든 LLM 실패는 rule 폴백(shortage + safety_buffer).
    async def _replenishment_qty_advisor(
        self, policy: dict, shortage: dict,
    ) -> Dict[str, Any]:
        """발주 수량/긴급도 추천. 실패 시 rule 폴백 (fail-safe)."""
        unmet = float(shortage.get("unmet_qty") or shortage.get("total_shortage") or 0)
        safety_buffer = float(policy.get("safety_buffer_units", 0) or 0)
        # rule baseline — 부족분 + 안전버퍼 (LLM 실패 시 이 값 사용).
        # ceil 사용 — 부족분을 반드시 "덮어야" 하므로 절대 내림하지 않는다(분수 UoM 안전).
        rule_qty = int(math.ceil(unmet + safety_buffer)) if unmet > 0 else 0

        blocked = shortage.get("blocked_orders") or []
        vip_blocked = [b for b in blocked if b.get("tier") == "VIP"]
        facts = {
            "product_name": shortage.get("product_name"),
            "available": shortage.get("available"),
            "incoming_already_on_the_way": shortage.get("incoming"),
            "total_unmet_demand": unmet,
            "blocked_order_count": len(blocked),
            "vip_blocked_count": len(vip_blocked),
            "blocked_orders": [
                {"tier": b.get("tier"), "shortage": b.get("shortage"),
                 "account": b.get("account_name")}
                for b in blocked[:8]
            ],
            "lead_time_days": policy.get("lead_time_days", 3),
            "safety_buffer_units_hint": safety_buffer,
        }
        system = (
            "You are a B2B inventory replenishment planner. An order cannot be "
            "fulfilled because stock is depleted and there is nothing to re-allocate. "
            "Given the unmet demand, blocked orders (with customer tier), incoming "
            "stock already on the way, lead time, and a safety-buffer hint, recommend "
            "HOW MANY UNITS to order now and the urgency. Cover the unmet demand at "
            "minimum; you may add a reasonable safety buffer, but do not over-order "
            "wildly. You ADVISE ONLY — a purchasing manager reviews before anything "
            "is committed. Respond with ONLY a JSON object: "
            '{"recommended_qty":<integer>,"urgency":"HIGH|MEDIUM|LOW",'
            '"rationale":"<1-2 short sentences>","confidence":<0.0-1.0>}'
        )
        user = json.dumps(facts, ensure_ascii=False)
        try:
            from ..services.openai_service import generate_text_with_system
            raw = await asyncio.to_thread(
                generate_text_with_system,
                system_prompt=system, user_prompt=user,
                temperature=0.0, max_tokens=300,
            )
            advice = self._parse_advisor_json(raw)
            qty = int(advice.get("recommended_qty"))
            if qty <= 0:
                raise ValueError(f"invalid recommended_qty: {qty!r}")
            urgency = str(advice.get("urgency", "MEDIUM")).upper()
            if urgency not in ("HIGH", "MEDIUM", "LOW"):
                urgency = "MEDIUM"
            # 안전 가드: 미충족분은 반드시 덮도록 하한 보정 (ceil — 내림 금지)
            if unmet > 0 and qty < unmet:
                qty = int(math.ceil(unmet))
            return {
                "recommended_qty": qty,
                "urgency": urgency,
                "rationale": str(advice.get("rationale", ""))[:400],
                "confidence": advice.get("confidence"),
                "source": "llm",
            }
        except Exception as e:
            logger.warning(
                f"[replenish_advisor] LLM 실패 → rule baseline(qty={rule_qty}) 폴백: {e}"
            )
            # rule 폴백 긴급도: VIP 블록 있으면 HIGH, 아니면 MEDIUM
            return {
                "recommended_qty": rule_qty,
                "urgency": "HIGH" if vip_blocked else "MEDIUM",
                "rationale": "LLM 미가용 — 부족분 + 안전버퍼로 안전 폴백",
                "confidence": None,
                "source": "fallback_rule",
            }

    # ═══════════════════════════════════════════════════════════════
    # Policy Actions
    # ═══════════════════════════════════════════════════════════════
    def _register_policy_actions(self, user_id: str = None):
        """ontology.yaml 의 delegate_to 가 호출하는 정책 기반 액션."""

        # ─────────────────────────────────────────────────────────
        # split_fulfillment_path — SO confirmed → 라인 type 분기
        # Type 1: Pure code, LLM 0회.
        # rule: order_split_by_line_type (420)
        # policy: {service_path: {...}, storable_path: {spawn_event, tier_priority_score, ...}}
        # context: {sales_order, payload}
        #
        # Returns:
        #   {
        #     service_plan: [...],          # email_agent / calendar_agent 후속용 (demo가 fanout)
        #     storable_pickings: [...],     # list_pickings_for_order 결과
        #     spawn_events: [               # demo / dispatcher 가 re-feed 할 이벤트
        #       {"entity": "delivery_ready_check", "payload": {...}}, ...
        #     ],
        #   }
        # ─────────────────────────────────────────────────────────
        async def split_fulfillment_path(policy: dict, context: dict) -> dict:
            so = context.get("sales_order") or {}
            so_id = so.get("id")
            tier = so.get("tier") or context.get("tier") or "Standard"
            has_storable = bool(so.get("has_storable_lines"))
            has_service = bool(so.get("has_service_lines"))

            service_policy = policy.get("service_path", {}) or {}
            storable_policy = policy.get("storable_path", {}) or {}

            service_plan: List[Dict[str, Any]] = []
            storable_pickings: List[Dict[str, Any]] = []
            spawn_events: List[Dict[str, Any]] = []

            # ─── service 라인 처리 (라이선스 자동 활성화 + VIP kickoff) ───
            if has_service:
                consulting_tiers = service_policy.get("consulting_kickoff_for_tier", []) or []
                activation_mode = service_policy.get("activation", "license_auto")

                service_plan.append({
                    "agent": "email_agent",
                    "action": "send_license_activation",
                    "policy": {
                        "tone": "premium" if tier == "VIP" else "professional",
                        "template": "license_ready",
                        "activation_mode": activation_mode,
                        "sla_hours": 2 if tier == "VIP" else 24,
                    },
                })
                if tier in consulting_tiers:
                    service_plan.append({
                        "agent": "calendar_agent",
                        "action": "book_kickoff_meeting",
                        "policy": {
                            "sla_hours": 48,
                            "duration_min": 60,
                            "priority": "high",
                            "attendees_role": ["account_owner", "consulting_lead"],
                            "kickoff_kind": "consulting",
                        },
                    })

            # ─── storable 라인 처리 (Delivery Order 조회 후 spawn) ───
            if has_storable:
                tier_score_table = storable_policy.get("tier_priority_score", {}) or {}
                priority_score = self._tier_priority(tier, tier_score_table)

                available = await self._odoo_available()
                if available and so_id:
                    try:
                        pickings = await asyncio.to_thread(
                            odoo_service.list_pickings_for_order, so_id
                        )
                    except _ODOO_RPC_ERRORS as e:
                        logger.warning(
                            f"[split_fulfillment_path] list_pickings 실패 "
                            f"({type(e).__name__}): {e}"
                        )
                        pickings = []
                else:
                    pickings = []

                for p in pickings:
                    storable_pickings.append({
                        "id": p.get("id"),
                        "name": p.get("name"),
                        "state": p.get("state"),
                        "scheduled_date": p.get("scheduled_date"),
                    })
                    spawn_events.append({
                        "entity": "delivery_ready_check",
                        "payload": {
                            "picking": {
                                "id": p.get("id"),
                                "name": p.get("name"),
                                "state": p.get("state"),
                                "scheduled_date": p.get("scheduled_date"),
                                "sale_order_id": so_id,
                                "tier": tier,
                                "priority_score": priority_score,
                            },
                            "account_name": so.get("account_name"),
                        },
                    })

                # Odoo 없는 plan-only 모드 — picking 가공 정보 없이도 일단 spawn
                if not pickings:
                    spawn_events.append({
                        "entity": "delivery_ready_check",
                        "payload": {
                            "picking": {
                                "id": None,
                                "name": f"WH/OUT/(planned for {so.get('name')})",
                                "state": "confirmed",
                                "scheduled_date": so.get("target_delivery_date"),
                                "sale_order_id": so_id,
                                "tier": tier,
                                "priority_score": priority_score,
                            },
                            "account_name": so.get("account_name"),
                            "note": "Odoo 미연결 — 합성 picking. 실제 DO 정보는 Odoo confirmed 후 보강.",
                        },
                    })

            return {
                "action": "split_fulfillment_path",
                "success": True,
                "sales_order_id": so_id,
                "tier": tier,
                "service_plan": service_plan,
                "storable_pickings": storable_pickings,
                "spawn_events": spawn_events,
                "policy_applied": {
                    "service_path": service_policy,
                    "storable_path": storable_policy,
                },
                "note": (
                    "service_plan 은 별도 dispatch 가 필요 (fanout). "
                    "spawn_events 는 demo/dispatcher 가 engine 에 re-feed 해 "
                    "후속 allocation rule 을 발화시킴."
                ),
            }

        # ─────────────────────────────────────────────────────────
        # allocate_with_preemption — VIP shortage → Standard 'assigned' 회수
        # Type 1: Pure code. soft preempt mode 만 구현 (옵션 1 선택).
        # rule: inventory_allocate_vip_preempt (410)
        # policy: {tier: VIP, preempt_mode: soft, preempt_target_states,
        #          preempt_exclude_states, backorder_against_incoming, notify_account_owner}
        # context: {picking, inventory}
        # ─────────────────────────────────────────────────────────
        async def allocate_with_preemption(policy: dict, context: dict) -> dict:
            picking = context.get("picking") or {}
            inventory = context.get("inventory") or {}
            picking_id = picking.get("id")
            # BC3 HIGH #5 — 자기 SO 의 다른 picking 을 회수 후보에서 제외하려면
            # SO id 가 필요. picking payload 에 sale_order_id 가 들어오면 그걸,
            # 없으면 context.sales_order.id 를 폴백으로.
            own_sale_order_id = (
                picking.get("sale_order_id")
                or (context.get("sales_order") or {}).get("id")
            )
            tier_policy = policy.get("tier", "VIP")
            preempt_mode = policy.get("preempt_mode", "soft")

            if preempt_mode != "soft":
                return {
                    "action": "allocate_with_preemption",
                    "success": False,
                    "error": f"preempt_mode '{preempt_mode}' is not implemented "
                             f"(현재 정책: soft only — 옵션 1)",
                }

            # Odoo 미연결: plan-only
            available = await self._odoo_available()
            if not available:
                status = await self._odoo_status()
                return {
                    "action": "allocate_with_preemption",
                    "success": True,
                    "skipped": True,
                    "reason": f"Odoo 미연결: {status.get('reason', 'unknown')}",
                    "intended_plan": {
                        "operation": "soft_preempt_standard_assigned",
                        "tier": tier_policy,
                        "picking_id": picking_id,
                        "demand": picking.get("qty_demand"),
                        "available": inventory.get("available"),
                        "incoming": inventory.get("incoming"),
                        "would_unreserve": "Standard moves with state='assigned' (not partially_available/done)",
                        "would_reserve_for_vip": True,
                        "would_backorder_against_incoming": policy.get("backorder_against_incoming", True),
                    },
                    "policy_applied": policy,
                    "note": "Odoo 미연결 — 정책 분기 검증용 plan 만 반환",
                }

            # 실제 Odoo 작업 — 제품/수량 단위로 처리
            product_id = inventory.get("product_id") or (picking.get("product_id"))
            demand = float(picking.get("qty_demand") or 0)
            available_qty = float(inventory.get("available") or 0)
            shortage = max(0.0, demand - available_qty)

            preempted_moves: List[Dict[str, Any]] = []
            reserved_for_vip: bool = False
            backorder_against_incoming: List[Dict[str, Any]] = []

            if shortage > 0 and product_id:
                target_states = policy.get("preempt_target_states", ["assigned"]) or ["assigned"]
                exclude_states = policy.get("preempt_exclude_states",
                                            ["partially_available", "done"]) or []

                # Standard reservation 후보 검색
                try:
                    candidates = await asyncio.to_thread(
                        odoo_service.list_open_moves_for_product,
                        product_id, target_states,
                    )
                except _ODOO_RPC_ERRORS as e:
                    candidates = []
                    logger.warning(
                        f"[allocate_with_preemption] list_open_moves 실패 "
                        f"({type(e).__name__}): {e}"
                    )

                # 자기 자신 picking 의 move 는 제외 (회수 무의미)
                # BC3 HIGH #5 — picking_id 비교만으론 같은 SO 가 여러 picking 으로
                # split 된 케이스를 못 거른다. odoo_service 가 sale_order_id 를
                # attach 해 줬으면 그것도 함께 제외.
                def _same_self(c: Dict[str, Any]) -> bool:
                    if self._picking_id_of_move(c) == picking_id:
                        return True
                    c_so = c.get("sale_order_id")
                    if (own_sale_order_id is not None
                            and c_so is not None
                            and c_so == own_sale_order_id):
                        return True
                    return False

                candidates = [
                    c for c in candidates
                    if c.get("state") not in exclude_states
                    and not _same_self(c)
                ]

                # 회수 (soft) — shortage 채울 만큼만
                covered = 0.0
                for cand in candidates:
                    move_id = cand.get("id")
                    if not isinstance(move_id, int) or move_id <= 0:
                        # odoo_service 가 normalize 하지만 방어적으로 한 번 더.
                        # picking_id (many2one [id, name]) 로 fallback 하면 안 됨 —
                        # unreserve_move 가 picking 전체를 풀어버릴 위험 (BC3 CRIT #1).
                        logger.warning(
                            "[allocate_with_preemption] candidate missing 'id' — skip: %r",
                            cand,
                        )
                        continue
                    qty = float(cand.get("reserved_availability") or cand.get("product_uom_qty") or 0)
                    try:
                        ok = await asyncio.to_thread(odoo_service.unreserve_move, move_id)
                    except _ODOO_RPC_ERRORS as e:
                        ok = False
                        logger.warning(
                            f"[allocate_with_preemption] unreserve {move_id} 실패 "
                            f"({type(e).__name__}): {e}"
                        )
                    if ok:
                        preempted_moves.append({
                            "move_id": move_id,
                            "picking_id": self._picking_id_of_move(cand),
                            "qty_freed": qty,
                        })
                        covered += qty
                        if covered >= shortage:
                            break

                # VIP move(s) 재할당
                try:
                    vip_moves = await asyncio.to_thread(
                        odoo_service.list_open_moves_for_product,
                        product_id, ["confirmed", "waiting", "partially_available"],
                    )
                    for m in vip_moves:
                        mid = m.get("id")
                        if not isinstance(mid, int) or mid <= 0:
                            continue
                        if self._picking_id_of_move(m) != picking_id:
                            continue
                        await asyncio.to_thread(odoo_service.reserve_move, mid)
                        reserved_for_vip = True
                        break
                except _ODOO_RPC_ERRORS as e:
                    logger.warning(
                        f"[allocate_with_preemption] VIP reserve_move 실패 "
                        f"({type(e).__name__}): {e}"
                    )

                # 여전히 부족하면 incoming 매칭
                still_short = shortage - covered
                if still_short > 0 and policy.get("backorder_against_incoming", True):
                    try:
                        incoming = await asyncio.to_thread(
                            odoo_service.get_pending_receipts,
                            product_id, (picking.get("scheduled_date") or ""),
                        )
                        backorder_against_incoming = [
                            {
                                "move_id": x.get("id"),
                                "qty": x.get("product_uom_qty"),
                                "expected_date": x.get("date"),
                            }
                            for x in incoming
                        ]
                    except _ODOO_RPC_ERRORS as e:
                        logger.warning(
                            f"[allocate_with_preemption] incoming 조회 실패 "
                            f"({type(e).__name__}): {e}"
                        )

            else:
                # shortage 없음 — 그대로 reserve 시도
                if picking_id:
                    try:
                        moves = await asyncio.to_thread(
                            odoo_service.get_picking_moves, picking_id
                        )
                        for m in moves:
                            mid = m.get("id")
                            if isinstance(mid, int) and mid > 0:
                                await asyncio.to_thread(odoo_service.reserve_move, mid)
                                reserved_for_vip = True
                    except _ODOO_RPC_ERRORS as e:
                        logger.warning(
                            f"[allocate_with_preemption] reserve_move 실패 "
                            f"({type(e).__name__}): {e}"
                        )

            return {
                "action": "allocate_with_preemption",
                "success": True,
                "tier": tier_policy,
                "preempt_mode": "soft",
                "picking_id": picking_id,
                "demand": demand,
                "available_before": available_qty,
                "shortage": shortage,
                "preempted_moves": preempted_moves,
                "reserved_for_vip": reserved_for_vip,
                "backorder_against_incoming": backorder_against_incoming,
                "notify_account_owner": policy.get("notify_account_owner", True),
                "policy_applied": policy,
                "note": (
                    f"Soft preempt: Standard 'assigned' move {len(preempted_moves)}건 회수, "
                    f"VIP 에 재할당. 부족분은 incoming PO 에 backorder."
                ),
            }

        # ─────────────────────────────────────────────────────────
        # allocate_fifo — Standard 단순 할당
        # Type 1: Pure code. 가용재고만 reserve. 부족 시 Waiting.
        # rule: inventory_allocate_standard (390)
        # ─────────────────────────────────────────────────────────
        async def allocate_fifo(policy: dict, context: dict) -> dict:
            picking = context.get("picking") or {}
            picking_id = picking.get("id")

            available = await self._odoo_available()
            if not available:
                status = await self._odoo_status()
                return {
                    "action": "allocate_fifo",
                    "success": True,
                    "skipped": True,
                    "reason": f"Odoo 미연결: {status.get('reason', 'unknown')}",
                    "intended_plan": {
                        "operation": "reserve_available_only",
                        "tier": policy.get("tier", "Standard"),
                        "picking_id": picking_id,
                        "wait_for_incoming": policy.get("wait_for_incoming", True),
                    },
                    "policy_applied": policy,
                    "note": "Odoo 미연결 — plan 만 반환",
                }

            # 실제: picking 의 모든 move 에 _action_assign 시도 → Odoo 가 가용재고 범위 내에서 자동 분배.
            # BC3 HIGH #8: reserve_move 호출 전의 m['reserved_availability'] 를 기준으로
            # 분류하면, 방금 잡힌 reservation 이 stale 로 보여 moves_waiting 으로 오분류된다.
            # → reserve_move 후 해당 move 를 다시 읽어 최신값으로 판정.
            moves_reserved = []
            moves_waiting = []
            try:
                moves = await asyncio.to_thread(odoo_service.get_picking_moves, picking_id) if picking_id else []
                for m in moves:
                    mid = m.get("id")
                    if not isinstance(mid, int) or mid <= 0:
                        continue
                    demand = float(m.get("product_uom_qty") or 0)
                    ok = await asyncio.to_thread(odoo_service.reserve_move, mid)
                    # 최신 reservation 값 재조회 — _action_assign 직후의 실제 상태로 판정.
                    reserved_now = float(m.get("reserved_availability") or 0)
                    if ok:
                        try:
                            fresh = await asyncio.to_thread(
                                odoo_service.get_move, mid,
                            )
                            if isinstance(fresh, dict):
                                reserved_now = float(
                                    fresh.get("reserved_availability") or 0
                                )
                        except _ODOO_RPC_ERRORS as e_fresh:
                            logger.debug(
                                f"[allocate_fifo] get_move({mid}) 재조회 실패 "
                                f"({type(e_fresh).__name__}), stale 값 사용: {e_fresh}"
                            )
                    if ok and reserved_now >= demand and demand > 0:
                        moves_reserved.append(mid)
                    else:
                        moves_waiting.append({
                            "move_id": mid,
                            "demand": demand,
                            "reserved": reserved_now,
                        })
            except _ODOO_RPC_ERRORS as e:
                logger.warning(
                    f"[allocate_fifo] reserve_move loop 실패 "
                    f"({type(e).__name__}): {e}"
                )

            return {
                "action": "allocate_fifo",
                "success": True,
                "tier": policy.get("tier", "Standard"),
                "picking_id": picking_id,
                "moves_reserved": moves_reserved,
                "moves_waiting": moves_waiting,
                "wait_for_incoming": policy.get("wait_for_incoming", True),
                "wait_alert_after_days": policy.get("wait_alert_after_days", 7),
                "policy_applied": policy,
                "note": (
                    f"Standard FIFO: 가용범위 내 {len(moves_reserved)}건 reserved, "
                    f"{len(moves_waiting)}건 Waiting."
                ),
            }

        # ─────────────────────────────────────────────────────────
        # replenish_priority_queue — 입고 발생 → VIP backorder 우선 채움
        # Type 1: Pure code. tier+date 정렬 후 reserve_move 시퀀스 호출.
        # rule: stock_received_replenish (400)
        # policy: {ordering, tier_priority, consume_all_for_vip_first, unblock_waiting_pickings}
        # context: {receipt: {product_id, qty, ...}}
        # ─────────────────────────────────────────────────────────
        async def replenish_priority_queue(policy: dict, context: dict) -> dict:
            receipt = context.get("receipt") or {}
            product_id = receipt.get("product_id")
            received_qty = float(receipt.get("qty") or 0)
            if not product_id or received_qty <= 0:
                return {
                    "action": "replenish_priority_queue",
                    "success": False,
                    "error": "receipt.product_id 또는 receipt.qty 누락",
                    "receipt": receipt,
                }

            available = await self._odoo_available()
            if not available:
                status = await self._odoo_status()
                return {
                    "action": "replenish_priority_queue",
                    "success": True,
                    "skipped": True,
                    "reason": f"Odoo 미연결: {status.get('reason', 'unknown')}",
                    "intended_plan": {
                        "operation": "replenish_in_priority_order",
                        "product_id": product_id,
                        "received_qty": received_qty,
                        "ordering": policy.get("ordering", ["tier", "target_delivery_date", "sale_order_id"]),
                        "consume_all_for_vip_first": policy.get("consume_all_for_vip_first", True),
                    },
                    "policy_applied": policy,
                    "note": "Odoo 미연결 — plan 만 반환",
                }

            # 대기 큐 조회 (waiting + confirmed + partially_available)
            try:
                queue = await asyncio.to_thread(
                    odoo_service.list_open_moves_for_product,
                    product_id, ["waiting", "confirmed", "partially_available"],
                )
            except _ODOO_RPC_ERRORS as e:
                queue = []
                logger.warning(
                    f"[replenish] list_open_moves 실패 ({type(e).__name__}): {e}"
                )

            # tier 결정 — BC3 MED #M1 봉합:
            # 이전엔 origin 문자열에 "VIP" 가 있으면 VIP 로 보는 휴리스틱이었다.
            # 정책이 raw 데이터 모양에 의존 (yaml 추상화 새는 중) → SO → partner.category
            # 를 한 번 묶어 조회한 결과로 정확히 매핑.
            #   1) context.tier_lookup (dispatcher 가 미리 attach) 가 있으면 우선.
            #   2) 없으면 sale_order_id 모아서 get_sale_order_tier_map 으로 일괄 조회.
            #   3) 그래도 매칭 안 되면 Standard default.
            tier_table = policy.get("tier_priority", {}) or {}
            tier_lookup = context.get("tier_lookup") or {}

            sos_to_lookup = []
            for m in queue:
                so_id = m.get("sale_order_id")
                if (so_id
                        and so_id not in tier_lookup
                        and so_id not in sos_to_lookup):
                    sos_to_lookup.append(so_id)
            if sos_to_lookup:
                try:
                    fetched = await asyncio.to_thread(
                        odoo_service.get_sale_order_tier_map, sos_to_lookup
                    )
                    if isinstance(fetched, dict):
                        tier_lookup = {**tier_lookup, **fetched}
                except _ODOO_RPC_ERRORS as e:
                    logger.warning(
                        f"[replenish] get_sale_order_tier_map 실패 — Standard default 사용 "
                        f"({type(e).__name__}): {e}"
                    )

            # BC4 S0: priority override — 명시된 so_ids 의 _score 만 boost (결정론)
            override = self._resolve_override(policy, context)
            override_ids = set(override["so_ids"]) if override else set()
            override_applied: List[int] = []

            for m in queue:
                so_id = m.get("sale_order_id")
                m["_tier"] = tier_lookup.get(so_id, "Standard") if so_id else "Standard"
                base_score = self._tier_priority(m["_tier"], tier_table)
                if so_id in override_ids:
                    base_score = self._apply_override_score(base_score, override, tier_table)
                    m["_override"] = True
                    if so_id not in override_applied:
                        override_applied.append(so_id)
                m["_score"] = base_score

            queue.sort(key=lambda x: (
                -int(x.get("_score") or 0),                    # tier 점수 내림차순
                x.get("date") or "9999-12-31T23:59:59",        # 가까운 date 우선
                x.get("picking_id") or 0,                      # sale_order_id 대용
            ))

            remaining = received_qty
            replenished: List[Dict[str, Any]] = []
            for m in queue:
                if remaining <= 0:
                    break
                mid = m.get("id")
                demand = float(m.get("product_uom_qty") or 0)
                if not isinstance(mid, int) or mid <= 0 or demand <= 0:
                    continue
                try:
                    ok = await asyncio.to_thread(odoo_service.reserve_move, mid)
                except _ODOO_RPC_ERRORS as e:
                    ok = False
                    logger.warning(
                        f"[replenish] reserve_move {mid} 실패 ({type(e).__name__}): {e}"
                    )
                if ok:
                    consumed = min(demand, remaining)
                    remaining -= consumed
                    replenished.append({
                        "move_id": mid,
                        "tier": m.get("_tier"),
                        "qty_assigned": consumed,
                        "picking_id": self._picking_id_of_move(m),
                        "override": bool(m.get("_override")),
                    })

            # 정책: VIP 다 채우지 않은 채로 Standard 시작 금지
            #   → 위의 sort 가 VIP 부터 처리하므로 자연스럽게 충족.
            #   consume_all_for_vip_first=False 일 때 별도 처리 추가 가능 (추후).

            return {
                "action": "replenish_priority_queue",
                "success": True,
                "product_id": product_id,
                "received_qty": received_qty,
                "remaining_after_replenish": remaining,
                "replenished": replenished,
                "vip_first_count": sum(1 for r in replenished if r.get("tier") == "VIP"),
                "standard_count": sum(1 for r in replenished if r.get("tier") == "Standard"),
                "override": {
                    "applied": bool(override),
                    "mode": (override.get("cfg") or {}).get("mode") if override else None,
                    "so_ids": override.get("so_ids") if override else [],
                    "reserved_so_ids": override_applied,
                    "requested_by": override.get("requested_by") if override else None,
                    "reason": override.get("reason") if override else None,
                } if override else None,
                "policy_applied": policy,
                "note": (
                    f"입고 {received_qty} 중 {received_qty - remaining} 소비. "
                    f"VIP backorder 우선 — Standard Waiting 큐가 그 다음 자동 충족."
                ),
            }

        # ─────────────────────────────────────────────────────────
        # dispatch_shipment — 전 라인 reserved → button_validate
        # Type 1: Pure code.
        # rule: delivery_ready_to_ship_vip (405)
        # ─────────────────────────────────────────────────────────
        async def dispatch_shipment(policy: dict, context: dict) -> dict:
            picking = context.get("picking") or {}
            picking_id = picking.get("id")
            target_state = policy.get("target_state", "done")

            available = await self._odoo_available()
            if not available:
                status = await self._odoo_status()
                return {
                    "action": "dispatch_shipment",
                    "success": True,
                    "skipped": True,
                    "reason": f"Odoo 미연결: {status.get('reason', 'unknown')}",
                    "intended_plan": {
                        "operation": "validate_picking",
                        "picking_id": picking_id,
                        "target_state": target_state,
                        "carrier_lookup": policy.get("carrier_lookup", "by_tier"),
                    },
                    "policy_applied": policy,
                    "note": "Odoo 미연결 — plan 만 반환. 실제 출고는 Odoo 환경에서.",
                }

            if not picking_id:
                return {
                    "action": "dispatch_shipment",
                    "success": False,
                    "error": "picking_id 누락",
                }

            # BC3 MED #M2 — 멱등성 가드:
            # 이미 done 상태의 picking 을 다시 validate 하면 Odoo 가 RPC 오류로 응답한다.
            # button_validate 는 idempotent 가 아니므로 우리가 short-circuit 한다.
            # context.picking.state 가 'done' 이거나, 최신 조회 결과가 'done' 이면 skip.
            payload_state = (picking.get("state") or "").lower()
            if payload_state == "done":
                return {
                    "action": "dispatch_shipment",
                    "success": True,
                    "skipped": True,
                    "reason": "picking 이 이미 'done' (context state) — 멱등 skip",
                    "picking_id": picking_id,
                    "target_state": target_state,
                    "policy_applied": policy,
                }
            try:
                latest = await asyncio.to_thread(odoo_service.get_picking, picking_id)
            except _ODOO_RPC_ERRORS as e:
                latest = None
                logger.debug(
                    f"[dispatch_shipment] get_picking({picking_id}) idempotency 체크 실패 "
                    f"({type(e).__name__}) — validate 시도로 진행: {e}"
                )
            if isinstance(latest, dict) and (latest.get("state") or "").lower() == "done":
                return {
                    "action": "dispatch_shipment",
                    "success": True,
                    "skipped": True,
                    "reason": "picking 이 이미 'done' (Odoo 조회) — 멱등 skip",
                    "picking_id": picking_id,
                    "target_state": target_state,
                    "policy_applied": policy,
                }

            try:
                result = await asyncio.to_thread(odoo_service.validate_picking, picking_id)
            except _ODOO_RPC_ERRORS as e:
                return {
                    "action": "dispatch_shipment",
                    "success": False,
                    "error": f"{type(e).__name__}: {e}",
                    "picking_id": picking_id,
                }

            wizard = result.get("wizard")
            if not wizard:
                # 전량 검증 완료 (기존 경로 — 회귀 불변)
                return {
                    "action": "dispatch_shipment",
                    "success": bool(result.get("validated")),
                    "picking_id": picking_id,
                    "target_state": target_state,
                    "validation_result": result,
                    "carrier_lookup": policy.get("carrier_lookup", "by_tier"),
                    "policy_applied": policy,
                    "note": "Odoo button_validate — 전량 출고 완료.",
                }

            # ── BC4 S1: PARTIAL 분기 (wizard = stock.backorder.confirmation) ──
            # partial_handling 정책:
            #   auto_backorder    — 가용분 출하 + 나머지 backorder (rule, default·무해)
            #   cancel_remainder  — 가용분 출하 + 나머지 취소 (파괴적, 명시 결정만)
            #   llm_advisor       — LLM 이 split/wait 추천 (추천만, 사람 승인 후 실행)
            ph = policy.get("partial_handling", "auto_backorder")
            res_id = wizard.get("res_id")
            wctx = wizard.get("context") or {}
            try:
                shortage = await asyncio.to_thread(
                    odoo_service.get_picking_shortage, picking_id)
            except _ODOO_RPC_ERRORS as e:
                logger.warning(f"[dispatch_shipment] get_picking_shortage 실패: {e}")
                shortage = {"demand": None, "reserved": None, "shortage": None}

            if ph == "llm_advisor":
                advice = await self._partial_shipment_advisor(policy, context, shortage)
                # 안전 원칙: 추천만. auto_execute_advisor=False(기본)면 실행 코드에
                # 도달하지 않고 사람 승인 대기 상태로 반환한다.
                if not policy.get("auto_execute_advisor", False):
                    return {
                        "action": "dispatch_shipment",
                        "success": True,
                        "pending_confirmation": True,
                        "partial": True,
                        "picking_id": picking_id,
                        "shortage": shortage,
                        "advisor": advice,          # {recommendation, rationale, confidence, source}
                        "wizard": wizard,           # confirm_partial_shipment 가 재사용
                        "policy_applied": policy,
                        "note": "부분출하 추천 생성 — 사람 승인 대기 (자동 실행 안 함).",
                    }
                # auto_execute_advisor=True (데모 전용): 추천대로 즉시 실행
                if advice.get("recommendation") == "wait":
                    exec_res = {"action": "wait", "ok": True}
                    decision = "wait"
                else:
                    exec_res = await asyncio.to_thread(
                        odoo_service.process_backorder, res_id, wctx)
                    decision = "split"
                source = advice.get("source", "llm")
            elif ph == "cancel_remainder":
                exec_res = await asyncio.to_thread(
                    odoo_service.cancel_backorder, res_id, wctx)
                decision, source, advice = "cancel", "rule_baseline", None
            else:
                # auto_backorder (기본) + 알 수 없는 값 → 안전 폴백
                exec_res = await asyncio.to_thread(
                    odoo_service.process_backorder, res_id, wctx)
                decision = "split"
                source = "rule_baseline" if ph == "auto_backorder" else "rule_baseline_fallback"
                advice = None

            return {
                "action": "dispatch_shipment",
                "success": bool(exec_res.get("ok")),
                "partial": True,
                "picking_id": picking_id,
                "decision": decision,
                "decision_source": source,
                "shortage": shortage,
                "execution_result": exec_res,
                "advisor": advice,
                "policy_applied": policy,
                "note": f"부분출하 처리: {decision} ({source}).",
            }

        # ─────────────────────────────────────────────────────────
        # create_replenishment_po — BC5: 충족 불가 → 자율 보충 발주
        # Type 2: LLM 1회(발주량 advisor) + 결정론 실행(incoming picking 생성).
        # rule: inventory_replenish_on_shortage
        # policy: {auto_create_po, vendor_name, safety_buffer_units, lead_time_days,
        #          dry_run}
        # context: {shortage: {product_id, product_name, unmet_qty, blocked_orders,...}}
        #
        # 흐름:
        #   1. shortage 컨텍스트 확인 (server 가 get_open_demand_for_product 결과 주입)
        #   2. _replenishment_qty_advisor (LLM) → recommended_qty + urgency
        #   3. auto_create_po=True && not dry_run → create_incoming_picking
        #      (purchase 모듈 없음 — incoming stock.picking 직접 생성)
        #   4. 결과 반환 (email_agent.send_replenishment_alert 가 이어서 브리핑)
        # ─────────────────────────────────────────────────────────
        async def create_replenishment_po(policy: dict, context: dict) -> dict:
            # 안 C — inventory agent 가 inventory 도메인을 end-to-end 소유:
            #   context.shortage 가 주어지면 사용(하위호환), 없으면 product_id 로 agent 가
            #   직접 부족분을 조회한다. replenish_priority_queue 의 tier_lookup
            #   "있으면 쓰고 없으면 조회" 패턴(아래 800줄대)과 동형. → '감지'를 agent 가 소유.
            shortage = context.get("shortage")
            product_id = (shortage or {}).get("product_id") or context.get("product_id")
            if not shortage and product_id and await self._odoo_available():
                try:
                    shortage = await asyncio.to_thread(
                        odoo_service.get_open_demand_for_product, product_id)
                except _ODOO_RPC_ERRORS as e:
                    logger.warning(
                        f"[create_replenishment_po] 부족분 조회 실패 "
                        f"({type(e).__name__}): {e}")
                    shortage = None
            shortage = shortage or {}
            product_id = product_id or shortage.get("product_id")
            unmet = float(shortage.get("unmet_qty") or shortage.get("total_shortage") or 0)
            blocked = shortage.get("blocked_orders") or []
            dry_run = bool(policy.get("dry_run", False))
            auto_create = bool(policy.get("auto_create_po", True))
            vendor_name = policy.get("vendor_name", "TechSupply Co")

            if not product_id:
                return {
                    "action": "create_replenishment_po",
                    "success": False,
                    "error": "product_id 누락 — 보충 대상 제품 식별 불가 "
                             "(context.shortage.product_id / context.product_id 모두 없음)",
                }
            if unmet <= 0:
                return {
                    "action": "create_replenishment_po",
                    "success": True,
                    "skipped": True,
                    "reason": "미충족 수요(unmet_qty)가 0 — 보충 발주 불필요",
                    "shortage": shortage,
                }

            # 판단 ① — 발주량 / 긴급도 (LLM, 실패 시 rule 폴백)
            advice = await self._replenishment_qty_advisor(policy, shortage)
            recommended_qty = int(advice.get("recommended_qty") or 0)
            if recommended_qty <= 0:
                recommended_qty = int(math.ceil(unmet))

            vip_blocked = [b for b in blocked if b.get("tier") == "VIP"]
            base_result = {
                "action": "create_replenishment_po",
                "success": True,
                "product_id": product_id,
                "product_name": shortage.get("product_name"),
                "unmet_qty": unmet,
                "recommended_qty": recommended_qty,
                "advisor": advice,            # {recommended_qty, urgency, rationale, confidence, source}
                "vendor_name": vendor_name,
                "blocked_orders": blocked,
                "vip_blocked_count": len(vip_blocked),
                "shortage": shortage,         # agent 가 소유한 부족분 스냅샷 (trigger audit/narrative 용)
                "policy_applied": {
                    "auto_create_po": auto_create,
                    "vendor_name": vendor_name,
                    "safety_buffer_units": policy.get("safety_buffer_units", 0),
                    "lead_time_days": policy.get("lead_time_days", 3),
                    "dry_run": dry_run,
                },
            }

            # Odoo 미연결 → plan-only
            available = await self._odoo_available()
            if not available:
                status = await self._odoo_status()
                base_result["po"] = None
                base_result["skipped"] = True
                base_result["reason"] = f"Odoo 미연결: {status.get('reason', 'unknown')}"
                base_result["intended_plan"] = {
                    "operation": "create_incoming_picking",
                    "product_id": product_id,
                    "qty": recommended_qty,
                    "vendor_name": vendor_name,
                }
                base_result["note"] = "Odoo 미연결 — 발주 plan 만 반환 (실제 picking 미생성)"
                return base_result

            # dry_run 또는 auto_create_po=False → 추천만, 발주 보류 (사람 승인 대기)
            if dry_run or not auto_create:
                base_result["po"] = None
                base_result["pending_confirmation"] = True
                base_result["note"] = (
                    "발주 추천만 생성 — auto_create_po=False 또는 dry_run. "
                    "사람 승인 후 incoming picking 생성."
                )
                return base_result

            # 실행 — incoming picking 생성 (purchase.order 없음).
            # 멱등 키는 안정적으로: origin="" → service 가 product 기준 안정 키 생성.
            # 수량/긴급도는 origin 에 넣지 않는다(advisor 변동 시 중복 발주 방지).
            try:
                po = await asyncio.to_thread(
                    odoo_service.create_incoming_picking,
                    product_id, recommended_qty, vendor_name, "", True,
                )
            except _ODOO_RPC_ERRORS as e:
                base_result["success"] = False
                base_result["po"] = None
                base_result["error"] = f"{type(e).__name__}: {str(e)[:300]}"
                base_result["note"] = "incoming picking 생성 실패 — 발주 미완료"
                return base_result

            base_result["po"] = po
            # confirm 실패(draft 잔존)는 입고 큐에 안 잡혀 루프가 끊기므로 성공으로 위장하지 않는다.
            if po.get("confirm_error") or (po.get("state") == "draft"):
                base_result["success"] = False
                base_result["note"] = (
                    f"⚠️ 보충 입고건 {po.get('picking_name')} 생성됐으나 confirm 실패"
                    f"(state={po.get('state')}) — 입고 큐에 안 잡힐 수 있음. 수동 확인 필요. "
                    f"err={po.get('confirm_error')}"
                )
            elif po.get("skipped"):
                base_result["note"] = (
                    f"기존 미검증 보충건 재사용(멱등): {po.get('picking_name')} "
                    f"({shortage.get('product_name')}). 중복 발주 방지."
                )
            else:
                base_result["note"] = (
                    f"보충 발주 생성: {po.get('picking_name')} "
                    f"({shortage.get('product_name')} × {recommended_qty}, "
                    f"urgency={advice.get('urgency')}). "
                    f"입고 검증 시 rule 400(stock_received_replenish)이 VIP backorder 부터 충족."
                )
            return base_result

        # ─────────────────────────────────────────────────────────
        # allocate_batched_by_tier — 사전 배치 (Pre-allocation Batched Priority)
        # Pattern B (vs. allocate_with_preemption 의 소급 재할당 Pattern A):
        #   · 전제: stock.picking.type.reservation_method='manual' — SO confirm 시
        #     picking 만 생성, reserve 는 보류된 상태.
        #   · cut-off 이벤트 발화 시 confirmed / waiting / partially_available 의
        #     outgoing picking 전체를 query → tier 우선순위로 정렬 →
        #     stock.picking.action_assign (public, 19.2 호환 OK) 을 순서대로 호출.
        #   · VIP 가 먼저 가용재고 다 잡고, 남는 양으로 Standard 보충 → Standard 입장에서
        #     "받았다 뺏긴" UX 없음. 일배송 cycle 비즈니스에 적합.
        # rule: inventory_batch_allocation_window (priority 415)
        # policy: {ordering, tier_priority, consume_all_for_vip_first,
        #          unblock_waiting_pickings}
        # context: {cutoff_at?, window_start?}
        # ─────────────────────────────────────────────────────────
        async def allocate_batched_by_tier(policy: dict, context: dict) -> dict:
            ordering = policy.get("ordering") or ["tier", "scheduled_date", "sale_order_id"]
            tier_priority = policy.get("tier_priority") or {
                "VIP": 100, "Standard": 50, "Bronze": 25,
            }

            available = await self._odoo_available()
            if not available:
                status = await self._odoo_status()
                return {
                    "action": "allocate_batched_by_tier",
                    "success": True, "skipped": True,
                    "reason": f"Odoo 미연결: {status.get('reason', 'unknown')}",
                    "intended_plan": {
                        "operation": "batched_priority_allocation",
                        "ordering": ordering, "tier_priority": tier_priority,
                        "would_call": "stock.picking.action_assign per picking in priority order",
                    },
                    "policy_applied": policy,
                    "note": "Odoo 미연결 — plan only",
                }

            # 1. 대상 picking 수집
            states = ["confirmed", "waiting", "partially_available"]
            try:
                pickings = await asyncio.to_thread(
                    odoo_service.list_pickings_by_state, states,
                )
            except _ODOO_RPC_ERRORS as e:
                logger.warning(
                    f"[allocate_batched_by_tier] list_pickings_by_state 실패 "
                    f"({type(e).__name__}): {e}"
                )
                return {
                    "action": "allocate_batched_by_tier", "success": False,
                    "error": f"{type(e).__name__}: {e}",
                    "policy_applied": policy,
                }

            if not pickings:
                return {
                    "action": "allocate_batched_by_tier", "success": True,
                    "candidates": 0, "assigned": [],
                    "note": "처리 대상 picking 없음",
                    "policy_applied": policy,
                }

            # 2. tier 매핑
            def _so_id(p: Dict[str, Any]) -> Optional[int]:
                s = p.get("sale_id")
                return s[0] if isinstance(s, list) else s

            sale_ids = sorted({sid for p in pickings if (sid := _so_id(p))})
            try:
                tier_map = await asyncio.to_thread(
                    odoo_service.get_sale_order_tier_map, sale_ids,
                ) if sale_ids else {}
            except _ODOO_RPC_ERRORS:
                tier_map = {}

            # BC4 S0: priority override — 명시된 so_ids 의 정렬 점수 boost (결정론)
            override = self._resolve_override(policy, context)
            override_ids = set(override["so_ids"]) if override else set()

            # 3. 정렬: tier desc, scheduled_date asc, sale_order_id asc
            def _sort_key(p: Dict[str, Any]) -> Tuple:
                sid = _so_id(p)
                tier = tier_map.get(sid, "Standard") if sid else "Standard"
                prio = tier_priority.get(tier, 0)
                if sid in override_ids:
                    prio = self._apply_override_score(prio, override, tier_priority)
                return (-prio, p.get("scheduled_date") or "", sid or 0)

            pickings_sorted = sorted(pickings, key=_sort_key)

            # 4. 순차 action_assign (public method, 19.2 호환)
            results: List[Dict[str, Any]] = []
            for p in pickings_sorted:
                pid = p.get("id")
                if not isinstance(pid, int) or pid <= 0:
                    logger.warning(
                        f"[allocate_batched_by_tier] picking missing valid 'id': {p}"
                    )
                    continue
                sid = _so_id(p)
                tier = tier_map.get(sid, "Standard") if sid else "Standard"
                row: Dict[str, Any] = {
                    "picking_id": pid,
                    "picking_name": p.get("name"),
                    "sale_order_id": sid,
                    "tier": tier,
                    "before_state": p.get("state"),
                    "override": bool(sid in override_ids),
                }
                try:
                    await asyncio.to_thread(
                        odoo_service.call,
                        "stock.picking", "action_assign", [pid],
                    )
                    after_rec = await asyncio.to_thread(odoo_service.get_picking, pid)
                    row["after_state"] = (after_rec or {}).get("state") if after_rec else None
                except _ODOO_RPC_ERRORS as e:
                    logger.warning(
                        f"[allocate_batched_by_tier] action_assign {pid} 실패 "
                        f"({type(e).__name__}): {e}"
                    )
                    row["error"] = f"{type(e).__name__}: {str(e)[:200]}"
                results.append(row)

            assigned = sum(1 for r in results if r.get("after_state") == "assigned")
            partial = sum(1 for r in results if r.get("after_state") == "partially_available")

            return {
                "action": "allocate_batched_by_tier",
                "success": True,
                "candidates": len(pickings),
                "processed": len(results),
                "summary": {
                    "fully_assigned": assigned,
                    "partially_available": partial,
                    "still_waiting": len(results) - assigned - partial,
                },
                "results_by_priority": results,
                "override": {
                    "applied": bool(override),
                    "mode": (override.get("cfg") or {}).get("mode") if override else None,
                    "so_ids": override.get("so_ids") if override else [],
                    "requested_by": override.get("requested_by") if override else None,
                    "reason": override.get("reason") if override else None,
                } if override else None,
                "policy_applied": policy,
                "note": f"Batched priority allocation: {assigned}/{len(results)} fully assigned",
            }

        # ─────────────────────────────────────────────────────────
        # 등록
        # ─────────────────────────────────────────────────────────
        self.register_action(
            'split_fulfillment_path', split_fulfillment_path,
            'BC3: SO confirmed → service / storable 라인 분기, '
            'storable 은 delivery_ready_check 로 spawn'
        )
        self.register_action(
            'allocate_with_preemption', allocate_with_preemption,
            'BC3: VIP DO + 재고부족 → Standard "assigned" move 회수 (soft preempt) → VIP 재할당'
        )
        self.register_action(
            'allocate_batched_by_tier', allocate_batched_by_tier,
            'BC3-v3 (사전 배치): cut-off 시점 confirmed/waiting picking 들을 '
            'tier 우선순위로 일괄 reserve (Odoo reservation_method=manual 전제)'
        )
        self.register_action(
            'allocate_fifo', allocate_fifo,
            'BC3: Standard DO → 가용재고 범위 reserve, 부족 시 Waiting'
        )
        self.register_action(
            'replenish_priority_queue', replenish_priority_queue,
            'BC3: 입고 발생 → VIP backorder 큐 우선 → 남는 양 Standard 보충'
        )
        self.register_action(
            'dispatch_shipment', dispatch_shipment,
            'BC3: 전 라인 reserved → stock.picking button_validate → 출고 (state=done)'
        )
        self.register_action(
            'create_replenishment_po', create_replenishment_po,
            'BC5: 충족 불가 shortage → 발주량 advisor(LLM) → incoming picking 생성 (자율 보충 발주)'
        )

    # ═══════════════════════════════════════════════════════════════
    # internals
    # ═══════════════════════════════════════════════════════════════
    @staticmethod
    def _picking_id_of_move(move: Dict[str, Any]) -> Optional[int]:
        """
        Odoo many2one 응답 형식 흡수: [id, name] tuple 또는 단일 id.
        """
        pid = move.get("picking_id")
        if isinstance(pid, list) and pid:
            return pid[0]
        if isinstance(pid, int):
            return pid
        return None
