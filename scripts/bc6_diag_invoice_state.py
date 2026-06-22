# scripts/bc6_diag_invoice_state.py
"""출고 <-> 청구 연결 진단 (read-only).

확인 항목:
  1. USB 제품의 invoice_policy ('order'=주문기준 / 'delivery'=인도기준)
  2. account.move(Invoicing 앱) 접근 가능 여부 + 기존 청구서
  3. 각 SO 의 invoice_status / invoice_count / delivered qty
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

    banner("1. USB 제품 invoice_policy")
    prods = odoo_service.call(
        "product.product", "search_read",
        [("id", "=", 2)],
        fields=["name", "invoice_policy", "type"],
    )
    for p in prods:
        print(f"  · {p.get('name')!r}: invoice_policy={p.get('invoice_policy')!r} type={p.get('type')!r}")

    banner("2. account.move (Invoicing 앱) 접근 가능 여부")
    try:
        cnt = odoo_service.call("account.move", "search_count",
                                [("move_type", "=", "out_invoice")])
        print(f"  · account.move 접근 OK — 기존 고객청구서(out_invoice) {cnt}건")
    except Exception as e:
        print(f"  · account.move 접근 실패(앱 미설치 가능): {e}")

    banner("3. SO 별 invoice_status / invoice_count / 인도수량")
    sos = odoo_service.call(
        "sale.order", "search_read",
        [("name", "in", ["S00048", "S00049", "S00050", "S00051"])],
        fields=["name", "partner_id", "invoice_status", "invoice_count", "amount_total"],
        order="name",
    )
    for so in sos:
        partner = so.get("partner_id")
        pname = partner[1] if isinstance(partner, list) else partner
        lines = odoo_service.call(
            "sale.order.line", "search_read",
            [("order_id", "=", so["id"])],
            fields=["product_id", "product_uom_qty", "qty_delivered", "qty_invoiced"],
        )
        print(f"\n  · {so.get('name')} ({pname}): invoice_status={so.get('invoice_status')!r} "
              f"invoice_count={so.get('invoice_count')} total={so.get('amount_total')}")
        for ln in lines:
            prod = ln.get("product_id")
            prod_name = prod[1] if isinstance(prod, list) else prod
            print(f"      {prod_name!r}: ordered={ln.get('product_uom_qty')} "
                  f"delivered={ln.get('qty_delivered')} invoiced={ln.get('qty_invoiced')}")


if __name__ == "__main__":
    main()
