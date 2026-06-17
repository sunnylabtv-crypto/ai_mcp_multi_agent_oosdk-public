# scripts/bc4_demo_priority_override.py
"""
BC4 S0 — Priority Override (Case A: 명시적 지정형) 시연.

목적:
    평소 VIP-first 결정론 배정에, 사람이 "이 Standard SO 를 먼저" 라고 명시하면
    (priority_override_so_ids) 해당 SO 의 정렬 _score 를 boost 해서 우선 reserve.
    LLM 미개입. 결정론·감사가능. equal_vip 모드 = VIP 동급(약속 안 깸).

이 스크립트가 보여주는 것:
    A. Baseline (override 없음)   → VIP SO 가 먼저 reserve
    B. equal_vip override(Standard) → 그 Standard 가 VIP 동급으로 올라
                                       (더 이른 납기면) 먼저 reserve
    C. 가드 — requested_by 빈 값 → 발행 거부 → VIP-first 폴백 (server 게이트 로직)

주의:
    · 실제 Odoo (your-tenant.odoo.com) 연결 필요. 미연결 시 agent 가 plan-only 반환.
    · override 주입은 server 의 dispatch 가 policy["priority_override_runtime"] 로
      넣는 것과 동일 형태를 직접 구성한다 (여기선 server 게이트/audit 는 C 에서 별도 시연).
    · reset: USB outgoing move 들을 unreserve 해서 매 라운드 동일 출발선으로 맞춘다.

실행:
    cd ai_mcp_multi_agent_oosdk
    python scripts/bc4_demo_priority_override.py
    # 특정 SO 를 override 대상으로: --override-so S00008
"""
import argparse
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from mcp_server.services import odoo_service  # noqa: E402
from mcp_server.agents.inventory_agent import InventoryAgent  # noqa: E402

USB_PRODUCT_NAME = "USB SecureKey-100"
TIER_TABLE = {"VIP": 100, "Standard": 50, "Bronze": 25}


def banner(s):
    print("\n" + "=" * 78 + "\n " + s + "\n" + "=" * 78)


def _usb_id():
    pids = odoo_service.call("product.product", "search", [("name", "=", USB_PRODUCT_NAME)])
    return int(pids[0]) if pids else None


def _usb_outgoing_moves(usb_id):
    """USB 가 있는 outgoing move 들 (reserve 대상 후보) — sale_order_id 부착."""
    moves = odoo_service.call(
        "stock.move", "search_read",
        [("product_id", "=", usb_id),
         ("state", "in", ["confirmed", "waiting", "partially_available", "assigned"]),
         ("picking_id.picking_type_id.code", "=", "outgoing")],
        fields=["id", "picking_id", "product_uom_qty", "quantity", "state",
                "sale_line_id", "date"],
    )
    # sale_order_id 매핑 (picking → sale_id)
    pick_ids = list({m["picking_id"][0] for m in moves
                     if isinstance(m.get("picking_id"), list) and m["picking_id"]})
    picks = odoo_service.call("stock.picking", "read", pick_ids,
                              fields=["id", "name", "sale_id"]) if pick_ids else []
    pick_to_so = {p["id"]: (p["sale_id"][0] if isinstance(p.get("sale_id"), list)
                            and p["sale_id"] else None) for p in picks}
    for m in moves:
        pid = m["picking_id"][0] if isinstance(m.get("picking_id"), list) else None
        m["sale_order_id"] = pick_to_so.get(pid)
    return moves


def _reset_reservations(usb_id):
    """USB outgoing move 중 assigned/partially 인 것을 unreserve → 동일 출발선."""
    moves = _usb_outgoing_moves(usb_id)
    n = 0
    for m in moves:
        if m.get("state") in ("assigned", "partially_available"):
            try:
                odoo_service.call("stock.move", "_do_unreserve", [m["id"]])
                n += 1
            except Exception:
                # 일부 Odoo 버전은 picking 단위 do_unreserve 만 노출 — 폴백
                try:
                    odoo_service.unreserve_move(m["id"])
                    n += 1
                except Exception:
                    pass
    print(f"  reset: {n} move unreserve 시도")


def _so_name_map(sale_ids):
    if not sale_ids:
        return {}
    sos = odoo_service.call("sale.order", "read", list(set(sale_ids)), fields=["id", "name"])
    return {s["id"]: s["name"] for s in sos}


async def _run_replenish(agent, usb_id, qty, override=None):
    """server dispatch 와 동일하게 policy 에 priority_override_runtime 주입 후 실행."""
    policy = {
        "ordering": ["tier", "target_delivery_date", "sale_order_id"],
        "tier_priority": dict(TIER_TABLE),
        "consume_all_for_vip_first": True,
    }
    if override:
        policy["priority_override_runtime"] = override
    context = {"receipt": {"product_id": usb_id, "qty": qty}}
    res = await agent.execute_action("replenish_priority_queue", policy=policy, context=context)
    return res.get("result") or {}


def _print_replenished(inner, name_map):
    replenished = inner.get("replenished") or []
    if not replenished:
        print("  (reserve 된 것 없음)")
    for r in replenished:
        so = r.get("picking_id")
        mark = "🔧OVERRIDE" if r.get("override") else "         "
        print(f"  {mark}  move={r.get('move_id'):>6}  tier={r.get('tier'):8s}  "
              f"qty={r.get('qty_assigned'):>6.0f}  picking={so}")
    ov = inner.get("override")
    if ov:
        print(f"  override: applied={ov['applied']} mode={ov['mode']} "
              f"reserved_so_ids={ov.get('reserved_so_ids')} by={ov.get('requested_by')}")


def _resolve_so_id(target_so_name):
    """'S00008' 같은 이름 → sale.order id."""
    sos = odoo_service.call("sale.order", "search_read",
                            [("name", "=", target_so_name)], fields=["id", "name"])
    return int(sos[0]["id"]) if sos else None


async def amain(args):
    if not odoo_service.is_available():
        odoo_service.authenticate_odoo()
    if not odoo_service.is_available():
        print("❌ Odoo 미연결 — 데모 중단 (.env 확인)")
        return 1

    usb_id = _usb_id()
    if not usb_id:
        print(f"❌ {USB_PRODUCT_NAME} 못 찾음")
        return 1

    agent = InventoryAgent(llm_config={"config_list": []})
    agent.register_tools_from_services(user_id="demo")

    # override 대상 Standard SO 결정
    target_so_name = args.override_so
    target_so_id = _resolve_so_id(target_so_name)
    if not target_so_id:
        print(f"⚠️ override 대상 SO '{target_so_name}' 못 찾음 — B/C 라운드 건너뜀")

    qty = float(args.qty)

    # ── A. Baseline (override 없음) ──
    banner(f"A. Baseline — override 없음 (입고 {qty:.0f})")
    _reset_reservations(usb_id)
    inner_a = await _run_replenish(agent, usb_id, qty, override=None)
    _print_replenished(inner_a, {})

    if target_so_id:
        # ── B. equal_vip override (Standard 지정) ──
        banner(f"B. equal_vip override — {target_so_name}(id={target_so_id}) 우선 (입고 {qty:.0f})")
        _reset_reservations(usb_id)
        override = {
            "so_ids": [target_so_id],
            "requested_by": "ops.manager@acme",
            "reason": f"긴급 계약 페널티 회피 — {target_so_name} 우선",
            "issued_at": datetime.utcnow().isoformat() + "Z",
            "expires_at": (datetime.utcnow() + timedelta(hours=24)).isoformat() + "Z",
            "cfg": {"mode": "equal_vip", "boost_score": 1000,
                    "requires_authorization": True},
        }
        inner_b = await _run_replenish(agent, usb_id, qty, override=override)
        _print_replenished(inner_b, {})

        # ── C. 가드 — requested_by 빈 값 → 발행 거부 (server 게이트 로직) ──
        banner("C. 가드 시연 — requires_authorization (requested_by 빈 값)")
        from mcp_server.server import _build_priority_override, _enforce_override_gate
        from mcp_server.server import get_or_create_ontology_engine
        engine = get_or_create_ontology_engine()
        cfg = {"enabled": True, "mode": "equal_vip", "requires_authorization": True,
               "max_per_day": 3, "auto_expire_hours": 24}
        bad = _build_priority_override(so_ids=[target_so_id], requested_by="",
                                       reason="무권한 시도", cfg=cfg)
        gate = _enforce_override_gate(engine, bad)
        print(f"  발행 게이트 결과: allowed={gate['allowed']}  reason={gate['reason']!r}")
        print("  → 거부 시 server 는 override 를 폐기하고 정상 VIP-first 로 진행 (시스템 안 멈춤)")

    banner("끝")
    print("""
관찰 포인트:
  · A 에서는 VIP SO 가 먼저 reserve (Standard 는 남는 양만).
  · B 에서는 지정한 Standard 가 VIP 동급(_score=100)으로 올라,
    납기가 더 이르면 먼저 reserve — VIP 를 뒤로 밀지 않음(equal_vip).
  · C 에서는 권한 없는 override 가 게이트에서 거부됨.

Dashboard: http://REDACTED_VM_IP:9601 → SO 재고 탭 / Recent Decisions(override 배지)
""")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--override-so", default="S00008", help="override 대상 Standard SO 이름")
    ap.add_argument("--qty", default="500", help="입고 수량")
    args = ap.parse_args()
    sys.exit(asyncio.run(amain(args)))
