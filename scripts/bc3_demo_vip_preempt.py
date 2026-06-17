"""
BC3 — 실 Odoo 에서 VIP 소급 재할당 시연 (allocate_with_preemption).

데이터 전제:
  · USB SecureKey-100 on_hand = 1000
  · S00008 Standard USB 5  → assigned (Odoo FIFO 로 먼저 잡힘)
  · S00009 VIP      USB 1200 → 995 reserved, partially_available (5 부족)

기대 동작:
  · inventory_agent.allocate_with_preemption (rule 410) 호출
  · S00008 의 5개를 unreserve (soft preempt — assigned 인 Standard 만 회수 OK)
  · 회수한 5개를 S00009 에 reserve → S00009 reserved 995 → 1000
  · 여전히 1200 demand 대비 200 부족 (incoming PO 없음 → backorder 로 표기)
  · S00008 USB move: assigned → confirmed/waiting

이 시연이 보여주는 것:
  1. BC3 ontology 정책이 실 Odoo state 를 실제로 변경한다 (mock 이 아님)
  2. unit test 가 검증한 가드들 (HIGH #5 자기 SO 제외, CRIT #1 invalid id skip 등)
     이 실 환경에서도 동일하게 동작
  3. 이게 "소급 재할당" 모델의 한계 — Standard 입장에선 이미 받은 reserve 가
     뒤집힘. 사용자가 제안한 "사전 배치" 모델 (cut-off + batch) 의 필요성.
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


USB_PRODUCT_ID = 2          # 'USB SecureKey-100' (bc3_setup_storable_products 결과)
S00008_SALE_ID = 8          # Standard (small)
S00009_SALE_ID = 9          # VIP (Enterprise)


def snapshot(label: str) -> Dict[str, Any]:
    """현재 USB 의 inventory state + S00008/9 picking·move 상태 dump."""
    print(f"\n{'═' * 72}")
    print(f"  {label}")
    print(f"{'═' * 72}")
    inv = odoo_service.get_inventory_state(USB_PRODUCT_ID)
    print(f"  inventory: {inv}")
    snap: Dict[str, Any] = {"inventory": inv, "sos": {}}
    for so_id, kind in ((S00008_SALE_ID, "Standard"), (S00009_SALE_ID, "VIP")):
        pids = odoo_service.call("stock.picking", "search", [("sale_id", "=", so_id)])
        so_block: List[Dict[str, Any]] = []
        for pid in pids:
            p = odoo_service.get_picking(pid)
            print(f"  S{so_id:05d} ({kind}) picking {p['name']:<14} state={p['state']}")
            for m in odoo_service.get_picking_moves(pid):
                prod = m.get("product_id")
                if not (isinstance(prod, list) and prod and prod[0] == USB_PRODUCT_ID):
                    continue
                row = {
                    "move_id": m.get("id"),
                    "demand": m.get("product_uom_qty"),
                    "reserved": m.get("reserved_availability"),
                    "state": m.get("state"),
                }
                so_block.append(row)
                print(
                    f"    USB move id={row['move_id']:<3}  "
                    f"demand={row['demand']:>6g}  "
                    f"reserved={row['reserved']:>6g}  "
                    f"state={row['state']}"
                )
        snap["sos"][so_id] = so_block
    return snap


async def main() -> None:
    if not odoo_service.authenticate_odoo():
        print(f"FAIL: Odoo {odoo_service.get_service_status()}")
        sys.exit(1)
    print(f"OK: Odoo uid={odoo_service.get_service_status()['uid']}")

    before = snapshot("BEFORE — Odoo default FIFO 결과")

    # ─ inventory_agent.allocate_with_preemption 직접 호출 ─
    agent = InventoryAgent(llm_config={"config_list": []})
    agent.register_tools_from_services(user_id="demo")

    # S00009 의 USB picking 컨텍스트
    s00009_pids = odoo_service.call(
        "stock.picking", "search", [("sale_id", "=", S00009_SALE_ID)],
    )
    s00009_pid = s00009_pids[0]

    policy = {
        "tier": "VIP",
        "preempt_mode": "soft",
        "preempt_target_states": ["assigned"],
        "preempt_exclude_states": ["partially_available", "done"],
        "backorder_against_incoming": True,
        "notify_account_owner": True,
    }
    context = {
        "picking": {
            "id": s00009_pid,
            "tier": "VIP",
            "product_id": USB_PRODUCT_ID,
            "qty_demand": 1200,
            "sale_order_id": S00009_SALE_ID,
            "state": "partially_available",
        },
        "inventory": odoo_service.get_inventory_state(USB_PRODUCT_ID),
    }

    print(f"\n{'─' * 72}")
    print(f"  EXECUTING: inventory_agent.allocate_with_preemption")
    print(f"  policy.tier={policy['tier']} preempt_mode={policy['preempt_mode']}")
    print(f"  context.picking id={s00009_pid} demand={context['picking']['qty_demand']}")
    print(f"{'─' * 72}")

    res = await agent.execute_action("allocate_with_preemption",
                                     policy=policy, context=context)

    print(f"\n  >>> agent result success={res.get('success')}")
    inner = res.get("result") or {}
    for k, v in inner.items():
        print(f"      {k} = {v}")

    after = snapshot("AFTER — VIP preempt 실행 후")

    # ─ Diff ─
    print(f"\n{'═' * 72}")
    print("  DIFF")
    print(f"{'═' * 72}")
    for so_id in (S00008_SALE_ID, S00009_SALE_ID):
        bs = before["sos"][so_id]
        as_ = after["sos"][so_id]
        for b, a in zip(bs, as_):
            if b["reserved"] != a["reserved"] or b["state"] != a["state"]:
                print(
                    f"  S{so_id:05d}  move {b['move_id']}: "
                    f"reserved {b['reserved']} → {a['reserved']}, "
                    f"state {b['state']} → {a['state']}"
                )


if __name__ == "__main__":
    asyncio.run(main())
