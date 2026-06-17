# mcp_server/server.py
"""
FastMCP Multi-Agent Enterprise AI 서버 (OOSDK 실험 버전)
- Orchestrator + 6 전문 Agent (Email, CRM, Calendar, CS, Helpdesk, Report)
- 포트: 9100 (MCP), 9101 (Log API)
- 기존 Multi-Agent(9000)과 동일 머신에서 공존 (OOSDK 적용 실험용)
"""
import sys
import os
import json
import logging
import asyncio
from pathlib import Path
from typing import Dict, Any, List
from fastmcp import FastMCP, Context
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.dependencies import get_http_request

# 프로젝트 루트를 Python path에 추가
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from mcp_server.config import (
    CONFIG, validate_config, print_config_summary,
    UserConfig, SUPPORTED_USERS,
    MCP_PORT, LOG_API_PORT, AGENT_LLM_CONFIG, AGENT_DEFINITIONS
)
from mcp_server.services.service_manager import (
    initialize_all_services, get_all_service_status,
    initialize_user_services, get_user_service_status,
    set_current_user, get_current_user
)
from mcp_server.logging_middleware import LoggingMiddleware
from mcp_server.log_receiver import router as log_api_router
from mcp_server.dashboard_api import router as dashboard_api_router

# Agent imports
from mcp_server.agents.orchestrator import Orchestrator
from mcp_server.agents.email_agent import EmailAgent
from mcp_server.agents.crm_agent import CRMAgent
from mcp_server.agents.calendar_agent import CalendarAgent
from mcp_server.agents.cs_agent import CSAgent
from mcp_server.agents.helpdesk_agent import HelpdeskAgent
from mcp_server.agents.report_agent import ReportAgent
# BC2 신규 agents (ontology dispatch on_won / on_lost 분기 담당)
from mcp_server.agents.erp_agent import ERPAgent
from mcp_server.agents.analytics_agent import AnalyticsAgent
# BC3 신규: Inventory Agent (SO Confirmed → Allocation → Shipping)
from mcp_server.agents.inventory_agent import InventoryAgent

# 로깅 설정
log_handlers = [logging.StreamHandler()]
if os.getenv('MCP_MODE', 'stdio') == 'stdio':
    try:
        log_handlers.append(logging.FileHandler('mcp_server.log', encoding='utf-8'))
    except:
        pass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=log_handlers
)
logger = logging.getLogger(__name__)

# ============================================================
# 사용자별 서비스 캐시
# ============================================================

_user_services_cache = {}


def get_or_create_user_services(user_id: str):
    """사용자별 서비스 인스턴스 생성/반환"""
    if user_id not in _user_services_cache:
        if not UserConfig.is_valid_user(user_id):
            logger.warning(f"⚠️ 알 수 없는 사용자: {user_id}, 기본값 'admin' 사용")
            user_id = 'admin'

        logger.info(f"🔄 사용자 '{user_id}' 서비스 초기화 중...")
        config = UserConfig.get_config(user_id)
        services = initialize_user_services(config)
        _user_services_cache[user_id] = {
            'config': config,
            'services': services
        }
        logger.info(f"✅ 사용자 '{user_id}' 서비스 초기화 완료")

    return _user_services_cache[user_id]


# ============================================================
# Orchestrator & Agent 인스턴스
# ============================================================

_orchestrator = None
_user_agents_cache = {}  # {user_id: {agent_id: agent}}

# BC3 CRIT #3 — spawn_events / service_plan fanout 의 재귀 깊이 제한.
# 의도: 한 webhook 진입점 (예: email → sale_order_confirmed) 이
#       sale_order_confirmed → delivery_ready_check → (allocation 결과)
# 까지 자연스럽게 흐르되, 잘못된 정책 사이클이 무한 재귀하지 않도록 보호.
# 현재 BC3 흐름 최대 깊이: 0=email, 1=sale_order_confirmed(spawn),
# 2=delivery_ready_check. 3 이면 충분 (한 단계 여유).
_DISPATCH_DEPTH_LIMIT = 3


def get_or_create_orchestrator(user_id: str = 'admin') -> Orchestrator:
    """Orchestrator 및 Agent 인스턴스 생성/반환"""
    global _orchestrator

    if user_id not in _user_agents_cache:
        logger.info(f"🤖 Agent 시스템 초기화 (user: {user_id})...")

        # Orchestrator 생성 (공유)
        if _orchestrator is None:
            _orchestrator = Orchestrator(llm_config=AGENT_LLM_CONFIG)

        # 전문 Agent 생성 (사용자별)
        email_agent = EmailAgent(llm_config=AGENT_LLM_CONFIG)
        email_agent.register_tools_from_services(user_id=user_id)

        crm_agent = CRMAgent(llm_config=AGENT_LLM_CONFIG)
        crm_agent.register_tools_from_services(user_id=user_id)

        calendar_agent = CalendarAgent(llm_config=AGENT_LLM_CONFIG)
        calendar_agent.register_tools_from_services(user_id=user_id)

        cs_agent = CSAgent(llm_config=AGENT_LLM_CONFIG)
        cs_agent.register_tools_from_services(user_id=user_id)

        helpdesk_agent = HelpdeskAgent(llm_config=AGENT_LLM_CONFIG)
        helpdesk_agent.register_tools_from_services(user_id=user_id)

        report_agent = ReportAgent(llm_config=AGENT_LLM_CONFIG)
        report_agent.register_tools_from_services(user_id=user_id)

        # ─── BC2 신규: ERP Agent (Closed Won → Odoo SO) ───
        erp_agent = ERPAgent(llm_config=AGENT_LLM_CONFIG)
        erp_agent.register_tools_from_services(user_id=user_id)

        # ─── BC2 신규: Analytics Agent (Closed Lost 사유 분석) ───
        # ontology engine 주입 — warm tier 누적 적재용
        try:
            _ontology_for_analytics = get_or_create_ontology_engine()
        except Exception as _e:
            logger.warning(f"[init] analytics_agent 에 ontology_engine 주입 실패: {_e}")
            _ontology_for_analytics = None
        analytics_agent = AnalyticsAgent(
            llm_config=AGENT_LLM_CONFIG,
            ontology_engine=_ontology_for_analytics,
        )
        analytics_agent.register_tools_from_services(user_id=user_id)

        # ─── BC3 신규: Inventory Agent (Allocation / Shipping) ───
        inventory_agent = InventoryAgent(llm_config=AGENT_LLM_CONFIG)
        inventory_agent.register_tools_from_services(user_id=user_id)

        # Orchestrator에 Agent 등록
        _orchestrator.register_agent('email_agent', email_agent)
        _orchestrator.register_agent('crm_agent', crm_agent)
        _orchestrator.register_agent('calendar_agent', calendar_agent)
        _orchestrator.register_agent('cs_agent', cs_agent)
        _orchestrator.register_agent('helpdesk_agent', helpdesk_agent)
        _orchestrator.register_agent('report_agent', report_agent)
        # BC2 신규 — ontology delegate_to.agent 매칭 키와 일치
        _orchestrator.register_agent('erp_agent', erp_agent)
        _orchestrator.register_agent('analytics_agent', analytics_agent)
        # BC3 신규
        _orchestrator.register_agent('inventory_agent', inventory_agent)

        _user_agents_cache[user_id] = {
            'email_agent': email_agent,
            'crm_agent': crm_agent,
            'calendar_agent': calendar_agent,
            'cs_agent': cs_agent,
            'helpdesk_agent': helpdesk_agent,
            'report_agent': report_agent,
            'erp_agent': erp_agent,
            'analytics_agent': analytics_agent,
            'inventory_agent': inventory_agent,
        }

        logger.info(f"✅ Agent 시스템 초기화 완료 (user: {user_id})")

    return _orchestrator


# ============================================================
# MCP 인스턴스
# ============================================================

mcp = FastMCP("Enterprise AI Assistant")


# ============================================================
# 유저 식별 미들웨어
# ============================================================

class UserIdentificationMiddleware(Middleware):
    """URL 파라미터에서 user_id와 client_type을 추출하여 서비스 초기화"""

    # 지원하는 client_type 값
    VALID_CLIENT_TYPES = {
        "claude_desktop", "cursor", "adk", "mcp",
    }

    @staticmethod
    def _detect_client_from_ua(user_agent: str) -> str:
        """User-Agent 헤더로 클라이언트 자동 감지 (fallback)"""
        ua = (user_agent or "").lower()
        if "claude-desktop" in ua or "claude_desktop" in ua or "anthropic" in ua:
            return "claude_desktop"
        if "cursor" in ua:
            return "cursor"
        if "mcp-remote" in ua or "npx" in ua:
            return "claude_desktop"  # Cursor는 URL에 client_type=cursor가 있으므로 여기 안 옴
        return "mcp"  # 기본값

    async def _extract_and_set_user(self, context: MiddlewareContext):
        try:
            request = get_http_request()
            user_id = request.query_params.get("user_id", "admin")
            client_type = request.query_params.get("client_type", "")

            # client_type이 명시되지 않았으면 User-Agent로 자동 감지
            if not client_type or client_type not in self.VALID_CLIENT_TYPES:
                user_agent = request.headers.get("user-agent", "")
                client_type = self._detect_client_from_ua(user_agent)

            if user_id not in SUPPORTED_USERS:
                logger.warning(f"⚠️ 알 수 없는 사용자: {user_id}, 기본값 'admin' 사용")
                user_id = "admin"

            get_or_create_user_services(user_id)
            set_current_user(user_id)

            await context.fastmcp_context.set_state("user_id", user_id)
            await context.fastmcp_context.set_state("client_type", client_type)
            await context.fastmcp_context.set_state("user_config", _user_services_cache[user_id]['config'])

            logger.debug(f"🔗 요청 처리: user_id={user_id}, client_type={client_type}")

        except Exception as e:
            logger.warning(f"⚠️ 사용자 식별 실패, 기본값 사용: {e}")
            set_current_user("admin")
            await context.fastmcp_context.set_state("user_id", "admin")
            await context.fastmcp_context.set_state("client_type", "mcp")

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        await self._extract_and_set_user(context)
        return await call_next(context)

    async def on_read_resource(self, context: MiddlewareContext, call_next):
        await self._extract_and_set_user(context)
        return await call_next(context)

    async def on_list_tools(self, context: MiddlewareContext, call_next):
        await self._extract_and_set_user(context)
        return await call_next(context)


# 미들웨어 등록
mcp.add_middleware(UserIdentificationMiddleware())
mcp.add_middleware(LoggingMiddleware())


# ============================================================
# OOSDK Ontology Engine (Phase 2 — orchestration entrypoint)
# ============================================================

_ontology_engine = None


def get_or_create_ontology_engine():
    """OntologyEngine 싱글톤. yaml/메모리 1회만 로드."""
    global _ontology_engine
    if _ontology_engine is None:
        try:
            from mcp_server.ontology_engine import OntologyEngine, ThreeTierMemory
            yaml_path = Path(__file__).parent.parent / "ontology" / "ontology.yaml"
            data_dir = Path(__file__).parent.parent / "data" / "memory"
            data_dir.mkdir(parents=True, exist_ok=True)

            memory = ThreeTierMemory({
                "hot":  {"backend": "in_memory", "ttl_sec": 3600, "max_size": 1000},
                "warm": {"backend": "sqlite",    "ttl_sec": 2592000,
                         "path": str(data_dir / "warm.db")},
                "cold": {"backend": "jsonl",
                         "path": str(data_dir / "cold")},
            })
            _ontology_engine = OntologyEngine(str(yaml_path), memory=memory)
            logger.info(f"✅ OntologyEngine 초기화 완료 (yaml={yaml_path.name})")
        except Exception as e:
            logger.error(f"❌ OntologyEngine 초기화 실패: {e}", exc_info=True)
            raise
    return _ontology_engine


# ============================================================
# Multi-Agent MCP 도구 등록
# ============================================================

# orchestrate_task 제거됨
# → Claude Desktop의 Claude AI가 직접 적절한 Agent를 선택합니다.
# → OpenAI 중복 호출(Orchestrator → Agent) 제거로 속도 2~3배 개선


async def _run_agent_safe(agent_key: str, agent_label: str, task: str) -> dict:
    """Agent 실행 공통 헬퍼 (에러 핸들링 + 로깅)"""
    import time as _time
    start = _time.time()
    user_id = get_current_user() or 'admin'

    try:
        get_or_create_user_services(user_id)
        get_or_create_orchestrator(user_id)

        agent = _user_agents_cache.get(user_id, {}).get(agent_key)
        if not agent:
            logger.error(f"❌ {agent_label} not initialized for user: {user_id}")
            return {'success': False, 'error': f'{agent_label} not initialized'}

        logger.info(f"🤖 {agent_label} 실행 시작: {task[:80]}...")
        result = await agent.run(task, {'user_id': user_id})
        duration = (_time.time() - start) * 1000
        logger.info(f"✅ {agent_label} 완료 ({duration:.0f}ms, success={result.success})")
        return result.to_dict()

    except Exception as e:
        duration = (_time.time() - start) * 1000
        logger.error(f"❌ {agent_label} 실행 실패 ({duration:.0f}ms): {e}", exc_info=True)
        return {
            'success': False,
            'error': f'{agent_label} 실행 오류: {str(e)}',
            'duration_ms': round(duration, 2),
        }


@mcp.tool()
async def run_email_agent(task: str) -> dict:
    """
    Email Agent에게 직접 작업을 요청합니다.
    이메일 조회, AI 분석, 답변 생성, 발송에 특화된 Agent입니다.

    Args:
        task: 이메일 관련 작업 설명 (예: "최근 30분간 이메일을 확인하고 고객 정보를 추출해줘")
    """
    return await _run_agent_safe('email_agent', 'Email Agent', task)


@mcp.tool()
async def run_crm_agent(task: str) -> dict:
    """
    CRM Agent에게 직접 작업을 요청합니다.
    Salesforce Lead 생성, 조회, 관리에 특화된 Agent입니다.

    Args:
        task: CRM 관련 작업 설명 (예: "홍길동(ABC회사) Lead를 생성해줘")
    """
    return await _run_agent_safe('crm_agent', 'CRM Agent', task)


@mcp.tool()
async def run_calendar_agent(task: str) -> dict:
    """
    Calendar Agent에게 직접 작업을 요청합니다.
    Google Calendar 일정 생성, 조회, 수정, 삭제에 특화된 Agent입니다.

    Args:
        task: 일정 관련 작업 설명 (예: "이번 주 일정을 확인해줘")
    """
    return await _run_agent_safe('calendar_agent', 'Calendar Agent', task)


@mcp.tool()
async def run_cs_agent(task: str) -> dict:
    """
    CS Agent에게 직접 작업을 요청합니다.
    고객 서비스 전문 Agent입니다. 제품 FAQ, 반품/교환 절차, 고객 문의 응대에 특화되어 있습니다.
    VectorDB의 product_docs 컬렉션에서 제품 관련 문서를 검색합니다.

    Args:
        task: 고객 서비스 관련 작업 설명 (예: "이 제품의 반품 절차를 안내해줘")
    """
    return await _run_agent_safe('cs_agent', 'CS Agent', task)


@mcp.tool()
async def run_helpdesk_agent(task: str) -> dict:
    """
    Helpdesk Agent에게 직접 작업을 요청합니다.
    내부 직원용 헬프데스크 Agent입니다. IT, HR, Finance 등 내부 정책/절차 문서를 기반으로 답변합니다.
    VectorDB의 internal_docs 컬렉션에서 내부 문서를 검색합니다.

    Args:
        task: 내부 헬프데스크 관련 작업 설명 (예: "연차 신청 방법을 알려줘", "VPN 설정 방법은?")
    """
    return await _run_agent_safe('helpdesk_agent', 'Helpdesk Agent', task)


@mcp.tool()
async def run_report_agent(task: str) -> dict:
    """
    Report Agent에게 직접 작업을 요청합니다.
    시스템 로그 분석, 사용 통계, 성능 모니터링에 특화된 Agent입니다.

    Args:
        task: 로그/통계 분석 관련 작업 설명 (예: "오늘 도구 사용 통계를 보여줘")
    """
    return await _run_agent_safe('report_agent', 'Report Agent', task)


# ============================================================
# OOSDK 핵심 진입점 — Ontology 기반 자동 분기
# ============================================================

@mcp.tool()
async def process_with_ontology(
    from_email: str,
    subject: str = "",
    body: str = "",
    from_name: str = "",
    dispatch: bool = False,
) -> dict:
    """
    OOSDK Ontology 파이프라인 실행 — 이메일 1건을 받아 자동으로 분류/분기합니다.

    수행 단계:
      1. resolve_links: SFDC Lead 조회 → Customer Tier(VIP/Standard/Unknown) 식별
      2. check_rules: ontology.yaml 의 rule 평가 → 매칭 rule 결정
      3. trigger_events: rule.then.events → 실행 계획(plan) 생성
      4. manage_memory: 처리 이력을 3-Tier 메모리에 저장
      5. (옵션) dispatch=True 면 plan 의 첫 단계를 실제로 dispatch

    이 도구는 다음을 한 번에 보여줍니다:
      - SFDC 의 Customer_Tier__c 가 어떻게 식별되는지
      - 어느 rule 이 매칭되는지 (existing_vip / existing_standard / new_prospect)
      - 어느 agent/tool 이 호출되어야 하는지 (자동 plan)

    Args:
        from_email: 발신자 이메일 (예: "vip.customer@example.com")
        subject:    이메일 제목 (선택)
        body:       이메일 본문 (선택)
        from_name:  발신자 이름 (선택)
        dispatch:   True 면 plan 의 첫 step 을 실제로 호출 (기본 False — 안전)

    Returns:
        {
          "ok": True,
          "ontology_trace": {
            "person": {...},
            "customer": {...} or None,
            "matched_rule": "existing_vip" or None,
            "action": {...},
            "event_plan": [...],
            "memory": {"key": ..., "tier": ...},
            "lookup_by": "email",
            "lookup_value": "...",
            "customer_source": "SalesforceAdapter"
          },
          "narrative": "VIP 고객으로 식별 — 미팅 제안 + 우선 답장 plan 수립",
          "dispatched": null or {agent, tool, result}
        }
    """
    import time as _time
    start = _time.time()

    # 1) 엔진 준비
    try:
        engine = get_or_create_ontology_engine()
    except Exception as e:
        return {"ok": False, "error": f"ontology engine init failed: {e}"}

    # 2) Payload 구성
    payload = {
        "id": f"chat_{int(start)}",
        "from": from_email,
        "from_name": from_name,
        "subject": subject,
        "body": body,
    }

    # 3) Pipeline 실행
    try:
        ctx = engine.resolve_links("email", payload)
        action = engine.check_rules(ctx)
        plan = engine.trigger_events(action, ctx)

        # ─── 메모리 저장 (audit-first 정책) ───
        # Why: 이전 구현은 action.memory_tier 한 곳에만 썼는데, ontology.yaml 이
        # VIP/Standard 룰에 memory_tier=hot 을 지정해서 in-memory 만 쌓이고
        # 프로세스 재시작/24h TTL 만료 후 audit trail 이 사라졌다 (Medium 기고문
        # "What Went Wrong" 섹션 참조).
        # → audit 용도로는 *항상* warm 에 저장.
        # → "hot 도 동시에 보고 싶다" 는 룰 지정 (memory_tier=hot) 은 warm 위에
        #   추가 write 로 유지 (warm 의 영속성 + hot 의 즉시성 둘 다).
        mem_key = f"ontology_decision:{payload['id']}"
        decision_record = {
            "ts": _time.time(),
            "email": {"from": from_email, "subject": subject},
            "customer": ctx.get("customer"),
            "matched_rule": (action or {}).get("rule_name"),
            "plan": plan,
        }
        # 1) warm 에 항상 기록 (audit trail — 모든 rule, 30 days)
        engine.manage_memory(mem_key, decision_record, tier="warm")
        # 2) rule 이 hot 을 요청했으면 hot 에도 기록 (즉시 조회용 캐시)
        rule_tier = (action or {}).get("memory_tier", "warm")
        tier = rule_tier  # ontology_trace 응답 호환 — 룰이 의도한 tier
        if rule_tier == "hot":
            engine.manage_memory(mem_key, decision_record, tier="hot")

        # narrative 생성
        customer = ctx.get("customer")
        rule_name = (action or {}).get("rule_name")
        if rule_name == "existing_vip":
            narrative = (
                f"🌟 VIP 고객 식별 — {customer.get('name', '')} ({customer.get('company', '')}, "
                f"AnnualRevenue={customer.get('annual_revenue')}). "
                f"긴급 미팅 제안 + 프리미엄 답장 plan({len(plan)}개) 수립."
            )
        elif rule_name == "existing_standard":
            narrative = (
                f"✅ Standard 고객 — {customer.get('name', '')} ({customer.get('company', '')}). "
                f"일반 응대 plan({len(plan)}개) 수립."
            )
        elif rule_name == "new_prospect":
            narrative = (
                f"🆕 신규 프로스펙트 — SFDC 에 등록되지 않은 발신자. "
                f"자격 검증 + enrichment plan({len(plan)}개) 수립."
            )
        else:
            narrative = "rule 매칭 없음 — 기본 처리 필요."

        # 4) (옵션) Dispatch — plan 의 모든 step 을 순차 실행
        # ─────────────────────────────────────────────────────────
        # v1.2+ Policy-driven Multi-Agent Dispatch:
        #   plan 의 각 step 은 두 종류 중 하나
        #     • kind="delegate"  → agent.execute_action(action, policy, context)
        #         (Ontology = WHAT, Agent = HOW. 결정 LLM 0회.)
        #     • kind="event"     → (Deprecated) 단일 tool 호출
        #         create_salesforce_lead 는 멱등성/결정론 직접 호출 유지.
        #         그 외는 자연어 task 로 agent.run() (LLM 사용).
        #
        #   step 간 데이터 전달: 직전 step 의 result 를 agent_outputs 에 누적해
        #   다음 step 의 context 로 넘김 (예: Calendar.book_priority_meeting →
        #   Email.send_meeting_invite 가 미팅 정보를 받아 본문 구성).
        #
        # BC3 CRIT #3 봉합 — spawn_events / service_plan fanout:
        #   inventory_agent.split_fulfillment_path 같은 액션은 자체 dispatch 없이
        #   spawn_events ([{entity, payload}]) 와 service_plan ([{agent, action, policy}])
        #   을 반환만 한다. 이전엔 demo 스크립트가 수동 fanout 했고 prod 경로엔
        #   재진입 코드가 없었다 (CRIT #3). 이제 직전 step 의 inner result 에서
        #   둘 다 처리한다.
        #     · service_plan  → 그 자리에서 agent.execute_action 직접 호출
        #     · spawn_events  → engine 재진입 (resolve_links→check_rules→trigger_events)
        #                       후 같은 dispatcher 로 재귀. 깊이 _DISPATCH_DEPTH_LIMIT
        #                       으로 무한 재귀 방지. dispatched_keys 셋으로 동일
        #                       (entity, picking.id, sales_order.id) 중복 진입 차단.
        # ─────────────────────────────────────────────────────────
        dispatched = None
        if dispatch and plan:
            user_id = get_current_user() or 'admin'
            # 에이전트 캐시가 비어있으면 초기화 (delegate dispatch 는 agent 인스턴스가 필요)
            if user_id not in _user_agents_cache:
                try:
                    get_or_create_orchestrator(user_id)
                except Exception as _e:
                    logger.warning(f"[dispatch] agent 초기화 실패 ({user_id}): {_e}")
            agents_for_user = _user_agents_cache.get(user_id) or _user_agents_cache.get('admin') or {}

            # 공통 context (모든 step 에 전달)
            base_context = {
                "payload": payload,
                "customer": ctx.get("customer"),
                "person": ctx.get("person"),
                "rule_name": rule_name,
                "from_email": from_email,
            }
            agent_outputs: Dict[str, Any] = {}
            step_results: List[Dict[str, Any]] = []
            # spawn_event 멱등성 키 집합 — (entity, picking.id, sales_order.id)
            dispatched_keys: set = set()

            async def _run_delegate(
                agent_id_: str, action_name_: str, policy_: Dict[str, Any],
                step_ctx_: Dict[str, Any],
            ) -> Dict[str, Any]:
                agent_obj_ = agents_for_user.get(agent_id_)
                if not agent_obj_:
                    return {
                        "success": False, "agent": agent_id_,
                        "action": action_name_,
                        "error": f"agent '{agent_id_}' 가 등록되어 있지 않음",
                    }
                try:
                    res_ = await agent_obj_.execute_action(
                        action_name_, policy=policy_, context=step_ctx_
                    )
                except Exception as e_:
                    res_ = {
                        "success": False, "agent": agent_id_,
                        "action": action_name_, "error": str(e_),
                    }
                inner_ = res_.get("result") if isinstance(res_, dict) else None
                if isinstance(inner_, dict):
                    agent_outputs[action_name_] = inner_
                    agent_outputs[agent_obj_.name] = inner_
                return res_

            async def _fanout(
                inner_result: Any, parent_base: Dict[str, Any], depth: int,
            ) -> List[Dict[str, Any]]:
                """직전 delegate result 의 service_plan / spawn_events 를 처리."""
                extra: List[Dict[str, Any]] = []
                if not isinstance(inner_result, dict):
                    return extra
                if depth >= _DISPATCH_DEPTH_LIMIT:
                    if (inner_result.get("service_plan")
                            or inner_result.get("spawn_events")):
                        extra.append({
                            "kind": "fanout_skipped", "depth": depth,
                            "reason": f"depth>={_DISPATCH_DEPTH_LIMIT} — 재진입 차단",
                        })
                    return extra

                # ─ service_plan: 단일 action 직접 dispatch ─
                for sp in (inner_result.get("service_plan") or []):
                    if not isinstance(sp, dict):
                        continue
                    sp_agent = sp.get("agent")
                    sp_action = sp.get("action")
                    sp_policy = sp.get("policy", {}) or {}
                    if not sp_agent or not sp_action:
                        continue
                    sp_ctx = {**parent_base, "agent_outputs": dict(agent_outputs)}
                    sp_res = await _run_delegate(sp_agent, sp_action, sp_policy, sp_ctx)
                    extra.append({
                        "kind": "service_plan", "depth": depth,
                        "agent": sp_agent, "action": sp_action,
                        "policy_applied": sp_policy, "result": sp_res,
                    })
                    logger.info(
                        f"[OOSDK fanout d={depth}] service_plan "
                        f"{sp_agent}.{sp_action} → success={sp_res.get('success')}"
                    )

                # ─ spawn_events: engine 재진입 → 새 plan dispatch ─
                for ev in (inner_result.get("spawn_events") or []):
                    if not isinstance(ev, dict):
                        continue
                    sub_entity = ev.get("entity")
                    sub_payload = ev.get("payload") or {}
                    if not sub_entity:
                        continue
                    key = (
                        sub_entity,
                        (sub_payload.get("picking") or {}).get("id"),
                        (sub_payload.get("sales_order") or {}).get("id"),
                        (sub_payload.get("receipt") or {}).get("id"),
                    )
                    if key in dispatched_keys:
                        extra.append({
                            "kind": "spawn_event", "depth": depth,
                            "entity": sub_entity, "skipped": True,
                            "reason": "이미 dispatch 된 spawn_event (멱등성)",
                        })
                        continue
                    dispatched_keys.add(key)
                    try:
                        sub_ctx = engine.resolve_links(sub_entity, sub_payload)
                        sub_action = engine.check_rules(sub_ctx)
                        sub_plan = engine.trigger_events(sub_action, sub_ctx)
                    except Exception as e_:
                        extra.append({
                            "kind": "spawn_event", "depth": depth,
                            "entity": sub_entity, "success": False,
                            "error": f"engine 재진입 실패: {e_}",
                        })
                        continue

                    # 새 base_context — 자식 entity 의 핵심 객체를 surface
                    sub_base = {**parent_base, "payload": sub_payload}
                    for k_ in ("picking", "inventory", "receipt",
                               "sales_order", "account_name"):
                        if k_ in sub_payload:
                            sub_base[k_] = sub_payload[k_]

                    sub_results = await _dispatch_steps(
                        sub_plan, sub_base, depth + 1
                    )
                    extra.append({
                        "kind": "spawn_event", "depth": depth,
                        "entity": sub_entity,
                        "matched_rule": (sub_action or {}).get("rule_name"),
                        "sub_step_count": len(sub_results),
                        "sub_results": sub_results,
                    })
                    logger.info(
                        f"[OOSDK fanout d={depth}] spawn_event entity={sub_entity} "
                        f"matched={(sub_action or {}).get('rule_name')} "
                        f"sub_steps={len(sub_results)}"
                    )
                return extra

            async def _dispatch_steps(
                plan_: List[Dict[str, Any]], local_base: Dict[str, Any], depth: int,
            ) -> List[Dict[str, Any]]:
                """단일 plan 의 steps + 그 결과의 fanout 까지 수행."""
                results: List[Dict[str, Any]] = []
                for step_idx, step in enumerate(plan_):
                    kind = step.get("kind", "event")
                    agent_id = step.get("agent")
                    step_context = {**local_base, "agent_outputs": dict(agent_outputs)}

                    # ─── A) v1.2+ delegate_to ───
                    if kind == "delegate":
                        action_name = step.get("action")
                        policy = step.get("policy", {}) or {}
                        res = await _run_delegate(
                            agent_id, action_name, policy, step_context
                        )
                        results.append({
                            "step": step_idx, "depth": depth, "kind": kind,
                            "agent": agent_id, "action": action_name,
                            "policy_applied": policy, "result": res,
                        })
                        logger.info(
                            f"[OOSDK dispatch d={depth}] step {step_idx} delegate "
                            f"{agent_id}.{action_name} → success={res.get('success')}"
                        )
                        # fanout: 직전 result 의 service_plan / spawn_events
                        inner = res.get("result") if isinstance(res, dict) else None
                        if isinstance(inner, dict):
                            results.extend(await _fanout(inner, local_base, depth))
                        continue

                    # ─── B) (Deprecated) event 경로 ───
                    tool_name = step.get("tool")
                    if tool_name == "create_salesforce_lead":
                        from mcp_server.services import salesforce_service as _sfs
                        try:
                            existing = _sfs.search_leads_by_email(
                                from_email, user_id=user_id
                            )
                        except Exception as e:
                            existing = None
                            logger.warning(
                                f"[dispatch] search_leads_by_email 실패: {e}"
                            )
                        if existing:
                            results.append({
                                "step": step_idx, "depth": depth, "kind": kind,
                                "tool": tool_name, "skipped": True,
                                "reason": "이미 SFDC 에 동일 이메일 Lead 존재 (멱등성)",
                                "existing_lead": existing,
                            })
                        else:
                            name_for_lead = from_name or from_email.split("@")[0]
                            customer_info = {
                                "name": name_for_lead,
                                "company": "(unknown — to be enriched)",
                                "email": from_email, "title": "", "phone": "",
                            }
                            try:
                                new_lead_id = _sfs.create_lead(
                                    customer_info, user_id=user_id
                                )
                                results.append({
                                    "step": step_idx, "depth": depth, "kind": kind,
                                    "tool": tool_name,
                                    "success": bool(new_lead_id),
                                    "lead_id": new_lead_id,
                                    "params": customer_info,
                                })
                            except Exception as e:
                                results.append({
                                    "step": step_idx, "depth": depth, "kind": kind,
                                    "tool": tool_name,
                                    "success": False, "error": str(e),
                                })
                    else:
                        # 자연어 task 로 변환 — LLM 기반 fallback
                        task = (
                            f"[OOSDK 자동 dispatch] rule={rule_name}, "
                            f"customer={ctx.get('customer')}, "
                            f"이메일 from={from_email} subject={subject}. "
                            f"적절한 도구를 골라 처리해주세요. "
                            f"(event: {step.get('event_name')})"
                        )
                        try:
                            res = await _run_agent_safe(agent_id, agent_id, task)
                        except Exception as e:
                            res = {"success": False, "error": str(e)}
                        results.append({
                            "step": step_idx, "depth": depth, "kind": kind,
                            "agent": agent_id, "tool": tool_name, "result": res,
                        })
                return results

            step_results = await _dispatch_steps(plan, base_context, depth=0)

            # all_success 정확도 fix:
            # step entry 구조: {"step", "agent", "action", "result": <execute_action 결과>}
            # execute_action 결과: {"success": <agent-level OK>, "result": <action 내부 결과>}
            # 진짜 비즈니스 성공은 result.result.success — 두 레이어 모두 True 여야 함.
            #
            # BC3 CRIT #3 추가 entry kind:
            #   · "service_plan" → 일반 delegate 와 동일 result 구조.
            #   · "spawn_event"  → sub_results 의 AND. skipped (멱등) / error 는 처리.
            #   · "fanout_skipped" → 정보성 (depth limit). 성공 카운트에서 제외.
            def _step_success(s: Dict[str, Any]) -> bool:
                if s.get("kind") == "fanout_skipped":
                    return True  # 정보성 entry — 실패로 치지 않음
                if s.get("kind") == "spawn_event":
                    if s.get("skipped"):
                        return True
                    if s.get("success") is False:
                        return False
                    subs = s.get("sub_results") or []
                    return all(_step_success(x) for x in subs)
                if "success" in s:
                    return bool(s.get("success"))
                outer = s.get("result") or {}
                if outer.get("success") is False:
                    return False
                inner = outer.get("result") if isinstance(outer, dict) else None
                if isinstance(inner, dict) and inner.get("success") is False:
                    return False
                return True

            dispatched = {
                "steps": step_results,
                "step_count": len(step_results),
                "all_success": all(_step_success(s) for s in step_results),
            }

        duration_ms = (_time.time() - start) * 1000

        # ontology_trace — dashboard 가 파싱할 구조
        ontology_trace = {
            "person": ctx.get("person"),
            "customer": ctx.get("customer"),
            "lookup_by": engine.last_trace.get("lookup_by") if engine.last_trace else None,
            "lookup_value": engine.last_trace.get("lookup_value") if engine.last_trace else None,
            "customer_source": engine.last_trace.get("customer_source") if engine.last_trace else None,
            # customer 가 None 일 때 사유 (sfdc_session_unavailable / no_match / http_4xx 등)
            "customer_error": engine.last_trace.get("customer_error") if engine.last_trace else None,
            "matched_rule": (action or {}).get("rule_name"),
            "action": action,
            "event_plan": plan,
            "memory": {"key": mem_key, "tier": tier},
        }

        return {
            "ok": True,
            "ontology_trace": ontology_trace,
            "narrative": narrative,
            "dispatched": dispatched,
            "duration_ms": round(duration_ms, 2),
        }

    except Exception as e:
        logger.error(f"❌ process_with_ontology 실패: {e}", exc_info=True)
        return {
            "ok": False,
            "error": str(e),
            "duration_ms": round((_time.time() - start) * 1000, 2),
        }


# ============================================================
# BC2 — Sales Opportunity 진입점 (견적 문의 + Win/Lost 분기)
# ============================================================

@mcp.tool()
async def process_sales_opportunity(
    event: str,
    account_name: str,
    tier: str = "",
    stage: str = "",
    opportunity_id: str = "",
    opportunity_name: str = "",
    amount: float = 0,
    contact_email: str = "",
    subject: str = "",
    body: str = "",
    lost_reason: str = "",
    record_type_dev_name: str = "",
    lead_id: str = "",
    account_id: str = "",
    requested_discount_pct: float = 0,
    products_json: str = "",
    dispatch: bool = False,
) -> dict:
    """
    BC2 — Sales Opportunity 파이프라인 진입점.

    "한 번의 stage 변경 (Closed Won 클릭) 이 최대 4 에이전트를 동시 호출.
     영업 정책 yaml 이 모든 분기를 결정." (BC2 narrative)

    Args:
        event: "inquiry" (견적 문의 → Opp 생성) 또는 "close" (Win/Lost 분기)
        account_name: SFDC Account 이름 (예: "VIP Tech")
        tier: "VIP" | "Standard" — 명시 안 하면 SFDC Account.CustomerPriority__c 로 lookup
        stage: event="close" 시 필수, "Closed Won" | "Closed Lost"
        opportunity_id, opportunity_name, amount: Opp 메타 (close 시)
        contact_email, subject, body: 견적 문의 정보 (inquiry 시)
        lost_reason: Closed Lost 사유 (Analytics Agent 가 분석)
        record_type_dev_name: "Opp_VIP" | "Opp_Standard" (조회/검증용)
        dispatch: True 면 ontology plan 의 모든 step 을 실제 dispatch

    Returns:
        process_with_ontology 와 동일 구조의 trace + plan + (옵션) dispatched
    """
    import time as _time
    start = _time.time()

    if event not in ("inquiry", "close"):
        return {
            "ok": False,
            "error": f"event 는 'inquiry' 또는 'close' 만 지원 (받은 값: {event})",
        }

    entity_type = (
        "sales_opportunity_inquiry" if event == "inquiry"
        else "sales_opportunity_close"
    )

    # payload 구성
    payload: Dict[str, Any] = {
        "id": f"sales_{event}_{int(start)}",
        "account_name": account_name,
        "account_id": account_id or None,    # SFDC adapter 우회용 직접 Id 주입
        "tier": tier or None,
    }
    # products_json (선택) — multi-line / storable 지원 (BC3 진입점)
    products_list = None
    if products_json:
        try:
            products_list = json.loads(products_json)
        except Exception as e:
            logger.warning(f"[process_sales_opportunity] products_json parse 실패: {e}")
    if event == "inquiry":
        payload.update({
            "contact_email": contact_email,
            "subject": subject,
            "body": body,
            "amount": amount or None,
            "lead_id": lead_id or None,
            "requested_discount_pct": requested_discount_pct or 0,
            "products": products_list,
        })
    else:
        payload["opportunity"] = {
            "id": opportunity_id or None,
            "name": opportunity_name or None,
            "stage": stage,
            "tier": tier or None,
            "account_name": account_name,
            "amount": amount or None,
            "lost_reason": lost_reason or None,
            "record_type_dev_name": record_type_dev_name or None,
            "products": products_list,
        }

    try:
        engine = get_or_create_ontology_engine()
    except Exception as e:
        return {"ok": False, "error": f"ontology engine init failed: {e}"}

    try:
        ctx = engine.resolve_links(entity_type, payload)
        action = engine.check_rules(ctx)
        plan = engine.trigger_events(action, ctx)

        # ─── 메모리 적재 (audit-first) ───
        mem_key = f"ontology_decision:{payload['id']}"
        opp_ctx = ctx.get("opportunity") or {}
        decision_record = {
            "ts": _time.time(),
            "entity": entity_type,
            "event": event,                    # "inquiry" | "close"
            "account_name": account_name,      # "VIP Tech" 같은 사람 친화적 ID
            "account_id": account_id or None,  # SFDC Account 18-char Id (clickable)
            "opportunity_id": opp_ctx.get("id"),
            "opportunity_name": opp_ctx.get("name"),
            # customer_tier — VIP/Standard. "tier" 명칭 충돌 회피 (API memory tier 와 분리)
            "customer_tier": (ctx.get("customer") or {}).get("tier"),
            "stage": opp_ctx.get("stage"),
            "matched_rule": (action or {}).get("rule_name"),
            "plan": plan,
        }
        engine.manage_memory(mem_key, decision_record, tier="warm")
        rule_tier = (action or {}).get("memory_tier", "warm")
        if rule_tier == "hot":
            engine.manage_memory(mem_key, decision_record, tier="hot")

        rule_name = (action or {}).get("rule_name")
        # narrative
        n_steps = len(plan)
        if rule_name == "sales_inquiry_vip":
            narrative = (
                f"💎 VIP 견적 문의 — {account_name}. "
                f"Opp_VIP RecordType + 5-stage(VIP_Sales_Process) 로 Opportunity 생성 plan({n_steps}개)."
            )
        elif rule_name == "sales_inquiry_standard":
            narrative = (
                f"🎯 Standard 견적 문의 — {account_name}. "
                f"Opp_Standard RecordType + 4-stage 로 Opportunity 생성 plan({n_steps}개)."
            )
        elif rule_name == "opp_won_vip":
            narrative = (
                f"🏆 VIP Closed Won — {account_name}. "
                f"4 에이전트 동시 발화 (ERP SO + 감사 메일 + Kickoff 미팅 + 활동 로그) plan({n_steps}개)."
            )
        elif rule_name == "opp_won_standard":
            narrative = (
                f"✅ Standard Closed Won — {account_name}. "
                f"3 에이전트 동시 발화 (ERP SO + 감사 메일 + 활동 로그) plan({n_steps}개)."
            )
        elif rule_name == "opp_lost":
            narrative = (
                f"❌ Closed Lost — {account_name}. "
                f"Analytics 사유 분석 + 180일 후 Re-engage Task + 활동 로그 plan({n_steps}개). "
                f"ERP push 안 됨 (정책 분기 검증)."
            )
        else:
            narrative = "rule 매칭 없음 — 정책 누락 또는 조건 불일치."

        # ─── (옵션) 실제 dispatch ───
        dispatched = None
        if dispatch and plan:
            user_id = get_current_user() or 'admin'
            if user_id not in _user_agents_cache:
                try:
                    get_or_create_orchestrator(user_id)
                except Exception as _e:
                    logger.warning(f"[sales dispatch] agent 초기화 실패 ({user_id}): {_e}")
            agents_for_user = _user_agents_cache.get(user_id) or _user_agents_cache.get('admin') or {}

            base_context = {
                "entity": entity_type,
                "payload": payload,
                "customer": ctx.get("customer"),
                # account_id 가 직접 주어졌으면 우선 사용 (SFDC adapter 우회)
                "account": (ctx.get("account")
                            or ({"id": account_id, "name": account_name}
                                if account_id else None)),
                "account_name": account_name,
                "opportunity": ctx.get("opportunity"),
                "lead": ({"id": lead_id} if lead_id else None),
                "rule_name": rule_name,
            }
            agent_outputs: Dict[str, Any] = {}
            step_results: List[Dict[str, Any]] = []

            for step_idx, step in enumerate(plan):
                if step.get("kind") != "delegate":
                    step_results.append({"step": step_idx, **step,
                                         "skipped": True,
                                         "reason": "BC2 는 delegate_to 만 지원"})
                    continue
                agent_id = step.get("agent")
                action_name = step.get("action")
                policy = step.get("policy", {}) or {}
                step_context = {**base_context, "agent_outputs": dict(agent_outputs)}
                agent_obj = agents_for_user.get(agent_id)
                if not agent_obj:
                    step_results.append({
                        "step": step_idx, "kind": "delegate", "agent": agent_id,
                        "action": action_name, "success": False,
                        "error": f"agent '{agent_id}' 가 등록되어 있지 않음",
                    })
                    continue
                try:
                    res = await agent_obj.execute_action(
                        action_name, policy=policy, context=step_context
                    )
                except Exception as e:
                    res = {"success": False, "agent": agent_id,
                           "action": action_name, "error": str(e)}
                inner = res.get("result") if isinstance(res, dict) else None
                if isinstance(inner, dict):
                    agent_outputs[action_name] = inner
                    agent_outputs[agent_obj.name] = inner
                step_results.append({
                    "step": step_idx, "kind": "delegate", "agent": agent_id,
                    "action": action_name, "policy_applied": policy,
                    "result": res,
                })
                logger.info(
                    f"[BC2 dispatch] step {step_idx} delegate {agent_id}.{action_name} "
                    f"→ success={res.get('success')}"
                )

            # all_success 정확도 fix:
            # step entry 구조: {"step", "agent", "action", "result": <execute_action 결과>}
            # execute_action 결과: {"success": <agent-level OK>, "result": <action 내부 결과>}
            # 진짜 비즈니스 성공은 result.result.success — 두 레이어 모두 True 여야 함.
            def _step_success(s: Dict[str, Any]) -> bool:
                if "success" in s:
                    return bool(s.get("success"))
                outer = s.get("result") or {}
                if outer.get("success") is False:
                    return False
                inner = outer.get("result") if isinstance(outer, dict) else None
                if isinstance(inner, dict) and inner.get("success") is False:
                    return False
                return True

            dispatched = {
                "steps": step_results,
                "step_count": len(step_results),
                "all_success": all(_step_success(s) for s in step_results),
            }

        duration_ms = (_time.time() - start) * 1000
        ontology_trace = {
            "entity": entity_type,
            "event": event,
            "customer": ctx.get("customer"),
            "account": ctx.get("account"),
            "opportunity": ctx.get("opportunity"),
            "matched_rule": rule_name,
            "action": action,
            "event_plan": plan,
            "memory": {"key": mem_key, "tier": rule_tier},
        }

        return {
            "ok": True,
            "ontology_trace": ontology_trace,
            "narrative": narrative,
            "dispatched": dispatched,
            "duration_ms": round(duration_ms, 2),
        }

    except Exception as e:
        logger.error(f"❌ process_sales_opportunity 실패: {e}", exc_info=True)
        return {
            "ok": False,
            "error": str(e),
            "duration_ms": round((_time.time() - start) * 1000, 2),
        }


# ════════════════════════════════════════════════════════════════
# BC4 S0 — Priority Override (Case A: 명시적 지정형) 공용 헬퍼
# ════════════════════════════════════════════════════════════════
# 결정론적 override. 사람이 trigger 에 so_ids 를 명시 → 해당 SO 의 정렬 점수 boost.
# LLM 미개입. 발행 시점 hard-gate(requires_authorization / max_per_day) +
# 소비 시점 soft-gate(auto_expire_hours, agent 측). 모든 발행은 audit 기록.

def _parse_override_so_ids(so_ids_csv: str) -> List[int]:
    """CSV "8, 11" → [8, 11]. 숫자 토큰만 인정."""
    out: List[int] = []
    for tok in (so_ids_csv or "").split(","):
        tok = tok.strip()
        if tok.isdigit():
            out.append(int(tok))
    return out


def _override_cfg_from_plan(plan: list) -> dict:
    """plan 의 inventory_agent step policy 에서 priority_override(yaml 블록) 추출."""
    for step in plan or []:
        if step.get("agent") == "inventory_agent":
            ov = (step.get("policy") or {}).get("priority_override")
            if isinstance(ov, dict):
                return ov
    return {}


def _build_priority_override(
    *, so_ids: List[int], requested_by: str, reason: str, cfg: dict,
) -> dict:
    """override 런타임 객체 생성 — issued_at / expires_at(auto_expire_hours) 계산."""
    from datetime import datetime as _dt, timedelta as _td
    now = _dt.utcnow()
    hours = int((cfg or {}).get("auto_expire_hours", 24))
    return {
        "so_ids": so_ids,
        "requested_by": (requested_by or "").strip(),
        "reason": (reason or "").strip(),
        "issued_at": now.isoformat() + "Z",
        "expires_at": (now + _td(hours=hours)).isoformat() + "Z",
        "cfg": dict(cfg or {}),
    }


def _enforce_override_gate(engine, override: dict) -> dict:
    """발행 시점 hard-gate. Returns {allowed, reason, today_count, max_per_day}.

    · requires_authorization: requested_by 비어있으면 거부.
    · max_per_day: 오늘(UTC) 발행된 allowed override_audit 건수 >= 상한이면 거부.
    실패해도 호출부는 시스템을 멈추지 않고 정상 VIP-first 로 폴백한다.
    """
    cfg = override.get("cfg") or {}
    if cfg.get("requires_authorization", True) and not override.get("requested_by"):
        return {"allowed": False, "reason": "requires_authorization: requested_by 누락",
                "today_count": 0, "max_per_day": cfg.get("max_per_day")}

    max_per_day = cfg.get("max_per_day")
    today_count = 0
    if max_per_day is not None:
        from datetime import datetime as _dt
        today = _dt.utcnow().date().isoformat()
        try:
            for k in (engine.memory.list_keys(tier="warm", limit=500) or []):
                if not str(k).startswith("override_audit:"):
                    continue
                rec = engine.memory.get(k, tier="warm") or {}
                if (rec.get("issued_at") or "")[:10] == today \
                        and (rec.get("gate") or {}).get("allowed"):
                    today_count += 1
        except Exception as e:
            logger.warning(f"[override gate] max_per_day count 실패: {e}")
        if today_count >= int(max_per_day):
            return {"allowed": False,
                    "reason": f"max_per_day 초과 ({today_count}/{max_per_day})",
                    "today_count": today_count, "max_per_day": max_per_day}
    return {"allowed": True, "reason": "ok",
            "today_count": today_count, "max_per_day": max_per_day}


def _write_override_audit(
    engine, *, payload_id: str, override: dict, gate: dict, trigger: str,
) -> str:
    """override_audit:<payload_id> 를 warm 메모리에 기록 (ttl = audit_retention_days)."""
    import time as _t
    cfg = override.get("cfg") or {}
    retention_days = int(cfg.get("audit_retention_days", 90))
    key = f"override_audit:{payload_id}"
    rec = {
        "ts": _t.time(),
        "issued_at": override.get("issued_at"),
        "expires_at": override.get("expires_at"),
        "so_ids": override.get("so_ids"),
        "requested_by": override.get("requested_by"),   # 누가
        "reason": override.get("reason"),               # 왜
        "trigger": trigger,                             # 어디서
        "mode": cfg.get("mode"),
        "gate": gate,
    }
    try:
        engine.manage_memory(key, rec, tier="warm", ttl_sec=retention_days * 86400)
    except Exception as e:
        logger.warning(f"[override audit] write 실패: {e}")
    return key


def _prepare_override(engine, *, payload_id: str, plan: list, so_ids_csv: str,
                      requested_by: str, reason: str, trigger: str) -> tuple:
    """so_ids_csv 가 있으면 cfg 추출 → 빌드 → 가드 → audit.

    Returns (override_or_None, gate_or_None).  override 가 None 이면 정상 VIP-first.
    """
    so_ids = _parse_override_so_ids(so_ids_csv)
    if not so_ids:
        return None, None
    cfg = _override_cfg_from_plan(plan)
    if not cfg.get("enabled"):
        return None, {"allowed": False,
                      "reason": "priority_override 비활성 (yaml enabled=false 또는 미설정)",
                      "so_ids": so_ids}
    override = _build_priority_override(
        so_ids=so_ids, requested_by=requested_by, reason=reason, cfg=cfg,
    )
    gate = _enforce_override_gate(engine, override)
    _write_override_audit(
        engine, payload_id=payload_id, override=override, gate=gate, trigger=trigger,
    )
    if not gate.get("allowed"):
        return None, gate    # 폴백 — 정상 VIP-first
    return override, gate


def _override_decision_record(so_ids_csv, override, gate, requested_by, reason):
    """audit decision_record 의 'override' 필드 — 두 trigger 공용 (중복 제거).

    requested_by/reason 는 raw 파라미터 — 게이트가 거부해 override=None 이어도
    "누가 시도했나" 를 audit 에 남긴다.
    """
    so_ids = _parse_override_so_ids(so_ids_csv)
    if not so_ids:
        return None
    return {
        "requested": True,
        "applied": bool(override),
        "so_ids": so_ids,
        "requested_by": requested_by or None,
        "reason": reason or None,
        "gate": gate,
    }


@mcp.tool()
async def trigger_inventory_allocation_window(
    cutoff_at: str = "",
    note: str = "",
    dispatch: bool = True,
    priority_override_so_ids: str = "",
    override_reason: str = "",
    override_requested_by: str = "",
) -> dict:
    """
    BC3 — 사전 배치 (Pre-allocation Batched Priority) cut-off 트리거.

    "지금 13:00 cut-off 시점입니다. 모인 outgoing picking 들을 tier 우선순위로
    일괄 reserve 해주세요." 같은 자연어 요청 시 Claude 가 호출하는 도구.

    전제: Odoo 의 outgoing stock.picking.type.reservation_method='manual'
    (SO confirm 시 picking 만 생성, reserve 보류된 상태가 누적).

    동작:
      1. ontology engine — entity 'inventory_allocation_window_cutoff' 발화
      2. rule 415 (inventory_batch_allocation_window) 매칭 → plan 생성
      3. 결정 이력을 warm 메모리에 audit 기록 — dashboard 의 "Recent Decisions" 가 표시
      4. dispatch=True 면 plan 의 inventory_agent.allocate_batched_by_tier 실행:
         · confirmed / waiting / partially_available outgoing picking 전체 query
         · tier 정렬 (VIP > Standard > Bronze)
         · stock.picking.action_assign (public, Odoo 19.2 호환) 순서대로 호출

    Args:
        cutoff_at: ISO datetime — cut-off 시점 (생략 시 호출 시각).
        note:      cut-off 메모 (audit log).
        dispatch:  True (기본) → inventory_agent 실제 실행. False → plan 만 반환 (dry run).

    Returns:
        ontology_trace + dispatched results
    """
    import time as _time
    from datetime import datetime as _dt
    start = _time.time()

    try:
        engine = get_or_create_ontology_engine()
    except Exception as e:
        return {"ok": False, "error": f"ontology engine init failed: {e}"}

    cutoff_at = cutoff_at or (_dt.utcnow().isoformat() + "Z")

    payload = {
        "id": f"inv_cutoff_{int(start)}",
        "cutoff_at": cutoff_at,
        "note": note,
    }

    try:
        ctx = engine.resolve_links("inventory_allocation_window_cutoff", payload)
        action = engine.check_rules(ctx)
        plan = engine.trigger_events(action, ctx) if action else []

        # BC4 S0: priority override 준비 (cutoff entity 는 resolve_links 가 raw 통과 →
        # context.receipt 가 비어있으므로 policy 채널만 신뢰)
        override, override_gate = _prepare_override(
            engine, payload_id=payload["id"], plan=plan,
            so_ids_csv=priority_override_so_ids,
            requested_by=override_requested_by, reason=override_reason,
            trigger="inventory_allocation_window_cutoff",
        )

        # audit 기록 (process_with_ontology 와 동일 패턴)
        mem_key = f"ontology_decision:{payload['id']}"
        decision_record = {
            "ts": _time.time(),
            "entity": "inventory_allocation_window_cutoff",
            "cutoff_at": cutoff_at,
            "note": note,
            "matched_rule": (action or {}).get("rule_name"),
            "plan": plan,
            "override": _override_decision_record(
                priority_override_so_ids, override, override_gate,
                override_requested_by, override_reason),
        }
        engine.manage_memory(mem_key, decision_record, tier="warm")
        rule_tier = (action or {}).get("memory_tier", "warm")
        if rule_tier == "hot":
            engine.manage_memory(mem_key, decision_record, tier="hot")

        # dispatch — cut-off rule 의 plan 은 inventory_agent + crm_agent(log) 만
        # (spawn_events / service_plan 없음, 단순 순차)
        dispatched: List[Dict[str, Any]] = []
        if dispatch and plan:
            user_id = get_current_user() or 'admin'
            if user_id not in _user_agents_cache:
                try:
                    get_or_create_orchestrator(user_id)
                except Exception as _e:
                    logger.warning(
                        f"[trigger_inventory_allocation_window] agent 초기화 실패 "
                        f"({user_id}): {_e}"
                    )
            agents_for_user = (
                _user_agents_cache.get(user_id)
                or _user_agents_cache.get('admin')
                or {}
            )
            for idx, step in enumerate(plan):
                agent_id = step.get("agent")
                action_name = step.get("action")
                policy = step.get("policy") or {}
                # BC4 S0: override 통과 시 inventory_agent step 에 런타임 주입 (policy 채널)
                if override and agent_id == "inventory_agent":
                    policy = {**policy, "priority_override_runtime": override}
                agent_obj = agents_for_user.get(agent_id)
                if not agent_obj:
                    dispatched.append({
                        "step": idx, "agent": agent_id, "action": action_name,
                        "success": False,
                        "error": f"agent '{agent_id}' 미등록",
                    })
                    continue
                try:
                    result = await agent_obj.execute_action(
                        action_name, policy=policy, context=ctx,
                    )
                    dispatched.append({
                        "step": idx, "agent": agent_id, "action": action_name,
                        "success": result.get("success"),
                        "result": result.get("result"),
                    })
                except Exception as e:
                    logger.error(
                        f"[trigger_inventory_allocation_window] dispatch step {idx} "
                        f"{agent_id}.{action_name} 실패: {e}", exc_info=True,
                    )
                    dispatched.append({
                        "step": idx, "agent": agent_id, "action": action_name,
                        "success": False,
                        "error": f"{type(e).__name__}: {str(e)[:300]}",
                    })

        # narrative
        rule_name = (action or {}).get("rule_name") or "none"
        if rule_name == "inventory_batch_allocation_window":
            inv_step = next(
                (d for d in dispatched if d.get("agent") == "inventory_agent"), None,
            )
            if inv_step and inv_step.get("result"):
                summary = (inv_step["result"] or {}).get("summary") or {}
                narrative = (
                    f"📦 Cut-off {cutoff_at} — batched allocation 완료. "
                    f"fully_assigned={summary.get('fully_assigned', 0)} / "
                    f"partially={summary.get('partially_available', 0)} / "
                    f"waiting={summary.get('still_waiting', 0)}."
                )
            else:
                narrative = (
                    f"📦 Cut-off {cutoff_at} — rule fire, plan {len(plan)} step "
                    f"(dispatch={dispatch})."
                )
        else:
            narrative = f"⚠️ Cut-off {cutoff_at} — rule 매칭 없음."

        return {
            "ok": True,
            "ontology_trace": {
                "entity": "inventory_allocation_window_cutoff",
                "matched_rule": rule_name,
                "action": action,
                "event_plan": plan,
                "memory": {"key": mem_key, "tier": rule_tier},
            },
            "narrative": narrative,
            "dispatched": dispatched if dispatch else None,
            "duration_ms": round((_time.time() - start) * 1000, 2),
        }
    except Exception as e:
        logger.error(f"trigger_inventory_allocation_window 실패: {e}", exc_info=True)
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {str(e)[:300]}",
            "duration_ms": round((_time.time() - start) * 1000, 2),
        }


@mcp.tool()
async def trigger_stock_received(
    qty: float,
    product_id: int = 0,
    product_name: str = "",
    source_note: str = "",
    add_to_inventory: bool = True,
    dispatch: bool = True,
    incoming_picking_id: int = 0,
    priority_override_so_ids: str = "",
    override_reason: str = "",
    override_requested_by: str = "",
) -> dict:
    """
    BC3 — 입고 (Stock Received) 이벤트 트리거.

    "지금 USB SecureKey-100 500개 입고됐어요. backorder VIP 부터 채워주세요." 같은
    자연어 요청 시 Claude 가 호출하는 도구.

    동작:
      1. (옵션, 기본 True) add_to_inventory → register_stock_receipt 로 실 stock 증가.
         실 PO 흐름 우회 — vendor / purchase.order / incoming picking 없이 stock.quant
         직접 주입. 실 운영이면 PO receipt validate 시점에 들어올 이벤트의 모방.
      2. ontology engine — entity 'stock_received' 발화. receipt={product_id, qty, ...}
      3. rule 400 (stock_received_replenish) 매칭 → plan 생성
      4. dispatch=True 면 inventory_agent.replenish_priority_queue 실행:
         · waiting / confirmed / partially_available picking 들을 product 별로 묶고
         · VIP 부터 reserve 시도 — backorder 채움 (consume_all_for_vip_first)
         · 남는 양으로 Standard Waiting picking 해소

    Args:
        qty: 입고 수량 (필수).
        product_id: Odoo product.product id (선택, product_name 우선).
        product_name: Odoo product 이름 (예: "USB SecureKey-100"). product_id 비어있을 때
                      이름으로 검색.
        source_note: 입고 메모 (PO 번호, vendor 등 audit log).
        add_to_inventory: True (기본) → 실 stock 증가. False → 이벤트만 발화 (dry run).
        dispatch: True (기본) → inventory_agent.replenish 실행.

    Returns:
        ontology_trace + dispatched results + stock_update
    """
    import time as _time
    from datetime import datetime as _dt
    start = _time.time()

    try:
        engine = get_or_create_ontology_engine()
    except Exception as e:
        return {"ok": False, "error": f"ontology engine init failed: {e}"}

    # product 식별 — product_name 우선
    if not product_id and product_name:
        try:
            from mcp_server.services import odoo_service
            pids = odoo_service.call(
                "product.product", "search", [("name", "=", product_name)],
            )
            if pids:
                product_id = pids[0]
        except Exception as e:
            logger.warning(f"[trigger_stock_received] product 이름 검색 실패: {e}")

    if not product_id:
        return {
            "ok": False,
            "error": f"product 식별 실패 — product_id 또는 product_name 필요 "
                     f"(받은 값: id={product_id}, name={product_name!r})",
        }

    if qty <= 0:
        return {"ok": False, "error": f"qty 는 0 보다 커야 함 (받은 값: {qty})"}

    # 1. 실 stock 증가 (옵션) — Odoo 19.2 운영성 정합 v2:
    #    옛 구현은 register_stock_receipt 로 stock.quant 직접 write (PO/incoming
    #    picking 우회). 그러면 Odoo UI 의 Incoming Transfers 에 표시 안 되고
    #    audit trail 빈약. 새 구현은 product 의 pending incoming picking 을 찾아
    #    button_validate (Odoo 의 공식 입고 확정) → stock.move done →
    #    quant 자동 증가. PO 흐름과 동일한 결과 (PO 없이도).
    stock_update = None
    if add_to_inventory:
        from mcp_server.services import odoo_service
        try:
            # BC5 루프 닫힘: 호출자가 정확한 picking_id 를 주면 그걸 검증한다(결정적).
            # 안 주면 product 기준으로 가장 이른 scheduled_date 의 incoming 1건을 고른다
            # (이전엔 정렬 없이 [0] → 여러 incoming 중 엉뚱한 걸 validate 할 위험).
            if incoming_picking_id:
                pending_pids = [incoming_picking_id]
            else:
                pending_pids = await asyncio.to_thread(
                    odoo_service.call, "stock.picking", "search",
                    [
                        ("picking_type_id.code", "=", "incoming"),
                        ("state", "in", ["assigned", "confirmed", "waiting"]),
                        ("move_ids.product_id", "=", product_id),
                    ],
                    order="scheduled_date asc, id asc",
                )
            if pending_pids:
                picking_id = pending_pids[0]
                await asyncio.to_thread(
                    odoo_service.call, "stock.picking", "button_validate",
                    [picking_id],
                )
                p_after = await asyncio.to_thread(
                    odoo_service.get_picking, picking_id,
                )
                stock_update = {
                    "method": "incoming_picking_validated",
                    "picking_id": picking_id,
                    "picking_name": (p_after or {}).get("name"),
                    "state_after": (p_after or {}).get("state"),
                }
            else:
                # fallback: pending incoming 없으면 옛 방식 (quant 직접) — demo 호환
                stock_update = await asyncio.to_thread(
                    odoo_service.register_stock_receipt,
                    product_id=product_id, qty=qty,
                )
                stock_update["method"] = (
                    f"quant_direct (no pending incoming picking — fallback). "
                    f"orig={stock_update.get('method')}"
                )
        except Exception as e:
            logger.warning(f"[trigger_stock_received] inventory update 실패: {e}")
            stock_update = {"error": f"{type(e).__name__}: {str(e)[:300]}"}

    # 2. ontology engine 발화
    payload = {
        "id": f"stock_received_{int(start)}",
        "receipt": {
            "product_id": product_id,
            "qty": qty,
            "source": source_note or "manual_simulation",
            "received_at": _dt.utcnow().isoformat() + "Z",
        },
    }

    try:
        ctx = engine.resolve_links("stock_received", payload)
        action = engine.check_rules(ctx)
        plan = engine.trigger_events(action, ctx) if action else []

        # BC4 S0: priority override 준비 (so_ids 있으면 빌드 → 가드 → audit)
        override, override_gate = _prepare_override(
            engine, payload_id=payload["id"], plan=plan,
            so_ids_csv=priority_override_so_ids,
            requested_by=override_requested_by, reason=override_reason,
            trigger="stock_received",
        )
        if override:
            # context 채널 폴백용 — ctx["receipt"] 는 payload["receipt"] 와 동일 참조
            payload["receipt"]["priority_override"] = override

        # 3. audit memory write
        mem_key = f"ontology_decision:{payload['id']}"
        decision_record = {
            "ts": _time.time(),
            "entity": "stock_received",
            "receipt": payload["receipt"],
            "matched_rule": (action or {}).get("rule_name"),
            "plan": plan,
            "stock_update": stock_update,
            "override": _override_decision_record(
                priority_override_so_ids, override, override_gate,
                override_requested_by, override_reason),
        }
        engine.manage_memory(mem_key, decision_record, tier="warm")
        rule_tier = (action or {}).get("memory_tier", "warm")
        if rule_tier == "hot":
            engine.manage_memory(mem_key, decision_record, tier="hot")

        # 4. dispatch (cut-off tool 과 동일 패턴 — 단순 순차)
        dispatched: List[Dict[str, Any]] = []
        if dispatch and plan:
            user_id = get_current_user() or 'admin'
            if user_id not in _user_agents_cache:
                try:
                    get_or_create_orchestrator(user_id)
                except Exception as _e:
                    logger.warning(
                        f"[trigger_stock_received] agent 초기화 실패 ({user_id}): {_e}"
                    )
            agents_for_user = (
                _user_agents_cache.get(user_id)
                or _user_agents_cache.get('admin')
                or {}
            )
            for idx, step in enumerate(plan):
                agent_id = step.get("agent")
                action_name = step.get("action")
                policy = step.get("policy") or {}
                # BC4 S0: override 통과 시 inventory_agent step 에 런타임 주입 (policy 채널)
                if override and agent_id == "inventory_agent":
                    policy = {**policy, "priority_override_runtime": override}
                agent_obj = agents_for_user.get(agent_id)
                if not agent_obj:
                    dispatched.append({
                        "step": idx, "agent": agent_id, "action": action_name,
                        "success": False,
                        "error": f"agent '{agent_id}' 미등록",
                    })
                    continue
                try:
                    result = await agent_obj.execute_action(
                        action_name, policy=policy, context=ctx,
                    )
                    dispatched.append({
                        "step": idx, "agent": agent_id, "action": action_name,
                        "success": result.get("success"),
                        "result": result.get("result"),
                    })
                except Exception as e:
                    logger.error(
                        f"[trigger_stock_received] dispatch step {idx} "
                        f"{agent_id}.{action_name} 실패: {e}", exc_info=True,
                    )
                    dispatched.append({
                        "step": idx, "agent": agent_id, "action": action_name,
                        "success": False,
                        "error": f"{type(e).__name__}: {str(e)[:300]}",
                    })

        # narrative
        rule_name = (action or {}).get("rule_name") or "none"
        inv_step = next(
            (d for d in dispatched if d.get("agent") == "inventory_agent"), None,
        )
        if rule_name == "stock_received_replenish" and inv_step and inv_step.get("result"):
            inner = inv_step["result"] or {}
            replenished = inner.get("replenished") or []
            narrative = (
                f"📥 Stock received: product_id={product_id} qty={qty}. "
                f"replenished {len(replenished)} backorder picking(s)."
            )
        elif rule_name == "stock_received_replenish":
            narrative = (
                f"📥 Stock received: product_id={product_id} qty={qty}. "
                f"rule fire, plan {len(plan)} step (dispatch={dispatch})."
            )
        else:
            narrative = (
                f"⚠️ Stock received but no rule matched. "
                f"product_id={product_id} qty={qty}."
            )

        return {
            "ok": True,
            "stock_update": stock_update,
            "ontology_trace": {
                "entity": "stock_received",
                "matched_rule": rule_name,
                "action": action,
                "event_plan": plan,
                "memory": {"key": mem_key, "tier": rule_tier},
            },
            "narrative": narrative,
            "dispatched": dispatched if dispatch else None,
            "duration_ms": round((_time.time() - start) * 1000, 2),
        }
    except Exception as e:
        logger.error(f"trigger_stock_received 실패: {e}", exc_info=True)
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {str(e)[:300]}",
            "stock_update": stock_update,
            "duration_ms": round((_time.time() - start) * 1000, 2),
        }


@mcp.tool()
async def trigger_replenishment_check(
    product_id: int = 0,
    product_name: str = "",
    notify_to: str = "",
    vendor_name: str = "TechSupply Co",
    safety_buffer: float = 0,
    auto_create_po: bool = True,
    auto_send: bool = True,
    dry_run: bool = False,
    dispatch: bool = True,
) -> dict:
    """
    BC5 — 충족 불가 → 자율 보충 발주 + 담당자 브리핑 트리거.

    "USB SecureKey-100 재고 부족한데, 입고요청하고 담당자한테 알려줘" 같은 자연어
    요청 시 Claude 가 호출하는 도구. 규칙으로 못 푸는 예외(재고 0 + 선점 후보 없음)를
    agent 가 감지→판단(LLM 발주량/긴급도)→발주(incoming picking 생성)→담당자 브리핑.

    동작:
      1. product 식별 (product_name 우선) → get_open_demand_for_product 로 미충족 수요 집계
      2. unmet_qty <= 0 면 보충 불필요 → early return
      3. entity 'inventory_shortage_detected' 발화 (context.shortage 주입)
         → rule inventory_replenish_on_shortage 매칭
      4. dispatch=True 면:
         · inventory_agent.create_replenishment_po — 발주량 advisor(LLM) + incoming picking 생성
         · email_agent.send_replenishment_alert    — 담당자 브리핑 메일 (LLM 작성)
         · crm_agent.log_interaction
      agent_outputs 를 step 간 누적 → email step 이 발주 결과를 받아 본문 구성.

    Args:
        product_id / product_name: 보충 대상 제품 (이름 우선 검색).
        notify_to: 운영/구매 담당자 이메일 (브리핑 수신자). 비면 메일 draft 만.
        vendor_name: 보충 공급처 (없으면 자동 생성).
        safety_buffer: advisor rule 폴백 시 부족분에 더할 안전버퍼.
        auto_create_po: True(기본) → 실제 incoming picking 생성. False → 추천만.
        auto_send: True(기본) → 담당자 메일 발송. False → draft 만.
        dry_run: True → 발주·메일 모두 보류(추천/draft만). 라이브 시연 안전토글.
        dispatch: False → rule 매칭만 (plan 확인, 실행 안 함).

    Returns:
        ontology_trace + shortage + dispatched results
    """
    import os as _os
    import time as _time
    from datetime import datetime as _dt
    from mcp_server.services import odoo_service
    start = _time.time()

    # 담당자 주소는 데이터/config — ontology(전략) 에 박지 않는다.
    # 우선순위: 명시 인자 > 환경변수(REPLENISH_NOTIFY_TO) > 데모 기본(공개 채널 메일).
    notify_to = notify_to or _os.getenv("REPLENISH_NOTIFY_TO", "finance@example.com")

    try:
        engine = get_or_create_ontology_engine()
    except Exception as e:
        return {"ok": False, "error": f"ontology engine init failed: {e}"}

    # product 식별 — product_name 우선
    if not product_id and product_name:
        try:
            pids = await asyncio.to_thread(
                odoo_service.call, "product.product", "search",
                [("name", "=", product_name)],
            )
            if pids:
                product_id = pids[0]
        except Exception as e:
            logger.warning(f"[trigger_replenishment_check] product 이름 검색 실패: {e}")

    if not product_id:
        return {
            "ok": False,
            "error": f"product 식별 실패 — product_id 또는 product_name 필요 "
                     f"(받은 값: id={product_id}, name={product_name!r})",
        }

    # 안 C — trigger 는 '센서': 부족분 계산/판정은 inventory_agent 가 소유한다.
    # 여기선 제품 식별 + 이벤트 발화만. shortage 는 agent 가 직접 조회해서 결과로 돌려준다.
    shortage: Dict[str, Any] = {}   # 아래 dispatch 후 agent 결과에서 채움 (except 안전용 초기화)
    payload = {
        "id": f"replenish_check_{int(start)}",
        "product_id": product_id,
        "notify_to": notify_to,
    }

    try:
        ctx = engine.resolve_links("inventory_shortage_detected", payload)
        action = engine.check_rules(ctx)
        plan = engine.trigger_events(action, ctx) if action else []
        mem_key = f"ontology_decision:{payload['id']}"
        rule_tier = (action or {}).get("memory_tier", "warm")
        # audit 는 dispatch 후(agent 가 shortage 채운 뒤)에 기록 — 아래 참조.

        # dispatch — agent_outputs 를 step 간 누적 (email 이 발주 결과 참조)
        dispatched: List[Dict[str, Any]] = []
        agent_outputs: Dict[str, Any] = {}
        if dispatch and plan:
            user_id = get_current_user() or 'admin'
            if user_id not in _user_agents_cache:
                try:
                    get_or_create_orchestrator(user_id)
                except Exception as _e:
                    logger.warning(
                        f"[trigger_replenishment_check] agent 초기화 실패 ({user_id}): {_e}"
                    )
            agents_for_user = (
                _user_agents_cache.get(user_id)
                or _user_agents_cache.get('admin')
                or {}
            )
            for idx, step in enumerate(plan):
                agent_id = step.get("agent")
                action_name = step.get("action")
                policy = dict(step.get("policy") or {})

                # 트리거 인자로 rule policy 오버라이드
                if agent_id == "inventory_agent" and action_name == "create_replenishment_po":
                    policy["auto_create_po"] = auto_create_po
                    policy["dry_run"] = dry_run
                    if vendor_name:
                        policy["vendor_name"] = vendor_name
                    policy["safety_buffer_units"] = safety_buffer
                if agent_id == "email_agent" and action_name == "send_replenishment_alert":
                    policy["auto_send"] = auto_send and not dry_run
                    if notify_to:
                        policy["notify_to"] = notify_to

                # step context — 안 C: shortage 대신 product_id 주입(agent 가 직접 조회).
                # email step 은 agent_outputs[create_replenishment_po] 에서 shortage·po 를 읽음.
                step_ctx = {
                    **ctx,
                    "product_id": product_id,
                    "notify_to": notify_to,
                    "agent_outputs": dict(agent_outputs),
                }

                agent_obj = agents_for_user.get(agent_id)
                if not agent_obj:
                    dispatched.append({
                        "step": idx, "agent": agent_id, "action": action_name,
                        "success": False, "error": f"agent '{agent_id}' 미등록",
                    })
                    continue
                try:
                    result = await agent_obj.execute_action(
                        action_name, policy=policy, context=step_ctx,
                    )
                    inner = result.get("result") if isinstance(result, dict) else None
                    # 다음 step 이 참조하도록 action 명으로 결과 누적
                    if isinstance(inner, dict):
                        agent_outputs[action_name] = inner
                    dispatched.append({
                        "step": idx, "agent": agent_id, "action": action_name,
                        "success": result.get("success"),
                        "result": inner,
                    })
                except Exception as e:
                    logger.error(
                        f"[trigger_replenishment_check] dispatch step {idx} "
                        f"{agent_id}.{action_name} 실패: {e}", exc_info=True,
                    )
                    dispatched.append({
                        "step": idx, "agent": agent_id, "action": action_name,
                        "success": False,
                        "error": f"{type(e).__name__}: {str(e)[:300]}",
                    })

        # 안 C: shortage 는 agent(create_replenishment_po)가 조회·반환 → 여기서 수거.
        rule_name = (action or {}).get("rule_name") or "none"
        repl_step = next(
            (d for d in dispatched
             if d.get("action") == "create_replenishment_po"), None,
        )
        mail_step = next(
            (d for d in dispatched
             if d.get("action") == "send_replenishment_alert"), None,
        )
        r = (repl_step or {}).get("result") or {}
        shortage = r.get("shortage") or {}
        unmet = float(shortage.get("unmet_qty") or 0)

        # audit memory (agent 결과 기반 — advisor/po 까지 포함해 더 풍부)
        decision_record = {
            "ts": _time.time(),
            "entity": "inventory_shortage_detected",
            "shortage": shortage,
            "matched_rule": rule_name,
            "plan": plan,
            "dispatched": dispatched if dispatch else None,
        }
        engine.manage_memory(mem_key, decision_record, tier="warm")
        if rule_tier == "hot":
            engine.manage_memory(mem_key, decision_record, tier="hot")

        # narrative
        if repl_step and r.get("skipped"):
            narrative = (
                f"✅ {shortage.get('product_name') or product_id}: 미충족 수요 없음 "
                f"(available={shortage.get('available')}, incoming={shortage.get('incoming')}). "
                f"보충 발주 불필요."
            )
        elif rule_name == "inventory_replenish_on_shortage" and repl_step:
            po = (r.get("po") or {})
            adv = (r.get("advisor") or {})
            mode = "DRY-RUN(추천만)" if dry_run else (
                "발주 생성" if po.get("picking_name") else "발주 보류")
            mail_res = (mail_step or {}).get("result") or {}
            mail_to = mail_res.get("to")
            # 실제 발송 성공(success=True) + 진짜 주소(@)일 때만 '통보' 표기.
            mail_sent = bool(
                (mail_step or {}).get("success")
                and not mail_res.get("skipped")
                and mail_to and "@" in str(mail_to)
            )
            narrative = (
                f"🟠 충족 불가 감지: {shortage.get('product_name')} 미충족 {int(unmet)}개, "
                f"블록 주문 {len(shortage.get('blocked_orders') or [])}건"
                f"(VIP {r.get('vip_blocked_count', 0)}건). "
                f"AI 권장 발주 {r.get('recommended_qty')}개 "
                f"[urgency={adv.get('urgency')}, src={adv.get('source')}] → {mode}"
                f"{', 입고건 ' + po.get('picking_name') if po.get('picking_name') else ''}"
                f"{', 담당자 통보 ' + str(mail_to) if mail_sent else ''}."
            )
        elif rule_name == "inventory_replenish_on_shortage":
            narrative = (
                f"🟠 재고 점검 발화 — rule fire, plan {len(plan)} step "
                f"(dispatch={dispatch}). product_id={product_id}."
            )
        else:
            narrative = (
                f"⚠️ rule 매칭 안 됨. product_id={product_id}."
            )

        return {
            "ok": True,
            "shortage": shortage,
            "ontology_trace": {
                "entity": "inventory_shortage_detected",
                "matched_rule": rule_name,
                "action": action,
                "event_plan": plan,
                "memory": {"key": mem_key, "tier": rule_tier},
            },
            "narrative": narrative,
            "dispatched": dispatched if dispatch else None,
            "duration_ms": round((_time.time() - start) * 1000, 2),
        }
    except Exception as e:
        logger.error(f"trigger_replenishment_check 실패: {e}", exc_info=True)
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {str(e)[:300]}",
            "shortage": shortage,
            "duration_ms": round((_time.time() - start) * 1000, 2),
        }


@mcp.tool()
async def trigger_delivery_dispatch(picking_id: int, dispatch: bool = True) -> dict:
    """
    BC4 S1 — VIP 출하 트리거 (부분출하 advisor 포함, 2-step 중 1단계).

    "S00009 출하해줘" 같은 요청 시 호출. picking 을 delivery_ready_check 로 발화 →
    rule 405(delivery_ready_to_ship_vip) → inventory_agent.dispatch_shipment.

    동작:
      · 전량 reserved → 즉시 출고(done) + 후속 알림(email/calendar/crm) 진행.
      · 부분 reserved(partially_available) + partial_handling=llm_advisor →
        advisor 가 split/wait 추천만 반환하고 **출하는 보류**(pending_confirmation).
        추천 + wizard 를 warm 메모리에 저장 → confirm_partial_shipment 가 승인 실행.
        (보류 시 '출고됨' 알림은 보내지 않도록 후속 step 을 건너뛴다.)
    """
    import time as _time
    from datetime import datetime as _dt
    from mcp_server.services import odoo_service
    start = _time.time()

    try:
        engine = get_or_create_ontology_engine()
    except Exception as e:
        return {"ok": False, "error": f"ontology engine init failed: {e}"}

    # 1. picking → tier/state/demand 부착해 delivery_ready_check payload 구성
    try:
        p = await asyncio.to_thread(odoo_service.get_picking, picking_id)
    except Exception as e:
        return {"ok": False, "error": f"picking 조회 실패: {type(e).__name__}: {e}"}
    if not p:
        return {"ok": False, "error": f"picking {picking_id} 없음"}

    sale_field = p.get("sale_id")
    sale_id = sale_field[0] if isinstance(sale_field, list) and sale_field else sale_field
    tier = "Standard"
    if sale_id:
        try:
            tmap = await asyncio.to_thread(odoo_service.get_sale_order_tier_map, [sale_id])
            tier = (tmap or {}).get(sale_id, "Standard")
        except Exception as e:
            logger.warning(f"[trigger_delivery_dispatch] tier 조회 실패: {e}")
    try:
        shortage = await asyncio.to_thread(odoo_service.get_picking_shortage, picking_id)
    except Exception:
        shortage = {"demand": None, "reserved": None, "shortage": None}

    payload = {
        "id": f"delivery_dispatch_{int(start)}",
        "picking": {
            "id": picking_id,
            "name": p.get("name"),
            "state": p.get("state"),
            "scheduled_date": p.get("scheduled_date"),
            "sale_order_id": sale_id,
            "tier": tier,
            "qty_demand": shortage.get("demand"),
        },
        "inventory": {"reserved": shortage.get("reserved"),
                      "shortage": shortage.get("shortage")},
    }

    try:
        ctx = engine.resolve_links("delivery_ready_check", payload)
        action = engine.check_rules(ctx)
        plan = engine.trigger_events(action, ctx) if action else []
        rule_name = (action or {}).get("rule_name") or "none"

        dispatched: List[Dict[str, Any]] = []
        advisor_pending = None
        if dispatch and plan:
            user_id = get_current_user() or 'admin'
            if user_id not in _user_agents_cache:
                try:
                    get_or_create_orchestrator(user_id)
                except Exception as _e:
                    logger.warning(f"[trigger_delivery_dispatch] agent 초기화 실패: {_e}")
            agents_for_user = (_user_agents_cache.get(user_id)
                               or _user_agents_cache.get('admin') or {})
            for idx, step in enumerate(plan):
                agent_id = step.get("agent")
                action_name = step.get("action")
                policy = step.get("policy") or {}
                agent_obj = agents_for_user.get(agent_id)
                if not agent_obj:
                    dispatched.append({"step": idx, "agent": agent_id,
                                       "action": action_name, "success": False,
                                       "error": f"agent '{agent_id}' 미등록"})
                    continue
                try:
                    result = await agent_obj.execute_action(
                        action_name, policy=policy, context=ctx)
                    inner = result.get("result") or {}
                    dispatched.append({"step": idx, "agent": agent_id,
                                       "action": action_name,
                                       "success": result.get("success"),
                                       "result": inner})
                    # 부분출하 보류 → 후속 알림 step 중단 (premature 'shipped' 방지)
                    if agent_id == "inventory_agent" and inner.get("pending_confirmation"):
                        advisor_pending = inner
                        break
                except Exception as e:
                    logger.error(f"[trigger_delivery_dispatch] step {idx} 실패: {e}",
                                 exc_info=True)
                    dispatched.append({"step": idx, "agent": agent_id,
                                       "action": action_name, "success": False,
                                       "error": f"{type(e).__name__}: {str(e)[:300]}"})

        # 2. audit + (보류 시) wizard 저장
        mem_key = f"ontology_decision:{payload['id']}"
        decision_record = {
            "ts": _time.time(),
            "entity": "delivery_ready_check",
            "picking": payload["picking"],
            "matched_rule": rule_name,
            "plan": plan,
        }
        if advisor_pending:
            decision_record["partial_advice"] = {
                "picking_id": picking_id,
                "advisor": advisor_pending.get("advisor"),
                "shortage": advisor_pending.get("shortage"),
            }
            # confirm 단계가 재사용할 wizard + 추천 저장 (24h)
            engine.manage_memory(
                f"partial_advice:{picking_id}",
                {"ts": _time.time(), "picking_id": picking_id,
                 "wizard": advisor_pending.get("wizard"),
                 "advisor": advisor_pending.get("advisor"),
                 "shortage": advisor_pending.get("shortage")},
                tier="warm", ttl_sec=86400)
        engine.manage_memory(mem_key, decision_record, tier="warm")

        if advisor_pending:
            adv = advisor_pending.get("advisor") or {}
            narrative = (
                f"🟡 {p.get('name')} 부분출하 판단 필요 — advisor 추천: "
                f"{adv.get('recommendation')} ({adv.get('source')}). "
                f"부족 {shortage.get('shortage')}. confirm_partial_shipment 로 승인 필요."
            )
        elif rule_name == "delivery_ready_to_ship_vip":
            narrative = f"📦 {p.get('name')} 출고 처리 (rule {rule_name}, {len(dispatched)} step)."
        else:
            narrative = f"⚠️ {p.get('name')} — rule 매칭 없음 (tier={tier}, state={p.get('state')})."

        return {
            "ok": True,
            "ontology_trace": {"entity": "delivery_ready_check",
                               "matched_rule": rule_name, "event_plan": plan,
                               "memory": {"key": mem_key, "tier": "warm"}},
            "narrative": narrative,
            "pending_confirmation": bool(advisor_pending),
            "advisor": (advisor_pending or {}).get("advisor"),
            "shortage": shortage,
            "dispatched": dispatched if dispatch else None,
            "duration_ms": round((_time.time() - start) * 1000, 2),
        }
    except Exception as e:
        logger.error(f"trigger_delivery_dispatch 실패: {e}", exc_info=True)
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}",
                "duration_ms": round((_time.time() - start) * 1000, 2)}


@mcp.tool()
async def confirm_partial_shipment(
    picking_id: int, decision: str, confirmed_by: str = "",
) -> dict:
    """
    BC4 S1 — 부분출하 승인 (2-step 중 2단계, 결정론 실행).

    trigger_delivery_dispatch 가 보류해둔 wizard 를 읽어, 사람이 내린 decision 으로
    실제 출하를 실행한다. advisor 추천을 따를 수도, 무시할 수도 있다(사람 권한).

    Args:
        picking_id: 대상 picking.
        decision: 'split'(가용분+backorder) | 'cancel'(가용분+나머지취소) | 'wait'(보류 유지).
        confirmed_by: 승인자 (audit). 빈 값이면 거부.
    """
    import time as _time
    from mcp_server.services import odoo_service
    start = _time.time()

    decision = (decision or "").strip().lower()
    if decision not in ("split", "cancel", "wait"):
        return {"ok": False, "error": f"decision 은 split|cancel|wait (받음: {decision!r})"}
    if not (confirmed_by or get_current_user()):
        return {"ok": False, "error": "confirmed_by 필요 (승인자 미상)"}

    try:
        engine = get_or_create_ontology_engine()
    except Exception as e:
        return {"ok": False, "error": f"ontology engine init failed: {e}"}

    stored = None
    for _tier in ("warm", "hot"):
        try:
            stored = engine.memory.get(f"partial_advice:{picking_id}", tier=_tier)
        except Exception:
            stored = None
        if stored:
            break
    if not stored:
        return {"ok": False,
                "error": f"picking {picking_id} 의 보류된 부분출하 건 없음 "
                         f"(trigger_delivery_dispatch 먼저 호출 필요)"}

    wizard = stored.get("wizard") or {}
    res_id = wizard.get("res_id")
    wctx = wizard.get("context") or {}

    try:
        if decision == "split":
            exec_res = await asyncio.to_thread(odoo_service.process_backorder, res_id, wctx)
        elif decision == "cancel":
            exec_res = await asyncio.to_thread(odoo_service.cancel_backorder, res_id, wctx)
        else:  # wait — 아무것도 실행 안 함, picking 은 partially_available 유지
            exec_res = {"action": "wait", "ok": True}
    except Exception as e:
        logger.error(f"confirm_partial_shipment 실행 실패: {e}", exc_info=True)
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}

    # 멱등성: split/cancel 이 성공하면 보류 record 를 삭제 → 재호출로 중복 실행 방지.
    # (wait 은 보류 유지가 의도이므로 삭제 안 함.)
    if decision in ("split", "cancel") and exec_res.get("ok"):
        for _t in ("warm", "hot"):
            try:
                engine.memory.delete(f"partial_advice:{picking_id}", tier=_t)
            except Exception as e:
                logger.debug(f"[confirm_partial_shipment] memory delete({_t}) skip: {e}")

    # audit — 누가/무엇을 승인했나 (advisor 추천과 일치 여부 포함)
    advisor = stored.get("advisor") or {}
    audit = {
        "ts": _time.time(),
        "picking_id": picking_id,
        "decision": decision,
        "confirmed_by": confirmed_by or get_current_user(),
        "advisor_recommendation": advisor.get("recommendation"),
        "followed_advisor": (decision == "split" and advisor.get("recommendation") == "split")
                            or (decision == "wait" and advisor.get("recommendation") == "wait"),
        "execution_result": exec_res,
    }
    try:
        engine.manage_memory(f"partial_confirm:{picking_id}:{int(start)}", audit, tier="warm")
    except Exception as e:
        logger.warning(f"[confirm_partial_shipment] audit 기록 실패: {e}")

    return {
        "ok": bool(exec_res.get("ok")),
        "picking_id": picking_id,
        "decision": decision,
        "followed_advisor": audit["followed_advisor"],
        "advisor_recommendation": advisor.get("recommendation"),
        "execution_result": exec_res,
        "duration_ms": round((_time.time() - start) * 1000, 2),
    }


@mcp.tool()
def get_ontology_decisions(limit: int = 10) -> dict:
    """
    최근 OOSDK 의사결정 이력을 반환합니다 (warm + hot tier 합산, ts 역순).
    Dashboard "Recent Decisions" 패널이 사용.

    Args:
        limit: 반환할 최대 건수 (기본 10)

    Note:
        이전 구현은 "warm 비어있으면 hot fallback" 이라 warm 에 1건이라도
        있으면 hot 의 VIP/Standard 결정이 영원히 안 보였음. 두 tier 모두에서
        ontology_decision:* 키를 수집해 ts 역순 합산으로 변경.
        (dashboard_api.list_ontology_decisions 와 동일 로직)
    """
    try:
        engine = get_or_create_ontology_engine()
        rows: List[Dict[str, Any]] = []
        for tier in ("hot", "warm"):
            try:
                keys = engine.memory.list_keys(tier=tier, limit=limit * 4) or []
            except Exception as e:
                logger.warning(f"list_keys({tier}) 실패: {e}")
                continue
            for k in keys:
                if not str(k).startswith("ontology_decision:"):
                    continue
                try:
                    v = engine.memory.get(k, tier=tier) or {}
                except Exception:
                    continue
                rows.append({"key": k, "tier": tier, **v})

        # 동일 key 중복 제거 (hot 우선)
        dedup: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            key = r["key"]
            if key not in dedup or (r["tier"] == "hot" and dedup[key].get("tier") != "hot"):
                dedup[key] = r

        decisions = sorted(
            dedup.values(), key=lambda r: r.get("ts") or 0, reverse=True
        )[:limit]

        return {
            "ok": True,
            "count": len(decisions),
            "decisions": decisions,
        }
    except Exception as e:
        logger.error(f"get_ontology_decisions 실패: {e}", exc_info=True)
        return {"ok": False, "error": str(e), "decisions": []}


# ============================================================
# 시스템 도구 (서비스 상태, Agent 정보 등)
# ============================================================

@mcp.tool()
def check_all_services_status() -> dict:
    """모든 서비스와 Agent의 현재 상태를 확인합니다."""
    current_user = get_current_user()
    logger.info(f"📊 서비스 상태 확인 요청 (user: {current_user})")

    try:
        if current_user:
            status = get_user_service_status(current_user)
        else:
            status = get_all_service_status()

        # Agent 정보 추가
        agents_info = {}
        if _orchestrator:
            agents_info = _orchestrator.get_registered_agents()

        summary = {
            "mode": "multi-agent",
            "current_user": current_user,
            "services": {
                "gmail": "✅ 인증됨" if status['gmail']['authenticated'] else "❌ 미인증",
                "gmail_account": status['gmail'].get('user_email', 'unknown'),
                "openai": "✅ 설정됨" if (status['openai']['initialized'] and status['openai']['api_key_configured']) else "❌ 미설정",
                "salesforce": "✅ 인증됨" if status['salesforce']['authenticated'] else "❌ 미인증",
                "vectordb": "✅ 초기화됨" if status['vectordb']['initialized'] else "❌ 미초기화",
                "calendar": "✅ 인증됨" if status['calendar']['authenticated'] else "❌ 미인증",
            },
            "agents": agents_info,
        }

        return {"status": "success", "summary": summary, "details": status}

    except Exception as e:
        logger.error(f"❌ 상태 확인 실패: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


@mcp.tool()
def get_current_user_info() -> dict:
    """현재 연결된 사용자 정보를 반환합니다."""
    current_user = get_current_user()
    if current_user and current_user in _user_services_cache:
        user_data = _user_services_cache[current_user]
        return {
            "user_id": current_user,
            "gmail_account": user_data['config'].get('gmail_account', 'unknown'),
            "sfdc_enabled": user_data['config'].get('sfdc_enabled', False),
            "mode": "multi-agent",
        }
    return {"user_id": current_user or "unknown", "status": "not_initialized"}


@mcp.tool()
def get_agent_info() -> dict:
    """등록된 Agent들의 상세 정보를 반환합니다."""
    if _orchestrator:
        return {
            "status": "success",
            "agents": _orchestrator.get_registered_agents(),
            "total_agents": len(_orchestrator.get_registered_agents()),
        }
    return {"status": "not_initialized", "agents": {}}


@mcp.tool()
def get_execution_history(limit: int = 10) -> dict:
    """최근 Multi-Agent 실행 이력을 반환합니다."""
    if _orchestrator:
        history = _orchestrator.get_execution_history(limit)
        return {
            "status": "success",
            "count": len(history),
            "history": history,
        }
    return {"status": "not_initialized", "history": []}


# ============================================================
# 시스템/로깅 도구만 직접 노출 (Agent를 거칠 필요 없는 것들)
# 비즈니스 도구(Gmail, CRM, Calendar, Helpdesk)는 Agent 경유 전용
# ============================================================

from mcp_server.tools import register_logging_tools, register_erp_tools

def register_system_tools(mcp_instance):
    """시스템/로깅 도구만 직접 등록"""
    logger.info("🔧 시스템 도구 등록 중...")
    register_logging_tools(mcp_instance)
    # ERP read-only inspection 도구 — Claude Desktop 이 직접 조회 가능해야 함
    # (create_sales_order 같은 비즈니스 액션은 ontology dispatch 경유)
    register_erp_tools(mcp_instance)
    logger.info("✅ 시스템 도구 등록 완료!")

register_system_tools(mcp)


# ============================================================
# 서비스 초기화
# ============================================================

def initialize_default_services():
    """기본 서비스 초기화 (admin 사용자)"""
    logger.info("=" * 70)
    logger.info("🚀 Enterprise AI Multi-Agent Server 시작")
    logger.info("=" * 70)

    print_config_summary()

    if not validate_config():
        logger.warning("⚠️ 설정 검증 실패! 일부 기능이 제한될 수 있습니다.")

    logger.info("\n📡 기본 서비스 초기화 중 (admin)...")

    try:
        get_or_create_user_services('admin')
        set_current_user('admin')
        get_or_create_orchestrator('admin')
        logger.info("✅ 기본 서비스 + Agent 초기화 완료!")
    except Exception as e:
        logger.warning(f"⚠️ 서비스 초기화 중 오류: {e}")


# ============================================================
# 메인 함수
# ============================================================

def main():
    mode = os.getenv('MCP_MODE', 'stdio').lower()

    try:
        initialize_default_services()
    except Exception as e:
        logger.warning(f"⚠️ 서비스 초기화 중 오류: {e}")

    if mode == 'sse':
        host = os.getenv('HOST', '0.0.0.0')
        port = MCP_PORT  # 9100 (OOSDK)

        logger.info("\n" + "=" * 70)
        logger.info("✅ Enterprise AI Multi-Agent Server 준비 완료!")
        logger.info("=" * 70)
        logger.info("🌐 Streamable HTTP 모드로 서버 시작")
        logger.info(f"   Host: {host}")
        logger.info(f"   MCP Port: {port}")
        logger.info(f"   Log API Port: {LOG_API_PORT}")
        logger.info("")
        logger.info("   📌 엔드포인트:")
        logger.info(f"      http://{host}:{port}/mcp?user_id=admin")
        logger.info(f"      http://{host}:{port}/mcp?user_id=sales")
        logger.info(f"      http://{host}:{port}/mcp?user_id=finance")
        logger.info("")
        logger.info("   🤖 Agent 도구 (Claude AI가 직접 Agent 선택):")
        logger.info("      run_email_agent     - Email Agent (이메일 처리)")
        logger.info("      run_crm_agent       - CRM Agent (Salesforce)")
        logger.info("      run_calendar_agent  - Calendar Agent (일정 관리)")
        logger.info("      run_cs_agent        - CS Agent (고객 서비스)")
        logger.info("      run_helpdesk_agent  - Helpdesk Agent (내부 문서)")
        logger.info("      run_report_agent    - Report Agent (로그/통계)")
        logger.info("")
        logger.info(f"   지원 사용자: {', '.join(SUPPORTED_USERS)}")
        logger.info("=" * 70 + "\n")

        # 로그 API 서버 (별도 쓰레드)
        import threading

        def run_log_api():
            try:
                from fastapi import FastAPI
                from fastapi.middleware.cors import CORSMiddleware
                import uvicorn

                log_app = FastAPI(title="Multi-Agent MCP Log API")
                log_app.include_router(log_api_router, prefix="/api")
                # Dashboard 전용 HTTP API (ontology decisions / memory stats / logs).
                # 같은 프로세스에서 노출 → dashboard 는 fs/in-process 직접접근 끊고 HTTP fetch.
                log_app.include_router(dashboard_api_router, prefix="/api")
                log_app.add_middleware(
                    CORSMiddleware,
                    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
                )

                @log_app.get("/")
                async def root():
                    return {
                        "service": "Multi-Agent MCP Log API",
                        "mode": "multi-agent",
                        "status": "running"
                    }

                logger.info(f"📡 로그 API 서버 시작 (port {LOG_API_PORT})")
                uvicorn.run(log_app, host="0.0.0.0", port=LOG_API_PORT, log_level="warning")
            except Exception as e:
                logger.error(f"❌ 로그 API 서버 실패: {e}")

        log_thread = threading.Thread(target=run_log_api, daemon=True)
        log_thread.start()
        logger.info(f"📡 로그 API: http://0.0.0.0:{LOG_API_PORT}/api/logs/upload")

        # FastMCP 서버 실행
        mcp.run(transport="http", host=host, port=port)

    else:
        # stdio 모드
        logger.info("\n" + "=" * 70)
        logger.info("✅ Enterprise AI Multi-Agent Server 준비 완료!")
        logger.info("📟 stdio 모드로 서버 시작")
        logger.info("=" * 70 + "\n")
        mcp.run()


if __name__ == "__main__":
    main()
