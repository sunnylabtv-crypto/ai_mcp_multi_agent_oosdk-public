"""
BC3 — Odoo 에 Product Guide 의 storable 제품 3종 등록 + 초기 재고 부여.

근거: D:\\Dev\\projects\\archive\\ai_test_file\\Product_guide.txt §1 Hardware/Appliance.

  - USB SecureKey-100       : $60      / unit, avg stock 1000, lead 2w
  - SecureGate Appliance G2 : $115,000 / unit, avg stock 5,    lead 6w (★ 최위험)
  - SmartBox Edge Server    : $60,000  / unit, avg stock 8,    lead 4w

특성:
  · Idempotent — 이름으로 search 후 없으면 create, stock 도 register_stock_receipt
    가 merged/created 분기. 재실행 안전.
  · Odoo 19.2 의 storable 표현: type='consu' + is_storable=True
    (odoo_service.find_or_create_product 가 분기 처리.)
  · 초기 재고는 internal location (WH/재고, id 5) 에 stock.quant 로 직접 주입.

실행:
    python scripts/bc3_setup_storable_products.py

성공 시 stdout:
    [Setup] USB SecureKey-100        product_id=X  stock 0 -> 1000 (created)
    [Setup] SecureGate Appliance G2  product_id=Y  stock 0 ->    5 (created)
    [Setup] SmartBox Edge Server     product_id=Z  stock 0 ->    8 (created)
재실행 시:
    [Setup] USB SecureKey-100        product_id=X  stock 1000 -> 1000 (skip — already at target)
"""
import sys
from pathlib import Path
from typing import List, Dict, Any

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from mcp_server.services import odoo_service  # noqa: E402


STORABLE_PRODUCTS: List[Dict[str, Any]] = [
    {
        "name": "USB SecureKey-100",
        "price": 60.0,
        "target_stock": 1000,
        "note": "FIDO2/WebAuthn 2FA token, 2w lead time",
    },
    {
        "name": "SecureGate Appliance G2",
        "price": 115000.0,
        "target_stock": 5,
        "note": "1U rackmount firewall, 6w lead time (most-risk SKU)",
    },
    {
        "name": "SmartBox Edge Server",
        "price": 60000.0,
        "target_stock": 8,
        "note": "4U 24-bay on-prem storage, 4w lead time",
    },
]


def _current_qty(product_id: int) -> float:
    """internal location 합산 qty (qty_available 와 동일한 의미)."""
    quants = odoo_service.get_product_quants(product_id)
    return sum(q.get("quantity") or 0 for q in quants)


def setup_one(spec: Dict[str, Any]) -> Dict[str, Any]:
    name = spec["name"]
    target = spec["target_stock"]

    product_id = odoo_service.find_or_create_product(
        name=name,
        default_price=spec["price"],
        product_type="storable",
    )

    before = _current_qty(product_id)
    if before >= target:
        return {
            "name": name, "product_id": product_id,
            "before": before, "after": before,
            "action": f"skip - already at {before} (>= target {target})",
        }

    delta = target - before
    receipt = odoo_service.register_stock_receipt(product_id=product_id, qty=delta)
    after = _current_qty(product_id)
    return {
        "name": name, "product_id": product_id,
        "before": before, "after": after,
        "action": f"{receipt['method']} +{delta}",
    }


def main() -> None:
    print("=" * 72)
    print("BC3 — Setup storable products in Odoo")
    print("=" * 72)

    if not odoo_service.authenticate_odoo():
        print(f"FAIL: Odoo auth: {odoo_service.get_service_status()}")
        sys.exit(1)
    print(f"OK: Odoo connected (uid={odoo_service.get_service_status()['uid']})")

    results: List[Dict[str, Any]] = []
    for spec in STORABLE_PRODUCTS:
        try:
            r = setup_one(spec)
        except Exception as e:
            r = {"name": spec["name"], "error": str(e)[:200]}
        results.append(r)

    print("\n-- Results --")
    for r in results:
        if "error" in r:
            print(f"  FAIL: {r['name']:<28} {r['error']}")
        else:
            print(
                f"  OK:   {r['name']:<28} id={r['product_id']:<3} "
                f"stock {r['before']:>5} -> {r['after']:>5}  ({r['action']})"
            )

    print("\n-- Verify (is_storable + qty_available) --")
    for r in results:
        if "product_id" not in r:
            continue
        pids = odoo_service.call(
            "product.product", "read", [r["product_id"]],
            fields=["name", "type", "is_storable", "qty_available", "list_price"],
        )
        for p in pids:
            print(
                f"  {p['name']:<28} type={p['type']:<7} "
                f"is_storable={p['is_storable']!s:<5} "
                f"on_hand={p['qty_available']:>6}  price=${p['list_price']:,.2f}"
            )


if __name__ == "__main__":
    main()
