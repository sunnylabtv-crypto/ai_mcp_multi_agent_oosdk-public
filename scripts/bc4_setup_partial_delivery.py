# scripts/bc4_setup_partial_delivery.py
"""BC4 부분출고 라이브/시연용 상태 셋업 (동적 — 상태 변화·리셋에 강함).

목표 상태:
  · USB(product_id=2) invoice_policy = 'delivery' (출고분 기준 청구)
    (서비스 제품은 그대로 'order' 유지 — 건드리지 않음)
  · 대상 SO(기본 S00051, VIP D)의 '열린' 출고 picking 의 USB move 를
    TARGET_RESERVE 만큼만 reserve → demand > reserved → partially_available
    → delivery validate 시 split/cancel/wait 분기 발생.

picking/move id 를 하드코딩하지 않고 SO 이름으로 동적 해석 → 리셋 후에도 동작.

실행:
  python scripts/bc4_setup_partial_delivery.py                 # 기본 S00051, 100 reserve
  python scripts/bc4_setup_partial_delivery.py --so S00045 --qty 100
  python scripts/bc4_setup_partial_delivery.py --dry           # 현재 상태만
"""
import argparse
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from mcp_server.services import odoo_service as o  # noqa: E402

USB_PRODUCT_ID = 2
USB_TMPL_ID = 2
OPEN_STATES = ("draft", "waiting", "confirmed", "partially_available", "assigned")


def banner(s):
    print("\n" + "=" * 74 + "\n " + s + "\n" + "=" * 74)


def find_open_usb_picking(so_name):
    """SO 의 열린 출고 picking 중 USB move(demand>0) 가진 것 + move 반환."""
    picks = o.call("stock.picking", "search_read",
                   [("origin", "=", so_name), ("picking_type_id.code", "=", "outgoing"),
                    ("state", "in", list(OPEN_STATES))],
                   fields=["name", "state"], order="id")
    for p in picks:
        mv = o.call("stock.move", "search_read",
                    [("picking_id", "=", p["id"]), ("product_id", "=", USB_PRODUCT_ID)],
                    fields=["id", "product_uom_qty", "quantity", "state"])
        if mv and (mv[0].get("product_uom_qty") or 0) > 0:
            return p, mv[0]
    return None, None


def show_state(tag, so_name):
    banner(f"상태 [{tag}] — {so_name}")
    pol = o.call("product.product", "read", [USB_PRODUCT_ID], fields=["invoice_policy"])[0]
    inv = o.get_inventory_state(USB_PRODUCT_ID)
    print(f"  USB invoice_policy={pol.get('invoice_policy')!r} | "
          f"재고 on_hand={inv.get('on_hand')} reserved={inv.get('reserved')} available={inv.get('available')}")
    p, m = find_open_usb_picking(so_name)
    if not p:
        print(f"  (열린 USB 출고 picking 없음 — 이미 다 출고됐거나 SO 없음)")
        return None, None
    short = (m.get("product_uom_qty") or 0) - (m.get("quantity") or 0)
    print(f"  {p['name']} [{p['state']}]  USB demand={m.get('product_uom_qty')} "
          f"reserved={m.get('quantity')} shortage={short} move={m.get('state')!r}")
    return p, m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--so", default="S00051", help="대상 SO 이름 (VIP)")
    ap.add_argument("--qty", type=float, default=100, help="reserve 할 수량 (부분)")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    if not o.is_available():
        o.authenticate_odoo()

    p, m = show_state("BEFORE", args.so)
    if args.dry:
        print("\n[--dry] 변경 없이 종료.")
        return
    if not p:
        print("❌ 대상 picking 없음 — 셋업 불가.")
        return

    # 1) USB invoice_policy → delivery (idempotent)
    banner("1) USB invoice_policy → 'delivery'")
    o.call("product.template", "write", [USB_TMPL_ID], {"invoice_policy": "delivery"})
    print("  done.")

    # 2) 대상 move 를 args.qty 만큼 부분 reserve
    banner(f"2) {p['name']} USB {args.qty} reserve → partially_available")
    demand = m.get("product_uom_qty") or 0
    already = m.get("quantity") or 0
    target = min(args.qty, demand)
    if already >= target:
        print(f"  이미 reserved={already} (>= {target}) — 건너뜀.")
    else:
        inv = o.get_inventory_state(USB_PRODUCT_ID)
        avail = inv.get("available") or 0
        need = target - avail
        if need > 0:
            r = o.register_stock_receipt(USB_PRODUCT_ID, need)
            print(f"  재고 +{need} 입고: new_total={r.get('new_total')}")
        rr = o.reserve_move(m["id"])
        print(f"  reserve_move({m['id']}) → {rr}")

    pk, mv = show_state("AFTER", args.so)
    banner("다음 — Claude Desktop 시연")
    if pk:
        print(f"  대상 picking: {pk['name']} (id={pk['id']})")
        print(f"  → '{pk['name']} 출고해줘' → advisor split 추천 → 'split 승인' → '출고분 청구서 발행'")


if __name__ == "__main__":
    main()
