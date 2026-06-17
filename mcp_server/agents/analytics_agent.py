# mcp_server/agents/analytics_agent.py
"""
Analytics Agent: BC2 — Closed Lost 사유 분석 전담
- on_lost 이벤트에서 ontology dispatch 로 호출됨 (rule: opp_lost)
- Lost reason 을 정형 카테고리로 분류 + 누적 패턴을 warm tier 메모리에 적재
- BC5b (BI dashboard) 와 직접 연결되는 누적 자산
"""
import sys
import re
from collections import Counter
from datetime import datetime
from typing import Dict, Any, Optional

from .base_agent import BaseAgent


# Lost 사유 카테고리 매핑 (한국어 키워드 → 정형 카테고리)
# 회사 정책: 영업 관리자가 yaml 로 추후 외부화 예정 (BC5+ option 5b)
LOST_REASON_CATEGORIES = {
    "price": ["가격", "비싸", "예산", "단가", "discount", "price", "cost"],
    "competitor": ["경쟁사", "타사", "competitor", "competition"],
    "timing": ["타이밍", "시기", "내년", "연기", "보류", "timing", "later"],
    "feature_gap": ["기능", "스펙", "요구사항", "feature", "missing", "spec"],
    "no_decision": ["결정", "보류", "no decision", "stalled"],
    "internal": ["내부", "사내", "정책", "internal"],
}


def _categorize_reason(reason_text: str) -> str:
    """Lost reason 텍스트를 정형 카테고리로 분류 (LLM 0회 — 키워드 매칭)."""
    if not reason_text:
        return "unspecified"
    text = reason_text.lower()
    for category, keywords in LOST_REASON_CATEGORIES.items():
        for kw in keywords:
            if kw.lower() in text:
                return category
    return "other"


class AnalyticsAgent(BaseAgent):
    """BC2 Lost 사유 분석 + 누적 패턴 전담 Agent"""

    def __init__(self, llm_config: dict, service_manager=None,
                 ontology_engine=None):
        super().__init__(
            name="Analytics Agent",
            description=(
                "Closed Lost Opportunity 의 사유를 분석하고 정형 카테고리로 분류합니다. "
                "누적 패턴을 warm tier 메모리에 저장하여 BI dashboard 와 ontology rule 최적화에 사용됩니다. "
                "BC2 의 Lost 분기 (rule: opp_lost) 에서 ontology dispatch 로 호출됩니다."
            ),
            llm_config=llm_config,
        )
        self.service_manager = service_manager
        # ontology engine 주입 — 누적 패턴을 warm tier 에 직접 저장하기 위해
        self._ontology_engine = ontology_engine

    def set_ontology_engine(self, engine):
        """server.py 가 OntologyEngine 싱글톤을 주입 (lazy)."""
        self._ontology_engine = engine

    def register_tools_from_services(self, user_id: str = None):
        """도구 + 정책 액션 등록"""

        async def categorize_lost_reason(reason_text: str) -> dict:
            """자유 텍스트 Lost reason 을 정형 카테고리로 분류"""
            return {
                "reason_text": reason_text,
                "category": _categorize_reason(reason_text),
            }

        async def get_lost_reason_summary(limit: int = 100) -> dict:
            """warm tier 에 누적된 Lost 분석 결과 요약 (BC5b BI dashboard 용)"""
            if not self._ontology_engine:
                return {"success": False, "error": "ontology_engine 미주입"}
            try:
                keys = self._ontology_engine.memory.list_keys(
                    tier="warm", limit=limit * 4
                ) or []
            except Exception as e:
                return {"success": False, "error": f"list_keys 실패: {e}"}

            categories = Counter()
            tier_split = Counter()
            sample = []
            for k in keys:
                if not str(k).startswith("lost_analysis:"):
                    continue
                try:
                    rec = self._ontology_engine.memory.get(k, tier="warm")
                except Exception:
                    continue
                if not rec:
                    continue
                cat = rec.get("category", "other")
                categories[cat] += 1
                tier_split[rec.get("tier", "unknown")] += 1
                if len(sample) < 5:
                    sample.append(rec)

            return {
                "success": True,
                "total_records": sum(categories.values()),
                "categories": dict(categories.most_common()),
                "by_tier": dict(tier_split),
                "sample": sample,
            }

        self.register_tool('categorize_lost_reason', categorize_lost_reason,
                          'Lost reason 텍스트를 정형 카테고리로 분류합니다 (reason_text)')
        self.register_tool('get_lost_reason_summary', get_lost_reason_summary,
                          '누적된 Closed Lost 분석 결과의 카테고리 분포를 요약합니다 (limit)')

        # ─── Policy-driven actions (Ontology dispatch 용) ───
        self._register_policy_actions(user_id)

        print(f"[Analytics Agent] {len(self._tools)} tools, "
              f"{len(self._action_handlers)} actions registered for user: {user_id}",
              file=sys.stderr)

    # ═══════════════════════════════════════════════════════════════
    # Policy-driven Actions (Ontology dispatch 용)
    # ═══════════════════════════════════════════════════════════════
    def _register_policy_actions(self, user_id: str = None):
        """ontology.yaml 의 delegate_to 가 호출하는 정책 기반 액션."""

        # ─────────────────────────────────────────────────────────
        # analyze_lost_reason — Closed Lost Opp 사유 분석
        # Type 1: Pure code — LLM 0회. 키워드 매칭 + 누적 메모리 적재.
        # rule: opp_lost (BC2)
        # policy: {source, include_history, categorize_reason}
        # context: {opportunity: {id, name, stage, tier, account_name, lost_reason, amount}}
        # ─────────────────────────────────────────────────────────
        async def analyze_lost_reason(policy: dict, context: dict) -> dict:
            opp = context.get("opportunity") or {}
            customer = context.get("customer") or {}
            tier = opp.get("tier") or customer.get("tier") or "unknown"

            reason_text = (
                opp.get("lost_reason")
                or context.get("lost_reason")
                or (context.get("payload") or {}).get("lost_reason")
                or ""
            )
            category = _categorize_reason(reason_text) if policy.get(
                "categorize_reason", True
            ) else None

            # 정형화된 분석 레코드
            record = {
                "ts": datetime.utcnow().isoformat() + "Z",
                "opp_id": opp.get("id"),
                "opp_name": opp.get("name"),
                "account_name": opp.get("account_name"),
                "tier": tier,
                "amount": opp.get("amount"),
                "reason_text": reason_text,
                "category": category,
                "source": policy.get("source", "opportunity_close"),
            }

            # warm tier 에 누적 적재 (BC5b BI dashboard 자산)
            persisted = False
            if self._ontology_engine and opp.get("id"):
                try:
                    self._ontology_engine.manage_memory(
                        f"lost_analysis:{opp.get('id')}",
                        record,
                        tier="warm",
                    )
                    persisted = True
                except Exception as e:
                    print(f"[Analytics Agent.analyze_lost_reason] warm 적재 실패: {e}",
                          file=sys.stderr)

            return {
                "action": "analyze_lost_reason",
                "success": True,
                "analysis": record,
                "persisted_to_warm": persisted,
                "policy_applied": {
                    "categorize_reason": policy.get("categorize_reason", True),
                    "include_history": policy.get("include_history", False),
                    "source": policy.get("source"),
                },
                "note": (
                    "Lost reason 카테고리화 + warm tier 누적 — BC5b (BI dashboard) "
                    "와 ontology rule 최적화에 사용됨"
                ),
            }

        self.register_action(
            'analyze_lost_reason', analyze_lost_reason,
            'BC2: Closed Lost Opp 사유를 정형 카테고리로 분류 + warm tier 누적 적재'
        )
