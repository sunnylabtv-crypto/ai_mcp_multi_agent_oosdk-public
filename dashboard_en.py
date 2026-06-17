# dashboard_en.py
"""
Multi-Agent MCP Dashboard (English version) — OOSDK.

Run:
    streamlit run dashboard_en.py --server.port 9602

Architecture
------------
This dashboard is a **stateless HTTP viewer**. All data is fetched from the
MCP server (port 9101) `/api/dashboard/*` endpoints. It never touches the
SQLite file or in-process OntologyEngine instance directly.

Reason: the dashboard process and MCP server process are separate, so they
cannot share in-memory state. Patching this with a shared filesystem mount
breaks the moment we add a second container, horizontal scaling, or a
managed DB. Single source of truth = MCP server.

Override the API base via env: OOSDK_MCP_API_BASE (default http://localhost:9101/api)
"""
import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
import json

from dashboard_modules import api_client

# ============================================================
# Configuration
# ============================================================

AGENTS = {
    "email_agent": {"name": "Email Agent", "icon": "📧", "desc": "Gmail management"},
    "crm_agent": {"name": "CRM Agent", "icon": "💼", "desc": "Salesforce CRM"},
    "calendar_agent": {"name": "Calendar Agent", "icon": "📅", "desc": "Google Calendar"},
    "cs_agent": {"name": "CS Agent", "icon": "🎧", "desc": "Customer service (product docs)"},
    "helpdesk_agent": {"name": "Helpdesk Agent", "icon": "🏢", "desc": "Internal helpdesk (company docs)"},
    "report_agent": {"name": "Report Agent", "icon": "📊", "desc": "Log analytics & reporting"},
    # ─── BC2 new agents ───
    "erp_agent": {"name": "ERP Agent", "icon": "📦", "desc": "Odoo Sales Order / Inventory (BC2 Win branch)"},
    "analytics_agent": {"name": "Analytics Agent", "icon": "📈", "desc": "Lost reason analysis / patterns (BC2 Lost branch)"},
    "inventory_agent": {"name": "Inventory Agent", "icon": "🚚", "desc": "Odoo stock VIP preemption / re-allocation / autonomous replenishment (BC3~BC5)"},
}

AGENT_TOOLS = {
    "email_agent": ["run_email_agent", "fetch_unread_emails", "send_email_reply", "get_gmail_status", "analyze_email_with_ai", "generate_email_reply"],
    "crm_agent": ["run_crm_agent", "create_salesforce_lead", "verify_salesforce_lead", "get_salesforce_status",
                  "search_lead_by_email", "search_account_by_name", "query_soql",
                  "create_opportunity", "verify_opportunity"],
    "calendar_agent": ["run_calendar_agent", "add_calendar_event", "get_calendar_events", "update_calendar_event", "delete_calendar_event", "search_calendar_events", "get_calendar_status"],
    "cs_agent": ["run_cs_agent", "upload_product_document", "search_product_documents", "answer_customer_inquiry", "list_product_documents"],
    "helpdesk_agent": ["run_helpdesk_agent", "upload_internal_document", "search_internal_documents", "ask_helpdesk", "list_internal_documents", "delete_internal_document"],
    "report_agent": ["run_report_agent", "query_logs", "get_stats", "get_errors", "get_slow_tools"],
    # ─── BC2 new agent tools ───
    "erp_agent": ["get_odoo_status", "find_existing_sales_order"],
    "analytics_agent": ["categorize_lost_reason", "get_lost_reason_summary"],
    # ─── BC3~BC5 inventory agent tools ───
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
# Two-axis classification
# ============================================================
# source      = where the tool ran     (remote: GCP server / local: PC)
# client_type = entry point             (claude_desktop / cursor / adk / mcp)

CLIENT_TYPES = {
    "claude_desktop": {"name": "Claude Desktop", "icon": "🟣", "color": "#7C3AED"},
    "cursor":         {"name": "Cursor IDE", "icon": "📝", "color": "#10B981"},
    "adk":            {"name": "Web/Mobile (ADK)", "icon": "🌐", "color": "#E74C3C"},
    "mcp":            {"name": "MCP (Default)", "icon": "🔌", "color": "#4A90D9"},
    "local":          {"name": "Local Agent", "icon": "💻", "color": "#2ECC71"},
}

SOURCE_TYPES = {
    "remote": {"name": "Remote (Server)", "icon": "☁️"},
    "local":  {"name": "Local (PC)", "icon": "💻"},
}

st.set_page_config(
    page_title="Multi-Agent Dashboard",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded"
)


# ============================================================
# API helpers — all fetch from MCP server `/api/dashboard/*`
# ============================================================

def _resp_ok(resp: dict) -> bool:
    return bool(resp) and resp.get("ok", False)


def fetch_user_ids() -> list[str]:
    resp = api_client.get_user_ids()
    if not _resp_ok(resp):
        return []
    return resp.get("user_ids") or []


def fetch_overview(start_time, end_time, user_id, source, client_type):
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
    agent_tools = None
    if agent and agent != "All":
        agent_tools = AGENT_TOOLS.get(agent) or None

    # Map English status labels to api_client's accepted shape
    if success == "Success":
        success_param = True
    elif success == "Failed":
        success_param = False
    else:
        success_param = None

    resp = api_client.query_logs(
        start_time=start_time, end_time=end_time,
        tool_name=tool_name,
        agent_tools=agent_tools,
        user_id=user_id, source=source, client_type=client_type,
        success=success_param, keyword=keyword,
        limit=limit,
    )
    if not _resp_ok(resp):
        return pd.DataFrame()
    return pd.DataFrame(resp.get("logs") or [])


# ============================================================
# UI Components
# ============================================================

def render_summary_cards(overall: dict):
    col1, col2, col3, col4 = st.columns(4)
    total = int(overall.get('total_calls') or 0)
    success_count = int(overall.get('success_count') or 0)
    error_count = int(overall.get('error_count') or 0)
    avg_duration = overall.get('avg_duration_ms') or 0

    with col1:
        st.metric(label="Total Calls", value=f"{total:,}")
    with col2:
        success_rate = (success_count / total * 100) if total > 0 else 0
        st.metric(label="Success Rate", value=f"{success_rate:.1f}%")
    with col3:
        st.metric(label="Avg Response", value=f"{avg_duration:.0f}ms")
    with col4:
        st.metric(label="Errors", value=f"{error_count:,}")


def render_client_type_cards(client_stats: pd.DataFrame):
    if client_stats.empty:
        st.info("No client-type data available.")
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
                <div style="font-size: 11px; color: gray;">Calls | Errors: {errors} | Avg: {avg_dur:.0f}ms</div>
            </div>
            """, unsafe_allow_html=True)


def render_agent_status(agent_stats: dict):
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
                <div style="font-size: 12px;">Calls: <b>{calls}</b> | Errors: <b>{errors}</b></div>
                <div style="font-size: 11px; color: gray;">Avg: {avg_dur:.0f}ms</div>
            </div>
            """, unsafe_allow_html=True)


def render_chart(hourly_data: pd.DataFrame):
    if hourly_data.empty:
        st.info("No data available.")
        return

    chart_data = hourly_data.set_index('hour')[['success', 'errors']]
    chart_data.columns = ['Success', 'Errors']
    st.bar_chart(chart_data)


def render_log_table(logs: pd.DataFrame):
    if logs.empty:
        st.info("No results found.")
        return

    display_df = logs.copy()
    display_df['Status'] = display_df['success'].apply(lambda x: '✅' if x else '❌')

    _MCP_TO_AGENT = {f"run_{agent_key}": agent_key for agent_key in AGENTS}

    def get_agent_for_tool(tool_name):
        if not tool_name:
            return "⚙️ System"
        if tool_name in _MCP_TO_AGENT:
            info = AGENTS[_MCP_TO_AGENT[tool_name]]
            return f"{info['icon']} {info['name']}"
        # internal calls logged by BaseAgent.execute_action / execute_tool
        for prefix in ("agent_action:", "agent_tool:"):
            if tool_name.startswith(prefix):
                rest = tool_name[len(prefix):]
                agent_part = rest.split(".", 1)[0]
                if agent_part in AGENTS:
                    info = AGENTS[agent_part]
                    return f"{info['icon']} {info['name']}"
        for agent_key, tools in AGENT_TOOLS.items():
            if tool_name in tools:
                info = AGENTS[agent_key]
                return f"{info['icon']} {info['name']}"
        return "⚙️ System"

    display_df['Agent'] = display_df['tool_name'].apply(get_agent_for_tool)

    def get_task_summary(row):
        tool_name = row['tool_name'] or ''
        params_raw = row.get('parameters', '{}')
        if tool_name in _MCP_TO_AGENT:
            try:
                params = json.loads(params_raw) if isinstance(params_raw, str) else params_raw
                task = params.get('task', '') if isinstance(params, dict) else ''
                return task[:60] + '...' if len(task) > 60 else (task or tool_name)
            except Exception:
                return tool_name
        for prefix in ("agent_action:", "agent_tool:"):
            if tool_name.startswith(prefix):
                short = tool_name[len(prefix):]
                action_only = short.split(".", 1)[1] if "." in short else short
                try:
                    params = json.loads(params_raw) if isinstance(params_raw, str) else params_raw
                    if isinstance(params, dict):
                        policy = params.get('policy') or {}
                        if isinstance(policy, dict) and policy:
                            keys = ", ".join(list(policy.keys())[:3])
                            return f"{action_only} ({keys})"
                except Exception:
                    pass
                return action_only
        return tool_name

    display_df['Request'] = display_df.apply(get_task_summary, axis=1)

    def get_client_label(ct):
        info = CLIENT_TYPES.get(ct, {"icon": "❓", "name": ct or "N/A"})
        return f"{info['icon']} {info['name']}"

    if 'client_type' in display_df.columns:
        display_df['Client Type'] = display_df['client_type'].apply(get_client_label)
    else:
        display_df['Client Type'] = 'N/A'

    display_df['User'] = display_df['user_id'].fillna('N/A')
    display_df['Time'] = pd.to_datetime(display_df['timestamp']).dt.strftime('%m-%d %H:%M:%S')
    display_df['Latency'] = display_df['duration_ms'].apply(
        lambda x: f"{x:.0f}ms" if pd.notna(x) else "-"
    )

    columns = ['Time', 'Client Type', 'User', 'Agent', 'Request', 'Status', 'Latency', 'error_message']
    st.dataframe(display_df[columns], use_container_width=True, height=400)


def render_tool_stats(by_tool: pd.DataFrame):
    if by_tool.empty:
        st.info("No data available.")
        return

    by_tool = by_tool.copy()
    by_tool['success_rate'] = (by_tool['success'] / by_tool['calls'] * 100).round(1)
    by_tool['avg_duration'] = by_tool['avg_duration'].round(0)

    display_df = by_tool.rename(columns={
        'tool_name': 'Tool',
        'calls': 'Calls',
        'success': 'Success',
        'success_rate': 'Rate (%)',
        'avg_duration': 'Avg (ms)'
    })
    st.dataframe(display_df, use_container_width=True)


# ============================================================
# Main App
# ============================================================

def main():
    st.title("Multi-Agent MCP Dashboard")
    st.markdown("Enterprise AI Assistant — Agent Monitoring & Log Analytics")

    # ── Health check ──
    health = api_client.health()
    if not _resp_ok(health):
        st.error(
            f"Cannot reach MCP server API at `{api_client.base_url()}`.\n\n"
            f"Error: `{health.get('error', 'unknown')}`"
        )
        st.info(
            "Verify that the MCP server (port 9100/9101) is running. "
            "Override the API base via `OOSDK_MCP_API_BASE`."
        )
        st.subheader("Agent Configuration")
        for agent_key, agent_info in AGENTS.items():
            tools = AGENT_TOOLS.get(agent_key, [])
            st.markdown(
                f"**{agent_info['icon']} {agent_info['name']}** — {agent_info['desc']}  \n"
                f"Tools: `{'`, `'.join(tools)}`"
            )
        return

    # ── Sidebar: Filters ──
    st.sidebar.header("Filters")

    time_range = st.sidebar.selectbox(
        "Time Range",
        ["Last 1 Hour", "Today", "Last 7 Days", "Last 30 Days", "All Time"]
    )

    now = datetime.utcnow()
    if time_range == "Last 1 Hour":
        start_time = (now - timedelta(hours=1)).isoformat() + "Z"
    elif time_range == "Today":
        start_time = now.replace(hour=0, minute=0, second=0).isoformat() + "Z"
    elif time_range == "Last 7 Days":
        start_time = (now - timedelta(days=7)).isoformat() + "Z"
    elif time_range == "Last 30 Days":
        start_time = (now - timedelta(days=30)).isoformat() + "Z"
    else:
        start_time = None

    end_time = None

    user_ids = fetch_user_ids()
    user_id_filter = st.sidebar.selectbox("User ID", ["All"] + user_ids)

    agent_options = ["All"] + list(AGENTS.keys())
    agent_filter = st.sidebar.selectbox(
        "Agent",
        agent_options,
        format_func=lambda x: "All" if x == "All" else f"{AGENTS[x]['icon']} {AGENTS[x]['name']}"
    )

    # Client Type filter — populate from existing values in DB
    client_stats_for_dropdown = fetch_client_type_stats(start_time=None, end_time=None, user_id=None)
    if not client_stats_for_dropdown.empty:
        existing_clients = sorted(client_stats_for_dropdown['client_type'].dropna().unique().tolist())
    else:
        existing_clients = []

    client_type_filter = st.sidebar.selectbox(
        "Client Type",
        ["All"] + existing_clients,
        format_func=lambda x: "All" if x == "All" else (
            f"{CLIENT_TYPES[x]['icon']} {CLIENT_TYPES[x]['name']}"
            if x in CLIENT_TYPES
            else f"❓ {x}"
        )
    )

    source_filter = st.sidebar.selectbox(
        "Execution Source",
        ["All", "remote", "local"],
        format_func=lambda x: "All" if x == "All" else f"{SOURCE_TYPES.get(x, {}).get('icon', '❓')} {SOURCE_TYPES.get(x, {}).get('name', x)}"
    )

    success_filter = st.sidebar.selectbox("Status", ["All", "Success", "Failed"])
    tool_name = st.sidebar.text_input("Tool Name (partial match)")
    keyword = st.sidebar.text_input("Keyword Search")
    limit = st.sidebar.slider("Display Limit", 10, 500, 100)

    # ── Main: Dashboard ──
    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        ["Overview", "Agent Status", "Log Details", "Ontology View", "SO Inventory"]
    )

    with tab1:
        overall, by_tool, ok, err = fetch_overview(
            start_time, end_time, user_id_filter, source_filter, client_type_filter
        )
        if not ok:
            st.error(f"Failed to load stats: {err}")

        st.subheader("Summary")
        render_summary_cards(overall)

        if client_type_filter == "All":
            st.divider()
            st.subheader("Traffic by Client Type")
            client_stats = fetch_client_type_stats(start_time, end_time, user_id_filter)
            render_client_type_cards(client_stats)

        st.divider()

        col1, col2 = st.columns([2, 1])

        with col1:
            st.subheader("Hourly Calls")
            hourly_data = fetch_hourly_calls(
                start_time, end_time, user_id_filter, source_filter, client_type_filter
            )
            render_chart(hourly_data)

        with col2:
            st.subheader("Tool Statistics")
            render_tool_stats(by_tool)

    with tab2:
        st.subheader("Agent Status")
        agent_stats = fetch_agent_stats(
            start_time, end_time, user_id_filter, source_filter, client_type_filter
        )
        render_agent_status(agent_stats)

        st.divider()

        st.subheader("Agent Tool Call Breakdown")
        for agent_key, agent_info in AGENTS.items():
            stats = agent_stats.get(agent_key) or {"calls": 0}
            calls = int(stats.get("calls") or 0)
            if calls > 0:
                with st.expander(f"{agent_info['icon']} {agent_info['name']} — {calls} calls"):
                    tools = AGENT_TOOLS.get(agent_key, [])
                    if by_tool.empty:
                        st.caption("Tool stats unavailable.")
                        continue
                    action_prefix = f"agent_action:{agent_key}."
                    tool_prefix = f"agent_tool:{agent_key}."
                    mask = (
                        by_tool['tool_name'].isin(tools)
                        | by_tool['tool_name'].str.startswith(action_prefix, na=False)
                        | by_tool['tool_name'].str.startswith(tool_prefix, na=False)
                    )
                    sub = by_tool[mask].copy()
                    if sub.empty:
                        st.caption("No tool calls recorded for this agent.")
                        continue
                    sub['avg_duration'] = sub['avg_duration'].round(0)
                    sub = sub.rename(columns={
                        'tool_name': 'Tool', 'calls': 'Calls',
                        'success': 'Success', 'avg_duration': 'Avg (ms)'
                    })
                    st.dataframe(sub, use_container_width=True)

    with tab3:
        st.subheader("Log List")

        logs = fetch_logs(
            start_time=start_time,
            end_time=end_time,
            tool_name=tool_name if tool_name else None,
            agent=agent_filter if agent_filter != "All" else None,
            user_id=user_id_filter if user_id_filter != "All" else None,
            success=success_filter if success_filter != "All" else None,
            source=source_filter if source_filter != "All" else None,
            client_type=client_type_filter if client_type_filter != "All" else None,
            keyword=keyword if keyword else None,
            limit=limit,
        )

        render_log_table(logs)

        if not logs.empty:
            st.subheader("Detail View")
            selected_id = st.selectbox(
                "Select Log",
                logs['id'].tolist(),
                format_func=lambda x: f"#{x} — {logs[logs['id']==x]['tool_name'].values[0]} ({str(logs[logs['id']==x]['timestamp'].values[0])[:19]})"
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
                    st.write("**Parameters:**")
                    try:
                        params = json.loads(selected_log['parameters']) if selected_log['parameters'] else {}
                        st.json(params)
                    except Exception:
                        st.code(selected_log['parameters'])

                    if selected_log.get('error_message'):
                        st.error(f"**Error:** {selected_log['error_message']}")

                    if selected_log.get('result_summary'):
                        st.info(f"**Result:** {selected_log['result_summary']}")

                # ─── OOSDK Ontology Trace (process_with_ontology only) ───
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
                            st.metric("Matched Rule", trace.get('matched_rule') or '—')
                        with c2:
                            cust = trace.get('customer') or {}
                            st.metric("Tier", cust.get('tier') or 'Unknown')
                        with c3:
                            st.metric("Plan steps", len(trace.get('event_plan') or []))

                        with st.expander("🧬 resolve_links (person + customer)", expanded=False):
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
                        st.caption("Could not parse ontology_trace (result_summary missing or different shape).")

    # ═══════════════════════════════════════════════════════════
    # Tab 4: Ontology View (OOSDK Phase 1)
    # ═══════════════════════════════════════════════════════════
    with tab4:
        try:
            from dashboard_modules.ontology_view import render_ontology_view
            render_ontology_view(lang="en")
        except Exception as e:
            st.error(f"Failed to load Ontology View: {e}")
            import traceback
            st.code(traceback.format_exc())

    # ═══════════════════════════════════════════════════════════
    # Tab 5: SO Inventory (BC3 §2.1 4-state viewer)
    # ═══════════════════════════════════════════════════════════
    # Note: render_so_inventory_view currently accepts `lang` but uses Korean
    # labels regardless. Passed for forward compatibility; English label pass is
    # follow-up work (BC4 cosmetic).
    with tab5:
        try:
            from dashboard_modules.so_inventory_view import render_so_inventory_view
            render_so_inventory_view(lang="en")
        except Exception as e:
            st.error(f"Failed to load SO Inventory View: {e}")
            import traceback
            st.code(traceback.format_exc())

    # Footer
    st.divider()
    st.caption(
        f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
        f"Multi-Agent MCP OOSDK (:9100) + ADK (:7001) + Log API (:9101)"
    )

    if st.sidebar.checkbox("Auto-refresh (30s)", value=False):
        import time
        time.sleep(30)
        st.rerun()


if __name__ == "__main__":
    main()
