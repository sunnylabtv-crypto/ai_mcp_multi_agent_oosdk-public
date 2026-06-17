# dashboard_modules/ontology_view.py
"""
OOSDK Ontology View — Streamlit 탭 모듈.

Architecture (v3 — viewer-only, no simulation)
----------------------------------------------
Dashboard 는 stateless viewer. 모든 데이터는 MCP server (port 9101) 의
HTTP API 를 통해 fetch. 더 이상 dashboard 프로세스 안에 OntologyEngine
인스턴스를 만들지 않으며, 시뮬레이션 (dry-run) UI 도 제거.

Why
---
v1: Dashboard 가 자체 OntologyEngine 을 만들고 engine.memory.list_keys() 직접 호출.
    → Dashboard 프로세스 vs MCP server 프로세스 의 메모리가 분리됨.
v2: HTTP fetch 로 단일 진실 공급원화. 다만 시뮬레이션 패널 (read-only dry-run) 이
    실제 dispatch 와 혼동을 일으킴 — 사용자가 "왜 시뮬레이션은 되는데 메모리에 안 쌓이지?"
    로 오해.
v3 (현재): 시뮬레이션/Trace 컬럼 제거. 실제 dispatch 는 Claude Desktop 의
    `process_with_ontology` MCP tool 로만. 이 화면은 그 결과를 보여주는 역할.

3개 패널:
    1. Recent Decisions      — MCP server fetch (warm + hot 합산, 최근 10건)
    2. 3-Tier Memory Status  — MCP server fetch
    3. Active ontology.yaml  — MCP server fetch (dashboard 가 보고 있는 yaml = server 가 로드한 yaml)
"""
from typing import Dict

import streamlit as st

from dashboard_modules import api_client


# ---------------------------------------------------------------
# LANG 리소스 (KO / EN)
# ---------------------------------------------------------------
L = {
    "ko": {
        "header_help": (
            "이 화면은 MCP server 가 기록한 OOSDK 의사결정과 3-Tier Memory 를 보여주는 viewer 입니다. "
            "실제 ontology dispatch 는 Claude Desktop 의 `process_with_ontology` MCP 도구로 실행하세요."
        ),
        "section_recent": "📜 Recent Decisions (최근 의사결정 20건)",
        "recent_empty": (
            "아직 OOSDK 의사결정 기록이 없습니다. "
            "Claude Desktop 에서 `process_with_ontology` 를 호출해보세요 — "
            "결과가 이 화면에 자동으로 누적됩니다."
        ),
        "recent_col_time": "시각",
        "recent_col_customer": "고객",
        "recent_col_event": "이벤트",
        "recent_col_email": "이메일",
        "recent_col_tier": "Tier",
        "recent_col_rule": "매칭 룰",
        "recent_col_plan": "Plan",
        "pagination_caption": "전체 {total}건 · {pages} 페이지 (현재 {cur} 페이지, {start}~{end}건 표시)",
        "select_for_detail": "🔍 상세 보기 — 결정 선택",
        "detail_placeholder": "표에서 행을 선택하세요...",
        "detail_section_meta": "📋 메타",
        "detail_section_rule": "📐 매칭 룰",
        "detail_section_plan": "🤖 Plan (agent dispatch)",
        "detail_label_account": "Account",
        "detail_label_account_id": "Account Id",
        "detail_label_opp": "Opportunity",
        "detail_label_opp_id": "Opp Id",
        "detail_label_event": "Event",
        "detail_label_stage": "Stage",
        "detail_label_tier": "Tier",
        "detail_label_memory_key": "Memory Key",
        "section_memory": "🧠 3-Tier Memory",
        "section_yaml": "📄 Active ontology.yaml",
        "memory_hot": "🔥 Hot (24h)",
        "memory_warm": "🌤 Warm (30d)",
        "memory_cold": "❄️ Cold (∞)",
        "memory_size": "size",
        "memory_backend": "backend",
        "yaml_note": "이 yaml 한 줄만 수정하면 rule / 소스 / 메모리 정책이 바뀝니다.",
        "api_error": "MCP server 에 연결할 수 없습니다. 잠시 후 다시 시도하세요.",
        "inventory_labels": {
            "sale_order_confirmed":               "🧾 주문확정 → 분기",
            "delivery_ready_check":               "🚚 출고 점검 (VIP 선점/출하)",
            "stock_received":                     "📦 입고 → VIP 우선 재배정",
            "inventory_allocation_window_cutoff": "⏱ Cut-off 배치 배정",
            "inventory_shortage_detected":        "🟠 충족불가 → 자율 보충발주",
        },
    },
    "en": {
        "header_help": (
            "This view renders OOSDK decisions and 3-Tier Memory recorded by the MCP server. "
            "Run `process_with_ontology` from Claude Desktop to dispatch — results show up here automatically."
        ),
        "section_recent": "📜 Recent Decisions (last 20)",
        "recent_empty": (
            "No OOSDK decisions yet. Call `process_with_ontology` from Claude Desktop — "
            "results will populate this view automatically."
        ),
        "recent_col_time": "Time",
        "recent_col_customer": "Customer",
        "recent_col_event": "Event",
        "recent_col_email": "Email",
        "recent_col_tier": "Tier",
        "recent_col_rule": "Matched Rule",
        "recent_col_plan": "Plan",
        "pagination_caption": "{total} total · {pages} pages (page {cur}, showing {start}-{end})",
        "select_for_detail": "🔍 Detail — pick a decision",
        "detail_placeholder": "Select a row from the table...",
        "detail_section_meta": "📋 Meta",
        "detail_section_rule": "📐 Matched Rule",
        "detail_section_plan": "🤖 Plan (agent dispatch)",
        "detail_label_account": "Account",
        "detail_label_account_id": "Account Id",
        "detail_label_opp": "Opportunity",
        "detail_label_opp_id": "Opp Id",
        "detail_label_event": "Event",
        "detail_label_stage": "Stage",
        "detail_label_tier": "Tier",
        "detail_label_memory_key": "Memory Key",
        "section_memory": "🧠 3-Tier Memory",
        "section_yaml": "📄 Active ontology.yaml",
        "memory_hot": "🔥 Hot (24h)",
        "memory_warm": "🌤 Warm (30d)",
        "memory_cold": "❄️ Cold (∞)",
        "memory_size": "size",
        "memory_backend": "backend",
        "yaml_note": "Modify a single yaml line to change rules / sources / memory policy.",
        "api_error": "Cannot reach MCP server. Try again shortly.",
        "inventory_labels": {
            "sale_order_confirmed":               "🧾 Order Confirmed → Split",
            "delivery_ready_check":               "🚚 Delivery Check (VIP preempt/ship)",
            "stock_received":                     "📦 Stock Received → VIP-first Re-allocate",
            "inventory_allocation_window_cutoff": "⏱ Cut-off Batch Allocation",
            "inventory_shortage_detected":        "🟠 Unfulfillable → Autonomous Replenish",
        },
    },
}


# ---------------------------------------------------------------
# 메인 렌더 함수 — dashboard.py 가 호출
# ---------------------------------------------------------------
def render_ontology_view(lang: str = "ko"):
    lx = L.get(lang, L["ko"])

    st.caption(lx["header_help"])

    # ═══════════════════════════════════════════════════════════
    # Recent Decisions — MCP server fetch
    # ═══════════════════════════════════════════════════════════
    st.markdown(f"### {lx['section_recent']}")
    _render_recent_decisions(lx)

    st.divider()

    # ═══════════════════════════════════════════════════════════
    # 3-Tier Memory (full width)
    # ═══════════════════════════════════════════════════════════
    st.markdown(f"### {lx['section_memory']}")
    _render_memory_stats(lx)

    st.divider()

    # ═══════════════════════════════════════════════════════════
    # Active ontology.yaml — Memory 아래로 이동 (full width)
    # ═══════════════════════════════════════════════════════════
    st.markdown(f"### {lx['section_yaml']}")
    st.caption(lx["yaml_note"])
    _render_active_yaml(lx)


# ---------------------------------------------------------------
# Recent Decisions 패널 — HTTP fetch
# ---------------------------------------------------------------
def _render_recent_decisions(lx: Dict[str, str]):
    from datetime import datetime as _dt

    PAGE_SIZE = 20
    if "decisions_page" not in st.session_state:
        st.session_state.decisions_page = 0
    current_page = st.session_state.decisions_page
    offset = current_page * PAGE_SIZE

    resp = api_client.get_ontology_decisions(limit=PAGE_SIZE, offset=offset)
    if not resp.get("ok"):
        st.error(lx["api_error"])
        with st.expander("error detail"):
            st.json(resp)
        return

    decisions = resp.get("decisions", [])
    total = resp.get("total", len(decisions))
    if not decisions:
        if current_page > 0:
            st.session_state.decisions_page = 0
            st.rerun()
        st.info(lx["recent_empty"])
        return

    # 이벤트 라벨 — entity / event / stage 조합으로 사람 친화적 라벨
    EVENT_LABELS = {
        ("email", None, None):                              "📧 이메일 인입",
        ("sales_opportunity_inquiry", "inquiry", None):     "💰 견적 문의",
        ("sales_opportunity_close", "close", "Closed Won"): "🏆 Closed Won",
        ("sales_opportunity_close", "close", "Closed Lost"):"❌ Closed Lost",
    }
    # BC3~BC5 재고 이벤트 라벨 — lang 별 (lx 에서 가져옴 → EN 대시보드에선 영문)
    inv_labels = lx.get("inventory_labels", {})

    def _event_label(entity, event, stage):
        # 정확 매칭 우선
        key = (entity, event, stage)
        if key in EVENT_LABELS:
            return EVENT_LABELS[key]
        # 재고 이벤트 (BC3~BC5)
        if entity in inv_labels:
            return inv_labels[entity]
        # entity 만으로 fallback
        if entity == "email":
            return EVENT_LABELS[("email", None, None)]
        if entity == "sales_opportunity_inquiry":
            return EVENT_LABELS[("sales_opportunity_inquiry", "inquiry", None)]
        if entity == "sales_opportunity_close":
            if stage == "Closed Won":
                return EVENT_LABELS[("sales_opportunity_close", "close", "Closed Won")]
            if stage == "Closed Lost":
                return EVENT_LABELS[("sales_opportunity_close", "close", "Closed Lost")]
            return f"🔄 {stage or event or 'close'}"
        return entity or event or "—"

    rows = []
    for d in decisions:
        ts = d.get("ts")
        try:
            ts_str = _dt.fromtimestamp(ts).strftime("%m-%d %H:%M:%S") if ts else "-"
        except Exception:
            ts_str = d.get("ts_iso") or "-"

        cust = d.get("customer") or {}
        entity = d.get("entity") or ""
        event_kind = d.get("event")
        stage = d.get("stage")
        rule_name = d.get("matched_rule") or "—"

        # ─── 고객 컬럼 — Account Name 우선, 없으면 email from ───
        # sales_*: account_name 이 사람 친화적 ("VIP Tech")
        # email:   from 주소 (BC1 호환)
        customer = (
            d.get("account_name")
            or cust.get("name")
            or (d.get("email") or {}).get("from")
            or "—"
        )

        # ─── Tier — 명확할 때만 표시, Unknown 은 빈 칸 ───
        # API 가 customer_tier 로 분리해서 보내옴 (memory tier 와 충돌 회피).
        # 구 records 호환: tier 가 VIP/Standard 면 그대로 사용.
        tier_raw = (
            d.get("customer_tier")
            or cust.get("tier")
            or (d.get("tier") if d.get("tier") in ("VIP", "Standard") else None)
        )
        tier_display = tier_raw if tier_raw in ("VIP", "Standard") else ""

        rows.append({
            lx["recent_col_time"]: ts_str,
            lx["recent_col_customer"]: customer,
            lx["recent_col_event"]: _event_label(entity, event_kind, stage),
            lx["recent_col_tier"]: tier_display,
            lx["recent_col_rule"]: rule_name,
            lx["recent_col_plan"]: len(d.get("plan") or []),
            "memory_tier": d.get("memory_tier") or d.get("tier"),
        })

    import pandas as _pd
    df = _pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)

    # ─── 페이지 네비게이션 ───
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if total_pages > 1:
        caption_tpl = lx.get(
            "pagination_caption",
            "전체 {total}건 · {pages} 페이지 (현재 {cur} 페이지, {start}~{end}건)",
        )
        st.caption(caption_tpl.format(
            total=total, pages=total_pages, cur=current_page + 1,
            start=offset + 1, end=offset + len(decisions),
        ))

        if total_pages <= 10:
            page_nums = list(range(total_pages))
        else:
            start_p = max(0, current_page - 2)
            end_p = min(total_pages, current_page + 3)
            page_nums = list(range(start_p, end_p))
            if 0 not in page_nums:
                prefix = [0] + (["..."] if start_p > 1 else [])
                page_nums = prefix + page_nums
            if (total_pages - 1) not in page_nums:
                suffix = (["..."] if end_p < total_pages - 1 else []) + [total_pages - 1]
                page_nums = page_nums + suffix

        n_cols = len(page_nums) + 2
        cols = st.columns(n_cols)

        with cols[0]:
            if st.button("◀", key="decisions_prev",
                         disabled=(current_page == 0),
                         use_container_width=True):
                st.session_state.decisions_page = max(0, current_page - 1)
                st.rerun()

        for i, p in enumerate(page_nums):
            with cols[i + 1]:
                if p == "...":
                    st.markdown(
                        "<div style='text-align:center;color:#888;'>…</div>",
                        unsafe_allow_html=True,
                    )
                else:
                    is_current = (p == current_page)
                    label = f"**{p + 1}**" if is_current else f"{p + 1}"
                    if st.button(label, key=f"decisions_page_{p}",
                                 disabled=is_current,
                                 use_container_width=True):
                        st.session_state.decisions_page = p
                        st.rerun()

        with cols[-1]:
            if st.button("▶", key="decisions_next",
                         disabled=(current_page >= total_pages - 1),
                         use_container_width=True):
                st.session_state.decisions_page = min(total_pages - 1, current_page + 1)
                st.rerun()


    # ─── 결정 상세 보기 (selectbox + expander) ───
    st.markdown("#### " + lx.get("select_for_detail", "🔍 상세 보기 — 결정 선택"))
    options = list(range(len(decisions)))
    def _fmt(i):
        d = decisions[i]
        ts = d.get("ts")
        try:
            ts_s = _dt.fromtimestamp(ts).strftime("%m-%d %H:%M:%S") if ts else "-"
        except Exception:
            ts_s = "-"
        cust = (d.get("account_name")
                or (d.get("customer") or {}).get("name")
                or (d.get("email") or {}).get("from")
                or "—")
        rule = d.get("matched_rule") or "—"
        return f"{ts_s}  ·  {cust}  ·  {rule}"

    sel_idx = st.selectbox(
        label=" ",
        options=options,
        format_func=_fmt,
        index=None,
        placeholder=lx.get("detail_placeholder", "표에서 행을 선택하세요..."),
        key="decision_detail_select",
        label_visibility="collapsed",
    )

    if sel_idx is not None:
        d = decisions[sel_idx]
        with st.expander("📋 " + _fmt(sel_idx), expanded=True):
            # ─── 1. 메타 정보 ───
            st.markdown("**" + lx.get("detail_section_meta", "📋 메타") + "**")
            meta_cols = st.columns(3)
            meta_cols[0].metric(lx.get("detail_label_account", "Account"),
                                d.get("account_name") or "—")
            meta_cols[1].metric(lx.get("detail_label_event", "Event"),
                                d.get("event") or d.get("entity") or "—")
            meta_cols[2].metric(lx.get("detail_label_tier", "Tier"),
                                d.get("customer_tier") or "—")

            meta_cols2 = st.columns(3)
            meta_cols2[0].caption(
                f"**{lx.get('detail_label_account_id','Account Id')}**: "
                f"`{d.get('account_id') or '—'}`"
            )
            meta_cols2[1].caption(
                f"**{lx.get('detail_label_opp','Opportunity')}**: "
                f"{d.get('opportunity_name') or '—'}"
            )
            meta_cols2[2].caption(
                f"**{lx.get('detail_label_opp_id','Opp Id')}**: "
                f"`{d.get('opportunity_id') or '—'}`"
            )
            if d.get("stage"):
                st.caption(
                    f"**{lx.get('detail_label_stage','Stage')}**: {d.get('stage')}"
                )
            st.caption(
                f"**{lx.get('detail_label_memory_key','Memory Key')}**: "
                f"`{d.get('key') or '—'}` "
                f"(tier: `{d.get('memory_tier') or d.get('tier') or '—'}`)"
            )

            st.divider()

            # ─── 2. 매칭 룰 ───
            st.markdown("**" + lx.get("detail_section_rule", "📐 매칭 룰") + "**")
            st.code(d.get("matched_rule") or "—", language="text")

            # ─── 3. Plan (각 step 의 agent.action + policy) ───
            st.markdown("**" + lx.get("detail_section_plan", "🤖 Plan (agent dispatch)") + "**")
            plan = d.get("plan") or []
            if not plan:
                st.caption("plan 정보 없음")
            else:
                for i, step in enumerate(plan):
                    kind = step.get("kind", "delegate")
                    agent = step.get("agent", "?")
                    action = step.get("action") or step.get("tool") or "?"
                    policy = step.get("policy") or step.get("params") or {}
                    with st.container(border=True):
                        st.markdown(
                            f"**Step {i}** · `{kind}` · "
                            f"**{agent}** → `{action}`"
                        )
                        if policy:
                            st.json(policy, expanded=False)



# ---------------------------------------------------------------
# Memory stats 패널 — HTTP fetch
# ---------------------------------------------------------------
def _render_memory_stats(lx: Dict[str, str]):
    resp = api_client.get_memory_stats()
    if not resp.get("ok"):
        st.error(lx["api_error"])
        return

    stats = resp.get("stats", {}) or {}
    cols = st.columns(3)
    labels = [("hot", lx["memory_hot"]), ("warm", lx["memory_warm"]), ("cold", lx["memory_cold"])]
    for col, (tier, label) in zip(cols, labels):
        with col:
            info = stats.get(tier, {}) or {}
            st.metric(label, f"{info.get('size', 0)}")
            st.caption(f"{lx['memory_backend']}: {info.get('backend', '?')}")

    # 최근 키 미리보기 (warm 우선)
    with st.expander("Recent keys", expanded=False):
        for tier in ("warm", "hot"):
            keys_resp = api_client.get_recent_keys(tier=tier, limit=10)
            if keys_resp.get("ok") and keys_resp.get("keys"):
                st.markdown(f"**{tier}**")
                for k in keys_resp["keys"]:
                    st.code(k, language="text")
                break


# ---------------------------------------------------------------
# Active ontology.yaml — HTTP fetch (server 가 보고 있는 yaml 일치 보장)
# ---------------------------------------------------------------
def _render_active_yaml(lx: Dict[str, str]):
    resp = api_client.get_active_yaml()
    if not resp.get("ok"):
        st.error(lx["api_error"])
        return
    text = resp.get("content") or ""
    # 너무 길면 자르기 (대시보드 부담)
    truncated = text[:4500] + ("\n\n... (truncated)" if len(text) > 4500 else "")
    st.code(truncated, language="yaml")
