# mcp_server/agents/calendar_agent.py
"""
Calendar Agent: Google Calendar 전담
- 일정 생성, 조회, 수정, 삭제, 검색
"""
import sys
from .base_agent import BaseAgent


class CalendarAgent(BaseAgent):
    """Google Calendar 전문 Agent"""

    def __init__(self, llm_config: dict, service_manager=None):
        super().__init__(
            name="Calendar Agent",
            description="Google Calendar에서 일정 생성, 조회, 수정, 삭제를 전담합니다. "
                       "미팅 일정을 잡거나, 앞으로의 일정을 확인하고, "
                       "일정을 검색합니다.",
            llm_config=llm_config,
        )
        self.service_manager = service_manager

    def register_tools_from_services(self, user_id: str = None):
        """서비스에서 도구 함수를 가져와 등록"""
        from ..services import calendar_service

        async def add_calendar_event(title: str, start_datetime: str, end_datetime: str,
                                      description: str = "", location: str = ""):
            return calendar_service.create_event(
                title=title, start_datetime=start_datetime,
                end_datetime=end_datetime, description=description,
                location=location, user_id=user_id,
            )

        async def get_calendar_events(days: int = 7, max_results: int = 10):
            return calendar_service.get_events(
                days=days, max_results=max_results, user_id=user_id
            )

        async def update_calendar_event(event_id: str, title: str = None,
                                         start_datetime: str = None,
                                         end_datetime: str = None,
                                         description: str = None,
                                         location: str = None):
            return calendar_service.update_event(
                event_id=event_id, title=title,
                start_datetime=start_datetime, end_datetime=end_datetime,
                description=description, location=location, user_id=user_id,
            )

        async def delete_calendar_event(event_id: str):
            return calendar_service.delete_event(event_id=event_id, user_id=user_id)

        async def search_calendar_events(query: str, days: int = 30):
            return calendar_service.search_events(
                query=query, days=days, user_id=user_id
            )

        async def get_calendar_status():
            return calendar_service.get_user_service_status(user_id) if user_id else calendar_service.get_service_status()

        self.register_tool('add_calendar_event', add_calendar_event,
                          '새 일정을 생성합니다 (title, start_datetime, end_datetime)')
        self.register_tool('get_calendar_events', get_calendar_events,
                          '앞으로의 일정을 조회합니다 (days: 일수, max_results: 최대 건수)')
        self.register_tool('update_calendar_event', update_calendar_event,
                          '기존 일정을 수정합니다 (event_id 필수)')
        self.register_tool('delete_calendar_event', delete_calendar_event,
                          '일정을 삭제합니다 (event_id)')
        self.register_tool('search_calendar_events', search_calendar_events,
                          '키워드로 일정을 검색합니다 (query, days)')
        self.register_tool('get_calendar_status', get_calendar_status,
                          'Google Calendar 서비스 연결 상태를 확인합니다')

        # ─── Policy-driven actions (Ontology dispatch 용) ───
        # Type 1: Pure code 핸들러. LLM 0회.
        self._register_policy_actions(user_id)

        print(f"[Calendar Agent] {len(self._tools)} tools, {len(self._action_handlers)} actions registered for user: {user_id}", file=sys.stderr)

    # ═══════════════════════════════════════════════════════════════
    # Policy-driven Actions (Ontology dispatch 용)
    # ═══════════════════════════════════════════════════════════════
    def _register_policy_actions(self, user_id: str = None):
        """ontology.yaml 의 delegate_to 가 호출하는 정책 기반 액션."""
        from datetime import datetime, timedelta
        from ..services import calendar_service

        # ─────────────────────────────────────────────────────────
        # book_priority_meeting — VIP 우선 미팅 슬롯 자동 예약
        # Type 1: Pure code — LLM 0회.
        # policy: {sla_hours: 24, duration_min: 30, priority: high, ...}
        # context: {payload: {from, subject, body}, customer, ...}
        # ─────────────────────────────────────────────────────────
        async def book_priority_meeting(policy: dict, context: dict) -> dict:
            sla_hours = int(policy.get("sla_hours", 24))
            duration_min = int(policy.get("duration_min", 30))
            priority = policy.get("priority", "high")

            payload = context.get("payload") or {}
            customer = context.get("customer") or {}
            from_email = payload.get("from") or (customer or {}).get("email") or ""
            customer_name = (customer or {}).get("name") or payload.get("from_name") or from_email.split("@")[0]
            customer_company = (customer or {}).get("company") or ""

            # 1) SLA window 내 첫 빈 슬롯 찾기 (간이 로직 — 다음 영업시간 슬롯)
            now = datetime.now()
            # 단순 휴리스틱: 오늘 다음 정시부터 시작, 09-18시 사이만, SLA 내
            candidate = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            sla_deadline = now + timedelta(hours=sla_hours)

            # 영업시간 강제 (09-18) — 오늘 18시 이후면 다음날 09시
            while True:
                if candidate > sla_deadline:
                    # SLA 내 자리 못 잡으면 그냥 SLA 마감 직전에라도 잡음
                    candidate = sla_deadline - timedelta(minutes=duration_min + 5)
                    break
                if candidate.hour < 9:
                    candidate = candidate.replace(hour=9, minute=0)
                    continue
                if candidate.hour >= 18:
                    next_day = candidate + timedelta(days=1)
                    candidate = next_day.replace(hour=9, minute=0)
                    continue
                break

            start_dt = candidate
            end_dt = start_dt + timedelta(minutes=duration_min)

            title = f"[{priority.upper()}] {customer_name} ({customer_company}) — Discovery Call"
            description = (
                f"Auto-scheduled by Ontology policy (priority={priority}, sla={sla_hours}h).\n"
                f"From: {from_email}\n"
                f"Subject: {payload.get('subject', '')}\n"
            )

            try:
                # calendar_service.create_event 직접 호출 (LLM think 우회)
                result = calendar_service.create_event(
                    title=title,
                    start_datetime=start_dt.strftime("%Y-%m-%d %H:%M"),
                    end_datetime=end_dt.strftime("%Y-%m-%d %H:%M"),
                    description=description,
                    location="(TBD)",
                    user_id=user_id,
                )
                if result and result.get("id"):
                    return {
                        "action": "book_priority_meeting",
                        "success": True,
                        "event_id": result.get("id"),
                        "title": title,
                        "start": start_dt.isoformat(),
                        "end": end_dt.isoformat(),
                        "html_link": result.get("html_link"),
                        "policy_applied": {
                            "sla_hours": sla_hours,
                            "duration_min": duration_min,
                            "priority": priority,
                        },
                    }
                return {"action": "book_priority_meeting", "success": False,
                        "error": "create_event returned None — Calendar 응답 확인 필요"}
            except Exception as e:
                return {"action": "book_priority_meeting", "success": False,
                        "error": str(e)}

        # ─────────────────────────────────────────────────────────
        # BC2 — book_kickoff_meeting (VIP Closed Won 후 60분 Kickoff)
        # Type 1: Pure code — LLM 0회. book_priority_meeting 의 변형.
        # rule: opp_won_vip
        # policy: {sla_hours: 48, duration_min: 60, priority, attendees_role}
        # context: {opportunity, customer, account_name}
        # ─────────────────────────────────────────────────────────
        async def book_kickoff_meeting(policy: dict, context: dict) -> dict:
            sla_hours = int(policy.get("sla_hours", 48))
            duration_min = int(policy.get("duration_min", 60))
            priority = policy.get("priority", "high")

            opp = context.get("opportunity") or {}
            customer = context.get("customer") or {}
            account_name = (
                opp.get("account_name")
                or customer.get("name")
                or context.get("account_name")
                or "VIP Account"
            )
            opp_name = opp.get("name") or "도입"

            now = datetime.now()
            candidate = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)
            sla_deadline = now + timedelta(hours=sla_hours)
            while True:
                if candidate > sla_deadline:
                    candidate = sla_deadline - timedelta(minutes=duration_min + 5)
                    break
                if candidate.hour < 9:
                    candidate = candidate.replace(hour=9, minute=0)
                    continue
                if candidate.hour >= 18:
                    next_day = candidate + timedelta(days=1)
                    candidate = next_day.replace(hour=9, minute=0)
                    continue
                break

            start_dt = candidate
            end_dt = start_dt + timedelta(minutes=duration_min)
            title = f"[KICKOFF — {priority.upper()}] {account_name} — {opp_name}"
            attendees_role = policy.get("attendees_role", []) or []
            description = (
                f"Auto-scheduled by Ontology policy (BC2 opp_won_vip).\n"
                f"Account: {account_name}\nOpportunity: {opp_name}\n"
                f"SLA: {sla_hours}h, Duration: {duration_min}min\n"
                f"Required roles: {', '.join(attendees_role) if attendees_role else 'TBD'}\n"
            )

            try:
                result = calendar_service.create_event(
                    title=title,
                    start_datetime=start_dt.strftime("%Y-%m-%d %H:%M"),
                    end_datetime=end_dt.strftime("%Y-%m-%d %H:%M"),
                    description=description,
                    location="(TBD — Kickoff)",
                    user_id=user_id,
                )
                if result and result.get("id"):
                    return {
                        "action": "book_kickoff_meeting",
                        "success": True,
                        "event_id": result.get("id"),
                        "title": title,
                        "start": start_dt.isoformat(),
                        "end": end_dt.isoformat(),
                        "html_link": result.get("html_link"),
                        "policy_applied": {
                            "sla_hours": sla_hours,
                            "duration_min": duration_min,
                            "priority": priority,
                            "attendees_role": attendees_role,
                        },
                        "note": "VIP Closed Won 후 후속 담당자 Kickoff 미팅 — 60분, 48h SLA",
                    }
                # Calendar 미설정 시 plan 만 반환 (정책 검증용)
                return {
                    "action": "book_kickoff_meeting",
                    "success": True,
                    "skipped": True,
                    "reason": "Calendar 미연결 — plan 만 반환",
                    "intended_event": {
                        "title": title,
                        "start": start_dt.isoformat(),
                        "end": end_dt.isoformat(),
                    },
                    "policy_applied": {
                        "sla_hours": sla_hours, "duration_min": duration_min,
                        "priority": priority, "attendees_role": attendees_role,
                    },
                }
            except Exception as e:
                return {"action": "book_kickoff_meeting", "success": False,
                        "error": str(e)}

        # ─────────────────────────────────────────────────────────
        # BC3 — update_delivery_milestone (출고 마일스톤 캘린더 표기)
        # Type 1: Pure code, LLM 0회. shipped / delivered 등 단계 표시.
        # rule: delivery_ready_to_ship_vip
        # policy: {tier, milestone: "shipped"|"delivered", ...}
        # context: {picking, sales_order, customer, agent_outputs.dispatch_shipment}
        # ─────────────────────────────────────────────────────────
        async def update_delivery_milestone(policy: dict, context: dict) -> dict:
            picking = context.get("picking") or {}
            sales_order = context.get("sales_order") or {}
            customer = context.get("customer") or {}
            outputs = context.get("agent_outputs") or {}

            tier = policy.get("tier") or picking.get("tier") or customer.get("tier") or "Standard"
            milestone = policy.get("milestone", "shipped")
            account_name = (
                sales_order.get("account_name")
                or context.get("account_name")
                or customer.get("name")
                or ""
            )
            picking_name = picking.get("name") or "DO"
            scheduled = picking.get("scheduled_date") or sales_order.get("target_delivery_date") or ""

            # 시연 목적 — Calendar 미설정 시 plan 만 반환.
            # 실제 환경에선 calendar_service.create_event 또는 update_event 호출.
            dispatch_result = outputs.get("dispatch_shipment") or {}

            title = f"[{tier} {milestone.upper()}] {account_name} — {picking_name}"
            description = (
                f"Auto-logged by Ontology policy (BC3 delivery_ready_to_ship_{tier.lower()}).\n"
                f"Picking: {picking_name}\n"
                f"Scheduled: {scheduled}\n"
                f"Validation result: {dispatch_result.get('validation_result') or 'pending'}\n"
            )

            # 실제로는 milestone 별 캘린더 이벤트 / 작업 생성하지 않고 기록만.
            # 추후 SFDC Task / Slack ping 으로 확장 가능 (BC4).
            return {
                "action": "update_delivery_milestone",
                "success": True,
                "tier": tier,
                "milestone": milestone,
                "logged": {
                    "title": title,
                    "scheduled": scheduled,
                    "account_name": account_name,
                    "picking_name": picking_name,
                },
                "policy_applied": policy,
                "note": "현재는 메모리/감사용 echo. Calendar / SFDC Task 통합은 BC4 확장.",
            }

        self.register_action('book_priority_meeting', book_priority_meeting,
                             'SLA 내 우선 미팅 슬롯 자동 예약 (VIP 정책용)')
        self.register_action('book_kickoff_meeting', book_kickoff_meeting,
                             'BC2: VIP Closed Won 후 Kickoff 미팅 자동 예약 (60분, 48h SLA)')
        self.register_action('update_delivery_milestone', update_delivery_milestone,
                             'BC3: 출고/배송 마일스톤 캘린더/감사 로그 기록 (VIP shipped 등)')
