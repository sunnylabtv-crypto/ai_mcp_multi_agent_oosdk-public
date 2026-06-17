# scripts/bc3_trigger_vip_first_reserve.py
"""
VIP-first reserve trigger — replenish_priority_queue 의 핵심 효과를 local XML-RPC 로 재현.

상황:
    · USB SecureKey-100 on_hand = 1,000 (WH/IN/00009 validate 완료)
    · 다수의 outgoing picking 이 confirmed 상태에서 reserve 대기 중
    · 우리가 원하는 것: VIP picking 부터 먼저 reserve 해서 limited stock 이 VIP 로 흘러가게

흐름:
    1. USB SecureKey-100 이 포함된 outgoing stock.picking 모음 (state in confirmed/waiting/
       partially_available)
    2. 각 picking 의 sale_id → partner → category 로 tier 추론 (VIP / Standard / Bronze)
    3. tier priority + sale_order_id (FIFO tie-break) 로 정렬
    4. 순서대로 stock.picking.action_assign 호출 — 가용 stock 우선 reserve
    5. 결과 보고: 각 picking 의 reserved qty / 새 state

이건 ontology engine 을 거치지 않고 Odoo 측 reserve 만 직접 함. dashboard 의 'Assigned (이 SO)'
컬럼 변화는 동일하게 나타남 (어차피 dashboard 는 stock.move.state='assigned' move 의 qty 합산).
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from mcp_server.services import odoo_service  # noqa: E402


USB_PRODUCT_NAME = "USB SecureKey-100"
TIER_PRIORITY = {"VIP": 100, "Standard": 50, "Bronze": 25, "": 0}


def banner(s):
    print("\n" + "=" * 78 + "\n " + s + "\n" + "=" * 78)


def main():
    if not odoo_service.is_available():
        odoo_service.authenticate_odoo()

    # 1. USB product id
    pids = odoo_service.call(
        "product.product", "search", [("name", "=", USB_PRODUCT_NAME)],
    )
    if not pids:
        print(f"❌ {USB_PRODUCT_NAME} 못 찾음")
        return 1
    usb_id = int(pids[0])

    # 2. USB 라인이 있는 outgoing picking 모으기 (reserve 대상 후보)
    banner(f"1. {USB_PRODUCT_NAME} 가 있는 outgoing pickings (reserve 대기)")
    move_records = odoo_service.call(
        "stock.move", "search_read",
        [
            ("product_id", "=", usb_id),
            ("state", "in", ["confirmed", "waiting", "partially_available"]),
            ("picking_id.picking_type_id.code", "=", "outgoing"),
        ],
        fields=["picking_id", "product_uom_qty", "quantity", "state", "sale_line_id"],
    )
    if not move_records:
        print("  (reserve 대기 중인 USB outgoing move 없음 — 모두 이미 assigned 또는 done)")
        return 0

    # picking_id 별로 묶기 + sale_id 추출
    pickings_set = {}
    for m in move_records:
        pid_field = m.get("picking_id")
        if not (isinstance(pid_field, list) and pid_field):
            continue
        pid = int(pid_field[0])
        pickings_set[pid] = pickings_set.get(pid, []) + [m]

    pick_ids = list(pickings_set.keys())
    print(f"  {len(pick_ids)} 개 picking 후보 (state=confirmed/waiting/partially_available):")

    # 3. picking → sale_id → partner → tier 매핑
    picks = odoo_service.call(
        "stock.picking", "read", pick_ids,
        fields=["id", "name", "state", "sale_id", "partner_id"],
    )
    sale_ids = []
    for p in picks:
        sid_field = p.get("sale_id")
        if isinstance(sid_field, list) and sid_field:
            sale_ids.append(int(sid_field[0]))
    sale_ids = list(set(sale_ids))

    # tier 매핑 (BC3 MED #M1 봉합 helper 재사용)
    tier_map = odoo_service.get_sale_order_tier_map(sale_ids) if sale_ids else {}
    so_name_map = {}
    if sale_ids:
        sos = odoo_service.call(
            "sale.order", "read", sale_ids, fields=["id", "name"],
        )
        so_name_map = {s["id"]: s["name"] for s in sos}

    # 4. picking 마다 tier / SO 이름 부여 + 정렬
    enriched = []
    for p in picks:
        sid_field = p.get("sale_id")
        sale_id = int(sid_field[0]) if isinstance(sid_field, list) and sid_field else None
        tier = tier_map.get(sale_id, "Standard") if sale_id else ""
        so_name = so_name_map.get(sale_id, "?") if sale_id else "(no SO)"
        moves = pickings_set[p["id"]]
        total_demand = sum(float(m.get("product_uom_qty") or 0) for m in moves)
        enriched.append({
            "picking_id": p["id"],
            "picking_name": p["name"],
            "state": p["state"],
            "sale_id": sale_id,
            "so_name": so_name,
            "tier": tier,
            "demand": total_demand,
            "tier_pri": TIER_PRIORITY.get(tier, 0),
        })

    enriched.sort(key=lambda x: (-x["tier_pri"], x["sale_id"] or 0))

    print(f"\n  정렬 순서 (VIP > Standard, then sale_id FIFO):")
    for e in enriched:
        print(
            f"    {e['so_name']:8s}  {e['picking_name']:12s}  tier={e['tier']:8s}  "
            f"demand={e['demand']:>7.0f}  state={e['state']!r}"
        )

    # 5. 순서대로 action_assign 호출
    banner("2. VIP-first reserve 실행 (stock.picking.action_assign 순서대로)")
    results = []
    for e in enriched:
        try:
            odoo_service.call("stock.picking", "action_assign", [e["picking_id"]])
        except Exception as ex:
            print(f"  ❌ {e['picking_name']} action_assign 실패: {ex}")
            results.append((e, None, f"error: {ex}"))
            continue

        # 다시 읽기로 결과 확인
        p_after = odoo_service.call(
            "stock.picking", "read", [e["picking_id"]],
            fields=["state"],
        )[0]
        moves_after = odoo_service.call(
            "stock.move", "search_read",
            [("picking_id", "=", e["picking_id"]), ("product_id", "=", usb_id)],
            fields=["state", "quantity"],
        )
        reserved = sum(
            float(m.get("quantity") or 0)
            for m in moves_after
            if m.get("state") == "assigned"
        )
        results.append((e, p_after["state"], reserved))
        print(
            f"  ✅ {e['so_name']:8s}  {e['picking_name']:12s}  tier={e['tier']:8s}  "
            f"demand={e['demand']:>7.0f}  → reserved={reserved:>7.0f}  state={p_after['state']!r}"
        )

    # 6. 요약
    banner("3. 최종 그림")
    total_demand = sum(e["demand"] for e, _, _ in results)
    total_reserved = sum(r for _, _, r in results if isinstance(r, (int, float)))
    print(f"  총 수요 = {total_demand:.0f}  /  총 reserve = {total_reserved:.0f}  "
          f"/  부족 = {total_demand - total_reserved:.0f}")
    print()
    for e, state, reserved in results:
        mark = "✅" if isinstance(reserved, (int, float)) and reserved == e["demand"] else "⚠️"
        r_str = f"{reserved:.0f}" if isinstance(reserved, (int, float)) else str(reserved)
        print(f"  {mark} {e['so_name']:8s}  tier={e['tier']:8s}  demand={e['demand']:>5.0f}  reserved={r_str}  state={state!r}")

    print("""
Dashboard 확인:
    http://REDACTED_VM_IP:9601 → SO 재고 탭 → 각 SO 조회
    · S00009 (VIP) USB SecureKey: Assigned (이 SO) = 1,000 (200 short)
    · S00008/S00011/S00012/S00013 (Standard): Assigned = 0 (모두 waiting 또는 partially)
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
