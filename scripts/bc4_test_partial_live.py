# scripts/bc4_test_partial_live.py
"""BC4 부분출고 라이브 2-step 테스트 (실제 MCP 경로).

  단계 1~2:  trigger_delivery_dispatch(picking)  → advisor split/wait 추천 (출하 안 함)
  단계 3~4:  confirm_partial_shipment(picking, decision) → 결정론 실행 (가용분 done + backorder)

실행:
  python scripts/bc4_test_partial_live.py                 # trigger 만 (추천 확인, 출하 안 함)
  python scripts/bc4_test_partial_live.py --confirm split # trigger + split 승인 실행
  python scripts/bc4_test_partial_live.py --confirm wait   # trigger + wait
"""
import argparse
import asyncio
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

import os  # noqa: E402
from mcp_server.services import odoo_service as o  # noqa: E402
from mcp_server.services import openai_service  # noqa: E402

PICKING_ID = 51   # WH/OUT/00044 (S00051, VIP D), USB 300 / 100 reserved


def banner(s):
    print("\n" + "=" * 74 + "\n " + s + "\n" + "=" * 74)


def show_pickings():
    pks = o.call("stock.picking", "search_read",
                 [("origin", "=", "S00051"), ("picking_type_id.code", "=", "outgoing")],
                 fields=["name", "state", "backorder_id"], order="id")
    for p in pks:
        mv = o.call("stock.move", "search_read",
                    [("picking_id", "=", p["id"]), ("product_id", "=", 2)],
                    fields=["product_uom_qty", "quantity", "state"])
        m = mv[0] if mv else {}
        bo = p.get("backorder_id")
        print(f"  {p['name']} [{p['state']}]"
              f"{'  (backorder of ' + str(bo[1]) + ')' if isinstance(bo, list) else ''}: "
              f"USB demand={m.get('product_uom_qty')} done/reserved={m.get('quantity')} move={m.get('state')!r}")


async def amain(args):
    if not o.is_available():
        o.authenticate_odoo()
    ok = openai_service.initialize_openai({
        "API_KEY": os.getenv("OPENAI_API_KEY"),
        "MODEL": "gpt-4o-mini", "BASE_URL": "https://api.openai.com/v1"})
    print(f"  OpenAI init: {ok}")

    banner("BEFORE — S00051(D) pickings")
    show_pickings()

    # server.py 는 gmail(google) 의존성으로 로컬 import 불가 →
    # agent 를 직접 구동(서버 trigger 와 동일한 dispatch_shipment 경로).
    from mcp_server.agents.inventory_agent import InventoryAgent  # noqa

    p = o.get_picking(PICKING_ID)
    sale_field = p.get("sale_id")
    sale_id = sale_field[0] if isinstance(sale_field, list) and sale_field else sale_field
    tier = (o.get_sale_order_tier_map([sale_id]) or {}).get(sale_id, "Standard") if sale_id else "Standard"
    ctx = {"picking": {"id": PICKING_ID, "name": p.get("name"), "state": p.get("state"),
                       "tier": tier, "sale_order_id": sale_id}}

    agent = InventoryAgent(llm_config={"config_list": []})
    agent.register_tools_from_services(user_id="test")
    policy = {"target_state": "done", "carrier_lookup": "by_tier",
              "partial_handling": "llm_advisor", "rule_baseline": "split",
              "auto_execute_advisor": False}

    # 단계 1~2: advisor 추천 (출하 보류)
    banner(f"STEP 1~2: dispatch_shipment(51, llm_advisor) — advisor 추천 (출하 안 함, tier={tier})")
    res = await agent.execute_action("dispatch_shipment", policy=policy, context=ctx)
    inner = res.get("result") or {}
    adv = inner.get("advisor") or {}
    sh = inner.get("shortage") or {}
    print(f"  pending_confirmation={inner.get('pending_confirmation')}")
    if adv:
        print(f"  🤖 advisor 추천: {adv.get('recommendation')}  "
              f"[source={adv.get('source')}, confidence={adv.get('confidence')}]")
        print(f"     근거: {adv.get('rationale')}")
    print(f"  부족: demand={sh.get('demand')} reserved={sh.get('reserved')} shortage={sh.get('shortage')}")
    wizard = inner.get("wizard") or {}

    # 단계 3~4: 사람 승인 실행 (wizard 재사용)
    if args.confirm:
        banner(f"STEP 3~4: confirm '{args.confirm}' — 결정론 실행 (wizard 재사용)")
        wctx = wizard.get("context") or {}
        if args.confirm == "wait":
            print("  wait — 아무것도 출하 안 함, picking 보류 유지.")
        elif not wctx and not wizard.get("res_id"):
            print(f"  ⚠️ wizard 없음 (context·res_id 모두 누락) — partial 분기 안 됨? inner.note={inner.get('note')}")
        else:
            # res_id None 이어도 process_backorder 가 context 로 wizard 생성 (버전차 폴백)
            fn = o.process_backorder if args.confirm == "split" else o.cancel_backorder
            exec_res = fn(wizard.get("res_id"), wctx)
            print(f"  execution_result={exec_res}")
        banner("AFTER — S00051(D) pickings")
        show_pickings()
    else:
        banner("(confirm 생략) — 출하 안 함. 실행하려면 --confirm split")

    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", choices=["split", "cancel", "wait"], default=None)
    args = ap.parse_args()
    sys.exit(asyncio.run(amain(args)))
