# scripts/bc3_create_products_from_guide.py
"""
Product_guide.txt 기준으로 누락된 product 들을 Odoo 에 일괄 등록.

이미 같은 이름 product 가 있으면 skip (idempotent). 새 product 만 create.

Source: d:/Dev/projects/archive/ai_test_file/Product_guide.txt
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


# Product_guide.txt §1 매핑 — Odoo 19.x 에서 type 은 'service' / 'consu' / 'combo'
# storable = type='consu' + is_storable=True
PRODUCTS = [
    # (name, type, is_storable, list_price, note)
    ("SmartBox Pro 1TB",         "service", False,    9.99, "Cloud Storage 1TB (월요금)"),
    ("SmartBox Pro 5TB",         "service", False,   29.99, "Cloud Storage 5TB (월요금)"),
    ("SmartBox Pro Unlimited",   "service", False,   99.00, "Cloud Storage Unlimited (월요금)"),
    ("SmartBox Lite 100GB",      "service", False,    2.99, "Cloud Storage Lite 100GB (월요금)"),
    ("SmartBox Lite 500GB",      "service", False,    5.99, "Cloud Storage Lite 500GB (월요금)"),
    ("SecureGate Software",      "service", False,    0.00, "VPN/Firewall SW — 견적 협의"),
    ("Onboarding Consulting 2w", "service", False, 25000.00, "Onboarding 2주 패키지"),
    ("Onboarding Consulting 12w","service", False,180000.00, "Onboarding 12주 패키지"),
]


def banner(s):
    print("\n" + "=" * 78 + "\n " + s + "\n" + "=" * 78)


def main():
    if not odoo_service.is_available():
        odoo_service.authenticate_odoo()

    banner("기존 product 조회 (sale_ok=True)")
    existing = odoo_service.call(
        "product.product", "search_read",
        [("sale_ok", "=", True)],
        fields=["id", "name", "type", "is_storable", "list_price"],
    )
    existing_names = {p["name"] for p in existing}
    print(f"  {len(existing)} 개 존재:")
    for p in existing:
        print(f"    id={p['id']:3d}  {p['name']}")

    banner("Product_guide.txt 기준 신규 등록 (idempotent)")
    created = []
    skipped = []
    for name, ptype, is_storable, price, note in PRODUCTS:
        if name in existing_names:
            skipped.append(name)
            print(f"  skip  : {name} (이미 존재)")
            continue
        vals = {
            "name": name,
            "type": ptype,
            "is_storable": is_storable,
            "list_price": price,
            "sale_ok": True,
            "purchase_ok": False if ptype == "service" else True,
        }
        try:
            raw = odoo_service.call("product.product", "create", [vals])
            # Odoo 19.x: create 가 list [id] 형식으로 반환 → int normalize
            pid = raw[0] if isinstance(raw, list) and raw else int(raw)
            created.append((pid, name, price))
            print(f"  ✅ {name:30s} id={pid:3d} price={price:>9.2f}  ({note})")
        except Exception as e:
            print(f"  ❌ {name}: {e}")

    banner("결과 요약")
    print(f"  생성: {len(created)} 건 / skip: {len(skipped)} 건")
    if created:
        print("\n  ── 새 product 들 ──")
        for pid, name, price in created:
            print(f"    id={pid:3d}  {name:30s}  ${price:>9.2f}")
    if skipped:
        print("\n  ── skip ──")
        for name in skipped:
            print(f"    {name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
