"""
BC3 사전점검 — Odoo 19.2 의 storable 표현 방식 확인.

Odoo 19 에서 product.template.type 가 ['consu','service','combo'] 로 축소되며
storable 여부는 별도 boolean (`is_storable` 등) 로 분리됨.

확인 사항:
  1. product.template 의 is_storable / tracking / storable 관련 필드
  2. 기존 'Module X' template 의 실제 필드값
  3. stock.location 안전 query (빈 domain 회피)
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


def main() -> None:
    if not odoo_service.authenticate_odoo():
        print(f"FAIL: {odoo_service.get_service_status()}")
        sys.exit(1)
    print("OK: Odoo auth")

    print("\n-- product.template 의 storable 관련 후보 필드 --")
    all_fields = odoo_service.call("product.template", "fields_get", [])
    candidates = [
        name for name in all_fields
        if any(k in name.lower() for k in ("storab", "track", "stock", "qty_av", "qty_on"))
    ]
    for name in sorted(candidates):
        f = all_fields[name]
        print(f"  {name:<30} type={f.get('type'):<10} string={f.get('string')!r}")

    print("\n-- 'Module X' template 의 실제 값 (storable 관련) --")
    tpl_ids = odoo_service.call("product.template", "search", [("name", "=", "Module X")])
    if tpl_ids:
        peek = ["name", "type"] + [c for c in candidates if c in all_fields][:8]
        tpls = odoo_service.call("product.template", "read", tpl_ids, fields=peek)
        for t in tpls:
            print(f"  {t}")

    print("\n-- stock.location (internal) --")
    try:
        loc_ids = odoo_service.call(
            "stock.location", "search", [("usage", "=", "internal")], limit=10,
        )
        locs = odoo_service.call(
            "stock.location", "read", loc_ids,
            fields=["id", "name", "complete_name", "usage"],
        )
        for loc in locs:
            print(f"  {loc}")
    except Exception as e:
        print(f"  FAIL: {str(e)[:200]}")

    print("\n-- storable 제품 (is_storable=True 가정) 카운트 --")
    if "is_storable" in all_fields:
        cnt = odoo_service.call(
            "product.template", "search_count", [("is_storable", "=", True)],
        )
        print(f"  is_storable=True : {cnt} templates")
    else:
        print("  is_storable 필드 자체가 없음")


if __name__ == "__main__":
    main()
