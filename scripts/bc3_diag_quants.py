# scripts/bc3_diag_quants.py
"""
get_product_quants 가 어느 location 까지 잡는지 raw dump.

가설: get_product_quants 의 domain 이 location_id.usage 필터 없어
internal 외 (supplier / customer / inventory_loss) 까지 합산되어
"validate 했는데 on_hand=0" 의 silent bug 발생.
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


def main():
    if not odoo_service.is_available():
        odoo_service.authenticate_odoo()

    pid = 2  # USB SecureKey-100 (방금 validate 한 것)

    print("=" * 78)
    print(f" USB SecureKey-100 (product_id={pid}) — 모든 stock.quant raw dump")
    print("=" * 78)

    # 1) get_product_quants 가 실제로 잡는 것 (no filter)
    quants = odoo_service.get_product_quants(pid)
    print(f"\n[1] odoo_service.get_product_quants({pid}) — domain=[('product_id','=',pid)]")
    print(f"    {len(quants)} 개:")
    sum_qty = 0
    for q in quants:
        loc = q.get("location_id")
        loc_label = loc[1] if isinstance(loc, list) else loc
        qty = q.get("quantity", 0)
        sum_qty += qty
        print(f"      location={loc_label!r}  qty={qty}  reserved={q.get('reserved_quantity')}")
    print(f"    SUM(quantity) = {sum_qty}")

    # 2) location detail 살펴보기
    print(f"\n[2] 각 quant 의 location.usage:")
    loc_ids = list({q["location_id"][0] for q in quants if isinstance(q.get("location_id"), list)})
    if loc_ids:
        locs = odoo_service.call("stock.location", "read", loc_ids,
                                  fields=["complete_name", "usage"])
        for L in locs:
            print(f"      id={L.get('id')} name={L.get('complete_name')!r} usage={L.get('usage')!r}")

    # 3) get_inventory_state 가 반환하는 값
    print(f"\n[3] odoo_service.get_inventory_state({pid}):")
    state = odoo_service.get_inventory_state(pid)
    print(f"      {state}")

    # 4) 만약 internal 만 filter 하면?
    print(f"\n[4] location_id.usage='internal' 만 filter:")
    internal_quants = odoo_service.call(
        "stock.quant", "search_read",
        [("product_id", "=", pid), ("location_id.usage", "=", "internal")],
        fields=["location_id", "quantity", "reserved_quantity"],
    )
    sum_internal = sum(q.get("quantity", 0) for q in internal_quants)
    for q in internal_quants:
        loc = q.get("location_id")
        loc_label = loc[1] if isinstance(loc, list) else loc
        print(f"      location={loc_label!r}  qty={q.get('quantity')}  reserved={q.get('reserved_quantity')}")
    print(f"    SUM(internal only) = {sum_internal}")


if __name__ == "__main__":
    main()
