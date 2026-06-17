# scripts/bc3_add_usb_to_test_sos.py
"""
S00011 / S00012 / S00013 각 SO 에 USB SecureKey-100 라인을 동일 qty 로 추가.

목적:
    기존 3 SO 는 service 라인만 있어 picking 자동 생성 X → VIP preempt 시연 데이터 부족.
    storable USB 라인 추가하면 Odoo 가 새 outgoing picking 생성 → 전체 Standard 큐 풍성.

결과 시나리오 (USB SecureKey-100):
    가용 = 1,000 units
    수요 = VIP S00009 1,200  +  Standard S00008 5  +  S00011 20  +  S00012 10  +  S00013 30
         = 1,265 units → 265 short
    trigger_stock_received 발화 시: VIP 가 1,000 다 가져감, Standard 65 = 모두 waiting.
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


# SO 이름 → USB 추가 qty
TARGETS = [
    ("S00011", 20),
    ("S00012", 10),
    ("S00013", 30),
]


def banner(s):
    print("\n" + "=" * 78 + "\n " + s + "\n" + "=" * 78)


def _normalize_id(raw):
    if isinstance(raw, list) and raw:
        return int(raw[0])
    return int(raw)


def main():
    if not odoo_service.is_available():
        odoo_service.authenticate_odoo()

    # 1. USB SecureKey-100 id 조회
    banner("1. USB SecureKey-100 조회")
    pids = odoo_service.call(
        "product.product", "search", [("name", "=", "USB SecureKey-100")],
    )
    if not pids:
        print("  ❌ USB SecureKey-100 못 찾음")
        return 1
    usb_id = int(pids[0])
    usb = odoo_service.call(
        "product.product", "read", [usb_id],
        fields=["name", "list_price"],
    )[0]
    print(f"  product_id={usb_id} list_price={usb['list_price']}")

    # 2. 각 SO 에 라인 추가
    banner("2. 각 SO 에 USB 라인 추가")
    results = []
    for so_name, qty in TARGETS:
        # SO id 찾기
        so_ids = odoo_service.call(
            "sale.order", "search", [("name", "=", so_name)],
        )
        if not so_ids:
            print(f"  ❌ {so_name} 못 찾음")
            results.append((so_name, qty, None, "SO 없음"))
            continue
        so_id = int(so_ids[0])

        # 라인 create — sale.order.line 에 직접 create
        line_vals = {
            "order_id": so_id,
            "product_id": usb_id,
            "product_uom_qty": qty,
            "price_unit": usb["list_price"] or 0,
        }
        try:
            raw = odoo_service.call("sale.order.line", "create", [line_vals])
            line_id = _normalize_id(raw)
        except Exception as e:
            print(f"  ❌ {so_name} 라인 추가 실패: {e}")
            results.append((so_name, qty, so_id, f"line create 실패: {e}"))
            continue

        # SO 다시 읽어서 picking 확인
        so = odoo_service.call(
            "sale.order", "read", [so_id],
            fields=["name", "state", "picking_ids", "amount_total"],
        )[0]
        pick_ids = so.get("picking_ids") or []
        pick_label = ""
        if pick_ids:
            picks = odoo_service.call(
                "stock.picking", "read", pick_ids,
                fields=["name", "state"],
            )
            pick_label = " / ".join(f"{p['name']}({p['state']})" for p in picks)
        results.append((so_name, qty, so_id, f"line_id={line_id}, picking: {pick_label or '(없음)'}"))
        print(
            f"  ✅ {so_name}  USB x{qty:3d} 추가  line_id={line_id}  picking: {pick_label or '(없음)'}"
        )

    # 3. 요약
    banner("3. 결과")
    print(f"  {len(results)} 건 처리")
    for so_name, qty, so_id, note in results:
        print(f"    {so_name} (id={so_id})  USB x{qty}  → {note}")

    print(f"""
VIP preempt 시연 시나리오 (USB SecureKey-100):
    가용 = 1,000 (validate 끝)
    수요 = VIP S00009 1,200 + Standard S00008 5 + S00011 20 + S00012 10 + S00013 30 = 1,265
    265 short → VIP 정책 따라 VIP 1000 우선, Standard 65 모두 waiting

다음:
    Dashboard SO 재고 탭에서 S00011/12/13 조회 → USB 라인 (재고/가용/Assigned) 확인.
    trigger_stock_received MCP tool 호출 (Claude Desktop) → VIP 우선 reserve 검증.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
