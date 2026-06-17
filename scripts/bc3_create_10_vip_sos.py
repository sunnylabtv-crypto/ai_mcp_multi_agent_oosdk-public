# scripts/bc3_create_10_vip_sos.py
"""
VIP SO 10건 일괄 생성 — USB SecureKey-100, qty 10/20/.../100 (incremental).

목적:
    VIP FIFO 정책 검증 — 같은 tier (VIP) 안에서 sale_id 순서 (= 생성 시간) 로
    reserve 받는지 확인. 또한 VIP 큐 깊을 때 ontology engine 의 분배 동작 시연.

분배 시나리오 (현재 USB 가용 0 + 신규 PO 시):
    예: +500 입고 시
      · 기존 VIP S00009 (이미 1200/1200) — 변화 없음
      · 신규 VIP S00014 (qty 10): 가용 10 흡수 → 충족 (남은 490)
      · 신규 VIP S00015 (qty 20): 가용 20 흡수 → 충족 (남은 470)
      ...
      · 부분 충족 또는 일부 waiting 까지 진행
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


PRODUCT_NAME = "USB SecureKey-100"
QUANTITIES = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]  # 총 550
VIP_PARTNER_NAME = "VIP Tech"


def banner(s):
    print("\n" + "=" * 78 + "\n " + s + "\n" + "=" * 78)


def _normalize_id(raw):
    if isinstance(raw, list) and raw:
        return int(raw[0])
    return int(raw)


def main():
    if not odoo_service.is_available():
        odoo_service.authenticate_odoo()

    # product
    pids = odoo_service.call("product.product", "search", [("name", "=", PRODUCT_NAME)])
    product_id = int(pids[0])
    prod = odoo_service.call(
        "product.product", "read", [product_id],
        fields=["name", "list_price"],
    )[0]
    list_price = prod.get("list_price", 0) or 0
    print(f"product: {prod['name']} (id={product_id}, list_price={list_price})")

    # VIP partner
    vids = odoo_service.call("res.partner", "search", [("name", "=", VIP_PARTNER_NAME)])
    if not vids:
        print(f"❌ {VIP_PARTNER_NAME} 못 찾음")
        return 1
    partner_id = int(vids[0])
    print(f"partner: {VIP_PARTNER_NAME} (id={partner_id})")

    # 10건 create + confirm
    banner(f"VIP SO 10건 생성 + confirm (qty: {QUANTITIES}, 총 {sum(QUANTITIES)})")
    created = []
    for qty in QUANTITIES:
        so_vals = {
            "partner_id": partner_id,
            "order_line": [(0, 0, {
                "product_id": product_id,
                "product_uom_qty": qty,
                "price_unit": list_price,
            })],
        }
        try:
            raw = odoo_service.call("sale.order", "create", [so_vals])
            so_id = _normalize_id(raw)
            odoo_service.call("sale.order", "action_confirm", [so_id])
            so = odoo_service.call(
                "sale.order", "read", [so_id],
                fields=["name", "state", "picking_ids"],
            )[0]
            pick_label = ""
            pids_list = so.get("picking_ids") or []
            if pids_list:
                picks = odoo_service.call(
                    "stock.picking", "read", pids_list,
                    fields=["name", "state"],
                )
                pick_label = ", ".join(f"{p['name']}({p['state']})" for p in picks)
            created.append((so_id, so["name"], qty, so["state"], pick_label))
            print(
                f"  ✅ {so['name']:8s}  USB x{qty:3d}  state={so['state']:6s}  picking: {pick_label or '(없음)'}"
            )
        except Exception as e:
            print(f"  ❌ qty={qty} 실패: {e}")
            created.append((None, "?", qty, "FAILED", str(e)))

    banner("결과")
    success = sum(1 for c in created if c[0] is not None)
    print(f"  성공: {success} / {len(QUANTITIES)}")
    total_qty = sum(c[2] for c in created if c[0] is not None)
    print(f"  총 USB demand 추가: {total_qty}")

    # 현재 USB 상태
    inv = odoo_service.get_inventory_state(product_id)
    print(f"\n  현재 USB on_hand={inv['on_hand']:.0f} reserved={inv['reserved']:.0f} available={inv['available']:.0f}")

    print(f"""
이제 VIP 큐 depth:
  · S00009 (VIP, 기존)   demand=1200, reserved=1200 (이미 fully assigned)
  · {', '.join(c[1] for c in created if c[0] is not None)} (VIP, 신규) demand=10..100 (총 550)
  · S00008/11/12/13 (Standard) — 일부 partial 또는 waiting

새 입고 발생 시 ontology engine 의 sort:
   tier (VIP > Standard)  →  sale_id FIFO (= 생성 순서)
   → 신규 VIP 들이 모든 Standard 보다 먼저 흡수.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
