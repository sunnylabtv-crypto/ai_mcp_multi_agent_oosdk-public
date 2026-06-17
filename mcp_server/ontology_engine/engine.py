# mcp_server/ontology_engine/engine.py
"""
OntologyEngine — 핵심 4 methods

- resolve_links:   이메일 payload → 온톨로지 객체 그래프
- check_rules:     rule 평가 → action 반환
- trigger_events:  action.events → 실행 계획 리스트
- manage_memory:   3-tier 메모리 위임

설계 원칙:
- yaml 의 object_types.<T>.source.type 에 따라 Adapter 주입 (SFDC / local_json)
- rule / event 평가는 이 파일 내부의 private 메서드 (별도 파일로 쪼개지 않음)
- SFDC 장애 시 fallback 어댑터 자동 사용
"""
import logging
import re
from pathlib import Path
from typing import Dict, Any, List, Optional

import yaml

from .adapters import SourceAdapter, SalesforceAdapter, LocalJsonAdapter
from .memory.facade import ThreeTierMemory

logger = logging.getLogger(__name__)


class OntologyEngine:
    """
    OOSDK Ontology 실행 엔진.

    사용:
        engine = OntologyEngine("ontology/ontology.yaml", memory=ThreeTierMemory(...))
        ctx    = engine.resolve_links("email", payload)
        action = engine.check_rules(ctx)
        plan   = engine.trigger_events(action, ctx)
        engine.manage_memory(key, value, tier="hot")
    """

    def __init__(self, yaml_path: str, memory: Optional[ThreeTierMemory] = None):
        self.yaml_path = Path(yaml_path)
        with open(self.yaml_path, encoding="utf-8") as f:
            self.spec = yaml.safe_load(f)

        # memory 주입 없으면 yaml 기반 기본 생성
        self.memory = memory or ThreeTierMemory(self.spec.get("memory", {}))

        # object_type 별 adapter 사전 생성
        self.adapters: Dict[str, SourceAdapter] = self._load_adapters()

        # 실행 이력 (대시보드용 trace)
        self.last_trace: Optional[Dict[str, Any]] = None

    # ═══════════════════════════════════════════════════════════
    # 1. resolve_links
    # ═══════════════════════════════════════════════════════════
    def resolve_links(self, entity_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        payload (예: email) 에서 온톨로지 객체들을 해석.

        지원 entity_type:
            - "email"                       (BC1 — 인바운드 분류)
            - "sales_opportunity_inquiry"   (BC2 — 견적 문의 인입 → Opp 생성 분기)
            - "sales_opportunity_close"     (BC2 — Opp Closed Won/Lost 후속 dispatch)
            - "sale_order_confirmed"        (BC3 — SO state='sale' → fulfillment 분기)
            - "stock_received"              (BC3 — 입고 이벤트 → backorder 우선 보충)
            - "delivery_ready_check"        (BC3 — DO 상태 점검 → allocate / ship)

        예시 흐름 (email):
            1. payload["from"] → Person (email, email_domain 추출)
            2. Customer 어댑터로 email_domain 으로 조회 → Customer 또는 None
            3. { person, customer, email } 컨텍스트 반환

        BC2 흐름 (sales_opportunity_*):
            payload 에서 account_name + tier 를 받아 Customer 동등 객체로 매핑.
            opportunity 정보는 그대로 통과 (rule 평가 시 dot 접근).

        BC3 흐름 (sale_order_confirmed / stock_received / delivery_ready_check):
            payload 에서 sales_order / picking / inventory / receipt 객체를 그대로 통과.
            tier 정보가 picking 에 없으면 SalesOrder → Account 로 거슬러 올라가 추론.
        """
        trace: Dict[str, Any] = {"step": "resolve_links", "entity_type": entity_type}

        # ───────────────────────────────────────────────────────
        # BC2 분기: sales_opportunity_inquiry / sales_opportunity_close
        # ───────────────────────────────────────────────────────
        if entity_type in ("sales_opportunity_inquiry", "sales_opportunity_close"):
            return self._resolve_sales_links(entity_type, payload, trace)

        # ───────────────────────────────────────────────────────
        # BC3 분기: sale_order_confirmed / stock_received / delivery_ready_check
        # ───────────────────────────────────────────────────────
        if entity_type in ("sale_order_confirmed", "stock_received", "delivery_ready_check",
                            "inventory_shortage_detected"):
            return self._resolve_inventory_links(entity_type, payload, trace)

        if entity_type != "email":
            # 확장 여지 — 다른 엔티티 타입 (ticket, call 등)
            trace["note"] = f"unhandled entity_type: {entity_type}"
            self.last_trace = trace
            return {"raw": payload, "entity": entity_type}

        # 1) Person 파싱
        from_field = payload.get("from", "")
        email_addr = self._extract_email(from_field)
        email_domain = self._extract_domain(from_field)
        person = {
            "email": email_addr,
            "name": payload.get("from_name", ""),
            "email_domain": email_domain,
        }
        trace["person"] = person

        # 2) Customer 조회 (adapter 경유)
        # lookup_by 가 'email' 이면 전체 이메일을, 'email_domain' 이면 도메인을 전달
        customer = None
        if "Customer" in self.adapters:
            cust_adapter = self.adapters["Customer"]
            lookup_by = (cust_adapter.config.get("lookup", {}) or {}).get("by", "email_domain")
            lookup_value = email_addr if lookup_by == "email" else email_domain
            trace["lookup_by"] = lookup_by
            trace["lookup_value"] = lookup_value

            if lookup_value:
                try:
                    customer = cust_adapter.fetch_one(lookup_value)
                    trace["customer_source"] = cust_adapter.__class__.__name__
                    # adapter 가 None 을 반환했다면 사유를 surface (인증실패/HTTP에러/no_match)
                    if customer is None:
                        adapter_err = getattr(cust_adapter, "last_error", None)
                        if adapter_err:
                            trace["customer_error"] = adapter_err
                except Exception as e:
                    logger.warning(f"[resolve_links] Customer 조회 실패: {e}")
                    trace["customer_error"] = str(e)

                # customer 못 찾았고, 에러가 인증/연결 문제면 fallback 어댑터로 재시도
                # (no_match 는 정상적인 0건이므로 fallback 안 씀)
                err = trace.get("customer_error")
                should_fallback = customer is None and err and err != "no_match"
                if should_fallback:
                    fb = self._fallback_adapter("Customer")
                    if fb:
                        try:
                            fb_lookup_by = (fb.config.get("lookup", {}) or {}).get("by", lookup_by)
                            fb_value = email_addr if fb_lookup_by == "email" else email_domain
                            customer = fb.fetch_one(fb_value)
                            if customer:
                                trace["customer_source"] = f"fallback:{fb.__class__.__name__}"
                                # fallback 으로 복구됐으면 에러 표시는 정보성으로 prefix
                                trace["customer_error"] = f"primary_failed_used_fallback ({err})"
                        except Exception as e2:
                            logger.error(f"[resolve_links] fallback 도 실패: {e2}")
                            trace["customer_error"] = f"{err}; fallback_error: {e2}"
        trace["customer"] = customer

        context = {
            "entity": "email",
            "email": payload,
            "person": person,
            "customer": customer,
        }
        self.last_trace = trace
        return context

    # ═══════════════════════════════════════════════════════════
    # 1.b resolve_links — BC2 sales 분기
    # ═══════════════════════════════════════════════════════════
    def _resolve_sales_links(
        self, entity_type: str, payload: Dict[str, Any], trace: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        BC2 sales_opportunity_* 엔티티 해석.

        payload 형태:
            sales_opportunity_inquiry:
                { account_name, tier?, contact_email?, subject?, body?, amount? }
            sales_opportunity_close:
                { opportunity: { id?, name, stage, tier, account_name, amount, lost_reason? } }
                (혹은 plain top-level: stage, account_name, tier, amount, ...)

        - tier 가 payload 에 직접 있으면 사용 (test/sandbox 친화).
        - 없고 Account 어댑터가 있으면 SOQL 로 lookup (account_name → CustomerPriority).
        - customer 형태로 매핑 (rule 의 customer.tier 조건과 호환).
        """
        # 1) Opportunity 객체 추출 (close 엔티티 전용)
        opportunity = None
        if entity_type == "sales_opportunity_close":
            opp = payload.get("opportunity") or {}
            # top-level 평탄화 지원: payload 에 직접 stage 등이 있어도 OK
            opportunity = {
                "id": opp.get("id") or payload.get("opportunity_id"),
                "name": opp.get("name") or payload.get("opportunity_name"),
                "stage": opp.get("stage") or payload.get("stage"),
                "tier": opp.get("tier") or payload.get("tier"),
                "account_name": opp.get("account_name") or payload.get("account_name"),
                "amount": opp.get("amount") or payload.get("amount"),
                "record_type_dev_name": opp.get("record_type_dev_name")
                                        or payload.get("record_type_dev_name"),
                "lost_reason": opp.get("lost_reason") or payload.get("lost_reason"),
            }
            trace["opportunity"] = opportunity

        # 2) Account / tier 결정
        account_name = (
            payload.get("account_name")
            or (opportunity or {}).get("account_name")
            or ""
        )
        tier = payload.get("tier") or (opportunity or {}).get("tier")

        # 3) tier 가 명시 안 됐으면 Account 어댑터로 SOQL lookup 시도
        account = None
        if not tier and account_name and "Account" in self.adapters:
            try:
                account = self.adapters["Account"].fetch_one(account_name)
                if account:
                    tier = account.get("customer_priority") or account.get("CustomerPriority__c")
                    trace["account_source"] = self.adapters["Account"].__class__.__name__
            except Exception as e:
                trace["account_error"] = str(e)
                logger.warning(f"[_resolve_sales_links] Account 조회 실패: {e}")

        # 4) Opportunity 의 tier 도 같이 채워주기 (rule 의 opportunity.tier 매칭용)
        if opportunity and not opportunity.get("tier") and tier:
            opportunity["tier"] = tier

        # 5) Customer 동등 객체 매핑 (rule 의 customer.tier 조건과 호환)
        customer = None
        if tier:
            customer = {
                "id": (account or {}).get("id"),
                "name": account_name,
                "company": account_name,
                "tier": tier,
                "annual_revenue": (account or {}).get("annual_revenue"),
            }

        trace["account_name"] = account_name
        trace["tier"] = tier
        trace["customer"] = customer

        context = {
            "entity": entity_type,
            "payload": payload,
            "account": account,
            "account_name": account_name,
            "customer": customer,
            "opportunity": opportunity,
        }
        self.last_trace = trace
        return context

    # ═══════════════════════════════════════════════════════════
    # 1.c resolve_links — BC3 inventory 분기
    # ═══════════════════════════════════════════════════════════
    def _resolve_inventory_links(
        self, entity_type: str, payload: Dict[str, Any], trace: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        BC3 inventory 관련 entity 해석.

        payload 형태:
          sale_order_confirmed:
              { sales_order: { id, name, state, client_order_ref, tier, account_name,
                               amount_total, target_delivery_date,
                               has_storable_lines, has_service_lines } }

          stock_received:
              { receipt: { product_id, product_name, qty, received_at, source_po_id? } }

          delivery_ready_check:
              { picking: { id, name, state, scheduled_date, sale_order_id,
                           tier, qty_demand },
                inventory: { product_id, on_hand, reserved, available,
                             incoming, projected_avail } }

        - tier 가 picking/sales_order 에 없으면 SO → Account 로 추론.
        - inventory 가 없으면 picking 만으로 evaluate 가능한 rule (delivery_ready_to_ship_vip 등) 매칭됨.
        """
        # 1) 각 엔티티 객체 평탄화 (rule 의 dot 접근과 호환)
        sales_order = payload.get("sales_order") or payload.get("order")
        picking = payload.get("picking") or payload.get("delivery_order")
        inventory = payload.get("inventory") or payload.get("inventory_state")
        receipt = payload.get("receipt") or payload.get("stock_receipt")
        # BC5: 충족 불가 보충 — get_open_demand_for_product 결과를 그대로 통과.
        shortage = payload.get("shortage")

        # 2) tier 추론 (picking → sales_order → account)
        tier = None
        if picking and picking.get("tier"):
            tier = picking["tier"]
        elif sales_order and sales_order.get("tier"):
            tier = sales_order["tier"]
        elif picking and picking.get("sale_order_id") and "Account" in self.adapters:
            # 보강 시점이 잦으면 cache 권장 — 지금은 그냥 시도
            try:
                account_name = (sales_order or {}).get("account_name") or ""
                if account_name:
                    account = self.adapters["Account"].fetch_one(account_name)
                    if account:
                        tier = account.get("customer_priority")
            except Exception as e:
                trace["tier_lookup_error"] = str(e)

        # 3) tier 채워주기 (rule 의 picking.tier / sales_order.tier 매칭용)
        if picking and not picking.get("tier") and tier:
            picking["tier"] = tier
        if sales_order and not sales_order.get("tier") and tier:
            sales_order["tier"] = tier

        # 4) account_name 보존 (rule / agent action 에서 알림 발송용)
        account_name = (
            (sales_order or {}).get("account_name")
            or (picking or {}).get("account_name")
            or payload.get("account_name")
            or ""
        )

        trace.update({
            "sales_order_id": (sales_order or {}).get("id"),
            "picking_id": (picking or {}).get("id"),
            "product_id": (receipt or inventory or {}).get("product_id"),
            "tier": tier,
            "account_name": account_name,
        })

        context = {
            "entity": entity_type,
            "payload": payload,
            "sales_order": sales_order,
            "picking": picking,
            "inventory": inventory,
            "receipt": receipt,
            "shortage": shortage,                 # BC5 (안 C: 보통 None — agent 가 직접 조회)
            "product_id": payload.get("product_id"),  # BC5 안 C — agent 가 부족분 조회에 사용
            "notify_to": payload.get("notify_to"),  # BC5 — 담당자 알림 수신자
            "tier": tier,
            "account_name": account_name,
            # customer 동등 (BC2 rule 일부 재사용 가능하게)
            "customer": {"tier": tier, "name": account_name} if tier else None,
        }
        self.last_trace = trace
        return context

    # ═══════════════════════════════════════════════════════════
    # 2. check_rules
    # ═══════════════════════════════════════════════════════════
    def check_rules(self, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        rules 를 priority 내림차순으로 평가, first_match 반환.

        Returns:
            {"rule_name": str, "priority": ..., "memory_tier": ..., "events": [...]}
            매칭 없으면 None.
        """
        rules = self.spec.get("rules", {})
        meta = rules.get("_meta", {}) if isinstance(rules, dict) else {}
        evaluation = meta.get("evaluation", "first_match")

        # _meta 제외하고 priority 내림차순 정렬
        named_rules = [(n, r) for n, r in rules.items() if n != "_meta"]
        named_rules.sort(key=lambda kv: kv[1].get("priority", 0), reverse=True)

        for name, rule in named_rules:
            cond = rule.get("if", "")
            if self._eval_condition(cond, context):
                action = {"rule_name": name, **rule.get("then", {})}
                if self.last_trace is not None:
                    self.last_trace["matched_rule"] = name
                    self.last_trace["action"] = action
                if evaluation == "first_match":
                    return action

        # no match
        if self.last_trace is not None:
            self.last_trace["matched_rule"] = None
        return None

    # ═══════════════════════════════════════════════════════════
    # 3. trigger_events
    # ═══════════════════════════════════════════════════════════
    def trigger_events(
        self, action: Optional[Dict[str, Any]], context: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        action 의 위임 선언을 실행 계획으로 변환.

        v1.2+ 우선순위:
          1. action.delegate_to (정책 기반 멀티 에이전트 dispatch)
             → kind="delegate", agent + action(handler) + policy
          2. action.events       (Deprecated, v1.1 호환)
             → kind="event", agent + tool + params

        Returns:
            [{"kind", "agent", ...}, ...]
            (이 단계에선 agent 를 실제로 호출하지 않고 '계획' 만 만듦)
        """
        if not action:
            return []

        plan: List[Dict[str, Any]] = []

        # ─── 1) v1.2+ delegate_to (선호) ───
        delegations = action.get("delegate_to") or []
        for idx, deleg in enumerate(delegations):
            if not isinstance(deleg, dict):
                logger.warning(f"[trigger_events] delegate_to[{idx}] 형식 오류 (dict 아님): {deleg}")
                continue
            agent_id = deleg.get("agent")
            action_name = deleg.get("action")
            policy = deleg.get("policy", {}) or {}
            if not agent_id or not action_name:
                logger.warning(
                    f"[trigger_events] delegate_to[{idx}] 필수 키 누락 (agent/action): {deleg}"
                )
                continue
            plan.append({
                "kind": "delegate",
                "step": idx,
                "agent": agent_id,
                "action": action_name,
                "policy": policy,
                "retry": deleg.get("retry"),
            })

        # ─── 2) Deprecated events (delegate_to 없을 때만 fallback) ───
        if not plan:
            event_names = action.get("events", [])
            event_defs = self.spec.get("events", {})
            for name in event_names:
                if name not in event_defs:
                    logger.warning(f"[trigger_events] 알 수 없는 event: {name}")
                    continue
                event = event_defs[name]
                plan.append({
                    "kind": "event",
                    "event_name": name,
                    "agent": event.get("agent"),
                    "tool": event.get("tool"),
                    "params": event.get("params", {}),
                    "retry": event.get("retry"),
                })

        if self.last_trace is not None:
            self.last_trace["event_plan"] = plan
            self.last_trace["plan_kinds"] = [p.get("kind") for p in plan]
        return plan

    # ═══════════════════════════════════════════════════════════
    # 4. manage_memory
    # ═══════════════════════════════════════════════════════════
    def manage_memory(
        self, key: str, value: Any, tier: str = "hot", ttl_sec: Optional[int] = None
    ) -> None:
        """3-tier 메모리에 저장 위임."""
        self.memory.put(key, value, tier=tier, ttl_sec=ttl_sec)

    def recall_memory(self, key: str, tier: str = "hot") -> Any:
        """조회 (대시보드/데모 편의)."""
        return self.memory.get(key, tier=tier)

    # ═══════════════════════════════════════════════════════════
    # internals
    # ═══════════════════════════════════════════════════════════
    def _load_adapters(self) -> Dict[str, SourceAdapter]:
        """yaml 의 object_types 각각에 대해 적절한 어댑터 생성."""
        adapters: Dict[str, SourceAdapter] = {}
        object_types = self.spec.get("object_types", {})
        connections = self.spec.get("connections", {})

        for type_name, type_def in object_types.items():
            src = type_def.get("source") or {}
            adapter = self._build_adapter(src, connections)
            if adapter:
                adapters[type_name] = adapter
        return adapters

    def _build_adapter(
        self, source_config: Dict, connections: Dict
    ) -> Optional[SourceAdapter]:
        stype = source_config.get("type")
        if stype == "salesforce":
            return SalesforceAdapter(source_config, connections)
        if stype == "local_json":
            return LocalJsonAdapter(source_config, connections)
        if stype == "inline":
            return None  # Person 등 — resolve_links 에서 직접 처리
        logger.warning(f"[_build_adapter] 알 수 없는 source.type: {stype}")
        return None

    def _fallback_adapter(self, type_name: str) -> Optional[SourceAdapter]:
        """object_type 의 source.fallback 블록이 있으면 어댑터 생성"""
        type_def = self.spec.get("object_types", {}).get(type_name, {})
        fallback_cfg = type_def.get("source", {}).get("fallback")
        if not fallback_cfg:
            return None
        return self._build_adapter(fallback_cfg, self.spec.get("connections", {}))

    @staticmethod
    def _extract_domain(email_address: str) -> str:
        """'John <john@acme-corp.com>' 또는 'john@acme-corp.com' → 'acme-corp.com'"""
        if not email_address:
            return ""
        m = re.search(r"<([^>]+)>", email_address)
        raw = m.group(1) if m else email_address
        if "@" in raw:
            return raw.split("@", 1)[1].strip().lower()
        return raw.strip().lower()

    @staticmethod
    def _extract_email(email_address: str) -> str:
        """'John <john@x.com>' 또는 'john@x.com' → 'john@x.com' (소문자 정규화)"""
        if not email_address:
            return ""
        m = re.search(r"<([^>]+)>", email_address)
        raw = m.group(1) if m else email_address
        return raw.strip().lower()

    # ---------- 조건 평가 (mini DSL) ----------
    def _eval_condition(self, expr: str, context: Dict[str, Any]) -> bool:
        """
        yaml 의 if 조건식을 평가. 제한된 mini DSL — 안전한 Python eval 사용.

        지원:
          customer == null / != null
          customer.tier == 'VIP'
          customer != null AND customer.tier == 'VIP'
          customer == null OR priority == 'high'
        """
        if not expr:
            return True

        # null / none 처리
        safe = expr.replace("null", "None").replace("NULL", "None")
        # and/or 대소문자 통일 (yaml 관례상 대문자 허용)
        safe = re.sub(r"\bAND\b", "and", safe)
        safe = re.sub(r"\bOR\b",  "or",  safe)
        safe = re.sub(r"\bNOT\b", "not", safe)
        # true / false 처리 (yaml 자연스러운 표기 → Python 키워드)
        safe = re.sub(r"\btrue\b",  "True",  safe)
        safe = re.sub(r"\bfalse\b", "False", safe)

        # dot 접근 (customer.tier) 처리 — dict 로 만들어진 context 에서 접근 가능하도록
        # 간단히 context dict 를 쓰되 '.' 접근을 위해 DotDict 래핑
        wrapped = {k: _DotDict.wrap(v) for k, v in context.items()}

        try:
            # 제한된 globals/locals — 빌트인 차단
            return bool(eval(safe, {"__builtins__": {}}, wrapped))  # noqa: S307
        except Exception as e:
            logger.warning(f"[_eval_condition] 평가 실패 expr={expr!r} err={e}")
            return False


class _DotDict(dict):
    """dict 의 키를 attribute 처럼 접근 가능하게 (mini DSL 용)"""

    def __getattr__(self, name):
        if name in self:
            return self[name]
        return None  # 없는 필드는 None (null 비교 가능)

    @classmethod
    def wrap(cls, obj):
        if obj is None:
            return None
        if isinstance(obj, dict):
            return cls({k: cls.wrap(v) for k, v in obj.items()})
        if isinstance(obj, list):
            return [cls.wrap(x) for x in obj]
        return obj
