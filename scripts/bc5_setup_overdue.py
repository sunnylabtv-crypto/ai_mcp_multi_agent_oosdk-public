# scripts/bc5_setup_overdue.py
"""BC5 수금 시연용 상태 셋업 — 전부 배송/청구 + 일부 연체 + 1건 입금완료(대조군).

목표:
  · A(Std, S00052) USB 출고+청구 → 20일 연체
  · B(Std, S00053) USB 출고+청구 → 40일 연체 (상습)
  · C(VIP, S00054) 청구 → 입금 완료(paid)  ← 대조군
  · D(VIP, S00055) 기존 INV → 10일 연체 (큰 금액)
→ 연령별 미수금(Aged Receivable)에 A/B/D 연체, C는 회수됨.

연체 = 청구서 invoice_date_due 를 과거로 write.

실행: python scripts/bc5_setup_overdue.py            # 실제 셋업
      python scripts/bc5_setup_overdue.py --dry      # 현재 상태만
"""
import sys, datetime
from pathlib import Path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass
from mcp_server.services import odoo_service as o  # noqa: E402

TODAY = datetime.date.today()
# so_name -> (label, tier, overdue_days, pay)
PLAN = {
    "S00052": ("A", "Std", 20, False),
    "S00053": ("B", "Std", 40, False),
    "S00054": ("C", "VIP", 0,  True),
    "S00055": ("D", "VIP", 10, False),
}


def banner(s):
    print("\n" + "=" * 72 + "\n " + s + "\n" + "=" * 72)


def so_id(name):
    r = o.call("sale.order", "search_read", [("name", "=", name)], fields=["id"])
    return r[0]["id"] if r else None


def open_usb_picking(name):
    pks = o.call("stock.picking", "search_read",
                 [("origin", "=", name), ("picking_type_id.code", "=", "outgoing"),
                  ("state", "in", ["confirmed", "waiting", "assigned", "partially_available"])],
                 fields=["name", "state"], order="id")
    for p in pks:
        mv = o.call("stock.move", "search_read",
                    [("picking_id", "=", p["id"]), ("product_id", "=", 2)],
                    fields=["id", "product_uom_qty", "quantity"])
        if mv:
            return p, mv[0]
    return None, None


def invoices_for(name):
    return o.call("account.move", "search",
                  [("move_type", "=", "out_invoice"), ("invoice_origin", "=", name),
                   ("state", "!=", "cancel")])


def show():
    banner("현재 청구서 / 미수")
    invs = o.call("account.move", "search_read", [("move_type", "=", "out_invoice")],
                  fields=["name", "partner_id", "invoice_date_due", "amount_residual", "payment_state"])
    for i in invs:
        pn = i["partner_id"][1] if isinstance(i.get("partner_id"), list) else ""
        print(f"  {i['name']} {pn} 만기={i.get('invoice_date_due')} 미수={i['amount_residual']} {i['payment_state']}")
    for pid, nm in [(11, "A"), (12, "B"), (13, "C"), (14, "D")]:
        pass
    for nm in ["Customer A", "Customer B", "Customer C", "Customer D"]:
        p = o.call("res.partner", "search_read", [("name", "=", nm)], fields=["total_due", "total_overdue"])
        if p:
            print(f"  {nm}: 미수합 {p[0]['total_due']} / 연체 {p[0]['total_overdue']}")


def main():
    dry = "--dry" in sys.argv
    if not o.is_available():
        o.authenticate_odoo()
    if dry:
        show(); return

    # 1) A·B USB 출고 (재고 보충 + 예약 + 검증)
    banner("1) A·B USB 출고 (다 배송)")
    need = 0
    for so in ["S00052", "S00053"]:
        _, m = open_usb_picking(so)
        if m:
            need += (m["product_uom_qty"] or 0) - (m["quantity"] or 0)
    inv = o.get_inventory_state(2)
    add = need - (inv.get("available") or 0)
    if add > 0:
        r = o.register_stock_receipt(2, add)
        print(f"  재고 +{add}: {r.get('new_total')}")
    for so in ["S00052", "S00053"]:
        p, m = open_usb_picking(so)
        if not p:
            print(f"  {so}: 열린 picking 없음 (이미 출고?)"); continue
        o.reserve_move(m["id"])
        res = o.validate_picking(p["id"])
        print(f"  {so} {p['name']} 검증 → validated={res.get('validated')} state={res.get('state')}")

    # 2) A·B·C 청구 (D는 기존 INV 사용)
    banner("2) 청구서 발행 (A·B·C)")
    for so in ["S00052", "S00053", "S00054"]:
        r = o.create_invoice_for_sale_order(so_id(so), post=True)
        print(f"  {so}: created={r.get('created')} {[i.get('name') for i in (r.get('invoices') or [])] or r.get('reason') or r.get('error')}")

    # 3) 연체 처리 (만기일 과거로) + C 입금
    banner("3) 연체 만기일 backdate + C 입금")
    for so, (label, tier, odays, pay) in PLAN.items():
        for iid in invoices_for(so):
            if pay:
                pr = o.register_invoice_payment(iid)
                print(f"  {so}({label}) 입금: {pr.get('after',{}).get('payment_state')}")
            elif odays > 0:
                due = (TODAY - datetime.timedelta(days=odays)).isoformat()
                try:
                    o.call("account.move", "write", [iid], {"invoice_date_due": due})
                    print(f"  {so}({label}) 만기→{due} ({odays}일 연체)")
                except Exception as e:
                    print(f"  ⚠️ {so} 만기 write 실패: {str(e)[:80]}")

    show()
    banner("다음")
    print("  Odoo 회계 → 보고 → 연령별 미수금 / 고객 청구서(연체) 에서 확인")


if __name__ == "__main__":
    main()
