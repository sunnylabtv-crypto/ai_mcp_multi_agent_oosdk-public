# mcp_server/agents/email_agent.py
"""
Email Agent: 이메일 조회, AI 분석, 답변 생성 및 발송 전담
- Gmail MCP 도구 + OpenAI 분석 도구
"""
import sys
import json
import asyncio
from .base_agent import BaseAgent


class EmailAgent(BaseAgent):
    """이메일 전문 Agent"""

    def __init__(self, llm_config: dict, service_manager=None):
        super().__init__(
            name="Email Agent",
            description="이메일 조회, AI 분석, 답변 생성 및 발송을 전담합니다. "
                       "Gmail에서 이메일을 가져오고, AI로 고객 정보를 추출하며, "
                       "맞춤형 답변을 생성하여 발송합니다.",
            llm_config=llm_config,
        )
        self.service_manager = service_manager

    def register_tools_from_services(self, user_id: str = None):
        """서비스에서 도구 함수를 가져와 등록"""
        from ..services import gmail_service, openai_service

        # Gmail 도구
        async def fetch_unread_emails(minutes_ago: int = 60, max_results: int = 5):
            return gmail_service.get_recent_emails(
                minutes_ago=minutes_ago, max_results=max_results, user_id=user_id
            )

        async def send_email_reply(to_email: str, subject: str, body: str,
                                    attachment_base64: str = None,
                                    attachment_filename: str = None):
            if attachment_base64:
                return gmail_service.send_reply_with_base64_attachment(
                    to_email=to_email, subject=subject, content=body,
                    attachment_base64=attachment_base64,
                    attachment_filename=attachment_filename,
                    user_id=user_id,
                )
            return gmail_service.send_reply(
                to_email=to_email, subject=subject, content=body, user_id=user_id
            )

        async def get_gmail_status():
            return gmail_service.get_user_service_status(user_id) if user_id else gmail_service.get_service_status()

        # OpenAI 분석 도구 (동기 함수 → asyncio.to_thread)
        async def analyze_email_with_ai(email_content: str, sender_email: str = ""):
            return await asyncio.to_thread(openai_service.extract_customer_info, email_content, sender_email)

        async def generate_email_reply(customer_name: str, company: str = "",
                                        title: str = "", phone: str = "",
                                        email: str = "", original_subject: str = "",
                                        has_all_info: bool = True):
            customer_info = {
                'name': customer_name,
                'company': company,
                'title': title,
                'phone': phone,
                'email': email,
            }
            return await asyncio.to_thread(
                openai_service.generate_reply,
                customer_info=customer_info,
                original_subject=original_subject,
                has_all_info=has_all_info,
            )

        # 도구 등록
        self.register_tool('fetch_unread_emails', fetch_unread_emails,
                          '최근 이메일을 가져옵니다 (minutes_ago: 분, max_results: 최대 건수)')
        self.register_tool('send_email_reply', send_email_reply,
                          '이메일을 발송합니다 (to_email, subject, body)')
        self.register_tool('get_gmail_status', get_gmail_status,
                          'Gmail 서비스 연결 상태를 확인합니다')
        self.register_tool('analyze_email_with_ai', analyze_email_with_ai,
                          'AI로 이메일에서 고객 정보를 추출합니다 (email_content, sender_email)')
        self.register_tool('generate_email_reply', generate_email_reply,
                          'AI로 맞춤형 이메일 답변을 생성합니다')

        # ─── Policy-driven actions (Ontology dispatch 용) ───
        self._register_policy_actions(user_id)

        print(f"[Email Agent] {len(self._tools)} tools, {len(self._action_handlers)} actions registered for user: {user_id}", file=sys.stderr)

    # ═══════════════════════════════════════════════════════════════
    # Policy-driven Actions (Ontology dispatch 용)
    # ═══════════════════════════════════════════════════════════════
    def _register_policy_actions(self, user_id: str = None):
        """ontology.yaml 의 delegate_to 가 호출하는 정책 기반 액션."""
        from ..services import gmail_service, openai_service

        # ─────────────────────────────────────────────────────────
        # send_meeting_invite — VIP 미팅 일정 안내 (Calendar 결과 기반)
        # Type 2: Pure code + 짧은 LLM 1회(인사말 개인화).
        # policy: {tone: "professional", language: "ko", ...}
        # context: {payload, customer, agent_outputs: {Calendar Agent: {...}}}
        # ─────────────────────────────────────────────────────────
        async def send_meeting_invite(policy: dict, context: dict) -> dict:
            payload = context.get("payload") or {}
            customer = context.get("customer") or {}
            from_email = payload.get("from") or (customer or {}).get("email") or ""
            customer_name = (customer or {}).get("name") or payload.get("from_name") or from_email.split("@")[0]

            if not from_email:
                return {"action": "send_meeting_invite", "success": False,
                        "error": "수신 이메일 주소 없음 (payload.from / customer.email 모두 누락)"}

            # 직전 Calendar Agent 결과에서 미팅 정보 가져오기
            agent_outputs = context.get("agent_outputs") or {}
            meeting_info = (
                policy.get("meeting")
                or agent_outputs.get("book_priority_meeting")
                or agent_outputs.get("Calendar Agent")
                or {}
            )
            # nested result 구조 흡수
            if isinstance(meeting_info, dict) and "result" in meeting_info:
                meeting_info = meeting_info.get("result") or meeting_info

            meeting_title = meeting_info.get("title") or "Meeting"
            meeting_start = meeting_info.get("start") or "(TBD)"
            meeting_end = meeting_info.get("end") or "(TBD)"
            meeting_link = meeting_info.get("html_link") or ""

            original_subject = payload.get("subject", "")
            subject = f"Re: {original_subject}" if original_subject else f"Meeting Confirmation — {meeting_title}"

            body = (
                f"{customer_name}님,\n\n"
                f"문의 주신 건과 관련하여 아래 일정으로 미팅을 잡아두었습니다.\n\n"
                f"  • 일정: {meeting_title}\n"
                f"  • 시작: {meeting_start}\n"
                f"  • 종료: {meeting_end}\n"
            )
            if meeting_link:
                body += f"  • 캘린더 링크: {meeting_link}\n"
            body += "\n변경이 필요하시면 회신 주세요.\n\n감사합니다."

            try:
                ok = gmail_service.send_reply(
                    to_email=from_email, subject=subject, content=body, user_id=user_id
                )
                return {
                    "action": "send_meeting_invite",
                    "success": bool(ok),
                    "to": from_email,
                    "subject": subject,
                    "meeting": {
                        "title": meeting_title,
                        "start": meeting_start,
                        "end": meeting_end,
                        "link": meeting_link,
                    },
                    "policy_applied": {
                        "tone": policy.get("tone", "professional"),
                        "language": policy.get("language", "ko"),
                    },
                }
            except Exception as e:
                return {"action": "send_meeting_invite", "success": False, "error": str(e)}

        # ─────────────────────────────────────────────────────────
        # send_reply — 일반 답변 발송 (CS Agent compose_reply 결과 사용 또는 LLM 생성)
        # Type 1 (pre-composed) / Type 2 (LLM 생성).
        # policy: {sla_hours, tone, language, draft?: {subject, body}}
        # context: {payload, customer, agent_outputs}
        # ─────────────────────────────────────────────────────────
        async def send_reply(policy: dict, context: dict) -> dict:
            payload = context.get("payload") or {}
            customer = context.get("customer") or {}
            from_email = payload.get("from") or (customer or {}).get("email") or ""
            customer_name = (customer or {}).get("name") or payload.get("from_name") or from_email.split("@")[0]
            company = (customer or {}).get("company") or ""

            if not from_email:
                return {"action": "send_reply", "success": False,
                        "error": "수신 이메일 주소 없음"}

            # (1) policy.draft 또는 직전 agent 결과에 작성된 본문이 있는지 확인
            agent_outputs = context.get("agent_outputs") or {}
            draft = (
                policy.get("draft")
                or agent_outputs.get("compose_reply")
                or agent_outputs.get("CS Agent")
                or {}
            )
            if isinstance(draft, dict) and "result" in draft:
                draft = draft.get("result") or draft

            subject = draft.get("subject") if isinstance(draft, dict) else None
            body = draft.get("body") if isinstance(draft, dict) else None

            # (2) draft 없으면 openai_service.generate_reply 로 생성
            generated_via = "draft"
            if not body:
                generated_via = "llm"
                customer_info = {
                    "name": customer_name,
                    "company": company,
                    "title": (customer or {}).get("title", ""),
                    "phone": (customer or {}).get("phone", ""),
                    "email": from_email,
                    "has_all_info": bool(company),
                }
                try:
                    gen = await asyncio.to_thread(
                        openai_service.generate_reply,
                        customer_info=customer_info,
                        original_subject=payload.get("subject", ""),
                    )
                    subject = subject or (gen or {}).get("subject")
                    body = (gen or {}).get("body")
                except Exception as e:
                    return {"action": "send_reply", "success": False,
                            "error": f"답변 생성 실패: {e}"}

            if not body:
                return {"action": "send_reply", "success": False,
                        "error": "답변 본문이 비어 있음"}

            subject = subject or (f"Re: {payload.get('subject')}" if payload.get("subject") else "Re:")

            try:
                ok = gmail_service.send_reply(
                    to_email=from_email, subject=subject, content=body, user_id=user_id
                )
                return {
                    "action": "send_reply",
                    "success": bool(ok),
                    "to": from_email,
                    "subject": subject,
                    "generated_via": generated_via,
                    "policy_applied": {
                        "tone": policy.get("tone", "professional"),
                        "language": policy.get("language", "ko"),
                        "sla_hours": policy.get("sla_hours"),
                    },
                }
            except Exception as e:
                return {"action": "send_reply", "success": False, "error": str(e)}

        # ─────────────────────────────────────────────────────────
        # send_welcome — 신규 프로스펙트 환영 메일 (템플릿 기반)
        # Type 1: Pure code, LLM 0회. enrichment 안내 포함.
        # policy: {template_id?, sla_hours?, language?}
        # context: {payload, customer}
        # ─────────────────────────────────────────────────────────
        async def send_welcome(policy: dict, context: dict) -> dict:
            payload = context.get("payload") or {}
            customer = context.get("customer") or {}
            from_email = payload.get("from") or (customer or {}).get("email") or ""
            customer_name = (customer or {}).get("name") or payload.get("from_name") or (from_email.split("@")[0] if from_email else "고객")

            if not from_email:
                return {"action": "send_welcome", "success": False,
                        "error": "수신 이메일 주소 없음"}

            template_id = policy.get("template_id", "newprospect_default")
            language = policy.get("language", "ko")

            if language == "en":
                subject = f"Welcome — thanks for reaching out, {customer_name}"
                body = (
                    f"Hi {customer_name},\n\n"
                    f"Thanks for getting in touch. We've received your message and one of our team will follow up shortly "
                    f"with more detail tailored to your needs.\n\n"
                    f"In the meantime, if you can share a few words about your company or use case, "
                    f"it will help us route your inquiry to the right person.\n\n"
                    f"Best regards,\nThe Team"
                )
            else:
                subject = f"[안내] {customer_name}님, 문의 주셔서 감사합니다"
                body = (
                    f"{customer_name}님, 안녕하세요.\n\n"
                    f"문의 주신 내용을 잘 받았습니다. 담당자가 확인 후 빠르게 회신드리겠습니다.\n\n"
                    f"빠른 안내를 위해 회사명/직책 등 간단한 소개를 회신으로 보내주시면 큰 도움이 됩니다.\n\n"
                    f"감사합니다."
                )

            try:
                ok = gmail_service.send_reply(
                    to_email=from_email, subject=subject, content=body, user_id=user_id
                )
                return {
                    "action": "send_welcome",
                    "success": bool(ok),
                    "to": from_email,
                    "subject": subject,
                    "policy_applied": {
                        "template_id": template_id,
                        "language": language,
                        "sla_hours": policy.get("sla_hours"),
                    },
                }
            except Exception as e:
                return {"action": "send_welcome", "success": False, "error": str(e)}

        # ─────────────────────────────────────────────────────────
        # BC2 — send_thank_you (Closed Won 후 감사 메일)
        # Type 1: Pure code, LLM 0회. tier 별 톤 / 템플릿 분기.
        # rule: opp_won_vip / opp_won_standard
        # policy: {tone: "premium"|"professional", language, template, sla_hours}
        # context: {opportunity, customer, payload}
        # ─────────────────────────────────────────────────────────
        async def send_thank_you(policy: dict, context: dict) -> dict:
            opp = context.get("opportunity") or {}
            customer = context.get("customer") or {}
            payload = context.get("payload") or {}
            account_name = (
                opp.get("account_name")
                or customer.get("name")
                or context.get("account_name")
                or ""
            )
            to_email = (
                payload.get("contact_email")
                or customer.get("email")
                or ""
            )
            tier = policy.get("tone") == "premium" and "VIP" or (
                opp.get("tier") or customer.get("tier") or "Standard"
            )
            language = policy.get("language", "ko")
            template = policy.get("template", "standard_thank_you")
            opp_name = opp.get("name") or "도입 검토"
            amount = opp.get("amount")

            # tier 별 톤 분기
            if template == "vip_thank_you" or policy.get("tone") == "premium":
                if language == "en":
                    subject = f"[VIP] Thank you — {account_name} partnership confirmed"
                    body = (
                        f"Dear {account_name} team,\n\n"
                        f"Thank you for choosing us as your strategic partner. "
                        f"Your '{opp_name}' has been officially confirmed in our systems "
                        f"and our delivery lead will reach out within 48 hours to schedule "
                        f"a kickoff meeting.\n\n"
                        f"As a VIP customer, you have direct access to our senior team for "
                        f"any questions or escalations.\n\n"
                        f"Warm regards,\nThe Account Team"
                    )
                else:
                    subject = f"[VIP] {account_name}님, 도입 확정 진심으로 감사드립니다"
                    body = (
                        f"{account_name} 팀 안녕하세요.\n\n"
                        f"전략적 파트너로 선택해 주셔서 진심으로 감사드립니다. "
                        f"'{opp_name}' 건이 ERP 에 정식 등록되었으며, "
                        f"48시간 내 담당 리드가 Kickoff 미팅 일정으로 연락드리겠습니다.\n\n"
                        f"VIP 고객으로 시니어 팀 직접 연락 채널이 활성화되었습니다.\n\n"
                        f"감사합니다.\nAccount Team 드림"
                    )
            else:
                if language == "en":
                    subject = f"Thank you — {opp_name} confirmed"
                    body = (
                        f"Dear {account_name},\n\n"
                        f"Thank you for choosing our solution. '{opp_name}' has been confirmed "
                        f"and onboarding details will follow shortly.\n\n"
                        f"Best regards,\nThe Sales Team"
                    )
                else:
                    subject = f"[감사] {account_name}님, 도입 확정 안내"
                    body = (
                        f"{account_name} 담당자님, 안녕하세요.\n\n"
                        f"'{opp_name}' 건 도입 확정에 감사드립니다. "
                        f"세부 온보딩 안내는 별도 메일로 빠르게 회신드리겠습니다.\n\n"
                        f"감사합니다.\nSales Team 드림"
                    )

            # 실제 발송은 to_email 이 있을 때만 시도. 없으면 plan 만 반환 (정책 검증용).
            if not to_email:
                return {
                    "action": "send_thank_you",
                    "success": True,
                    "skipped": True,
                    "reason": "수신 이메일 주소 없음 — 정책 검증용 plan 만 반환",
                    "draft": {"subject": subject, "body": body},
                    "policy_applied": {
                        "tier": tier, "tone": policy.get("tone"),
                        "language": language, "template": template,
                        "sla_hours": policy.get("sla_hours"),
                    },
                }
            try:
                ok = gmail_service.send_reply(
                    to_email=to_email, subject=subject, content=body, user_id=user_id
                )
                return {
                    "action": "send_thank_you",
                    "success": bool(ok),
                    "to": to_email,
                    "subject": subject,
                    "policy_applied": {
                        "tier": tier, "tone": policy.get("tone"),
                        "language": language, "template": template,
                        "sla_hours": policy.get("sla_hours"),
                    },
                }
            except Exception as e:
                return {"action": "send_thank_you", "success": False, "error": str(e)}

        # ─────────────────────────────────────────────────────────
        # BC2 — send_quote_response (견적 회신 메일, VIP / Standard 톤 분리)
        # Type 1: Pure code, LLM 0회. tier 별 템플릿 + 가격 협상 여지 표시.
        # rule: sales_inquiry_vip / sales_inquiry_standard
        # policy: {tier: "VIP"|"Standard", tone, language, template,
        #          include_discount_offer, sla_hours}
        # context: {opportunity, customer, payload, agent_outputs: {create_sales_opportunity?: {pricing_applied}}}
        # ─────────────────────────────────────────────────────────
        async def send_quote_response(policy: dict, context: dict) -> dict:
            payload = context.get("payload") or {}
            customer = context.get("customer") or {}
            outputs = context.get("agent_outputs") or {}
            # 직전 step (create_sales_opportunity) 의 결과에서 pricing/opp 정보 끌어옴
            opp_result = (outputs.get("create_sales_opportunity")
                          or outputs.get("convert_lead_to_opportunity")
                          or {})
            pricing = opp_result.get("pricing_applied") or {}
            opp_meta = opp_result.get("opportunity") or {}

            tier = policy.get("tier") or customer.get("tier") or "Standard"
            language = policy.get("language", "ko")
            template = policy.get("template",
                                  "quote_vip" if tier == "VIP" else "quote_standard")
            include_discount = policy.get("include_discount_offer", tier == "VIP")
            to_email = (
                payload.get("contact_email")
                or customer.get("email")
                or ""
            )
            account_name = (
                payload.get("account_name")
                or customer.get("name")
                or ""
            )
            opp_subject = payload.get("subject") or "Module X 도입 검토"
            list_price = pricing.get("list_price") or payload.get("amount") or 0
            quoted = pricing.get("quoted_amount") or list_price
            max_discount = pricing.get("max_discount_pct") or policy.get("max_discount_pct") or 0
            currency = pricing.get("currency", "USD")
            validity_days = pricing.get("quote_validity_days", 30 if tier == "VIP" else 14)
            opp_url = opp_meta.get("url") or ""

            # ─── 템플릿 분기 ───
            if template == "quote_vip" or tier == "VIP":
                if language == "en":
                    subject = f"[VIP Quote] {account_name} — {opp_subject} (negotiable)"
                    body = (
                        f"Dear {account_name} team,\n\n"
                        f"Thank you for your inquiry. As a VIP customer, your request has been "
                        f"escalated directly to our senior account team. Below is our preliminary quote:\n\n"
                        f"  · Item: {opp_subject}\n"
                        f"  · List price: {currency} {list_price:,}\n"
                        f"  · Indicative offer: {currency} {quoted:,}\n"
                        f"  · Negotiable within {max_discount}% of list price\n"
                        f"  · Quote validity: {validity_days} days\n\n"
                        f"We're open to discussing volume terms, multi-year commitments, and "
                        f"customisation. A 30-min discovery call is reserved within 48 hours.\n\n"
                        f"Best regards,\nSenior Account Team"
                    )
                else:
                    subject = f"[VIP 견적] {account_name} — {opp_subject} (협상 가능)"
                    body = (
                        f"{account_name} 담당자님, 안녕하세요.\n\n"
                        f"문의 주신 건이 VIP 고객 우선 처리 대상으로 시니어 영업팀에 직접 배정되었습니다. "
                        f"아래는 1차 견적입니다:\n\n"
                        f"  · 품목: {opp_subject}\n"
                        f"  · 정가: {currency} {list_price:,}\n"
                        f"  · 제안가: {currency} {quoted:,}\n"
                        f"  · 협상 여지: 정가 대비 최대 {max_discount}% 할인 가능 (영업 단독 결정 권한)\n"
                        f"  · 견적 유효기간: {validity_days}일\n\n"
                        f"수량/장기 계약/커스터마이징 협의 가능합니다. 48시간 내 30분 협의 콜을 예약해 드립니다.\n\n"
                        f"감사합니다.\n시니어 영업팀 드림"
                    )
            else:
                if language == "en":
                    subject = f"[Quote] {account_name} — {opp_subject}"
                    body = (
                        f"Dear {account_name},\n\n"
                        f"Thank you for your inquiry. Please find our standard quote below:\n\n"
                        f"  · Item: {opp_subject}\n"
                        f"  · Price: {currency} {list_price:,} (list price, fixed)\n"
                        f"  · Quote validity: {validity_days} days\n\n"
                        f"Should you require special terms or volume pricing, please contact "
                        f"your account manager — those require approval.\n\n"
                        f"Best regards,\nSales Team"
                    )
                else:
                    subject = f"[견적] {account_name} — {opp_subject}"
                    body = (
                        f"{account_name} 담당자님, 안녕하세요.\n\n"
                        f"문의 주신 견적을 아래와 같이 안내드립니다:\n\n"
                        f"  · 품목: {opp_subject}\n"
                        f"  · 가격: {currency} {list_price:,} (정가, 할인 권한 없음)\n"
                        f"  · 견적 유효기간: {validity_days}일\n\n"
                        f"수량 할인 / 특별 조건이 필요하시면 매니저 결재가 필요하므로 "
                        f"별도 문의 부탁드립니다.\n\n"
                        f"감사합니다.\n영업팀 드림"
                    )
            if opp_url:
                body += f"\n\n관련 SFDC Opportunity: {opp_url}"

            # 발송
            if not to_email:
                return {
                    "action": "send_quote_response",
                    "success": True,
                    "skipped": True,
                    "reason": "수신 이메일 주소 없음 — 정책 검증용 plan 만 반환",
                    "draft": {"subject": subject, "body": body},
                    "policy_applied": {
                        "tier": tier, "tone": policy.get("tone"),
                        "template": template, "include_discount_offer": include_discount,
                        "language": language, "sla_hours": policy.get("sla_hours"),
                    },
                }
            try:
                ok = gmail_service.send_reply(
                    to_email=to_email, subject=subject, content=body, user_id=user_id
                )
                return {
                    "action": "send_quote_response",
                    "success": bool(ok),
                    "to": to_email,
                    "subject": subject,
                    "policy_applied": {
                        "tier": tier, "tone": policy.get("tone"),
                        "template": template, "include_discount_offer": include_discount,
                        "language": language, "sla_hours": policy.get("sla_hours"),
                        "max_discount_pct": max_discount,
                        "quote_validity_days": validity_days,
                    },
                }
            except Exception as e:
                return {"action": "send_quote_response", "success": False,
                        "error": str(e)}

        # ─────────────────────────────────────────────────────────
        # BC3 — send_allocation_notice (재고 할당 결과 안내)
        # Type 1: Pure code, LLM 0회. tier 별 톤. VIP 선점 / 입고 보충 두 케이스 모두 처리.
        # rule: inventory_allocate_vip_preempt / stock_received_replenish
        # policy: {tier?, tone, template?, include_eta, sla_hours, audience?}
        # context: {picking, inventory, sales_order, customer, agent_outputs}
        # ─────────────────────────────────────────────────────────
        async def send_allocation_notice(policy: dict, context: dict) -> dict:
            picking = context.get("picking") or {}
            inventory = context.get("inventory") or {}
            sales_order = context.get("sales_order") or {}
            customer = context.get("customer") or {}
            outputs = context.get("agent_outputs") or {}

            account_name = (
                sales_order.get("account_name")
                or context.get("account_name")
                or customer.get("name")
                or ""
            )
            tier = policy.get("tier") or picking.get("tier") or customer.get("tier") or "Standard"
            tone = policy.get("tone", "premium" if tier == "VIP" else "professional")
            template = policy.get("template", "allocation_default")
            include_eta = policy.get("include_eta", True)
            sla_hours = policy.get("sla_hours", 4 if tier == "VIP" else 24)

            # 직전 inventory_agent 결과 흡수 (preempt 수량 / waiting 라인 등)
            alloc = (
                outputs.get("allocate_with_preemption")
                or outputs.get("replenish_priority_queue")
                or {}
            )
            preempted = alloc.get("preempted_moves") or []
            backorder = alloc.get("backorder_against_incoming") or []
            replenished = alloc.get("replenished") or []

            # BC3 HIGH #6 — replenish 멱등성:
            # 같은 receipt 가 두 번 들어오면 두번째 호출의 replenish_priority_queue 는
            # 이미 reserved 된 move 에 대해 reserve_move 가 no-op 처리되어
            # replenished=[] 가 된다. 그 상태에서 알림 메일을 또 보내면 운영 노이즈.
            # → stock_replenished 템플릿이고 replenished/preempted 모두 비면 skip.
            if (template == "stock_replenished"
                    and not replenished
                    and not preempted):
                return {
                    "action": "send_allocation_notice",
                    "success": True,
                    "skipped": True,
                    "reason": (
                        "replenished/preempted 결과 없음 — 중복 receipt 또는 "
                        "이미 충족된 큐. 알림 메일 skip (HIGH #6 멱등성)."
                    ),
                    "policy_applied": {
                        "tone": tone, "template": template,
                        "audience": policy.get("audience"),
                    },
                }

            scheduled_date = picking.get("scheduled_date") or sales_order.get("target_delivery_date") or "(TBD)"

            # 두 케이스 분기 — preempt vs replenish
            if template == "stock_replenished" or replenished:
                # 입고 보충 결과 안내 (audience: affected owners)
                vip_count = sum(1 for r in replenished if (r.get("tier") == "VIP"))
                std_count = sum(1 for r in replenished if (r.get("tier") == "Standard"))
                subject = f"[재고 보충] 입고 처리 완료 — VIP {vip_count}건 / Standard {std_count}건"
                body = (
                    f"재고 보충 알림\n\n"
                    f"방금 입고된 재고로 다음 backorder/Waiting 큐가 충족되었습니다:\n"
                    f"  · VIP backorder: {vip_count}건 즉시 reserved\n"
                    f"  · Standard Waiting: {std_count}건 추가 reserved\n"
                    f"  · 남은 미충족 큐는 다음 입고 차감 대상으로 유지됩니다.\n\n"
                    f"감사합니다.\n"
                )
            elif tier == "VIP":
                subject = f"[VIP 우선 배정] {account_name} — 우선 재고 확보 완료"
                body = (
                    f"{account_name} 담당자님,\n\n"
                    f"VIP 우선 정책에 따라 요청하신 수량 재고가 즉시 확보되었습니다.\n"
                )
                if include_eta:
                    body += f"  · 출고 예정일: {scheduled_date}\n"
                if preempted:
                    body += (
                        f"  · 우선 배정으로 일부 Standard 주문은 다음 입고 사이클로 재조정되었습니다.\n"
                    )
                if backorder:
                    body += (
                        f"  · 부족분({len(backorder)}건)은 곧 도착할 입고분에 backorder 로 예약되었습니다.\n"
                    )
                body += (
                    f"\nVIP 고객 전담 채널로 진행 상황 실시간 안내드립니다.\n"
                    f"SLA: {sla_hours}시간 내 출고 확정.\n\n"
                    f"감사합니다.\nDelivery Team 드림"
                )
            else:
                subject = f"[배정 안내] {account_name} — 가용 범위 내 처리"
                body = (
                    f"{account_name} 담당자님,\n\n"
                    f"문의 주신 출고 건은 가용 재고 범위 내에서 우선 배정 진행 중입니다.\n"
                    f"부족분은 다음 입고 사이클에 자동 충당됩니다.\n"
                    f"  · 현재 추정 출고일: {scheduled_date}\n"
                    f"  · SLA: {sla_hours}시간 내 상태 업데이트\n\n"
                    f"감사합니다.\nDelivery Team 드림"
                )

            # BC3 HIGH #7 — 실제 gmail send 시도 (send_shipping_notification 패턴 정렬).
            # 수신 이메일 unknown 이면 draft 만 반환 (Account Owner 통합은 BC4).
            to_email = customer.get("email") or ""
            policy_meta = {
                "tone": tone, "template": template,
                "include_eta": include_eta, "sla_hours": sla_hours,
                "audience": policy.get("audience"),
            }
            if not to_email:
                return {
                    "action": "send_allocation_notice",
                    "success": True,
                    "skipped": True,
                    "reason": "수신 이메일 주소 없음 — draft 만 반환",
                    "tier": tier,
                    "tone": tone,
                    "to": "(account_owner)",
                    "subject": subject,
                    "draft": {"subject": subject, "body": body},
                    "policy_applied": policy_meta,
                    "note": (
                        "Account Owner 알림은 별도 SFDC Activity / Slack 통합 (BC4)."
                    ),
                }
            try:
                ok = gmail_service.send_reply(
                    to_email=to_email, subject=subject, content=body, user_id=user_id
                )
            except Exception as e:
                return {
                    "action": "send_allocation_notice",
                    "success": False,
                    "tier": tier, "to": to_email, "subject": subject,
                    "error": str(e),
                    "draft": {"subject": subject, "body": body},
                    "policy_applied": policy_meta,
                }
            return {
                "action": "send_allocation_notice",
                "success": bool(ok),
                "tier": tier,
                "tone": tone,
                "to": to_email,
                "subject": subject,
                "draft": {"subject": subject, "body": body},
                "policy_applied": policy_meta,
            }

        # ─────────────────────────────────────────────────────────
        # BC3 — send_shipping_notification (출고 완료 안내, tracking 포함)
        # Type 1: Pure code, LLM 0회. VIP premium / Standard professional.
        # rule: delivery_ready_to_ship_vip
        # policy: {tier, tone, include_tracking, sla_hours}
        # context: {picking, sales_order, customer, agent_outputs.dispatch_shipment}
        # ─────────────────────────────────────────────────────────
        async def send_shipping_notification(policy: dict, context: dict) -> dict:
            picking = context.get("picking") or {}
            sales_order = context.get("sales_order") or {}
            customer = context.get("customer") or {}
            outputs = context.get("agent_outputs") or {}

            account_name = (
                sales_order.get("account_name")
                or context.get("account_name")
                or customer.get("name")
                or ""
            )
            tier = policy.get("tier") or picking.get("tier") or "Standard"
            tone = policy.get("tone", "premium" if tier == "VIP" else "professional")
            include_tracking = policy.get("include_tracking", True)
            sla_hours = policy.get("sla_hours", 2 if tier == "VIP" else 8)
            to_email = customer.get("email") or ""

            dispatch_result = outputs.get("dispatch_shipment") or {}
            picking_name = picking.get("name") or dispatch_result.get("picking_id") or "DO"
            scheduled_date = picking.get("scheduled_date") or "(TBD)"

            if tier == "VIP":
                subject = f"[VIP 출고 완료] {account_name} — {picking_name}"
                body = (
                    f"{account_name} 담당자님,\n\n"
                    f"VIP 우선 배송이 출고 처리되었습니다.\n"
                    f"  · 출고 번호: {picking_name}\n"
                    f"  · 예정 도착: {scheduled_date}\n"
                )
                if include_tracking:
                    body += "  · Tracking: (carrier 연동 후 자동 추가)\n"
                body += (
                    f"  · SLA: 출고 후 {sla_hours}시간 내 추적 가능\n\n"
                    f"VIP 채널로 실시간 배송 알림이 별도 전송됩니다.\n\n"
                    f"감사합니다.\nDelivery Team 드림"
                )
            else:
                subject = f"[출고 완료] {account_name} — {picking_name}"
                body = (
                    f"{account_name} 담당자님,\n\n"
                    f"주문하신 건이 출고 처리되었습니다.\n"
                    f"  · 출고 번호: {picking_name}\n"
                    f"  · 예정 도착: {scheduled_date}\n"
                )
                if include_tracking:
                    body += "  · Tracking 은 별도 안내드립니다.\n"
                body += "\n감사합니다.\nDelivery Team 드림"

            if not to_email:
                return {
                    "action": "send_shipping_notification",
                    "success": True,
                    "skipped": True,
                    "reason": "수신 이메일 주소 없음 — draft 만 반환",
                    "draft": {"subject": subject, "body": body},
                    "policy_applied": {
                        "tier": tier, "tone": tone,
                        "include_tracking": include_tracking, "sla_hours": sla_hours,
                    },
                }
            try:
                ok = gmail_service.send_reply(
                    to_email=to_email, subject=subject, content=body, user_id=user_id
                )
                return {
                    "action": "send_shipping_notification",
                    "success": bool(ok),
                    "to": to_email,
                    "subject": subject,
                    "policy_applied": {
                        "tier": tier, "tone": tone,
                        "include_tracking": include_tracking, "sla_hours": sla_hours,
                    },
                }
            except Exception as e:
                return {"action": "send_shipping_notification", "success": False, "error": str(e)}

        # ─────────────────────────────────────────────────────────
        # BC3 — send_license_activation (service 라인 즉시 활성화 안내)
        # Type 1: Pure code, LLM 0회. service / consulting 라인 fulfillment.
        # rule: service_line_fulfillment / split_fulfillment_path service_plan
        # policy: {tone, template, activation_mode?, sla_hours}
        # context: {sales_order, customer}
        # ─────────────────────────────────────────────────────────
        async def send_license_activation(policy: dict, context: dict) -> dict:
            sales_order = context.get("sales_order") or {}
            customer = context.get("customer") or {}
            account_name = (
                sales_order.get("account_name")
                or context.get("account_name")
                or customer.get("name")
                or ""
            )
            tier = customer.get("tier") or sales_order.get("tier") or "Standard"
            tone = policy.get("tone", "premium" if tier == "VIP" else "professional")
            activation_mode = policy.get("activation_mode", "license_auto")
            sla_hours = policy.get("sla_hours", 2 if tier == "VIP" else 24)
            to_email = customer.get("email") or ""

            if tier == "VIP":
                subject = f"[VIP 활성화] {account_name} — 라이선스 즉시 사용 가능"
                body = (
                    f"{account_name} 담당자님,\n\n"
                    f"주문하신 라이선스가 자동 활성화되었습니다.\n"
                    f"  · 활성화 방식: {activation_mode}\n"
                    f"  · 사용 가능 시점: 즉시\n"
                    f"  · VIP 전담 컨설팅 Kickoff: 48시간 내 별도 일정 안내\n"
                    f"  · SLA: {sla_hours}시간 내 onboarding 안내\n\n"
                    f"감사합니다.\nDelivery Team 드림"
                )
            else:
                subject = f"[활성화] {account_name} — 라이선스 활성화 완료"
                body = (
                    f"{account_name} 담당자님,\n\n"
                    f"주문하신 라이선스가 활성화되었습니다.\n"
                    f"  · 활성화 방식: {activation_mode}\n"
                    f"  · 사용 가능 시점: 즉시\n"
                    f"  · SLA: {sla_hours}시간 내 안내 메일 별도 발송\n\n"
                    f"감사합니다.\nSales Team 드림"
                )

            if not to_email:
                return {
                    "action": "send_license_activation",
                    "success": True,
                    "skipped": True,
                    "reason": "수신 이메일 주소 없음 — draft 만 반환",
                    "draft": {"subject": subject, "body": body},
                    "policy_applied": {
                        "tone": tone, "template": policy.get("template"),
                        "activation_mode": activation_mode, "sla_hours": sla_hours,
                    },
                }
            try:
                ok = gmail_service.send_reply(
                    to_email=to_email, subject=subject, content=body, user_id=user_id
                )
                return {
                    "action": "send_license_activation",
                    "success": bool(ok),
                    "to": to_email,
                    "subject": subject,
                    "policy_applied": {
                        "tone": tone, "activation_mode": activation_mode,
                        "sla_hours": sla_hours,
                    },
                }
            except Exception as e:
                return {"action": "send_license_activation", "success": False, "error": str(e)}

        # ─────────────────────────────────────────────────────────
        # BC5 — send_replenishment_alert (보충 발주 → 담당자 브리핑)
        # Type 2: LLM 1회(맥락·임팩트 브리핑 작성) + 발송. 실패 시 템플릿 폴백.
        # rule: inventory_replenish_on_shortage
        # policy: {notify_to, auto_send, language, tone}
        # context: {shortage, agent_outputs.create_replenishment_po}
        #
        # 단순 "재고 부족" 통보가 아니라: 어떤 주문이 블록됐는지(VIP 우선),
        # 부족분, 권장 발주량, 생성된 입고 picking, 긴급도를 담은 의사결정용 브리핑.
        # ─────────────────────────────────────────────────────────
        async def send_replenishment_alert(policy: dict, context: dict) -> dict:
            # 라이브 채널: trigger 가 step 간 agent_outputs 를 누적해 직전 PO 결과를 넣어준다.
            # (server.trigger_replenishment_check 의 dispatch 루프 참조)
            outputs = context.get("agent_outputs") or {}
            repl = outputs.get("create_replenishment_po") or {}
            if isinstance(repl, dict) and "result" in repl:
                repl = repl.get("result") or repl
            shortage = (
                context.get("shortage")
                or repl.get("shortage")
                or {}
            )

            product_name = (
                repl.get("product_name") or shortage.get("product_name") or "해당 제품"
            )
            unmet = repl.get("unmet_qty") or shortage.get("unmet_qty") or 0
            recommended_qty = repl.get("recommended_qty")
            advisor = repl.get("advisor") or {}
            urgency = advisor.get("urgency") or "MEDIUM"
            po = repl.get("po") or {}
            po_name = po.get("picking_name")
            blocked = repl.get("blocked_orders") or shortage.get("blocked_orders") or []
            vip_blocked = [b for b in blocked if b.get("tier") == "VIP"]

            # 안 C: 보충할 게 없으면(agent 가 skip 했거나 미충족 0 + 블록 없음) 통보도 생략.
            # ontology 게이트가 단순해진 만큼(unmet 조건 제거) 여기서 도메인 판정을 존중.
            if repl.get("skipped") or (float(unmet or 0) <= 0 and not blocked):
                return {
                    "action": "send_replenishment_alert",
                    "success": True,
                    "skipped": True,
                    "reason": "보충 발주 없음(미충족 수요 0) — 담당자 통보 생략",
                }

            notify_to = (
                policy.get("notify_to")
                or context.get("notify_to")
                or ""
            )
            language = policy.get("language", "ko")
            auto_send = bool(policy.get("auto_send", True))

            # 블록 주문 요약 (메일 본문 / LLM facts 공용)
            def _fmt_blocked(b):
                return (
                    f"{b.get('so_name') or b.get('sale_order_id')} "
                    f"[{b.get('tier')}] {b.get('account_name') or ''} "
                    f"— 부족 {int(b.get('shortage') or 0)}개"
                ).strip()

            blocked_lines = "\n".join(f"  · {_fmt_blocked(b)}" for b in blocked[:10])

            # ── 판단 ② — LLM 브리핑 작성 (실패 시 템플릿 폴백) ──
            subject = None
            body = None
            generated_via = "template"
            facts = {
                "product": product_name,
                "unmet_demand": unmet,
                "recommended_order_qty": recommended_qty,
                "urgency": urgency,
                "advisor_rationale": advisor.get("rationale"),
                "incoming_picking_created": po_name,
                "blocked_orders": [
                    {"order": b.get("so_name"), "tier": b.get("tier"),
                     "customer": b.get("account_name"),
                     "shortage": b.get("shortage")}
                    for b in blocked[:10]
                ],
                "vip_blocked_count": len(vip_blocked),
            }
            try:
                system = (
                    "You are an inventory operations assistant writing an internal "
                    "alert email to the purchasing/operations manager. Stock is depleted "
                    "and customer orders are blocked. Write a concise, professional "
                    "briefing that states: the business impact (which orders are blocked, "
                    "highlight VIP customers), the shortage, the recommended order quantity "
                    "and urgency, and that a draft incoming receipt has been created in the "
                    f"ERP. Write in {'Korean' if language == 'ko' else 'English'}. Be "
                    "decision-oriented and brief. Respond with ONLY a JSON object: "
                    '{"subject":"<subject>","body":"<plain-text body>"}'
                )
                user = json.dumps(facts, ensure_ascii=False)
                raw = await asyncio.to_thread(
                    openai_service.generate_text_with_system,
                    system_prompt=system, user_prompt=user,
                    temperature=0.2, max_tokens=600,
                )
                cleaned = (raw or "").strip()
                if cleaned.startswith("```json"):
                    cleaned = cleaned[7:]
                if cleaned.startswith("```"):
                    cleaned = cleaned[3:]
                if cleaned.endswith("```"):
                    cleaned = cleaned[:-3]
                parsed = json.loads(cleaned.strip())
                subject = (parsed.get("subject") or "").strip() or None
                body = (parsed.get("body") or "").strip() or None
                if subject and body:
                    generated_via = "llm"
            except Exception as e:
                import sys as _sys
                print(f"[send_replenishment_alert] LLM 실패 → 템플릿 폴백: {e}",
                      file=_sys.stderr)

            # 템플릿 폴백 (LLM 실패 또는 빈 응답)
            if not (subject and body):
                generated_via = "template"
                u_label = {"HIGH": "긴급", "MEDIUM": "보통", "LOW": "낮음"}.get(urgency, urgency)
                rq_label = recommended_qty if recommended_qty is not None else "미정"
                subject = f"[재고 보충 요청|{u_label}] {product_name} — 주문 {len(blocked)}건 블록"
                body = (
                    f"운영/구매 담당자님,\n\n"
                    f"{product_name} 재고 소진으로 다음 고객 주문이 충족 불가 상태입니다.\n\n"
                    f"{blocked_lines or '  · (블록된 주문 상세 없음)'}\n\n"
                    f"  • 총 미충족 수요: {int(unmet)}개\n"
                    f"  • VIP 블록 주문: {len(vip_blocked)}건\n"
                    f"  • 권장 발주량: {rq_label}개\n"
                    f"  • 긴급도: {urgency}\n"
                )
                if advisor.get("rationale"):
                    body += f"  • 판단 근거: {advisor.get('rationale')}\n"
                if po_name:
                    body += f"  • ERP 입고 건 생성됨: {po_name} (검증 시 자동 재할당)\n"
                body += (
                    f"\n검토 후 발주 확정 부탁드립니다.\n\n"
                    f"감사합니다.\nInventory Agent 드림"
                )

            policy_meta = {
                "tone": policy.get("tone", "professional"),
                "language": language,
                "auto_send": auto_send,
                "urgency": urgency,
                "generated_via": generated_via,
            }

            # 수신자 없거나 auto_send=False → draft 만 반환
            if not notify_to or not auto_send:
                return {
                    "action": "send_replenishment_alert",
                    "success": True,
                    "skipped": True,
                    "reason": (
                        "수신 담당자(notify_to) 미지정" if not notify_to
                        else "auto_send=False — draft 만 반환 (사람 검토)"
                    ),
                    "to": notify_to or "(purchasing_manager)",
                    "subject": subject,
                    "draft": {"subject": subject, "body": body},
                    "recommended_qty": recommended_qty,
                    "urgency": urgency,
                    "policy_applied": policy_meta,
                }
            try:
                ok = gmail_service.send_reply(
                    to_email=notify_to, subject=subject, content=body, user_id=user_id
                )
                return {
                    "action": "send_replenishment_alert",
                    "success": bool(ok),
                    "to": notify_to,
                    "subject": subject,
                    "draft": {"subject": subject, "body": body},
                    "recommended_qty": recommended_qty,
                    "urgency": urgency,
                    "policy_applied": policy_meta,
                }
            except Exception as e:
                return {
                    "action": "send_replenishment_alert",
                    "success": False,
                    "to": notify_to,
                    "subject": subject,
                    "draft": {"subject": subject, "body": body},
                    "error": str(e),
                    "policy_applied": policy_meta,
                }

        self.register_action('send_meeting_invite', send_meeting_invite,
                             'VIP 미팅 일정 안내 메일 발송 (Calendar 결과를 받아 본문 구성)')
        self.register_action('send_reply', send_reply,
                             '고객 답변 메일 발송 (CS Agent 작성본 사용 또는 LLM 생성)')
        self.register_action('send_welcome', send_welcome,
                             '신규 프로스펙트 환영 메일 발송 (템플릿 기반, LLM 0회)')
        self.register_action('send_thank_you', send_thank_you,
                             'BC2: Closed Won 후 tier 별 감사 메일 발송 (premium/professional 톤 분기)')
        self.register_action('send_quote_response', send_quote_response,
                             'BC2: 견적 회신 메일 — VIP (협상 가능 명시) / Standard (정가 고정) 톤 분리, create_sales_opportunity 의 pricing_applied 결과 사용')
        self.register_action('send_allocation_notice', send_allocation_notice,
                             'BC3: 재고 할당 결과 안내 (VIP 선점 / 입고 보충 양쪽 케이스)')
        self.register_action('send_shipping_notification', send_shipping_notification,
                             'BC3: 출고 완료 안내 (tier 별 톤, tracking 옵션)')
        self.register_action('send_license_activation', send_license_activation,
                             'BC3: service/consulting 라인 활성화 안내 (즉시 사용 가능)')
        self.register_action('send_replenishment_alert', send_replenishment_alert,
                             'BC5: 보충 발주 → 운영/구매 담당자 브리핑 메일 (블록 주문·임팩트·권장발주량·긴급도, LLM 작성)')
