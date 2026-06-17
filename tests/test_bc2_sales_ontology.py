# tests/test_bc2_sales_ontology.py
"""
BC2 — Sales Opportunity 분기 ontology 테스트
============================================
검증 시나리오 (4개):
  1. VIP 견적 문의 (sales_opportunity_inquiry + tier=VIP)
     → rule: sales_inquiry_vip → CRM Agent.create_sales_opportunity (Opp_VIP)
  2. Standard 견적 문의 (sales_opportunity_inquiry + tier=Standard)
     → rule: sales_inquiry_standard → CRM Agent.create_sales_opportunity (Opp_Standard)
  3. VIP Closed Won
     → rule: opp_won_vip → ERP + Email + Calendar + CRM(log) [4 agents]
  4. Standard Closed Won
     → rule: opp_won_standard → ERP + Email + CRM(log) [3 agents]
  5. Closed Lost (any tier)
     → rule: opp_lost → Analytics + CRM(reengage) + CRM(log) [3 agents]

이 테스트는 SFDC/Odoo 실호출 없이 ontology engine 의 rule matching + plan 생성만 검증합니다.
실제 dispatch (action 호출) 는 별도 통합 테스트.

실행:
    cd ai_mcp_multi_agent_oosdk
    python -m pytest tests/test_bc2_sales_ontology.py -v
혹은 직접:
    python tests/test_bc2_sales_ontology.py
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

# 프로젝트 루트를 path에 추가
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from mcp_server.ontology_engine.engine import OntologyEngine
from mcp_server.ontology_engine.memory.facade import ThreeTierMemory


def _make_engine(tmpdir: str):
    """이 테스트용 OntologyEngine — tmpdir 안에 sqlite/jsonl 만 생성."""
    yaml_path = PROJECT_ROOT / "ontology" / "ontology.yaml"
    memory = ThreeTierMemory({
        "hot":  {"backend": "in_memory", "ttl_sec": 3600, "max_size": 100},
        "warm": {"backend": "sqlite", "ttl_sec": 3600,
                 "path": os.path.join(tmpdir, "warm_test.db")},
        "cold": {"backend": "jsonl", "path": os.path.join(tmpdir, "cold/")},
    })
    return OntologyEngine(str(yaml_path), memory=memory)


class TestBC2SalesOntology(unittest.TestCase):
    """BC2 sales opportunity 분기 룰의 매칭 + plan 생성 검증."""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp(prefix="bc2_test_")
        cls.engine = _make_engine(cls._tmpdir)

    @classmethod
    def tearDownClass(cls):
        import shutil
        try:
            shutil.rmtree(cls._tmpdir, ignore_errors=True)
        except Exception:
            pass

    # ────────────────────────────────────────────────────────────
    # 견적 문의 인입 → Opp 생성 분기
    # ────────────────────────────────────────────────────────────
    def test_vip_sales_inquiry_matches_vip_rule(self):
        """VIP 견적 문의 → sales_inquiry_vip 매칭 + Opp_VIP + 협상 가능 pricing + 견적 메일."""
        payload = {
            "id": "test_vip_inquiry_1",
            "account_name": "VIP Tech",
            "tier": "VIP",
            "contact_email": "buyer@viptech.com",
            "subject": "Module X 도입 검토",
            "amount": 120000,
        }
        ctx = self.engine.resolve_links("sales_opportunity_inquiry", payload)
        action = self.engine.check_rules(ctx)
        plan = self.engine.trigger_events(action, ctx)

        self.assertIsNotNone(action, "rule이 매칭되어야 함")
        self.assertEqual(action["rule_name"], "sales_inquiry_vip")
        # v1.4: 3 step (create_sales_opportunity + send_quote_response + log_interaction)
        self.assertEqual(len(plan), 3)
        agents_actions = [(s["agent"], s["action"]) for s in plan]
        self.assertIn(("crm_agent", "create_sales_opportunity"), agents_actions)
        self.assertIn(("email_agent", "send_quote_response"), agents_actions)
        self.assertIn(("crm_agent", "log_interaction"), agents_actions)

        # 정책: tier=VIP, record_type=Opp_VIP, 5-stage + pricing.negotiable
        opp_step = next(s for s in plan if s["action"] == "create_sales_opportunity")
        policy = opp_step["policy"]
        self.assertEqual(policy["tier"], "VIP")
        self.assertEqual(policy["record_type_dev_name"], "Opp_VIP")
        self.assertEqual(policy["sales_process"], "VIP_Sales_Process")
        self.assertEqual(policy["initial_stage"], "Qualification")
        self.assertTrue(policy["require_analysis"])
        self.assertEqual(
            policy["stages"],
            ["Qualification", "Analysis", "Quote", "Closed Won", "Closed Lost"],
        )
        # 가격 협상 정책 검증
        pricing = policy["pricing"]
        self.assertEqual(pricing["mode"], "negotiable")
        self.assertGreater(pricing["max_discount_pct"], 0)
        self.assertTrue(policy["convert_lead_if_present"])

        # 견적 회신 메일 정책: VIP premium 톤
        email_step = next(s for s in plan if s["agent"] == "email_agent")
        self.assertEqual(email_step["policy"]["tier"], "VIP")
        self.assertEqual(email_step["policy"]["tone"], "premium")
        self.assertEqual(email_step["policy"]["template"], "quote_vip")
        self.assertTrue(email_step["policy"]["include_discount_offer"])

    def test_standard_sales_inquiry_matches_standard_rule(self):
        """Standard 견적 문의 → sales_inquiry_standard + 정가 + Standard 견적 메일."""
        payload = {
            "id": "test_std_inquiry_1",
            "account_name": "Standard Tech",
            "tier": "Standard",
            "amount": 5000,
        }
        ctx = self.engine.resolve_links("sales_opportunity_inquiry", payload)
        action = self.engine.check_rules(ctx)
        plan = self.engine.trigger_events(action, ctx)

        self.assertEqual(action["rule_name"], "sales_inquiry_standard")
        self.assertEqual(len(plan), 3)
        agents_actions = [(s["agent"], s["action"]) for s in plan]
        self.assertIn(("crm_agent", "create_sales_opportunity"), agents_actions)
        self.assertIn(("email_agent", "send_quote_response"), agents_actions)
        self.assertIn(("crm_agent", "log_interaction"), agents_actions)

        opp_step = next(s for s in plan if s["action"] == "create_sales_opportunity")
        policy = opp_step["policy"]
        self.assertEqual(policy["tier"], "Standard")
        self.assertEqual(policy["record_type_dev_name"], "Opp_Standard")
        self.assertEqual(policy["sales_process"], "Standard_Sales_Process")
        self.assertFalse(policy["require_analysis"])
        # Standard 는 4 stages (Analysis 없음)
        self.assertEqual(
            policy["stages"], ["Qualification", "Quote", "Closed Won", "Closed Lost"]
        )
        # 정가 고정 정책
        pricing = policy["pricing"]
        self.assertEqual(pricing["mode"], "list_price")
        self.assertEqual(pricing["max_discount_pct"], 0)

        # 견적 회신 메일: Standard professional 톤, 할인 offer 없음
        email_step = next(s for s in plan if s["agent"] == "email_agent")
        self.assertEqual(email_step["policy"]["tier"], "Standard")
        self.assertEqual(email_step["policy"]["tone"], "professional")
        self.assertEqual(email_step["policy"]["template"], "quote_standard")
        self.assertFalse(email_step["policy"]["include_discount_offer"])

    # ────────────────────────────────────────────────────────────
    # Closed Won 분기 — VIP 4 agents / Standard 3 agents
    # ────────────────────────────────────────────────────────────
    def test_vip_closed_won_dispatches_4_agents(self):
        """VIP Closed Won → opp_won_vip (ERP+Email+Calendar+CRM, 4 step)."""
        payload = {
            "id": "test_vip_won_1",
            "account_name": "VIP Tech",
            "tier": "VIP",
            "opportunity": {
                "id": "0061x00000ABCDE",
                "name": "VIP Tech - Module X 도입 검토",
                "stage": "Closed Won",
                "tier": "VIP",
                "account_name": "VIP Tech",
                "amount": 120000,
            },
        }
        ctx = self.engine.resolve_links("sales_opportunity_close", payload)
        action = self.engine.check_rules(ctx)
        plan = self.engine.trigger_events(action, ctx)

        self.assertEqual(action["rule_name"], "opp_won_vip")
        # Win VIP 정책: 4 에이전트 동시 발화
        self.assertEqual(len(plan), 4, f"VIP Win 은 4 step 이어야 함. 실제: {len(plan)}")

        # 호출 순서: erp → email → calendar → crm(log)
        agents_actions = [(s["agent"], s["action"]) for s in plan]
        self.assertIn(("erp_agent", "create_sales_order"), agents_actions)
        self.assertIn(("email_agent", "send_thank_you"), agents_actions)
        self.assertIn(("calendar_agent", "book_kickoff_meeting"), agents_actions)
        self.assertIn(("crm_agent", "log_interaction"), agents_actions)

        # ERP 정책: tier=VIP, target_state=sale, 즉시 확정
        erp_step = next(s for s in plan if s["agent"] == "erp_agent")
        self.assertEqual(erp_step["policy"]["tier"], "VIP")
        self.assertEqual(erp_step["policy"]["target_state"], "sale")
        self.assertTrue(erp_step["policy"]["confirm_immediately"])

        # Email 정책: premium 톤
        email_step = next(s for s in plan if s["agent"] == "email_agent")
        self.assertEqual(email_step["policy"]["tone"], "premium")
        self.assertEqual(email_step["policy"]["template"], "vip_thank_you")

        # Calendar 정책: 60분, 48h SLA
        cal_step = next(s for s in plan if s["agent"] == "calendar_agent")
        self.assertEqual(cal_step["policy"]["duration_min"], 60)
        self.assertEqual(cal_step["policy"]["sla_hours"], 48)

    def test_standard_closed_won_dispatches_3_agents(self):
        """Standard Closed Won → opp_won_standard (ERP+Email+CRM, 3 step)."""
        payload = {
            "id": "test_std_won_1",
            "account_name": "Standard Tech",
            "tier": "Standard",
            "opportunity": {
                "id": "0061x00000FGHIJ",
                "name": "Standard Tech - Module X",
                "stage": "Closed Won",
                "tier": "Standard",
                "account_name": "Standard Tech",
                "amount": 5000,
            },
        }
        ctx = self.engine.resolve_links("sales_opportunity_close", payload)
        action = self.engine.check_rules(ctx)
        plan = self.engine.trigger_events(action, ctx)

        self.assertEqual(action["rule_name"], "opp_won_standard")
        self.assertEqual(len(plan), 3)

        agents_actions = [(s["agent"], s["action"]) for s in plan]
        self.assertIn(("erp_agent", "create_sales_order"), agents_actions)
        self.assertIn(("email_agent", "send_thank_you"), agents_actions)
        self.assertIn(("crm_agent", "log_interaction"), agents_actions)
        # Standard 는 Calendar (Kickoff) 발화 안 함
        self.assertNotIn(("calendar_agent", "book_kickoff_meeting"), agents_actions)

        # Email 정책: professional 톤 (premium 아님)
        email_step = next(s for s in plan if s["agent"] == "email_agent")
        self.assertEqual(email_step["policy"]["tone"], "professional")

    # ────────────────────────────────────────────────────────────
    # Closed Lost 분기 — Analytics 발화, ERP는 SKIP
    # ────────────────────────────────────────────────────────────
    def test_closed_lost_dispatches_analytics_not_erp(self):
        """Closed Lost → opp_lost (Analytics+CRM_reengage+CRM_log, 3 step). ERP push 안 됨."""
        payload = {
            "id": "test_lost_1",
            "account_name": "VIP Tech",
            "tier": "VIP",
            "opportunity": {
                "id": "0061x00000LOST1",
                "name": "VIP Tech - 보류 건",
                "stage": "Closed Lost",
                "tier": "VIP",
                "account_name": "VIP Tech",
                "amount": 80000,
                "lost_reason": "타사 가격이 더 낮아 결정 보류",
            },
        }
        ctx = self.engine.resolve_links("sales_opportunity_close", payload)
        action = self.engine.check_rules(ctx)
        plan = self.engine.trigger_events(action, ctx)

        self.assertEqual(action["rule_name"], "opp_lost")
        self.assertEqual(len(plan), 3)

        agents_actions = [(s["agent"], s["action"]) for s in plan]
        # Analytics + Re-engage + Log
        self.assertIn(("analytics_agent", "analyze_lost_reason"), agents_actions)
        self.assertIn(("crm_agent", "create_reengage_task"), agents_actions)
        self.assertIn(("crm_agent", "log_interaction"), agents_actions)
        # ERP 는 Lost 시 호출 안 됨 (정책 분기 — BC2 핵심 검증)
        self.assertNotIn(("erp_agent", "create_sales_order"), agents_actions)

        # Re-engage Task 정책: 180일 후 follow-up
        reengage = next(s for s in plan
                        if s["agent"] == "crm_agent" and s["action"] == "create_reengage_task")
        self.assertEqual(reengage["policy"]["due_date_days"], 180)

    # ────────────────────────────────────────────────────────────
    # 회귀 — 기존 BC1 email 룰이 여전히 매칭되는지 검증
    # ────────────────────────────────────────────────────────────
    def test_bc1_email_vip_still_matches(self):
        """기존 BC1 email entity_type 의 VIP 룰이 회귀로 깨지지 않았는지."""
        payload = {
            "id": "test_bc1_vip",
            "from": "buyer@viptech.com",
            "from_name": "VIP Buyer",
            "subject": "도입 문의",
        }
        # adapter 없이 customer 가 None 일 수 있어 mock 으로 직접 customer 주입
        ctx = self.engine.resolve_links("email", payload)
        # 강제로 VIP customer 주입 (SFDC adapter mock 대용)
        ctx["customer"] = {"id": "00Q0", "name": "VIP Buyer", "tier": "VIP",
                           "company": "VIP Tech"}
        action = self.engine.check_rules(ctx)
        self.assertIsNotNone(action)
        self.assertEqual(action["rule_name"], "existing_vip")

    def test_bc1_email_new_prospect_still_matches(self):
        """BC1 new_prospect 룰 — entity=email + customer=null."""
        payload = {"id": "test_bc1_new", "from": "stranger@unknown.com"}
        ctx = self.engine.resolve_links("email", payload)
        ctx["customer"] = None
        action = self.engine.check_rules(ctx)
        self.assertIsNotNone(action)
        self.assertEqual(action["rule_name"], "new_prospect")

    # ────────────────────────────────────────────────────────────
    # entity 격리 — sales 룰이 email 컨텍스트에 매칭되면 안 됨
    # ────────────────────────────────────────────────────────────
    def test_sales_rules_dont_match_email_context(self):
        """email entity 인데 sales_inquiry_* 룰이 매칭되면 회귀 (entity 분리 검증)."""
        payload = {"id": "test_iso", "from": "buyer@viptech.com"}
        ctx = self.engine.resolve_links("email", payload)
        ctx["customer"] = {"id": "00Q0", "tier": "VIP", "name": "VIP Buyer"}
        action = self.engine.check_rules(ctx)
        # email 컨텍스트면 BC1 룰 (existing_vip) 만 매칭. sales 룰 매칭 X.
        self.assertNotIn(action["rule_name"],
                         {"sales_inquiry_vip", "sales_inquiry_standard",
                          "opp_won_vip", "opp_won_standard", "opp_lost"})


# ────────────────────────────────────────────────────────────
# Action 핸들러 단위 테스트 (Odoo/SFDC 실호출 없이)
# ────────────────────────────────────────────────────────────
class TestBC2ActionHandlers(unittest.TestCase):
    """신규 액션 핸들러의 정책 분기 / fallback 동작 검증."""

    def test_vip_pricing_policy_in_yaml(self):
        """yaml 의 sales_inquiry_vip 룰에 가격 협상 정책이 명시되어 있어야 한다."""
        import yaml as _yaml
        with open(PROJECT_ROOT / "ontology" / "ontology.yaml", encoding="utf-8") as f:
            spec = _yaml.safe_load(f)
        rule = spec["rules"]["sales_inquiry_vip"]
        opp_step = next(d for d in rule["then"]["delegate_to"]
                        if d["action"] == "create_sales_opportunity")
        pricing = opp_step["policy"]["pricing"]
        self.assertEqual(pricing["mode"], "negotiable")
        self.assertEqual(pricing["max_discount_pct"], 25)
        self.assertEqual(pricing["markup_pct"], 0)

    def test_standard_pricing_policy_in_yaml(self):
        """yaml 의 sales_inquiry_standard 룰은 list_price 고정 (할인 권한 없음)."""
        import yaml as _yaml
        with open(PROJECT_ROOT / "ontology" / "ontology.yaml", encoding="utf-8") as f:
            spec = _yaml.safe_load(f)
        rule = spec["rules"]["sales_inquiry_standard"]
        opp_step = next(d for d in rule["then"]["delegate_to"]
                        if d["action"] == "create_sales_opportunity")
        pricing = opp_step["policy"]["pricing"]
        self.assertEqual(pricing["mode"], "list_price")
        self.assertEqual(pricing["max_discount_pct"], 0)

    def test_send_quote_response_actions_in_rules(self):
        """sales_inquiry_* 룰이 send_quote_response 를 dispatch 해야 한다 (VIP/Standard)."""
        import yaml as _yaml
        with open(PROJECT_ROOT / "ontology" / "ontology.yaml", encoding="utf-8") as f:
            spec = _yaml.safe_load(f)
        for rule_name, expected_template, expected_offer in [
            ("sales_inquiry_vip", "quote_vip", True),
            ("sales_inquiry_standard", "quote_standard", False),
        ]:
            rule = spec["rules"][rule_name]
            email_step = next(
                d for d in rule["then"]["delegate_to"]
                if d["action"] == "send_quote_response"
            )
            self.assertEqual(email_step["agent"], "email_agent")
            self.assertEqual(email_step["policy"]["template"], expected_template)
            self.assertEqual(
                email_step["policy"]["include_discount_offer"], expected_offer
            )

    def test_lead_object_type_has_customer_tier_field(self):
        """Lead 객체에 customer_tier 필드가 정의돼있어야 함 (BC2 Lead Convert 흐름)."""
        import yaml as _yaml
        with open(PROJECT_ROOT / "ontology" / "ontology.yaml", encoding="utf-8") as f:
            spec = _yaml.safe_load(f)
        lead = spec["object_types"]["Lead"]
        field_names = {f["name"] for f in lead["fields"]}
        self.assertIn("customer_tier", field_names)
        # field_map 이 SFDC Customer_Tier__c 와 매핑되는지
        fmap = lead["source"].get("field_map", {})
        self.assertEqual(fmap.get("customer_tier"), "Customer_Tier__c")

    def test_analytics_categorize_reason(self):
        """Lost reason 키워드 → 정형 카테고리."""
        from mcp_server.agents.analytics_agent import _categorize_reason
        self.assertEqual(_categorize_reason("타사 가격이 더 낮아 결정 보류"), "price")
        self.assertEqual(_categorize_reason("경쟁사 제품 선택"), "competitor")
        self.assertEqual(_categorize_reason("내년에 다시 검토"), "timing")
        self.assertEqual(_categorize_reason("우리 요구 기능이 부족"), "feature_gap")
        self.assertEqual(_categorize_reason(""), "unspecified")
        self.assertEqual(_categorize_reason("기타 사유"), "other")

    def test_erp_returns_intended_plan_when_odoo_missing(self):
        """ODOO_API_KEY 미설정 시 intended_plan 만 반환 (정책 검증용)."""
        import asyncio
        from mcp_server.agents.erp_agent import ERPAgent

        # ODOO_API_KEY 임시 제거
        old = os.environ.pop("ODOO_API_KEY", None)
        try:
            agent = ERPAgent(llm_config={})
            agent.register_tools_from_services(user_id="admin")

            async def _run():
                return await agent.execute_action(
                    "create_sales_order",
                    policy={"tier": "VIP", "target_state": "sale",
                            "confirm_immediately": True},
                    context={
                        "opportunity": {
                            "id": "0061x00000TEST", "name": "Test Opp",
                            "stage": "Closed Won", "tier": "VIP",
                            "account_name": "VIP Tech", "amount": 120000,
                        },
                    },
                )

            res = asyncio.run(_run())
            # ODOO 미연결 시 success=False + intended_plan 반환
            self.assertFalse(res["result"]["success"])
            self.assertIn("intended_plan", res["result"])
            self.assertEqual(res["result"]["intended_plan"]["tier"], "VIP")
            self.assertEqual(res["result"]["intended_plan"]["amount"], 120000)
        finally:
            if old is not None:
                os.environ["ODOO_API_KEY"] = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
