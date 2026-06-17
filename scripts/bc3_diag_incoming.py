# scripts/bc3_diag_incoming.py
"""
Diagnostic — 왜 SO 의 qty_incoming 이 0 으로 나오는가.

가설:
  (H1) PO receipt 의 stock.move.date 가 SO commitment_date 보다 늦어,
       get_pending_receipts 의 (date <= by_date_iso) 필터에 걸려 제외됨.
  (H2) PO receipt 의 picking_type_id.code 가 'incoming' 이 아님.
  (H3) product_id 매칭이 안 됨 (variant / template 혼동).

raw 데이터를 보면 H1/H2/H3 중 어느 것인지 즉시 판명.
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
    # ASCII only — cp949 콘솔 호환
    print("\n" + "=" * 78 + "\n " + s + "\n" + "=" * 78)


def main():
    if not odoo_service.is_available():
        ok = odoo_service.authenticate_odoo()
        if not ok:
            print("❌ Odoo 인증 실패")
            return

    # 1. SO 헤더 — commitment_date 확인
    banner("1. SO S00009 헤더")
    so_ids = odoo_service.call("sale.order", "search", [("name", "=", "S00009")])
    if not so_ids:
        print("S00009 없음")
        return
    so = odoo_service.call("sale.order", "read", so_ids[:1],
                           fields=["name", "commitment_date", "state", "order_line"])[0]
    print(f"  commitment_date = {so.get('commitment_date')!r}")
    print(f"  state           = {so.get('state')!r}")

    # 2. 각 product 의 pending receipts — date 필터 ON vs OFF 비교
    for product_id, product_name in [(2, "USB SecureKey-100"),
                                      (3, "SecureGate Appliance G2"),
                                      (4, "SmartBox Edge Server")]:
        banner(f"2. product_id={product_id} ({product_name})")

        # OFF — 모든 pending incoming
        all_recs = odoo_service.get_pending_receipts(product_id, by_date_iso=None)
        print(f"  [필터 OFF] {len(all_recs)} 건:")
        for r in all_recs:
            print(f"      id={r.get('id')} qty={r.get('product_uom_qty')} "
                  f"date={r.get('date')!r} state={r.get('state')!r} "
                  f"picking={r.get('picking_id')}")

        # ON — commitment_date 까지만
        filt_recs = odoo_service.get_pending_receipts(product_id,
                                                      by_date_iso=so.get('commitment_date'))
        print(f"  [필터 ON  (date <= {so.get('commitment_date')!r})] {len(filt_recs)} 건:")
        for r in filt_recs:
            print(f"      id={r.get('id')} qty={r.get('product_uom_qty')} "
                  f"date={r.get('date')!r} state={r.get('state')!r}")

    # 3. picking_type 검증 — 우리 가설 H2
    banner("3. 'BC3 demo restock' picking 들의 picking_type 확인")
    pick_ids = odoo_service.call("stock.picking", "search",
                                  [("origin", "like", "BC3 demo restock")])
    print(f"  pickings: {pick_ids}")
    if pick_ids:
        picks = odoo_service.call("stock.picking", "read", pick_ids,
                                   fields=["name", "state", "picking_type_id",
                                           "scheduled_date", "origin"])
        for p in picks:
            pt = p.get("picking_type_id")
            print(f"  · {p.get('name')}: state={p.get('state')!r} "
                  f"scheduled_date={p.get('scheduled_date')!r} "
                  f"picking_type_id={pt!r} origin={p.get('origin')!r}")
        # picking_type code 직접 조회
        pt_ids = list({p["picking_type_id"][0] for p in picks
                       if isinstance(p.get("picking_type_id"), list)})
        if pt_ids:
            pts = odoo_service.call("stock.picking.type", "read", pt_ids,
                                     fields=["name", "code"])
            for pt in pts:
                print(f"  · type {pt.get('id')}: name={pt.get('name')!r} code={pt.get('code')!r}")


if __name__ == "__main__":
    main()
