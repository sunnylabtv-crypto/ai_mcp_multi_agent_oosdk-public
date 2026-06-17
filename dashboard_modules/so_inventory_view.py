# dashboard_modules/so_inventory_view.py
"""
OOSDK SO Inventory View — Streamlit 탭 모듈.

목적 (한 줄): "S00009 같은 SO 한 건의 라인을 골라서, **재고 / 가용 / 입고예정 /
예상가용 / Assigned (이 SO) / 배송완료** 6컬럼을 한 화면에서 본다."

설계 원칙 (ontology_view.py 와 정합)
------------------------------------
· Dashboard 는 stateless viewer. Odoo XML-RPC 직접 호출 X — MCP server 의
  `/api/dashboard/inventory/so_lines` 엔드포인트가 odoo_service 세션을 공유한
  상태에서 4-state 를 묶어서 내려준다.
· Odoo 미연결 / SO 누락 등 모든 에러는 endpoint 가 {ok:False, error:str} 로
  반환. 이 화면은 그걸 그대로 표시하고 죽지 않는다.

Spec 매핑 (docs/BC3_WEEK1_SPEC_v2_inventory_allocation.md §2.1):
  재고        = qty_on_hand         (on_hand)
  가용재고    = qty_available       (on_hand - reserved)
  입고예정    = qty_incoming        (commitment_date 까지 PO 합)
  예상가용    = qty_projected       (available + incoming)
  Assigned    = qty_assigned_for_so (이 SO 의 picking 중 state='assigned')
  배송완료    = qty_delivered       (표준 sale.order.line.qty_delivered)
"""
from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd
import streamlit as st

from dashboard_modules import api_client


# ─── tier 색상 (Recent Decisions 와 정합) ──────────────────────────
_TIER_COLOR = {
    "VIP": "#7C3AED",
    "Standard": "#4A90D9",
    "Bronze": "#9CA3AF",
}


# ─── picking state → 한국어 라벨 ─────────────────────────────────
_PICKING_STATE_LABEL = {
    "draft": "초안",
    "waiting": "대기",
    "confirmed": "확인",
    "partially_available": "부분 가용",
    "assigned": "예약 완료",
    "done": "완료",
    "cancel": "취소",
}


def _fmt_qty(v: Any) -> str:
    """None → '—', float → 소수점 자리 자동 (정수면 정수 표시)."""
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(f - round(f)) < 1e-6:
        return f"{int(round(f)):,}"
    return f"{f:,.2f}"


def _render_so_header(so: Dict[str, Any]) -> None:
    """SO 메타 4-카드 헤더 — Tier / state / commitment / pickings 개수."""
    c1, c2, c3, c4 = st.columns(4)

    with c1:
        tier = so.get("tier") or "Standard"
        color = _TIER_COLOR.get(tier, "#9CA3AF")
        st.markdown(
            f"""
            <div style="border:1px solid #ddd; border-radius:8px; padding:12px;
                        border-left:4px solid {color}; background:white;">
              <div style="font-size:11px; color:gray;">고객 (Tier)</div>
              <div style="font-size:16px; font-weight:bold;">{so.get('partner') or '—'}</div>
              <div style="font-size:12px; color:{color}; font-weight:bold;">{tier}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with c2:
        st.metric("SO 상태", so.get("state") or "—")

    with c3:
        cd = so.get("commitment_date") or "—"
        st.metric("Commitment Date", cd[:10] if isinstance(cd, str) and len(cd) >= 10 else cd)

    with c4:
        pickings = so.get("pickings") or []
        st.metric("배송 (Picking) 수", len(pickings))


def _render_pickings(pickings: List[Dict[str, Any]]) -> None:
    """SO 의 picking 목록 — state 한국어 라벨 + 예정일."""
    if not pickings:
        st.caption("이 SO 에 연결된 배송 (picking) 이 없습니다 — 모두 service 라인이거나 SO 가 confirmed 전.")
        return
    df = pd.DataFrame(pickings)
    df["state_label"] = df["state"].map(lambda s: f"{_PICKING_STATE_LABEL.get(s, s)} ({s})")
    df = df.rename(columns={
        "id": "ID",
        "name": "Picking",
        "state_label": "상태",
        "scheduled_date": "예정일",
    })
    cols = [c for c in ["ID", "Picking", "상태", "예정일"] if c in df.columns]
    st.dataframe(df[cols], use_container_width=True, hide_index=True)


def _render_lines(lines: List[Dict[str, Any]]) -> None:
    """SO 라인 테이블 — 6컬럼 + 부족분 강조."""
    if not lines:
        st.info("이 SO 에 라인이 없습니다.")
        return

    rows = []
    for ln in lines:
        # shortage 계산 — assigned 가 ordered 보다 적으면 "?" 마크
        ordered = ln.get("qty_ordered") or 0
        assigned = ln.get("qty_assigned_for_so")
        delivered = ln.get("qty_delivered") or 0
        if assigned is None:
            short_mark = ""  # service 라인
        else:
            still_needed = max(ordered - (assigned + delivered), 0)
            short_mark = "⚠️" if still_needed > 0 else "✅"

        rows.append({
            "품목": ln.get("product_name") or "—",
            "타입": ln.get("product_type") or "—",
            "주문": _fmt_qty(ordered),
            "재고": _fmt_qty(ln.get("qty_on_hand")),
            "가용재고": _fmt_qty(ln.get("qty_available")),
            "입고예정": _fmt_qty(ln.get("qty_incoming")),
            "총 입고예정": _fmt_qty(ln.get("qty_incoming_total")),
            "예상가용": _fmt_qty(ln.get("qty_projected")),
            "Assigned (이 SO)": _fmt_qty(assigned),
            "배송완료": _fmt_qty(delivered),
            "상태": short_mark,
        })
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)

    # 범례
    st.caption(
        "📘 **컬럼 설명** — "
        "**재고**=전사 on_hand · "
        "**가용재고**=on_hand − reserved · "
        "**입고예정**=commitment_date 까지 도착할 PO 합 (BC3 spec §2.1 — '약속 시점 안에 채워질 양') · "
        "**총 입고예정**=commitment 무관 모든 미수령 PO 합 ('곧 들어올 양' — 두 값이 다르면 commitment 이후 도착 PO 가 있다는 신호) · "
        "**예상가용**=가용재고 + 입고예정 (commitment 기준) · "
        "**Assigned (이 SO)**=이 SO 의 picking 중 state='assigned' 인 move 의 quantity 합 (sale_line_id 기준) · "
        "**배송완료**=sale.order.line.qty_delivered. "
        "⚠️ = 주문량 대비 Assigned+배송완료 부족 (입고 대기 또는 VIP 선점 후보)."
    )


# ════════════════════════════════════════════════════════════════
# 엔트리 — dashboard.py 의 탭에서 호출
# ════════════════════════════════════════════════════════════════

def render_so_inventory_view(lang: str = "ko") -> None:
    """탭 본체. 현재는 ko 만 지원 (ontology_view 와 정합)."""
    st.markdown(
        "이 화면은 Odoo 의 SO 한 건을 골라 **재고 / 가용 / 입고예정 / Assigned / 배송완료** "
        "4-state 를 라인별로 보여주는 viewer 입니다. 데이터는 MCP server 의 "
        "`/api/dashboard/inventory/so_lines` 엔드포인트에서 즉시 fetch."
    )

    col_in, col_btn = st.columns([3, 1])
    with col_in:
        so_name = st.text_input(
            "SO 이름 (예: S00009)",
            value=st.session_state.get("so_inv_last_name", "S00009"),
            key="so_inv_input",
        )
    with col_btn:
        st.write("")  # 정렬용
        st.write("")
        do_load = st.button("🔍 조회", type="primary")

    if not do_load and "so_inv_cache" not in st.session_state:
        st.info("위에 SO 이름을 입력하고 **조회** 를 누르세요.")
        return

    if do_load:
        st.session_state["so_inv_last_name"] = so_name
        with st.spinner(f"{so_name} 의 라인별 재고 조회 중…"):
            resp = api_client.get_so_inventory(so_name=so_name)
        st.session_state["so_inv_cache"] = resp

    resp = st.session_state.get("so_inv_cache") or {}
    if not resp.get("ok"):
        err = resp.get("error", "unknown")
        st.error(f"조회 실패: `{err}`")
        if resp.get("service_status"):
            with st.expander("Odoo service status", expanded=False):
                st.json(resp["service_status"])
        return

    so = resp.get("so") or {}
    lines = resp.get("lines") or []

    # ── Stale cache 가드 (code-review #2) ────────────────────────
    # Streamlit 은 text_input 을 키 입력할 때마다 rerun 되므로, 사용자가 새 SO
    # 이름을 타이핑하기만 하고 🔍 조회 를 누르지 않으면 이전 SO 의 캐시가 그대로
    # 표시된다 (header 만 옛 이름, input 박스는 새 이름 → 운영 혼동).
    # current input ≠ cached so.name 이면 명시적으로 갱신 요구.
    current_input = (so_name or "").strip()
    cached_name = (so.get("name") or "").strip()
    if current_input and cached_name and current_input != cached_name:
        st.warning(
            f"⚠️ 입력한 SO `{current_input}` 가 마지막 조회 결과 `{cached_name}` 와 다릅니다. "
            "위의 **🔍 조회** 를 눌러 갱신하세요."
        )
        return

    st.subheader(f"📦 {so.get('name', '?')} — SO 메타")
    _render_so_header(so)

    st.divider()

    st.subheader("🚚 배송 (Picking) 목록")
    _render_pickings(so.get("pickings") or [])

    st.divider()

    st.subheader("📊 라인별 재고 4-state")
    _render_lines(lines)
