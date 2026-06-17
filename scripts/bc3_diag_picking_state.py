# scripts/bc3_diag_picking_state.py
"""
"준비" (assigned) vs "완료" (done) — 진짜로 입고된 것인지 확인.

Odoo 의 stock.picking state machine:
    draft (초안) -> waiting (대기) -> confirmed (확정)
    -> partially_available (일부 가용) -> assigned (준비 됨)
    -> done (완료)

핵심:
    · assigned (준비)  = 시스템이 받을 준비 완료, 그러나 stockman 이 "검증" 클릭 전.
                         stock.quant 미반영. = 물리적으로 아직 안 들어옴.
    · done (완료)      = 검증 클릭됨. stock.quant 업데이트. = 진짜 입고.

이 스크립트는 stock.picking.state + stock.quant.quantity 를 직접 보여줘서
"준비" 가 실제로 입고된 건지 안 들어온 건지 사실 확인.
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

    # 1) WH/IN/00009-11 picking 상태
    banner("1. WH/IN/00009, 00010, 00011 — picking.state")
    picks = odoo_service.call(
        "stock.picking", "search_read",
        [("name", "in", ["WH/IN/00009", "WH/IN/00010", "WH/IN/00011"])],
        fields=["name", "state", "date_done", "scheduled_date"],
    )
    for p in picks:
        print(f"  · {p.get('name')}: state={p.get('state')!r}  "
              f"date_done={p.get('date_done')!r}  scheduled={p.get('scheduled_date')!r}")

    # 2) 각 product 의 실제 stock.quant (on_hand)
    banner("2. stock.quant — 진짜 창고 재고 (on_hand)")
    for product_id, product_name in [(2, "USB SecureKey-100"),
                                      (3, "SecureGate Appliance G2"),
                                      (4, "SmartBox Edge Server")]:
        quants = odoo_service.call(
            "stock.quant", "search_read",
            [("product_id", "=", product_id), ("location_id.usage", "=", "internal")],
            fields=["location_id", "quantity", "reserved_quantity"],
        )
        total_on_hand = sum(q.get("quantity", 0) for q in quants)
        print(f"\n  product_id={product_id} ({product_name}):")
        print(f"    총 on_hand = {total_on_hand}")
        for q in quants:
            loc = q.get("location_id")
            loc_label = loc[1] if isinstance(loc, list) else loc
            print(f"      location={loc_label!r}  qty={q.get('quantity')}  reserved={q.get('reserved_quantity')}")
        if not quants:
            print("    (stock.quant 레코드 없음 — 한 번도 입고된 적 없음)")

    # 3) 결론
    banner("3. 결론")
    print("""
  · picking.state='assigned' (준비) 는 'stockman 이 검증 클릭 대기 중' 의미.
    Odoo 의 stock.quant 는 변화 없음. = 물리적 입고 안 됨.
  · picking.state='done' (완료) 이 되어야 stock.quant 가 증가. = 진짜 입고.

  → 위 stock.quant 의 quantity 가 0 이면 = 진짜 안 들어옴 (= "준비"의 의미)
  → quantity > 0 이면 = 들어옴 (= 다른 경로로 입고된 재고가 있음)
""")


if __name__ == "__main__":
    main()
