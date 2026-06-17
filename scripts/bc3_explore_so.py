# scripts/bc3_explore_so.py
"""
임시 탐사 — S00006 (VIP) / S00007 (Standard) 의 SO line 구조 dump.

목적:
  · BC3 e2e flow 를 짜기 전에 Odoo 실 데이터의 모양 파악.
  · product.template.type (service / consu / product) 으로 storable / service 분기 가능한가?
  · target_delivery_date / commitment_date / scheduled_date 중 무엇이 채워져 있나?
  · partner 의 category (VIP/Standard) 이 res.partner.category_id 에 들어가 있나?

실행:
    python scripts/bc3_explore_so.py
"""
import json
import os
import sys
from pathlib import Path
from pprint import pformat

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from mcp_server.services import odoo_service  # noqa: E402


def banner(s: str) -> None:
    print("\n" + "═" * 78)
    print(f" {s}")
    print("═" * 78)


def explore_so_by_name(so_name: str) -> None:
    banner(f"SO: {so_name}")
    if not odoo_service.is_available():
        ok = odoo_service.authenticate_odoo()
        if not ok:
            print(f"❌ Odoo 인증 실패: {odoo_service.get_service_status()}")
            return

    # 1) name 으로 SO 검색
    so_ids = odoo_service.call("sale.order", "search", [("name", "=", so_name)])
    if not so_ids:
        print(f"❌ {so_name} 못 찾음")
        return
    so_id = so_ids[0]
    print(f"  so_id = {so_id}")

    # 2) SO 헤더 read
    so = odoo_service.call(
        "sale.order", "read", [so_id],
        fields=[
            "name", "state", "partner_id", "amount_total", "currency_id",
            "date_order", "commitment_date", "validity_date",
            "order_line", "company_id", "team_id",
        ],
    )[0]
    print("\n── SO header ──")
    print(pformat({k: v for k, v in so.items() if k != "order_line"}, width=120))

    # 3) partner → category
    partner_field = so.get("partner_id")
    partner_id = partner_field[0] if isinstance(partner_field, list) else partner_field
    if partner_id:
        partner = odoo_service.call(
            "res.partner", "read", [partner_id],
            fields=["name", "category_id", "commercial_partner_id"],
        )[0]
        print("\n── partner ──")
        print(pformat(partner, width=120))
        cat_ids = partner.get("category_id") or []
        if cat_ids:
            cats = odoo_service.call(
                "res.partner.category", "read", cat_ids, fields=["name"],
            )
            print(f"  category names: {[c.get('name') for c in cats]}")

    # 4) SO lines
    line_ids = so.get("order_line") or []
    if not line_ids:
        print("\n  (라인 없음)")
        return
    lines = odoo_service.call(
        "sale.order.line", "read", line_ids,
        fields=[
            "name", "product_id", "product_uom_qty", "price_unit", "price_subtotal",
            "product_template_id",
        ],
    )
    print(f"\n── SO lines ({len(lines)}건) ──")
    # 각 라인의 product → product.template 으로 type 조회
    template_ids = []
    for ln in lines:
        tpl_field = ln.get("product_template_id")
        if isinstance(tpl_field, list) and tpl_field:
            template_ids.append(tpl_field[0])
    template_ids = list(set(template_ids))
    tpl_info = {}
    if template_ids:
        tpls = odoo_service.call(
            "product.template", "read", template_ids,
            fields=["name", "type", "sale_ok", "purchase_ok"],
        )
        for t in tpls:
            tpl_info[t.get("id")] = t

    for idx, ln in enumerate(lines, 1):
        tpl_field = ln.get("product_template_id")
        tpl_id = tpl_field[0] if isinstance(tpl_field, list) else tpl_field
        tpl = tpl_info.get(tpl_id, {})
        print(
            f"  [{idx}] {ln.get('name')!r}\n"
            f"       qty={ln.get('product_uom_qty')} × {ln.get('price_unit')} = {ln.get('price_subtotal')}\n"
            f"       product_id={ln.get('product_id')}  template_id={tpl_id}\n"
            f"       template.type={tpl.get('type')!r}"
        )

    # 5) picking (delivery) 자동 생성 여부
    pickings = odoo_service.list_pickings_for_order(so_id)
    print(f"\n── delivery picking ({len(pickings)}건) ──")
    for p in pickings:
        print(f"  {p}")


def main() -> None:
    for so_name in ("S00006", "S00007"):
        explore_so_by_name(so_name)


if __name__ == "__main__":
    main()
