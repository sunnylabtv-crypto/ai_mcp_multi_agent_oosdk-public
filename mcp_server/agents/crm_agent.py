# mcp_server/agents/crm_agent.py
"""
CRM Agent: Salesforce CRM 전담
- Lead 생성, 조회, 관리
"""
import sys
from .base_agent import BaseAgent


class CRMAgent(BaseAgent):
    """Salesforce CRM 전문 Agent"""

    def __init__(self, llm_config: dict, service_manager=None):
        super().__init__(
            name="CRM Agent",
            description="Salesforce CRM에서 Lead 생성, 조회, 관리를 전담합니다. "
                       "고객 정보를 받아 Salesforce에 Lead를 등록하고, "
                       "기존 Lead를 조회/검증합니다.",
            llm_config=llm_config,
        )
        self.service_manager = service_manager

    def register_tools_from_services(self, user_id: str = None):
        """서비스에서 도구 함수를 가져와 등록"""
        from ..services import salesforce_service

        async def create_salesforce_lead(customer_name: str, customer_company: str,
                                          customer_email: str, customer_title: str = "",
                                          customer_phone: str = ""):
            customer_info = {
                'name': customer_name,
                'company': customer_company,
                'email': customer_email,
                'title': customer_title,
                'phone': customer_phone,
            }
            return salesforce_service.create_lead(customer_info, user_id=user_id)

        async def verify_salesforce_lead(lead_id: str):
            return salesforce_service.verify_lead(lead_id, user_id=user_id)

        async def search_lead_by_email(email: str):
            """이메일로 기존 Lead 조회 — Customer Tier(VIP/Standard) 식별에 사용"""
            return salesforce_service.search_leads_by_email(email, user_id=user_id)

        async def get_salesforce_status():
            return salesforce_service.get_user_service_status(user_id) if user_id else salesforce_service.get_service_status()

        # ─── BC2: Opportunity / Account / SOQL 도구 ───
        async def search_account_by_name(name: str):
            """이름으로 Account 조회 — Tier(CustomerPriority) 식별에 사용"""
            return salesforce_service.search_account_by_name(name, user_id=user_id)

        async def query_soql(soql: str):
            """범용 SOQL 쿼리 실행 (Account/Opportunity/RecordType 조회)"""
            return salesforce_service.query_soql(soql, user_id=user_id)

        async def create_opportunity(account_id: str, name: str, stage: str,
                                      record_type_dev_name: str = None,
                                      amount: float = None,
                                      close_date: str = None):
            """
            Salesforce Opportunity 생성.
            - account_id: 부모 Account Id (필수)
            - name: Opportunity 이름 (필수)
            - stage: StageName — Sales Process에 정의된 stage (필수)
            - record_type_dev_name: 'Opp_VIP' 또는 'Opp_Standard'
            - amount, close_date: 선택
            """
            return salesforce_service.create_opportunity(
                account_id=account_id,
                name=name,
                stage=stage,
                record_type_dev_name=record_type_dev_name,
                amount=amount,
                close_date=close_date,
                user_id=user_id,
            )

        async def verify_opportunity(opp_id: str):
            """생성된 Opportunity의 RecordType, Stage, Account 정보 검증"""
            return salesforce_service.verify_opportunity(opp_id, user_id=user_id)

        self.register_tool('create_salesforce_lead', create_salesforce_lead,
                          'Salesforce에 새 Lead를 생성합니다 (customer_name, customer_company, customer_email)')
        self.register_tool('verify_salesforce_lead', verify_salesforce_lead,
                          'Salesforce Lead 정보를 조회합니다 (lead_id)')
        self.register_tool('search_lead_by_email', search_lead_by_email,
                          '이메일 주소로 기존 Lead를 검색합니다. Customer Tier(VIP/Standard) 등 고객 등급 식별 시 사용 (email)')
        self.register_tool('get_salesforce_status', get_salesforce_status,
                          'Salesforce 서비스 연결 상태를 확인합니다')
        # BC2 신규 도구
        self.register_tool('search_account_by_name', search_account_by_name,
                          '이름으로 Salesforce Account를 조회합니다. CustomerPriority(VIP/Standard)와 함께 반환 (name)')
        self.register_tool('query_soql', query_soql,
                          '범용 SOQL 쿼리를 실행합니다. RecordType Id 조회, Account/Opportunity 검색 등에 사용 (soql)')
        self.register_tool('create_opportunity', create_opportunity,
                          'Salesforce Opportunity를 생성합니다. record_type_dev_name 으로 Opp_VIP / Opp_Standard 지정 가능 (account_id, name, stage, record_type_dev_name, amount, close_date)')
        self.register_tool('verify_opportunity', verify_opportunity,
                          '생성된 Opportunity의 RecordType, Stage, Account 정보를 조회/검증합니다 (opp_id)')

        # ─── Policy-driven actions (Ontology dispatch 용) ───
        # Type 1: Pure code. LLM 0회. 시퀀스가 비즈니스로 명확.
        self._register_policy_actions(user_id)

        print(f"[CRM Agent] {len(self._tools)} tools, {len(self._action_handlers)} actions registered for user: {user_id}", file=sys.stderr)

    # ═══════════════════════════════════════════════════════════════
    # Policy-driven Actions (Ontology dispatch 용)
    # ═══════════════════════════════════════════════════════════════
    def _register_policy_actions(self, user_id: str = None):
        """ontology.yaml 의 delegate_to 가 호출하는 정책 기반 액션."""
        from ..services import salesforce_service

        # ─────────────────────────────────────────────────────────
        # create_qualified_lead — 신규 프로스펙트 Lead 생성 (멱등성 보장)
        # Type 1: Pure code — LLM 0회.
        # policy: {status: "Open - Not Contacted", enrichment: required, ...}
        # context: {payload: {from, from_name, subject, body}, customer, ...}
        # ─────────────────────────────────────────────────────────
        async def create_qualified_lead(policy: dict, context: dict) -> dict:
            payload = context.get("payload") or {}
            from_email = payload.get("from") or context.get("from_email") or ""
            from_name = payload.get("from_name") or ""

            if not from_email:
                return {"action": "create_qualified_lead", "success": False,
                        "error": "from_email 누락"}

            # (1) 멱등성: 이미 동일 이메일 Lead 가 SFDC 에 있으면 skip
            try:
                existing = salesforce_service.search_leads_by_email(from_email, user_id=user_id)
            except Exception as e:
                existing = None
                print(f"[CRM Agent.create_qualified_lead] search_leads_by_email 실패: {e}", file=sys.stderr)

            if existing:
                return {
                    "action": "create_qualified_lead",
                    "skipped": True,
                    "reason": "이미 SFDC 에 동일 이메일 Lead 존재 (멱등성 보장)",
                    "existing_lead": existing,
                }

            # (2) 결정론적 필드 매핑 — agent LLM 우회
            name_for_lead = from_name or from_email.split("@")[0]
            customer_info = {
                "name": name_for_lead,
                "company": "(unknown — to be enriched)",
                "email": from_email,
                "title": "",
                "phone": "",
            }
            try:
                new_lead_id = salesforce_service.create_lead(customer_info, user_id=user_id)
                if new_lead_id:
                    return {
                        "action": "create_qualified_lead",
                        "success": True,
                        "lead_id": new_lead_id,
                        "params": customer_info,
                        "policy_applied": {
                            "status": policy.get("status", "Open - Not Contacted"),
                            "enrichment": policy.get("enrichment", "required"),
                        },
                        "note": "Company/Title 은 enrichment 단계에서 채워질 예정",
                    }
                return {"action": "create_qualified_lead", "success": False,
                        "error": "create_lead returned None — SFDC 응답 확인 필요"}
            except Exception as e:
                return {"action": "create_qualified_lead", "success": False,
                        "error": str(e)}

        # ─────────────────────────────────────────────────────────
        # log_interaction — SFDC 에 고객 응답/미팅 활동 기록
        # Type 1: Pure code. 메모리/감사 추적용 (실제 SFDC Activity 생성은 후속).
        # policy: {activity_type: "vip_meeting_request", ...}
        # ─────────────────────────────────────────────────────────
        async def log_interaction(policy: dict, context: dict) -> dict:
            payload = context.get("payload") or {}
            customer = context.get("customer") or {}
            activity_type = policy.get("activity_type", "email_received")

            # 현재는 메모리/대시보드 추적용 echo. SFDC Activity API 통합은 별도 작업.
            return {
                "action": "log_interaction",
                "success": True,
                "logged": {
                    "activity_type": activity_type,
                    "customer_id": customer.get("id") if customer else None,
                    "customer_email": (customer or {}).get("email") or payload.get("from"),
                    "subject": payload.get("subject"),
                    "tier": (customer or {}).get("tier"),
                },
                "note": "메모리 추적용 echo — SFDC Activity 통합은 후속 작업",
            }

        # ─────────────────────────────────────────────────────────
        # BC2 — create_sales_opportunity (견적 문의 → SFDC Opp 생성)
        # Type 1: Pure code — LLM 0회. tier 별 RecordType + Sales Process 적용.
        #
        # 분기:
        #   · context.lead_id 또는 context.lead 가 주어지면 Lead Convert 흐름 (Lead 마감 + Opp 생성)
        #     - SFDC 정식 Lead Convert API 는 Apex Database.LeadConvert 가 필요해 직접 호출 불가.
        #     - 대신 (a) Lead.Status = "Closed - Converted" 로 PATCH, (b) Account 매칭/생성,
        #       (c) Opp 생성 + Lead.ConvertedOpportunityId 연결을 따로 수행 (REST 만으로).
        #   · 그 외 경우 Account 직접 Opp 생성.
        #
        # policy: {tier, record_type_dev_name, initial_stage, sales_process, stages,
        #          require_analysis, close_date_days, convert_lead_if_present,
        #          lead_status_on_convert, pricing: {mode, max_discount_pct, ...}}
        # context: {payload: {account_name, amount, contact_email, subject, lead_id?,
        #                     products?: [{name, type, qty, price}]},
        #           customer: {tier, name, ...}, lead?: {id, customer_tier, ...}}
        # ─────────────────────────────────────────────────────────
        async def create_sales_opportunity(policy: dict, context: dict) -> dict:
            from datetime import date, timedelta
            payload = context.get("payload") or {}
            customer = context.get("customer") or {}
            lead_ctx = context.get("lead") or {}
            lead_id = (
                payload.get("lead_id")
                or lead_ctx.get("id")
                or lead_ctx.get("Id")
            )

            # ─── (0) Lead 가 주어지고 정책이 허용하면 Lead 정보 보강 후 Convert ───
            lead_record = None
            lead_converted = False
            if lead_id and policy.get("convert_lead_if_present", True):
                try:
                    lead_record = salesforce_service.get_lead(lead_id, user_id=user_id)
                except Exception as e:
                    print(f"[CRM.create_sales_opportunity] get_lead 실패: {e}",
                          file=sys.stderr)
                # Lead.Customer_Tier__c 가 있으면 정책의 tier 보다 우선 — "Lead 의 tier 를 따른다"
                if lead_record and lead_record.get("Customer_Tier__c"):
                    lead_tier = lead_record["Customer_Tier__c"]
                    customer = dict(customer)
                    customer["tier"] = lead_tier

            tier = policy.get("tier") or customer.get("tier") or "Standard"
            # tier 보정: lead 의 customer_tier 가 정책과 다르면 lead 우선
            if lead_record and lead_record.get("Customer_Tier__c"):
                tier = lead_record["Customer_Tier__c"]
            record_type_dev_name = (
                "Opp_VIP" if tier == "VIP" else "Opp_Standard"
            )

            account_name = (
                payload.get("account_name")
                or context.get("account_name")
                or customer.get("name")
                or (lead_record or {}).get("Company")
                or ""
            )
            initial_stage = policy.get("initial_stage", "Qualification")
            amount = payload.get("amount")
            opp_subject = payload.get("subject") or "Module X 도입 검토"
            opp_name = payload.get("opportunity_name") or f"{account_name} - {opp_subject}"

            if not account_name:
                return {
                    "action": "create_sales_opportunity",
                    "success": False,
                    "error": "account_name 누락 (Lead.Company 도 없음)",
                }

            # ─── (1) Account Id 결정 ───
            # 우선순위:
            #   1) payload.account_id (직접 주입, SFDC adapter 우회 — demo / 운영 안정성)
            #   2) context.account.id (engine resolve_links 가 lookup 한 결과)
            #   3) search_account_by_name (마지막 시도, session 문제 시 실패)
            account = context.get("account") or {}
            account_id = (
                payload.get("account_id")
                or account.get("id") or account.get("Id")
            )
            if not account_id:
                try:
                    found = salesforce_service.search_account_by_name(
                        account_name, user_id=user_id
                    )
                    if found:
                        account_id = found.get("Id") or found.get("id")
                except Exception as e:
                    print(f"[CRM.create_sales_opportunity] search_account 실패: {e}",
                          file=sys.stderr)
            if not account_id:
                return {
                    "action": "create_sales_opportunity",
                    "success": False,
                    "error": (
                        f"Account '{account_name}' 미발견 — "
                        f"payload.account_id 로 직접 주입하거나 SFDC 세션 확인 필요"
                    ),
                    "policy_applied": {"tier": tier, "record_type": record_type_dev_name},
                }

            # ─── (2) Pricing 정책 적용 (할인/마크업) ───
            pricing = policy.get("pricing", {}) or {}
            quoted_amount = amount
            discount_applied = 0.0
            requested_discount_pct = float(payload.get("requested_discount_pct", 0) or 0)
            if amount and requested_discount_pct > 0:
                if pricing.get("mode") == "negotiable":
                    max_pct = float(pricing.get("max_discount_pct", 0))
                    discount_pct = min(requested_discount_pct, max_pct)
                    discount_applied = round(amount * (discount_pct / 100.0), 2)
                    quoted_amount = round(amount - discount_applied, 2)
                else:
                    # list_price 모드 — 할인 권한 없음
                    discount_applied = 0.0
                    quoted_amount = amount

            close_date_days = int(policy.get("close_date_days", 60))
            close_date = (date.today() + timedelta(days=close_date_days)).isoformat()

            # ─── (3) Opportunity 생성 ───
            try:
                created = salesforce_service.create_opportunity(
                    account_id=account_id,
                    name=opp_name,
                    stage=initial_stage,
                    record_type_dev_name=record_type_dev_name,
                    amount=quoted_amount,
                    close_date=close_date,
                    user_id=user_id,
                )
                if not created:
                    return {
                        "action": "create_sales_opportunity",
                        "success": False,
                        "error": "create_opportunity returned None",
                    }
            except Exception as e:
                return {
                    "action": "create_sales_opportunity",
                    "success": False, "error": str(e),
                }

            # ─── (4) Lead Convert (있을 시) — Lead.Status / ConvertedOpportunityId ───
            if lead_id and policy.get("convert_lead_if_present", True):
                lead_status = policy.get("lead_status_on_convert", "Closed - Converted")
                try:
                    ok = salesforce_service.update_lead(
                        lead_id,
                        {
                            "Status": lead_status,
                            # Note: ConvertedOpportunityId 는 SFDC 가 readonly 로 막을 수 있음.
                            # 본 시연에서는 Status 만 변경 + Description 에 Opp Id 추적.
                            "Description": f"Converted to Opportunity: {created.get('id')}",
                        },
                        user_id=user_id,
                    )
                    lead_converted = bool(ok)
                except Exception as e:
                    print(f"[CRM.create_sales_opportunity] update_lead 실패: {e}",
                          file=sys.stderr)

            return {
                "action": "create_sales_opportunity",
                "success": True,
                "opportunity": created,
                "lead_conversion": {
                    "lead_id": lead_id,
                    "converted": lead_converted,
                    "lead_status": policy.get("lead_status_on_convert"),
                    "lead_customer_tier": (lead_record or {}).get("Customer_Tier__c"),
                } if lead_id else None,
                "pricing_applied": {
                    "mode": pricing.get("mode", "list_price"),
                    "list_price": amount,
                    "requested_discount_pct": requested_discount_pct,
                    "max_discount_pct": pricing.get("max_discount_pct", 0),
                    "discount_applied": discount_applied,
                    "quoted_amount": quoted_amount,
                    "currency": pricing.get("currency", "USD"),
                    "quote_validity_days": pricing.get("quote_validity_days", 30),
                    "approval_required": (
                        requested_discount_pct
                        > float(pricing.get("approval_required_above_pct", 0))
                    ),
                },
                "policy_applied": {
                    "tier": tier,
                    "record_type": record_type_dev_name,
                    "sales_process": policy.get("sales_process"),
                    "stages": policy.get("stages", []),
                    "require_analysis": policy.get("require_analysis", False),
                    "stage": initial_stage,
                    "close_date": close_date,
                },
                "note": (
                    "VIP 5-stage (Qualification → Analysis ★ → Quote → Win/Lost) — 가격 협상 가능"
                    if tier == "VIP"
                    else "Standard 4-stage (Qualification → Quote → Win/Lost) — 정가 고정"
                ),
            }

        # ─────────────────────────────────────────────────────────
        # BC2 — convert_lead_to_opportunity (Lead → Opp 단독 진입점)
        # Type 1: Pure code. Lead 만 주어졌을 때 (예: Lead 폼 제출 webhook)
        # context.payload.lead_id 만 받아서 안에서 모든 분기 수행.
        # 내부적으로 create_sales_opportunity 와 동일 로직 재사용.
        # ─────────────────────────────────────────────────────────
        async def convert_lead_to_opportunity(policy: dict, context: dict) -> dict:
            payload = context.get("payload") or {}
            lead_id = payload.get("lead_id") or (context.get("lead") or {}).get("id")
            if not lead_id:
                return {
                    "action": "convert_lead_to_opportunity",
                    "success": False,
                    "error": "lead_id 누락 — payload.lead_id 또는 context.lead.id 필요",
                }
            # context 에 lead 주입 후 create_sales_opportunity 재사용
            ctx2 = dict(context)
            ctx2["lead"] = {"id": lead_id}
            ctx2.setdefault("payload", {})["lead_id"] = lead_id
            policy2 = dict(policy)
            policy2["convert_lead_if_present"] = True
            res = await create_sales_opportunity(policy2, ctx2)
            res["action"] = "convert_lead_to_opportunity"
            return res

        # ─────────────────────────────────────────────────────────
        # BC2 — create_reengage_task (Closed Lost 후 N일 뒤 follow-up Task)
        # Type 1: Pure code — LLM 0회.
        # policy: {due_date_days: 180, subject_template: "Re-engage 검토 — {{ account_name }}", ...}
        # context: {opportunity, account_name, customer}
        # ─────────────────────────────────────────────────────────
        async def create_reengage_task(policy: dict, context: dict) -> dict:
            from datetime import date, timedelta
            opp = context.get("opportunity") or {}
            account_name = (
                opp.get("account_name")
                or context.get("account_name")
                or (context.get("customer") or {}).get("name")
                or ""
            )
            due_days = int(policy.get("due_date_days", 180))
            due_date = (date.today() + timedelta(days=due_days)).isoformat()
            subject_template = policy.get(
                "subject_template", "Re-engage 검토 — {{ account_name }}"
            )
            subject = (
                subject_template
                .replace("{{ account_name }}", account_name)
                .replace("{{account_name}}", account_name)
            )

            # 현재 단계: SFDC Task API 통합은 별도 작업 — 메모리/감사 추적용 echo
            # (실제 SFDC Task 생성은 update_opportunity / Task POST 로 후속 PR 에서 추가)
            return {
                "action": "create_reengage_task",
                "success": True,
                "scheduled": {
                    "subject": subject,
                    "due_date": due_date,
                    "account_name": account_name,
                    "opp_id": opp.get("id"),
                    "opp_name": opp.get("name"),
                    "priority": policy.get("priority", "normal"),
                },
                "note": (
                    "메모리 추적용 echo — SFDC Task 생성 통합은 후속 작업. "
                    "Closed Lost 분기 정책 검증용으로는 충분."
                ),
            }

        self.register_action('create_qualified_lead', create_qualified_lead,
                             '신규 프로스펙트용 SFDC Lead 생성 (멱등성 보장)')
        self.register_action('log_interaction', log_interaction,
                             '고객 응대/미팅 활동을 기록')
        self.register_action('create_sales_opportunity', create_sales_opportunity,
                             'BC2: tier 별 RecordType (Opp_VIP/Opp_Standard) + Sales Process 로 Opportunity 생성. context.lead 가 있으면 Lead Convert 후 생성, pricing 정책으로 할인/마크업 적용.')
        self.register_action('convert_lead_to_opportunity', convert_lead_to_opportunity,
                             'BC2: Lead 단독 진입 — payload.lead_id 만 받아 Lead 마감 + Opp 생성 (Customer_Tier__c → RecordType 자동 매핑)')
        self.register_action('create_reengage_task', create_reengage_task,
                             'BC2: Closed Lost 후 N일 뒤 Re-engage 검토 Task 예약')
