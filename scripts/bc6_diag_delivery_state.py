# scripts/bc6_diag_delivery_state.py
"""현재 outgoing(delivery) picking 상태 + 고객별 USB demand/reserved 조회 (read-only).

split/cancel/wait 를 현재 실제 데이터로 설명하기 위한 진단.
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


def banner(s):
    print("\n" + "=" * 78 + "\n " + s + "\n" + "=" * 78)


def main():
    if not odoo_service.is_available():
        odoo_service.authenticate_odoo()

    banner("1. 현재 sale.order (고객 / tier / state / invoice_status)")
    sos = odoo_service.call(
        "sale.order", "search_read",
        [("state", "in", ["sale", "done"])],
        fields=["name", "partner_id", "state", "invoice_status", "amount_total"],
        order="name",
    )
    so_by_partner = {}
    for so in sos:
        partner = so.get("partner_id")
        pname = partner[1] if isinstance(partner, list) else partner
        so_by_partner[so["name"]] = pname
        print(f"  · {so.get('name')}: partner={pname!r}  state={so.get('state')!r}  "
              f"invoice_status={so.get('invoice_status')!r}  total={so.get('amount_total')}")
    if not sos:
        print("  (확정 SO 없음)")

    banner("2. outgoing(delivery) picking 상태 + USB demand vs reserved")
    picks = odoo_service.call(
        "stock.picking", "search_read",
        [("picking_type_id.code", "=", "outgoing")],
        fields=["name", "state", "partner_id", "origin", "scheduled_date"],
        order="name",
    )
    for p in picks:
        partner = p.get("partner_id")
        pname = partner[1] if isinstance(partner, list) else partner
        moves = odoo_service.call(
            "stock.move", "search_read",
            [("picking_id", "=", p["id"])],
            fields=["product_id", "product_uom_qty", "quantity", "state"],
        )
        print(f"\n  · {p.get('name')} [{p.get('state')}]  partner={pname!r}  origin={p.get('origin')!r}")
        for m in moves:
            prod = m.get("product_id")
            prod_name = prod[1] if isinstance(prod, list) else prod
            demand = m.get("product_uom_qty")
            reserved = m.get("quantity")
            short = (demand or 0) - (reserved or 0)
            flag = "  ← 부분(short>0)" if short > 0 else ""
            print(f"      {prod_name!r}: demand={demand} reserved={reserved} "
                  f"short={short} move_state={m.get('state')!r}{flag}")
    if not picks:
        print("  (outgoing picking 없음)")

    banner("3. USB 재고 (on_hand / reserved) — location 별")
    quants = odoo_service.call(
        "stock.quant", "search_read",
        [("product_id", "=", 2), ("location_id.usage", "=", "internal")],
        fields=["location_id", "quantity", "reserved_quantity", "available_quantity"],
    )
    for q in quants:
        loc = q.get("location_id")
        loc_label = loc[1] if isinstance(loc, list) else loc
        print(f"  · {loc_label!r}: on_hand={q.get('quantity')} "
              f"reserved={q.get('reserved_quantity')} available={q.get('available_quantity')}")
    if not quants:
        print("  (USB quant 없음)")


if __name__ == "__main__":
    main()
