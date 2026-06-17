# scripts/bc3_create_test_sos.py
"""
VIP preempt 테스트용 — Standard tier SO 3건 신규 생성.

기본 분배 (사용자 지정, 2026-05-26):
    SO #1: SmartBox Pro 1TB     qty 20
    SO #2: Module X             qty 10
    SO #3: SmartBox Pro 1TB     qty 30

→ 모두 service 라인이라 stock.quant 무관 — dashboard 의 "타입=service" + 재고 컬럼 "—" 확인용.
   storable 라인 으로 VIP preempt 시연 원하시면 USB SecureKey-100 변형 별도 추가.

실행:
    python scripts/bc3_create_test_sos.py
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


# (product name, qty)
ORDERS = [
    ("SmartBox Pro 1TB", 20),
    ("Module X",          10),
    ("SmartBox Pro 1TB", 30),
]


def banner(s):
    print("\n" + "=" * 78 + "\n " + s + "\n" + "=" * 78)


def _normalize_create_id(raw):
    """Odoo 19.x: create 가 single dict 도 list [id] 형식으로 반환. int 로 정규화."""
    if isinstance(raw, list) and raw:
        return int(raw[0])
    return int(raw)


def main():
    if not odoo_service.is_available():
        odoo_service.authenticate_odoo()

    # 1. 상품 id lookup (이름 → id)
    banner("1. product 조회")
    needed_names = list({name for name, _ in ORDERS})
    prods = odoo_service.call(
        "product.product", "search_read",
        [("name", "in", needed_names)],
        fields=["id", "name", "list_price"],
    )
    name_to_prod = {p["name"]: p for p in prods}
    for name in needed_names:
        if name not in name_to_prod:
            print(f"  ❌ ERROR: {name!r} 못 찾음 — 카탈로그 확인 필요")
            return 1
        p = name_to_prod[name]
        print(f"  ✅ {p['name']:30s} id={p['id']:3d} list_price={p['list_price']}")

    # 2. Standard partner
    banner("2. Standard tier partner")
    cat_ids = odoo_service.call("res.partner.category", "search", [("name", "=", "Standard")])
    if not cat_ids:
        print("  ❌ 'Standard' category 못 찾음")
        return 1
    partner_ids = odoo_service.call(
        "res.partner", "search", [("category_id", "in", cat_ids)],
    )
    if not partner_ids:
        print("  ❌ Standard tier partner 없음")
        return 1
    partner_id = int(partner_ids[0])
    partner = odoo_service.call(
        "res.partner", "read", [partner_id], fields=["name"],
    )[0]
    print(f"  → partner_id={partner_id} ({partner['name']!r}) — 3건 모두 동일 partner")

    # 3. SO create + confirm
    banner(f"3. SO 3건 생성 + 확정")
    created = []
    for name, qty in ORDERS:
        p = name_to_prod[name]
        so_vals = {
            "partner_id": partner_id,
            "order_line": [(0, 0, {
                "product_id": p["id"],
                "product_uom_qty": qty,
                "price_unit": p["list_price"] or 0,
            })],
        }
        try:
            raw = odoo_service.call("sale.order", "create", [so_vals])
            so_id = _normalize_create_id(raw)
        except Exception as e:
            print(f"  ❌ create 실패 ({name} x{qty}): {e}")
            continue

        # confirm — draft → sale, picking 자동 생성
        # NOTE: args 는 [so_id] 그대로. [[so_id]] 로 한 단계 더 wrap 하면 Odoo 가
        # ids=[[id]] 로 받아 field cache hash 실패 ("unhashable type: list").
        try:
            odoo_service.call("sale.order", "action_confirm", [so_id])
        except Exception as e:
            print(f"  ⚠️ confirm 실패 (so_id={so_id}, {name} x{qty}): {e}")
            so = odoo_service.call(
                "sale.order", "read", [so_id],
                fields=["name", "state"],
            )[0]
            created.append((so_id, so["name"], name, qty, so["state"], None))
            continue

        # 읽기로 picking 확인
        so = odoo_service.call(
            "sale.order", "read", [so_id],
            fields=["name", "state", "picking_ids"],
        )[0]
        pick_ids = so.get("picking_ids") or []
        pick_label = ""
        if pick_ids:
            picks = odoo_service.call(
                "stock.picking", "read", pick_ids,
                fields=["name", "state"],
            )
            pick_label = " / ".join(f"{p['name']}({p['state']})" for p in picks)
        created.append((so_id, so["name"], name, qty, so["state"], pick_label))
        print(
            f"  ✅ {so['name']:8s}  {name:25s} qty={qty:3d}  "
            f"state={so['state']!r:10s}  picking: {pick_label or '(없음 — service 라인)'}"
        )

    # 4. 요약
    banner("4. 결과")
    print(f"  {len(created)} 건 처리:")
    for so_id, so_name, prod_name, qty, state, pick_label in created:
        print(f"    {so_name:8s}  {prod_name:25s} x{qty:3d}  state={state}  {pick_label or ''}")

    print(f"""
Dashboard: http://REDACTED_VM_IP:9601 → SO 재고 탭
  · 각 새 SO ({', '.join(c[1] for c in created)}) 조회로 4-state 확인
  · service 라인은 "타입=service" + 재고 컬럼이 "—" 로 표시되어야 정상
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
