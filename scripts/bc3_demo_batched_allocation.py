"""
BC3-v3 — 사전 배치 (Pre-allocation Batched Priority) 시연.

전제:
  · stock.picking.type.reservation_method = 'manual' (이미 변경됨)
  · S00008 Standard / S00009 VIP picking 존재 (각각 USB 수요 5 / 1200)
  · USB SecureKey-100 on_hand = 1000

흐름:
  1. authenticate + before snapshot
  2. 기존 reserved 풀기 (stock.picking.do_unreserve, public method)
     → 모든 picking 이 'confirmed' state 로 되돌아감 (manual reservation 모델 진입)
  3. inventory_agent.allocate_batched_by_tier 직접 호출
     → tier 우선순위 정렬 → VIP(S00009) 부터 action_assign
  4. after snapshot + diff

기대 결과:
  · S00009 (VIP): USB 1000 reserved (전량 잡음 — on_hand 한도), partially_available (1200 demand 중 1000)
    Appliance G2 ×2, Edge Server ×1 도 모두 assigned (재고 충분)
    picking state = assigned 또는 partially_available
  · S00008 (Standard): USB 0 reserved, state = 'confirmed' (waiting)
    Standard 는 VIP 가 다 가져간 후라 못 잡음

이게 사용자가 원한 "VIP 선점" 동작. Standard 입장에서 reserve 됐다가 뺏기는
UX 없음 — 처음부터 "VIP 우선 처리 대기" 로 명확하게 안내 가능.
"""
import asyncio
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
from mcp_server.agents.inventory_agent import InventoryAgent  # noqa: E402


USB_PRODUCT_ID = 2
SO_IDS = [(8, "Standard"), (9, "VIP")]


def snapshot(label: str) -> None:
    print(f"\n{'═' * 72}")
    print(f"  {label}")
    print(f"{'═' * 72}")
    inv = odoo_service.get_inventory_state(USB_PRODUCT_ID)
    print(f"  USB inventory: on_hand={inv['on_hand']} reserved={inv['reserved']} available={inv['available']}")
    for so_id, kind in SO_IDS:
        pids = odoo_service.call("stock.picking", "search", [("sale_id", "=", so_id)])
        for pid in pids:
            p = odoo_service.get_picking(pid)
            print(f"  S{so_id:05d} ({kind:<8}) picking {p['name']:<14} state={p['state']}")
            for m in odoo_service.get_picking_moves(pid):
                prod = m.get("product_id")
                if isinstance(prod, list) and prod:
                    pname = prod[1]
                else:
                    pname = "?"
                print(
                    f"      move id={m.get('id'):<3}  product={pname:<28} "
                    f"demand={m.get('product_uom_qty'):>5g}  "
                    f"reserved={m.get('reserved_availability'):>5g}  "
                    f"state={m.get('state')}"
                )


def reset_pickings_to_manual_state() -> None:
    """기존 reserved 를 풀어 confirmed state 로 되돌리기."""
    print(f"\n{'─' * 72}")
    print(f"  RESET: do_unreserve 호출 (manual state 복원)")
    print(f"{'─' * 72}")
    for so_id, _ in SO_IDS:
        pids = odoo_service.call("stock.picking", "search", [("sale_id", "=", so_id)])
        for pid in pids:
            try:
                odoo_service.call("stock.picking", "do_unreserve", [pid])
                print(f"  do_unreserve picking {pid} (SO {so_id}) OK")
            except Exception as e:
                print(f"  do_unreserve picking {pid} FAIL: {str(e)[:200]}")


async def main() -> None:
    if not odoo_service.authenticate_odoo():
        print(f"FAIL: {odoo_service.get_service_status()}")
        sys.exit(1)
    print(f"OK: Odoo uid={odoo_service.get_service_status()['uid']}")

    snapshot("STEP 1 — 현재 상태 (Odoo at_confirm 시절 reserve 잔재)")

    reset_pickings_to_manual_state()

    snapshot("STEP 2 — RESET 후 (모든 picking confirmed, USB available 회복)")

    # inventory_agent setup
    agent = InventoryAgent(llm_config={"config_list": []})
    agent.register_tools_from_services(user_id="demo")

    print(f"\n{'─' * 72}")
    print(f"  EXECUTING: inventory_agent.allocate_batched_by_tier")
    print(f"  (cut-off 이벤트 발화 모방 — VIP 부터 가용재고 잡음)")
    print(f"{'─' * 72}")

    res = await agent.execute_action(
        "allocate_batched_by_tier",
        policy={
            "ordering": ["tier", "scheduled_date", "sale_order_id"],
            "tier_priority": {"VIP": 100, "Standard": 50, "Bronze": 25},
            "consume_all_for_vip_first": True,
            "unblock_waiting_pickings": True,
        },
        context={
            "cutoff_at": "2026-05-21T13:00:00Z",
        },
    )
    inner = res.get("result") or {}
    print(f"\n  >>> agent result success={res.get('success')}")
    print(f"  candidates: {inner.get('candidates')}")
    print(f"  processed : {inner.get('processed')}")
    print(f"  summary   : {inner.get('summary')}")
    print(f"  results_by_priority:")
    for r in (inner.get("results_by_priority") or []):
        if "error" in r:
            print(f"    · ERROR {r.get('picking_name')} (tier={r.get('tier')}): {r['error']}")
        else:
            print(
                f"    · {r.get('picking_name'):<14} tier={r.get('tier'):<8} "
                f"sale_order={r.get('sale_order_id')}  "
                f"state {r.get('before_state')} → {r.get('after_state')}"
            )

    snapshot("STEP 3 — Batched allocation 후 (VIP 가 우선 reserve)")


if __name__ == "__main__":
    asyncio.run(main())
