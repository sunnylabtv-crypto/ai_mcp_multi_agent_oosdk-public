# scripts/bc3_rules_smoketest.py
"""
BC3 룰 매칭 smoke test — agents/services 부팅 없이 ontology 엔진만 검증.

목적:
  · 새 entity (sale_order_confirmed, stock_received, delivery_ready_check) 로
    payload 를 넣었을 때 의도한 룰이 first_match 로 발화하는지 확인.
  · agents/services 의존성 (google, openai, simple-salesforce 등) 없이도 검증 가능.

전체 demo (bc3_inventory_demo.py) 는 `pip install -r requirements.txt` 후 실행.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from mcp_server.ontology_engine.engine import OntologyEngine  # noqa: E402


def line(c: str = "─", n: int = 78) -> str:
    return c * n


def show_match(label: str, payload: dict, entity: str, expected_rule: str):
    engine = OntologyEngine(str(PROJECT_ROOT / "ontology" / "ontology.yaml"))
    ctx = engine.resolve_links(entity, payload)
    action = engine.check_rules(ctx)
    matched = action.get("rule_name") if action else None
    plan = engine.trigger_events(action, ctx)
    ok = (matched == expected_rule)
    print(f"\n  {'✅' if ok else '❌'}  {label}")
    print(f"     entity         : {entity}")
    print(f"     expected rule  : {expected_rule}")
    print(f"     matched rule   : {matched}")
    print(f"     delegations    : {len(plan)} step(s)")
    for i, s in enumerate(plan):
        print(f"        [{i}] {s.get('agent')}.{s.get('action')} "
              f"policy_keys={list((s.get('policy') or {}).keys())}")


TARGET_VIP = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")
TARGET_STD = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")


def main():
    print(line("═"))
    print(" BC3 룰 매칭 smoke test — agents/services 없이 ontology 엔진만 검증")
    print(line("═"))

    # 1) VIP SO confirmed (storable + service) → order_split_by_line_type
    show_match(
        "VIP SO confirmed (mixed lines) — order_split_by_line_type (420)",
        {"sales_order": {
            "id": 1001, "name": "S00101", "state": "sale",
            "tier": "VIP", "account_name": "VIP Tech",
            "target_delivery_date": TARGET_VIP,
            "has_storable_lines": True, "has_service_lines": True,
        }},
        "sale_order_confirmed",
        "order_split_by_line_type",
    )

    # 2) Standard SO confirmed (storable only) → 같은 룰 (Standard tier 로 분기)
    show_match(
        "Standard SO confirmed (storable only) — order_split_by_line_type (420)",
        {"sales_order": {
            "id": 1002, "name": "S00102", "state": "sale",
            "tier": "Standard", "account_name": "Standard Tech",
            "target_delivery_date": TARGET_STD,
            "has_storable_lines": True, "has_service_lines": False,
        }},
        "sale_order_confirmed",
        "order_split_by_line_type",
    )

    # 3) Service-only SO confirmed → service_line_fulfillment (360, 다른 룰)
    show_match(
        "Service-only SO confirmed — service_line_fulfillment (360, mutex)",
        {"sales_order": {
            "id": 1003, "name": "S00103", "state": "sale",
            "tier": "Standard", "account_name": "Pure SaaS Co",
            "target_delivery_date": TARGET_STD,
            "has_storable_lines": False, "has_service_lines": True,
        }},
        "sale_order_confirmed",
        "service_line_fulfillment",
    )

    # 4) VIP delivery_ready_check + shortage → inventory_allocate_vip_preempt (410)
    show_match(
        "VIP delivery_ready_check + shortage — inventory_allocate_vip_preempt (410)",
        {"picking": {"id": 5001, "name": "WH/OUT/00101", "state": "confirmed",
                     "scheduled_date": TARGET_VIP, "sale_order_id": 1001,
                     "tier": "VIP", "qty_demand": 1200, "product_id": 501},
         "inventory": {"product_id": 501, "on_hand": 5, "reserved": 5,
                       "available": 0, "incoming": 2000, "projected_avail": 2000}},
        "delivery_ready_check",
        "inventory_allocate_vip_preempt",
    )

    # 5) VIP delivery_ready_check + state='assigned' → delivery_ready_to_ship_vip (405)
    # BC3 CRIT #2 봉합 이후: 410 의 if 에 `picking.state != 'assigned'` 가드가 추가됐다.
    #   → state == 'assigned' 면 410 은 매칭 안 함 (inventory shortage 와 무관).
    #   → 따라서 inventory 가 부족해도 405 가 발화한다.
    show_match(
        "VIP picking 'assigned' — delivery_ready_to_ship_vip (405, inventory 풍부)",
        {"picking": {"id": 5001, "name": "WH/OUT/00101", "state": "assigned",
                     "scheduled_date": TARGET_VIP, "sale_order_id": 1001,
                     "tier": "VIP", "qty_demand": 1200, "product_id": 501},
         "inventory": {"product_id": 501, "on_hand": 2000, "reserved": 800,
                       "available": 1200, "incoming": 0, "projected_avail": 1200}},
        "delivery_ready_check",
        "delivery_ready_to_ship_vip",
    )
    # 5b) negative test — state='assigned' + shortage 인데도 410 안 발화, 405 만 발화 (BC3 CRIT #2)
    show_match(
        "VIP picking 'assigned' + shortage — 405 매칭 (410 은 state 가드로 막힘)",
        {"picking": {"id": 5001, "name": "WH/OUT/00101", "state": "assigned",
                     "scheduled_date": TARGET_VIP, "sale_order_id": 1001,
                     "tier": "VIP", "qty_demand": 1200, "product_id": 501},
         "inventory": {"product_id": 501, "on_hand": 5, "reserved": 5,
                       "available": 0, "incoming": 2000, "projected_avail": 2000}},
        "delivery_ready_check",
        "delivery_ready_to_ship_vip",
    )

    # 6) Standard delivery_ready_check → inventory_allocate_standard (390)
    show_match(
        "Standard delivery_ready_check — inventory_allocate_standard (390)",
        {"picking": {"id": 5002, "name": "WH/OUT/00102", "state": "confirmed",
                     "scheduled_date": TARGET_STD, "sale_order_id": 1002,
                     "tier": "Standard", "qty_demand": 5, "product_id": 501}},
        "delivery_ready_check",
        "inventory_allocate_standard",
    )

    # 7) stock_received → stock_received_replenish (400)
    show_match(
        "stock_received (incoming PO 도착) — stock_received_replenish (400)",
        {"receipt": {"product_id": 501, "product_name": "Encrypted USB 128GB",
                     "qty": 2000, "received_at": datetime.now().isoformat()}},
        "stock_received",
        "stock_received_replenish",
    )

    print()
    print(line("═"))
    print(" smoke test 완료 — 위 7개 케이스가 모두 ✅ 면 BC3 ontology rule 정합.")
    print(line("═"))


if __name__ == "__main__":
    main()
