# mcp_server/agents/collections_agent.py
"""
Collections Agent — 수금(AR) 도메인 전담 (BC5)
─────────────────────────────────────────────────────────────────
설계 원칙 (ontology→agent→ERP 일관):
  · 연체 "감지"는 결정론(odoo_service.list_overdue_invoices) — 규칙이 게이트.
  · LLM 판단은 단 한 곳: dunning advisor (회수 우선순위 + tier별 톤).
    → 이 판단 때문에 수금이 '에이전트'를 갖는다 (invoice 처럼 순수 결정론이면 에이전트 불필요).
  · 발송/입금 등록은 결정론 실행.

호출 흐름:
  ontology rule (collections_dunning, entity=='collections_overdue')
    └─> collections_agent.run_dunning(policy, context)     ← 감지 + ★dunning advisor(LLM)
  사람 승인 후:
    └─> collections_agent.send_dunning(...)                ← 독촉 발송 + chatter (결정론)
    └─> collections_agent.register_payment(...)            ← 입금 등록 (결정론)

판단 LLM 은 항상 결정론 폴백(연체일×금액 정렬 + 템플릿) — 모델 죽어도 라인 안 멈춤.
"""
import os
import sys
import json
import asyncio
import logging
from typing import Dict, Any, List

from .base_agent import BaseAgent
from ..services import odoo_service

logger = logging.getLogger(__name__)

_DUNNING_SYS = (
    "You are an accounts-receivable collections advisor for a B2B company. "
    "Given overdue customer invoices (name, customer, tier, days_overdue, amount), "
    "(1) rank them by collection priority (priority=1 = chase first; weigh risk ~ "
    "days_overdue x amount, but handle VIP relationships carefully), and "
    "(2) per invoice choose a dunning tone: 'gentle' / 'firm' / 'final', and write a "
    "1-2 sentence KOREAN dunning message in that tone. VIP = softer, relationship-"
    "preserving; Standard habitual-late = firmer. Respond with ONLY a JSON object: "
    '{"collections":[{"name":"INV..","priority":1,"tone":"gentle|firm|final",'
    '"message":"<korean>","rationale":"<short>"}]}'
)


class CollectionsAgent(BaseAgent):
    """BC5 수금(AR) 전담 Agent — dunning advisor(LLM 판단) + 결정론 발송/입금."""

    def __init__(self, llm_config: dict):
        super().__init__(
            name="Collections Agent",
            description=(
                "Odoo ERP 의 미수금(AR) 회수를 전담합니다. 연체 인보이스를 결정론으로 "
                "감지하고, 회수 우선순위와 tier 별 독촉 톤만 LLM advisor 로 판단한 뒤 "
                "(사람 승인 후) 독촉 발송·chatter 기록·입금 등록을 결정론으로 실행합니다. "
                "rule: collections_dunning (entity=='collections_overdue')."
            ),
            llm_config=llm_config,
        )

    # ═══════════════════════════════════════════════════════════════
    def register_tools_from_services(self, user_id: str = None):
        self._user_id = user_id or "admin"
        self._register_policy_actions()
        print(
            f"[Collections Agent] {len(self._action_handlers)} actions registered "
            f"for user: {user_id}", file=sys.stderr,
        )

    # ═══════════════════════════════════════════════════════════════
    # LLM 판단 (단 한 곳) — 실패 시 결정론 폴백
    # ═══════════════════════════════════════════════════════════════
    async def _dunning_advisor(self, overdue: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        facts = [{"name": o.get("name"), "customer": o.get("customer"),
                  "tier": o.get("tier"), "days_overdue": o.get("days_overdue"),
                  "amount": o.get("amount_residual")} for o in overdue]
        try:
            from ..services.openai_service import generate_text_with_system
            raw = await asyncio.to_thread(
                generate_text_with_system,
                system_prompt=_DUNNING_SYS,
                user_prompt=json.dumps(facts, ensure_ascii=False),
                temperature=0.2, max_tokens=800)
            cleaned = (raw or "").strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            elif cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            data = json.loads(cleaned.strip())
            by_name = {r.get("name"): r for r in data.get("collections", [])}
            out = []
            for o in overdue:
                r = by_name.get(o.get("name"), {})
                out.append({**o, "priority": r.get("priority"), "tone": r.get("tone"),
                            "message": r.get("message"), "rationale": r.get("rationale"),
                            "source": "llm"})
            out.sort(key=lambda x: x.get("priority") or 99)
            return out
        except Exception as e:
            logger.warning(f"[collections._dunning_advisor] LLM 실패 → rule 폴백: {e}")

            def _tone(d):
                d = d or 0
                return "gentle" if d < 15 else ("firm" if d < 35 else "final")
            out = sorted(overdue, key=lambda x: -((x.get("days_overdue") or 0)
                                                  * (x.get("amount_residual") or 0)))
            for i, o in enumerate(out):
                o.update({"priority": i + 1, "tone": _tone(o.get("days_overdue")),
                          "message": (f"{o.get('customer')}님, {o.get('name')} "
                                      f"({o.get('amount_residual')}) 가 {o.get('days_overdue')}일 "
                                      f"연체되었습니다. 빠른 확인 부탁드립니다."),
                          "rationale": "rule 폴백(연체일×금액)", "source": "fallback_rule"})
            return out

    # ═══════════════════════════════════════════════════════════════
    # Policy-driven actions (ontology delegate_to / 2-step confirm)
    # ═══════════════════════════════════════════════════════════════
    def _register_policy_actions(self):

        # ① run_dunning — 연체 감지(결정론) + dunning advisor(LLM). ontology rule 이 호출.
        async def run_dunning(policy: dict, context: dict) -> dict:
            overdue = context.get("overdue")
            if not overdue:
                overdue = await asyncio.to_thread(odoo_service.list_overdue_invoices)
            if not overdue:
                return {"action": "run_dunning", "count": 0, "collections": [],
                        "narrative": "연체 청구서 없음."}
            recs = await self._dunning_advisor(overdue)
            # 인보이스 id 부착 (send/payment 용)
            try:
                moves = await asyncio.to_thread(
                    odoo_service.call, "account.move", "search_read",
                    [("name", "in", [r.get("name") for r in recs])], fields=["name"])
                name_to_id = {m.get("name"): m.get("id") for m in (moves or [])}
            except Exception:
                name_to_id = {}
            for r in recs:
                r["invoice_id"] = name_to_id.get(r.get("name"))
            return {"action": "run_dunning", "count": len(recs),
                    "source": (recs[0].get("source") if recs else None),
                    "collections": recs,
                    "narrative": f"연체 {len(recs)}건 — 회수 우선순위/톤 추천 (승인 대기)."}

        # ② send_dunning — 사람 승인 후: 독촉 발송 + chatter 기록 (결정론)
        async def send_dunning(policy: dict, context: dict) -> dict:
            recs = context.get("recs") or []
            to_email = (context.get("notify_to") or policy.get("notify_to")
                        or os.getenv("DUNNING_NOTIFY_TO") or "finance@example.com")
            sent = []
            for r in recs:
                subj = f"[수금 안내] {r.get('name')} — {r.get('customer')} ({r.get('tone')})"
                body = ((r.get("message") or "")
                        + f"\n\n(청구서 {r.get('name')}, 미수 {r.get('amount_residual')}, "
                        + f"{r.get('days_overdue')}일 연체)")
                ok = False
                try:
                    from ..services import gmail_service
                    ok = bool(gmail_service.send_reply(
                        to_email=to_email, subject=subj, content=body,
                        user_id=self._user_id))
                except Exception as e:
                    logger.warning(f"[collections.send_dunning] 발송 실패 {r.get('name')}: {e}")
                chat = {}
                if r.get("invoice_id"):
                    chat = await asyncio.to_thread(
                        odoo_service.log_dunning_on_invoice, r["invoice_id"],
                        f"수금 독촉 발송 ({r.get('tone')}): {r.get('message')}")
                sent.append({"name": r.get("name"), "tone": r.get("tone"),
                             "emailed": ok, "chatter": chat.get("ok")})
            return {"action": "send_dunning", "to": to_email, "sent": sent,
                    "narrative": f"독촉 {len(sent)}건 발송 + chatter 기록 → {to_email}"}

        # ③ register_payment — 입금 등록 (결정론) → 인보이스 회수
        async def register_payment(policy: dict, context: dict) -> dict:
            iid = context.get("invoice_id")
            if not iid and context.get("invoice_name"):
                try:
                    r = await asyncio.to_thread(odoo_service.call, "account.move", "search",
                                                [("name", "=", context["invoice_name"])])
                    iid = r[0] if r else 0
                except Exception:
                    iid = 0
            if not iid:
                return {"action": "register_payment", "ok": False,
                        "error": "invoice_id/invoice_name 필요"}
            res = await asyncio.to_thread(odoo_service.register_invoice_payment, int(iid))
            after = res.get("after", {})
            return {"action": "register_payment", "ok": res.get("ok"), "detail": res,
                    "narrative": (f"입금 등록 → {after.get('name')} {after.get('payment_state')} "
                                  f"(미수 {after.get('amount_residual')})")}

        self.register_action("run_dunning", run_dunning,
                             "연체 감지 + dunning advisor(우선순위·톤) 추천")
        self.register_action("send_dunning", send_dunning,
                             "승인된 독촉 발송 + 인보이스 chatter 기록")
        self.register_action("register_payment", register_payment,
                             "입금 등록(account.payment) → 인보이스 회수")
