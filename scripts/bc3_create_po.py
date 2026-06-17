# scripts/bc3_create_po.py
"""
PO 우회 — incoming stock.picking 직접 create (USB SecureKey-100 × 30).

Why
───
your-tenant.odoo.com (Odoo SaaS trial) 에는 'purchase' 모듈이 설치 안 됨 →
purchase.order 흐름 사용 불가. 대신 stock.picking (picking_type=incoming) 을
직접 create. 결과는 동일 — Odoo UI 의 재고관리 → 작업 → 입고 화면에 표시되고
"검증" 클릭으로 stock.quant 증가.

이 패턴은 scripts/bc3_reset_to_inbound_state.py 에서 이미 사용 중.
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
QTY = 30
VENDOR_NAME = "TechSupply Co"

# Odoo Online 상수 (bc3_reset_to_inbound_state.py 와 동일)
INCOMING_PICKING_TYPE_ID = 1   # "입고", source=Vendors, dest=WH/재고
SUPPLIER_LOCATION_ID = 1       # "Vendors"
INTERNAL_LOCATION_ID = 5       # "WH/재고"


def banner(s):
    print("\n" + "=" * 78 + "\n " + s + "\n" + "=" * 78)


def _normalize_id(raw):
    if isinstance(raw, list) and raw:
        return int(raw[0])
    return int(raw)


def main():
    if not odoo_service.is_available():
        odoo_service.authenticate_odoo()

    # 1. product + uom
    banner(f"1. product 조회: {PRODUCT_NAME!r}")
    pids = odoo_service.call("product.product", "search", [("name", "=", PRODUCT_NAME)])
    if not pids:
        print(f"❌ {PRODUCT_NAME} 못 찾음")
        return 1
    product_id = int(pids[0])
    prod = odoo_service.call(
        "product.product", "read", [product_id],
        fields=["name", "uom_id"],
    )[0]
    uom_id = prod["uom_id"][0] if isinstance(prod.get("uom_id"), list) else prod["uom_id"]
    print(f"  product_id={product_id} uom_id={uom_id}")

    # 2. vendor
    banner(f"2. vendor: {VENDOR_NAME!r}")
    vids = odoo_service.call("res.partner", "search", [("name", "=", VENDOR_NAME)])
    if not vids:
        print(f"❌ {VENDOR_NAME} 못 찾음")
        return 1
    vendor_id = int(vids[0])
    print(f"  vendor_id={vendor_id}")

    # 3. incoming picking create
    banner(f"3. incoming picking 생성 ({PRODUCT_NAME} × {QTY})")
    origin = f"BC3 manual restock — {PRODUCT_NAME} +{QTY}"
    try:
        raw = odoo_service.call("stock.picking", "create", [{
            "partner_id": vendor_id,
            "picking_type_id": INCOMING_PICKING_TYPE_ID,
            "location_id": SUPPLIER_LOCATION_ID,
            "location_dest_id": INTERNAL_LOCATION_ID,
            "origin": origin,
            # Odoo 19.2: stock.move 의 product_uom → uom_id 로 rename, name 필드 제거
            "move_ids": [(0, 0, {
                "product_id": product_id,
                "product_uom_qty": QTY,
                "uom_id": uom_id,
                "location_id": SUPPLIER_LOCATION_ID,
                "location_dest_id": INTERNAL_LOCATION_ID,
            })],
        }])
        picking_id = _normalize_id(raw)
    except Exception as e:
        print(f"❌ picking create 실패: {e}")
        return 1
    print(f"  ✅ picking_id={picking_id} 생성됨")

    # 4. action_confirm → state='assigned' (supplier 무한공급)
    banner("4. picking confirm")
    try:
        odoo_service.call("stock.picking", "action_confirm", [picking_id])
    except Exception as e:
        print(f"  ⚠️ action_confirm 실패: {e}")

    # 5. 결과 보고
    p = odoo_service.call(
        "stock.picking", "read", [picking_id],
        fields=["name", "state", "scheduled_date", "origin"],
    )[0]
    banner("5. 결과")
    print(f"  picking_name = {p['name']!r}")
    print(f"  state        = {p['state']!r}   ← 한국어: 준비(assigned)/대기중(waiting)")
    print(f"  scheduled    = {p['scheduled_date']!r}")
    print(f"  origin       = {p['origin']!r}")

    print(f"""
다음 단계 (사용자):
  1. Odoo UI → 재고관리 → 작업 → 입고 (또는 직접: WH/IN 검색)
  2. {p['name']!r} 클릭 (state=준비)
  3. 우상단 [검증] 버튼 → state='완료' + stock.quant USB +{QTY}
  4. 그 직후 "USB 30 trigger" 알려주세요 → Standard SO 들 (S00008/11/12/13 합 65) 의
     reserve 가 VIP 우선순위 정책에 따라 일부 채워짐.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
