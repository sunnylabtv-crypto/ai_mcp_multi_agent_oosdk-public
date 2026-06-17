"""
BC3 — 데모 데이터 reset to "pre-cutoff" state.

목적 (사용자 요구):
  · 발화 전에는 두 outgoing picking (S00008/9) 모두 'waiting' state
  · 재고는 'On Hand' 가 아니라 **Incoming** (PO 입고 대기 중) 상태로
  · cutoff 발화 시점에 입고 확정 + tier 우선순위 할당이 동시에 일어나도록

흐름:
  1. 기존 storable 3종 (USB/Appliance/Edge) stock.quant qty=0 (보유 비우기)
  2. 기존 outgoing picking 의 reserve 풀기 (do_unreserve)
  3. vendor partner 'TechSupply Co' find or create
  4. 3개 PO create (USB ×1000, Appliance G2 ×5, Edge Server ×8) + button_confirm
  5. 자동 생성된 incoming picking 의 state 확인

결과:
  Odoo UI Inventory > Operations 에서:
    · Incoming Transfers: 3건 (state='Ready' 또는 'Waiting')
    · Outgoing Transfers: 2건 (state='Waiting')
  Inventory > Products 에서:
    · USB On Hand 0, Incoming 1000
    · Appliance G2 On Hand 0, Incoming 5
    · Edge Server On Hand 0, Incoming 8

실행:
    python scripts/bc3_reset_to_inbound_state.py
"""
import sys
from pathlib import Path
from typing import Dict, Any, List

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from mcp_server.services import odoo_service  # noqa: E402


STORABLE_PRODUCTS = [
    {"name": "USB SecureKey-100",       "qty": 1000, "price": 60.00},
    {"name": "SecureGate Appliance G2", "qty":    5, "price": 115000.00},
    {"name": "SmartBox Edge Server",    "qty":    8, "price": 60000.00},
]
VENDOR_NAME = "TechSupply Co"


def reset_quants():
    """기존 storable 3종 quant qty=0 write (보유 비우기)."""
    print("\n-- 1. 기존 stock.quant 비우기 --")
    for spec in STORABLE_PRODUCTS:
        pids = odoo_service.call("product.product", "search", [("name", "=", spec["name"])])
        if not pids:
            print(f"  WARN: product '{spec['name']}' 없음")
            continue
        product_id = pids[0]
        qids = odoo_service.call(
            "stock.quant", "search",
            [("product_id", "=", product_id), ("location_id.usage", "=", "internal")],
        )
        if not qids:
            print(f"  {spec['name']:<28} quant 없음 (이미 비어있음)")
            continue
        for qid in qids:
            try:
                odoo_service.call("stock.quant", "write", [qid], {"quantity": 0})
            except Exception as e:
                print(f"  WARN: quant {qid} write 실패: {str(e)[:120]}")
        # verify
        inv = odoo_service.get_inventory_state(product_id)
        print(f"  {spec['name']:<28} on_hand={inv['on_hand']:>6g} (target 0)")


def unreserve_outgoing():
    """기존 outgoing picking 의 reserve 풀기 (혹시 남아있다면)."""
    print("\n-- 2. outgoing picking 들의 reserved 풀기 --")
    for so_id in (8, 9):
        pids = odoo_service.call("stock.picking", "search", [("sale_id", "=", so_id)])
        for pid in pids:
            try:
                odoo_service.call("stock.picking", "do_unreserve", [pid])
                print(f"  do_unreserve picking {pid} (SO {so_id}) OK")
            except Exception as e:
                print(f"  do_unreserve picking {pid} WARN: {str(e)[:120]}")


def ensure_vendor() -> int:
    """vendor partner find or create."""
    print(f"\n-- 3. vendor '{VENDOR_NAME}' --")
    pids = odoo_service.call("res.partner", "search", [("name", "=", VENDOR_NAME)])
    if pids:
        print(f"  exists id={pids[0]}")
        return pids[0]
    pid = odoo_service.call("res.partner", "create", {
        "name": VENDOR_NAME,
        "is_company": True,
        "supplier_rank": 1,
        "email": f"orders@techsupplyco.com",
        "comment": "BC3 demo vendor — storable products supplier",
    })
    print(f"  CREATED id={pid}")
    return pid


# Odoo SaaS trial 에 Purchase 모듈이 없어서 purchase.order 흐름 못 씀.
# 대신 stock.picking (incoming) 을 직접 create — 결과적으로 Odoo UI 의 Incoming
# Transfers 에 표시되고 forecast_availability 가 incoming 으로 잡힘.
INCOMING_PICKING_TYPE_ID = 1   # "입고", source=Vendors, dest=WH/재고
SUPPLIER_LOCATION_ID = 1       # "Vendors"
INTERNAL_LOCATION_ID = 5       # "WH/재고"


def find_or_create_incoming_picking(vendor_id: int, spec: Dict[str, Any]) -> Dict[str, Any]:
    """제품별 incoming picking find (open state) or create + confirm. 멱등성."""
    pids = odoo_service.call("product.product", "search", [("name", "=", spec["name"])])
    if not pids:
        return {"name": spec["name"], "error": "product 없음"}
    product_id = pids[0]
    prod = odoo_service.call(
        "product.product", "read", [product_id], fields=["uom_id"],
    )[0]
    uom_id = prod["uom_id"][0] if isinstance(prod.get("uom_id"), list) else prod.get("uom_id")

    origin = f"BC3 demo restock — {spec['name']}"

    # 기존 open incoming picking 이 같은 origin 으로 있는지 — 멱등성
    existing = odoo_service.call(
        "stock.picking", "search",
        [
            ("picking_type_id", "=", INCOMING_PICKING_TYPE_ID),
            ("origin", "=", origin),
            ("state", "in", ["draft", "waiting", "confirmed", "assigned"]),
        ],
        limit=1,
    )
    if existing:
        p = odoo_service.get_picking(existing[0])
        return {
            "name": spec["name"], "picking_id": existing[0],
            "picking_name": p.get("name") if p else "?",
            "state": p.get("state") if p else "?",
            "method": "exists",
        }

    # create — picking + nested move 한 번에
    picking_id = odoo_service.call("stock.picking", "create", {
        "partner_id": vendor_id,
        "picking_type_id": INCOMING_PICKING_TYPE_ID,
        "location_id": SUPPLIER_LOCATION_ID,
        "location_dest_id": INTERNAL_LOCATION_ID,
        "origin": origin,
        # Odoo 19.2: stock.move.product_uom → uom_id 로 rename, name 필드 제거됨
        "move_ids": [(0, 0, {
            "product_id": product_id,
            "product_uom_qty": spec["qty"],
            "uom_id": uom_id,
            "location_id": SUPPLIER_LOCATION_ID,
            "location_dest_id": INTERNAL_LOCATION_ID,
        })],
    })
    # confirm (state → assigned, since supplier source 는 무한공급)
    try:
        odoo_service.call("stock.picking", "action_confirm", [picking_id])
    except Exception as e:
        return {
            "name": spec["name"], "picking_id": picking_id,
            "method": "created (confirm 실패)",
            "error": f"action_confirm: {type(e).__name__}: {str(e)[:200]}",
        }
    p = odoo_service.get_picking(picking_id)
    return {
        "name": spec["name"], "picking_id": picking_id,
        "picking_name": p.get("name") if p else "?",
        "state": p.get("state") if p else "?",
        "method": "created+confirmed",
    }


def dump_incoming_pickings(results: List[Dict[str, Any]]):
    """incoming picking 상태 dump (PO 우회 — 직접 생성)."""
    print("\n-- 5. incoming picking 상태 --")
    for r in results:
        if "error" in r:
            print(f"  FAIL: {r['name']:<28} {r['error']}")
            continue
        print(
            f"  {r['name']:<28} picking={r.get('picking_name','?'):<14} "
            f"state={r.get('state','?'):<10} ({r['method']})"
        )


def dump_final_inventory():
    """최종 inventory 상태 (on_hand vs forecasted) dump."""
    print("\n-- 6. 최종 inventory state --")
    for spec in STORABLE_PRODUCTS:
        pids = odoo_service.call("product.product", "search", [("name", "=", spec["name"])])
        if not pids:
            continue
        inv = odoo_service.get_inventory_state(pids[0])
        # forecast = on_hand + incoming - outgoing (대략)
        # qty_available + virtual_available 추가 read
        prod = odoo_service.call(
            "product.product", "read", [pids[0]],
            fields=["name", "qty_available", "virtual_available", "incoming_qty", "outgoing_qty"],
        )[0]
        print(
            f"  {spec['name']:<28} "
            f"On Hand={prod['qty_available']:>6g}  "
            f"Incoming={prod['incoming_qty']:>6g}  "
            f"Outgoing={prod['outgoing_qty']:>6g}  "
            f"Forecasted={prod['virtual_available']:>6g}"
        )


def dump_outgoing_pickings():
    """outgoing picking 상태 (S00008/9) 도 함께."""
    print("\n-- 7. outgoing picking 상태 (S00008/9) --")
    for so_id in (8, 9):
        pids = odoo_service.call("stock.picking", "search", [("sale_id", "=", so_id)])
        for pid in pids:
            p = odoo_service.get_picking(pid)
            print(f"  S{so_id:05d} {p['name']:<14} state={p['state']}")


def main():
    if not odoo_service.authenticate_odoo():
        print(f"FAIL: {odoo_service.get_service_status()}")
        sys.exit(1)
    print(f"OK: Odoo uid={odoo_service.get_service_status()['uid']}")

    reset_quants()
    unreserve_outgoing()
    vendor_id = ensure_vendor()

    print(f"\n-- 4. incoming picking {len(STORABLE_PRODUCTS)}개 (vendor id={vendor_id}) --")
    results = []
    for spec in STORABLE_PRODUCTS:
        try:
            r = find_or_create_incoming_picking(vendor_id, spec)
        except Exception as e:
            r = {"name": spec["name"], "error": f"{type(e).__name__}: {str(e)[:200]}"}
        results.append(r)

    dump_incoming_pickings(results)
    dump_final_inventory()
    dump_outgoing_pickings()


if __name__ == "__main__":
    main()
