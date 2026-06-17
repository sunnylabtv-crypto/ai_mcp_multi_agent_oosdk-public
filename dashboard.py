# dashboard.py
"""
Multi-Agent MCP 통합 대시보드 (Streamlit) — OOSDK 한국어 버전.

실행:
    streamlit run dashboard.py --server.port 9601

Architecture
------------
이 dashboard 는 **stateless HTTP viewer** 입니다.
모든 데이터는 MCP server (port 9101) 의 `/api/dashboard/*` 엔드포인트에서 fetch.
SQLite 파일 / OntologyEngine 인스턴스에 직접 접근하지 않습니다.

이렇게 분리한 이유: dashboard 와 MCP server 는 별개 프로세스이므로
in-memory state 공유 불가. 파일 mount 로 봉합하는 방식은 multi-container /
horizontal scale / managed DB 시점에 즉시 깨집니다.
→ 단일 source of truth = MCP server.

API base override: OOSDK_MCP_API_BASE  (기본값 http://localhost:9101/api)
"""
import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
import json

from dashboard_modules import api_client

# ============================================================
# 설정
# ============================================================

# Agent 정의
AGENTS = {
    "email_agent": {"name": "Email Agent", "icon": "📧", "desc": "Gmail 이메일 관리"},
    "crm_agent": {"name": "CRM Agent", "icon": "💼", "desc": "Salesforce CRM 관리"},
    "calendar_agent": {"name": "Calendar Agent", "icon": "📅", "desc": "Google Calendar 관리"},
    "cs_agent": {"name": "CS Agent", "icon": "🎧", "desc": "고객 서비스 (제품 문서)"},
    "helpdesk_agent": {"name": "Helpdesk Agent", "icon": "🏢", "desc": "내부 헬프데스크 (사내 문서)"},
    "report_agent": {"name": "Report Agent", "icon": "📊", "desc": "로그/통계 분석"},
    # ─── BC2 신규 agents ───
    "erp_agent": {"name": "ERP Agent", "icon": "📦", "desc": "Odoo Sales Order / 재고 (BC2 Win 분기)"},
    "analytics_agent": {"name": "Analytics Agent", "icon": "📈", "desc": "Lost 사유 분석 / 누적 패턴 (BC2 Lost 분기)"},
    # ─── BC3~BC5 재고 agent ───
    "inventory_agent": {"name": "Inventory Agent", "icon": "🚚", "desc": "Odoo 재고 VIP 선점 / 입고 재배정 / 자율 보충발주 (BC3~BC5)"},
}

# Agent → 도구 매핑
# MCP 도구명(run_*_agent) + 내부 서비스 도구명(local 로그 호환)
AGENT_TOOLS = {
    "email_agent": ["run_email_agent", "fetch_unread_emails", "send_email_reply", "get_gmail_status", "analyze_email_with_ai", "generate_email_reply"],
    "crm_agent": ["run_crm_agent", "create_salesforce_lead", "verify_salesforce_lead", "get_salesforce_status",
                  "search_lead_by_email", "search_account_by_name", "query_soql",
                  "create_opportunity", "verify_opportunity"],
    "calendar_agent": ["run_calendar_agent", "add_calendar_event", "get_calendar_events", "update_calendar_event", "delete_calendar_event", "search_calendar_events", "get_calendar_status"],
    "cs_agent": ["run_cs_agent", "upload_product_document", "search_product_documents", "answer_customer_inquiry", "list_product_documents"],
    "helpdesk_agent": ["run_helpdesk_agent", "upload_internal_document", "search_internal_documents", "ask_helpdesk", "list_internal_documents", "delete_internal_document"],
    "report_agent": ["run_report_agent", "query_logs", "get_stats", "get_errors", "get_slow_tools"],
    # ─── BC2 신규 agents 도구 ───
    "erp_agent": ["get_odoo_status", "find_existing_sales_order"],
    "analytics_agent": ["categorize_lost_reason", "get_lost_reason_summary"],
    # ─── BC3~BC5 재고 agent 도구 (MCP trigger + 내부 action) ───
    # agent_action:inventory_agent.* 는 prefix 로 자동 매칭되지만, MCP trigger 도구는
    # 명시해야 로그/통계에서 Inventory Agent 로 귀속된다.
    "inventory_agent": [
        "trigger_stock_received", "trigger_replenishment_check",
        "trigger_inventory_allocation_window", "trigger_delivery_dispatch",
        "confirm_partial_shipment",
        "create_replenishment_po", "allocate_with_preemption",
        "replenish_priority_queue", "allocate_fifo", "allocate_batched_by_tier",
        "dispatch_shipment", "split_fulfillment_path",
    ],
}

# ============================================================
# 2축 분류 체계
# ============================================================
# source     = 도구 실행 위치     (remote: GCP 서버 / local: PC 로컬)
# client_type = 호출 진입점(클라이언트)  (claude_desktop / cursor / adk / mcp)

CLIENT_TYPES = {
    "claude_desktop": {"name": "Claude Desktop", "icon": "🟣", "color": "#7C3AED"},
    "cursor":         {"name": "Cursor IDE", "icon": "📝", "color": "#10B981"},
    "adk":            {"name": "Web/Mobile (ADK)", "icon": "🌐", "color": "#E74C3C"},
    "mcp":            {"name": "MCP (기본)", "icon": "🔌", "color": "#4A90D9"},
    "local":          {"name": "Local Agent", "icon": "💻", "color": "#2ECC71"},
}

SOURCE_TYPES = {
    "remote": {"name": "Remote (서버)", "icon": "☁️"},
    "local":  {"name": "Local (PC)", "icon": "💻"},
}

st.set_page_config(
    page_title="Multi-Agent 대시보드",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded"
)


# ============================================================
# API helpers
# ============================================================
# 모든 함수는 MCP server `/api/dashboard/*` 호출.
# 실패 시 빈 DataFrame / 빈 dict 반환 → render 단계가 "데이터 없음" 으로 처리.

def _resp_ok(resp: dict) -> bool:
    return bool(resp) and resp.get("ok", False)


def fetch_user_ids() -> list[str]:
    resp = api_client.get_user_ids()
    if not _resp_ok(resp):
        return []
    return resp.get("user_ids") or []


def fetch_overview(start_time, end_time, user_id, source, client_type):
    """요약 카드 + 도구별 통계.

    Returns:
        (overall: dict, by_tool: pd.DataFrame, ok: bool, error: str|None)
    """
    resp = api_client.get_logs_overview(
        start_time=start_time, end_time=end_time,
        user_id=user_id, source=source, client_type=client_type,
    )
    if not _resp_ok(resp):
        return ({}, pd.DataFrame(), False, resp.get("error", "unknown"))
    overall = resp.get("overall") or {}
    by_tool = pd.DataFrame(resp.get("by_tool") or [])
    return (overall, by_tool, True, None)


def fetch_client_type_stats(start_time, end_time, user_id) -> pd.DataFrame:
    resp = api_client.get_client_type_stats(start_time=start_time, end_time=end_time, user_id=user_id)
    if not _resp_ok(resp):
        return pd.DataFrame()
    return pd.DataFrame(resp.get("rows") or [])


def fetch_hourly_calls(start_time, end_time, user_id, source, client_type) -> pd.DataFrame:
    resp = api_client.get_hourly_calls(
        start_time=start_time, end_time=end_time,
        user_id=user_id, source=source, client_type=client_type,
    )
    if not _resp_ok(resp):
        return pd.DataFrame()
    return pd.DataFrame(resp.get("rows") or [])


def fetch_agent_stats(start_time, end_time, user_id, source, client_type) -> dict:
    """Agent → {calls, success, errors, avg_duration} 매핑."""
    resp = api_client.get_agent_stats(
        agent_tools=AGENT_TOOLS,
        start_time=start_time, end_time=end_time,
        user_id=user_id, source=source, client_type=client_type,
    )
    if not _resp_ok(resp):
        return {}
    return resp.get("by_agent") or {}


def fetch_logs(start_time, end_time, tool_name, agent, user_id,
               success, source, client_type, keyword, limit) -> pd.DataFrame:
    """다축 필터 로그 검색."""
    agent_tools = None
    if agent and agent != "전체":
        agent_tools = AGENT_TOOLS.get(agent) or None

    resp = api_client.query_logs(
        start_time=start_time, end_time=end_time,
        tool_name=tool_name,
        agent_tools=agent_tools,
        user_id=user_id, source=source, client_type=client_type,
        success=success, keyword=keyword,
        limit=limit,
    )
    if not _resp_ok(resp):
        return pd.DataFrame()
    return pd.DataFrame(resp.get("logs") or [])


# ============================================================
# UI 컴포넌트
# ============================================================

def render_summary_cards(overall: dict):
    """상단 요약 카드"""
    col1, col2, col3, col4 = st.columns(4)

    total = int(overall.get('total_calls') or 0)
    success_count = int(overall.get('success_count') or 0)
    error_count = int(overall.get('error_count') or 0)
    avg_duration = overall.get('avg_duration_ms') or 0

    with col1:
        st.metric(label="총 호출", value=f"{total:,}")

    with col2:
        success_rate = (success_count / total * 100) if total > 0 else 0
        st.metric(label="성공률", value=f"{success_rate:.1f}%")

    with col3:
        st.metric(label="평균 응답", value=f"{avg_duration:.0f}ms")

    with col4:
        st.metric(label="에러 수", value=f"{error_count:,}")


def render_client_type_cards(client_stats: pd.DataFrame):
    """클라이언트(client_type)별 트래픽 카드"""
    if client_stats.empty:
        st.info("클라이언트별 데이터가 없습니다.")
        return

    active = []
    for _, row in client_stats.iterrows():
        ct = row['client_type'] or 'mcp'
        info = CLIENT_TYPES.get(ct, {"name": ct, "icon": "❓", "color": "#999"})
        active.append((ct, info, row))

    cols = st.columns(len(active))
    for i, (ct_key, ct_info, row) in enumerate(active):
        calls = int(row['calls'] or 0)
        errors = int(row['errors'] or 0)
        avg_dur = row['avg_duration'] or 0

        with cols[i]:
            st.markdown(f"""
            <div style="
                border: 1px solid #ddd;
                border-radius: 8px;
                padding: 14px;
                text-align: center;
                border-top: 4px solid {ct_info['color']};
                background: white;
            ">
                <div style="font-size: 22px;">{ct_info['icon']}</div>
                <div style="font-weight: bold; font-size: 13px;">{ct_info['name']}</div>
                <hr style="margin: 8px 0;">
                <div style="font-size: 20px; font-weight: bold; color: {ct_info['color']};">{calls:,}</div>
                <div style="font-size: 11px; color: gray;">호출 | 에러: {errors} | 평균: {avg_dur:.0f}ms</div>
            </div>
            """, unsafe_allow_html=True)


def render_agent_status(agent_stats: dict):
    """Agent별 상태 카드"""
    cols = st.columns(len(AGENTS))

    for i, (agent_key, agent_info) in enumerate(AGENTS.items()):
        stats = agent_stats.get(agent_key) or {"calls": 0, "success": 0, "errors": 0, "avg_duration": 0}
        calls = int(stats.get("calls") or 0)
        errors = int(stats.get("errors") or 0)
        avg_dur = stats.get("avg_duration") or 0

        with cols[i]:
            if calls == 0:
                status_color = "gray"
            elif errors > 0:
                status_color = "orange"
            else:
                status_color = "green"

            st.markdown(f"""
            <div style="
                border: 1px solid #ddd;
                border-radius: 8px;
                padding: 12px;
                text-align: center;
                border-left: 4px solid {status_color};
            ">
                <div style="font-size: 24px;">{agent_info['icon']}</div>
                <div style="font-weight: bold; font-size: 13px;">{agent_info['name']}</div>
                <div style="color: gray; font-size: 11px;">{agent_info['desc']}</div>
                <hr style="margin: 8px 0;">
                <div style="font-size: 12px;">호출: <b>{calls}</b> | 에러: <b>{errors}</b></div>
                <div style="font-size: 11px; color: gray;">평균: {avg_dur:.0f}ms</div>
            </div>
            """, unsafe_allow_html=True)


def render_chart(hourly_data: pd.DataFrame):
    """시간대별 호출 차트"""
    if hourly_data.empty:
        st.info("데이터가 없습니다.")
        return

    chart_data = hourly_data.set_index('hour')[['success', 'errors']]
    chart_data.columns = ['성공', '에러']
    st.bar_chart(chart_data)


def render_log_table(logs: pd.DataFrame):
    """로그 테이블"""
    if logs.empty:
        st.info("검색 결과가 없습니다.")
        return

    display_df = logs.copy()

    display_df['상태'] = display_df['success'].apply(lambda x: '✅' if x else '❌')

    # MCP 도구명 → Agent 키 역매핑
    _MCP_TO_AGENT = {f"run_{agent_key}": agent_key for agent_key in AGENTS}

    def get_agent_for_tool(tool_name):
        if not tool_name:
            return "⚙️ 시스템"
        # 1) MCP 도구 (run_<agent>_agent)
        if tool_name in _MCP_TO_AGENT:
            info = AGENTS[_MCP_TO_AGENT[tool_name]]
            return f"{info['icon']} {info['name']}"
        # 2) BaseAgent.execute_action / execute_tool 가 직접 기록한 내부 호출
        #    "agent_action:<agent>.<action>" 또는 "agent_tool:<agent>.<tool>"
        for prefix in ("agent_action:", "agent_tool:"):
            if tool_name.startswith(prefix):
                rest = tool_name[len(prefix):]  # "<agent>.<action>"
                agent_part = rest.split(".", 1)[0]
                if agent_part in AGENTS:
                    info = AGENTS[agent_part]
                    return f"{info['icon']} {info['name']}"
        # 3) Agent 의 정적 도구 매핑
        for agent_key, tools in AGENT_TOOLS.items():
            if tool_name in tools:
                info = AGENTS[agent_key]
                return f"{info['icon']} {info['name']}"
        return "⚙️ 시스템"

    display_df['Agent'] = display_df['tool_name'].apply(get_agent_for_tool)

    def get_task_summary(row):
        tool_name = row['tool_name'] or ''
        params_raw = row.get('parameters', '{}')
        # MCP 도구는 task 인자를 보여줌
        if tool_name in _MCP_TO_AGENT:
            try:
                params = json.loads(params_raw) if isinstance(params_raw, str) else params_raw
                task = params.get('task', '') if isinstance(params, dict) else ''
                return task[:60] + '...' if len(task) > 60 else (task or tool_name)
            except Exception:
                return tool_name
        # 내부 호출은 action 이름 + policy 요약
        for prefix in ("agent_action:", "agent_tool:"):
            if tool_name.startswith(prefix):
                short = tool_name[len(prefix):]  # "<agent>.<action>"
                action_only = short.split(".", 1)[1] if "." in short else short
                try:
                    params = json.loads(params_raw) if isinstance(params_raw, str) else params_raw
                    if isinstance(params, dict):
                        # policy 가 있으면 최상위 keys 만 짧게
                        policy = params.get('policy') or {}
                        if isinstance(policy, dict) and policy:
                            keys = ", ".join(list(policy.keys())[:3])
                            return f"{action_only} ({keys})"
                except Exception:
                    pass
                return action_only
        return tool_name

    display_df['요청내용'] = display_df.apply(get_task_summary, axis=1)

    def get_client_label(ct):
        info = CLIENT_TYPES.get(ct, {"icon": "❓", "name": ct or "N/A"})
        return f"{info['icon']} {info['name']}"

    if 'client_type' in display_df.columns:
        display_df['클라이언트'] = display_df['client_type'].apply(get_client_label)
    else:
        display_df['클라이언트'] = 'N/A'

    display_df['User'] = display_df['user_id'].fillna('N/A')
    display_df['시간'] = pd.to_datetime(display_df['timestamp']).dt.strftime('%m-%d %H:%M:%S')
    display_df['응답시간'] = display_df['duration_ms'].apply(
        lambda x: f"{x:.0f}ms" if pd.notna(x) else "-"
    )

    columns = ['시간', '클라이언트', 'User', 'Agent', '요청내용', '상태', '응답시간', 'error_message']
    st.dataframe(display_df[columns], use_container_width=True, height=400)


def render_tool_stats(by_tool: pd.DataFrame):
    """도구별 통계"""
    if by_tool.empty:
        st.info("데이터가 없습니다.")
        return

    by_tool = by_tool.copy()
    by_tool['success_rate'] = (by_tool['success'] / by_tool['calls'] * 100).round(1)
    by_tool['avg_duration'] = by_tool['avg_duration'].round(0)

    display_df = by_tool.rename(columns={
        'tool_name': '도구',
        'calls': '호출 수',
        'success': '성공',
        'success_rate': '성공률(%)',
        'avg_duration': '평균 응답(ms)'
    })
    st.dataframe(display_df, use_container_width=True)


# ============================================================
# 메인 앱
# ============================================================

def main():
    st.title("Multi-Agent MCP 대시보드")
    st.markdown("Enterprise AI Assistant - Agent별 모니터링 및 로그 분석")

    # ── MCP server health 확인 ──
    health = api_client.health()
    if not _resp_ok(health):
        st.error(
            f"MCP server API ({api_client.base_url()}) 에 연결할 수 없습니다.\n\n"
            f"오류: `{health.get('error', 'unknown')}`"
        )
        st.info(
            "MCP server (port 9100/9101) 가 실행 중인지 확인하세요. "
            "환경변수 `OOSDK_MCP_API_BASE` 로 다른 호스트를 지정할 수 있습니다."
        )
        st.subheader("Agent 구성")
        for agent_key, agent_info in AGENTS.items():
            tools = AGENT_TOOLS.get(agent_key, [])
            st.markdown(
                f"**{agent_info['icon']} {agent_info['name']}** - {agent_info['desc']}  \n"
                f"도구: `{'`, `'.join(tools)}`"
            )
        return

    # ── 사이드바: 필터 ──
    st.sidebar.header("필터")

    # 시간 범위
    time_range = st.sidebar.selectbox(
        "시간 범위",
        ["최근 1시간", "오늘", "최근 7일", "최근 30일", "전체"]
    )

    now = datetime.utcnow()
    if time_range == "최근 1시간":
        start_time = (now - timedelta(hours=1)).isoformat() + "Z"
    elif time_range == "오늘":
        start_time = now.replace(hour=0, minute=0, second=0).isoformat() + "Z"
    elif time_range == "최근 7일":
        start_time = (now - timedelta(days=7)).isoformat() + "Z"
    elif time_range == "최근 30일":
        start_time = (now - timedelta(days=30)).isoformat() + "Z"
    else:
        start_time = None

    end_time = None

    # User ID 필터
    user_ids = fetch_user_ids()
    user_id_options = ["전체"] + user_ids
    user_id_filter = st.sidebar.selectbox("User ID", user_id_options)

    # Agent 필터
    agent_options = ["전체"] + list(AGENTS.keys())
    agent_filter = st.sidebar.selectbox(
        "Agent",
        agent_options,
        format_func=lambda x: "전체" if x == "전체" else f"{AGENTS[x]['icon']} {AGENTS[x]['name']}"
    )

    # 클라이언트(client_type) 필터 — DB 에 실제 등장한 값만
    client_stats_for_dropdown = fetch_client_type_stats(start_time=None, end_time=None, user_id=None)
    if not client_stats_for_dropdown.empty:
        existing_clients = sorted(client_stats_for_dropdown['client_type'].dropna().unique().tolist())
    else:
        existing_clients = []

    client_type_filter = st.sidebar.selectbox(
        "클라이언트",
        ["전체"] + existing_clients,
        format_func=lambda x: "전체" if x == "전체" else (
            f"{CLIENT_TYPES[x]['icon']} {CLIENT_TYPES[x]['name']}"
            if x in CLIENT_TYPES
            else f"❓ {x}"
        )
    )

    # 실행 위치(source) 필터
    source_filter = st.sidebar.selectbox(
        "실행 위치",
        ["전체", "remote", "local"],
        format_func=lambda x: "전체" if x == "전체" else f"{SOURCE_TYPES.get(x, {}).get('icon', '❓')} {SOURCE_TYPES.get(x, {}).get('name', x)}"
    )

    # 상태 필터
    success_filter = st.sidebar.selectbox("상태", ["전체", "성공", "실패"])

    # 도구 필터
    tool_name = st.sidebar.text_input("도구 이름 (부분 일치)")

    # 키워드 검색
    keyword = st.sidebar.text_input("키워드 검색")

    # 결과 수
    limit = st.sidebar.slider("표시 개수", 10, 500, 100)

    # ── 메인: 대시보드 ──
    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        ["개요", "Agent 상태", "로그 상세", "Ontology View", "SO 재고"]
    )

    with tab1:
        overall, by_tool, ok, err = fetch_overview(
            start_time, end_time, user_id_filter, source_filter, client_type_filter
        )
        if not ok:
            st.error(f"통계 로드 실패: {err}")

        st.subheader("요약")
        render_summary_cards(overall)

        # 클라이언트별 트래픽 (client_type 필터가 "전체"일 때만 표시)
        if client_type_filter == "전체":
            st.divider()
            st.subheader("클라이언트별 트래픽")
            client_stats = fetch_client_type_stats(start_time, end_time, user_id_filter)
            render_client_type_cards(client_stats)

        st.divider()

        col1, col2 = st.columns([2, 1])

        with col1:
            st.subheader("시간대별 호출")
            hourly_data = fetch_hourly_calls(
                start_time, end_time, user_id_filter, source_filter, client_type_filter
            )
            render_chart(hourly_data)

        with col2:
            st.subheader("도구별 통계")
            render_tool_stats(by_tool)

    with tab2:
        st.subheader("Agent별 상태")
        agent_stats = fetch_agent_stats(
            start_time, end_time, user_id_filter, source_filter, client_type_filter
        )
        render_agent_status(agent_stats)

        st.divider()

        # Agent별 도구 호출 현황 — 이미 fetch 한 by_tool 을 client-side filter
        st.subheader("Agent별 도구 호출 현황")
        for agent_key, agent_info in AGENTS.items():
            stats = agent_stats.get(agent_key) or {"calls": 0}
            calls = int(stats.get("calls") or 0)
            if calls > 0:
                with st.expander(f"{agent_info['icon']} {agent_info['name']} - {calls}건"):
                    tools = AGENT_TOOLS.get(agent_key, [])
                    if by_tool.empty:
                        st.caption("도구별 통계가 비어있습니다.")
                        continue
                    # 정적 tool 매핑 + agent_action:/agent_tool: prefix 매칭
                    action_prefix = f"agent_action:{agent_key}."
                    tool_prefix = f"agent_tool:{agent_key}."
                    mask = (
                        by_tool['tool_name'].isin(tools)
                        | by_tool['tool_name'].str.startswith(action_prefix, na=False)
                        | by_tool['tool_name'].str.startswith(tool_prefix, na=False)
                    )
                    sub = by_tool[mask].copy()
                    if sub.empty:
                        st.caption("이 Agent 의 도구 호출 기록이 없습니다.")
                        continue
                    sub['avg_duration'] = sub['avg_duration'].round(0)
                    sub = sub.rename(columns={
                        'tool_name': '도구', 'calls': '호출',
                        'success': '성공', 'avg_duration': '평균(ms)'
                    })
                    st.dataframe(sub, use_container_width=True)

    with tab3:
        st.subheader("로그 목록")

        logs = fetch_logs(
            start_time=start_time,
            end_time=end_time,
            tool_name=tool_name if tool_name else None,
            agent=agent_filter if agent_filter != "전체" else None,
            user_id=user_id_filter if user_id_filter != "전체" else None,
            success=success_filter if success_filter != "전체" else None,
            source=source_filter if source_filter != "전체" else None,
            client_type=client_type_filter if client_type_filter != "전체" else None,
            keyword=keyword if keyword else None,
            limit=limit,
        )

        render_log_table(logs)

        if not logs.empty:
            st.subheader("상세 보기")
            selected_id = st.selectbox(
                "로그 선택",
                logs['id'].tolist(),
                format_func=lambda x: f"#{x} - {logs[logs['id']==x]['tool_name'].values[0]} ({str(logs[logs['id']==x]['timestamp'].values[0])[:19]})"
            )

            if selected_id:
                selected_log = logs[logs['id'] == selected_id].iloc[0]

                col1, col2 = st.columns(2)

                with col1:
                    st.json({
                        "id": int(selected_log['id']),
                        "timestamp": selected_log['timestamp'],
                        "user_id": selected_log.get('user_id', 'N/A'),
                        "tool_name": selected_log['tool_name'],
                        "success": bool(selected_log['success']),
                        "duration_ms": selected_log['duration_ms']
                    })

                with col2:
                    st.write("**파라미터:**")
                    try:
                        params = json.loads(selected_log['parameters']) if selected_log['parameters'] else {}
                        st.json(params)
                    except Exception:
                        st.code(selected_log['parameters'])

                    if selected_log.get('error_message'):
                        st.error(f"**에러:** {selected_log['error_message']}")

                    if selected_log.get('result_summary'):
                        st.info(f"**결과:** {selected_log['result_summary']}")

                # ─── OOSDK Ontology Trace (process_with_ontology 전용) ───
                if selected_log['tool_name'] == 'process_with_ontology':
                    st.divider()
                    st.markdown("### 🧭 OOSDK Ontology Decision Trace")
                    summary = selected_log.get('result_summary') or ''
                    trace = None
                    try:
                        if summary.strip().startswith('{'):
                            parsed = json.loads(summary)
                            trace = parsed.get('ontology_trace') or parsed
                    except Exception:
                        pass

                    if trace:
                        c1, c2, c3 = st.columns(3)
                        with c1:
                            st.metric("매칭 Rule", trace.get('matched_rule') or '—')
                        with c2:
                            cust = trace.get('customer') or {}
                            st.metric("Tier", cust.get('tier') or 'Unknown')
                        with c3:
                            st.metric("Plan steps", len(trace.get('event_plan') or []))

                        with st.expander("🧬 resolve_links 결과 (person + customer)", expanded=False):
                            st.json({
                                "person": trace.get('person'),
                                "customer": trace.get('customer'),
                                "lookup_by": trace.get('lookup_by'),
                                "lookup_value": trace.get('lookup_value'),
                                "customer_source": trace.get('customer_source'),
                            })

                        with st.expander("📐 check_rules → action", expanded=False):
                            st.json(trace.get('action'))

                        with st.expander("🚀 trigger_events → event_plan", expanded=True):
                            for i, p in enumerate(trace.get('event_plan') or [], 1):
                                st.markdown(f"**{i}.** `[{p.get('agent')}]` → `{p.get('tool')}` ({p.get('event_name')})")
                                if p.get('params'):
                                    st.json(p['params'])

                        with st.expander("🧠 manage_memory", expanded=False):
                            st.json(trace.get('memory'))
                    else:
                        st.caption("ontology_trace 를 파싱할 수 없습니다 (result_summary 가 비었거나 형식 다름).")

    # ═══════════════════════════════════════════════════════════
    # Tab 4: Ontology View (OOSDK Phase 1)
    # ═══════════════════════════════════════════════════════════
    with tab4:
        try:
            from dashboard_modules.ontology_view import render_ontology_view
            render_ontology_view(lang="ko")
        except Exception as e:
            st.error(f"Ontology View 로드 실패: {e}")
            import traceback
            st.code(traceback.format_exc())

    # ═══════════════════════════════════════════════════════════
    # Tab 5: SO Inventory View (BC3 §2.1 4-state)
    # ═══════════════════════════════════════════════════════════
    with tab5:
        try:
            from dashboard_modules.so_inventory_view import render_so_inventory_view
            render_so_inventory_view(lang="ko")
        except Exception as e:
            st.error(f"SO 재고 View 로드 실패: {e}")
            import traceback
            st.code(traceback.format_exc())

    # Footer
    st.divider()
    st.caption(
        f"마지막 업데이트: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
        f"Multi-Agent MCP OOSDK (:9100) + ADK (:7001) + Log API (:9101)"
    )

    # 자동 새로고침
    if st.sidebar.checkbox("자동 새로고침 (30초)", value=False):
        import time
        time.sleep(30)
        st.rerun()


if __name__ == "__main__":
    main()
