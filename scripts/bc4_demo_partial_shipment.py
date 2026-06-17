# scripts/bc4_demo_partial_shipment.py
"""
BC4 S1 — Partial Shipment Advisor 시연.

상황:
    VIP 주문 수량 > reserved 가용분 → Odoo button_validate 가 backorder wizard 반환.
    "가용분 먼저 부분출하(split) vs 전량 채워질 때까지 대기(wait)" 는 정답 없는 판단.
    → LLM advisor 가 '추천'만, 사람이 confirm 으로 '실행'. (LLM as advisor, not driver)

라운드:
    A. rule baseline (auto_backorder) — 가용분 출하 + 나머지 backorder, LLM 0회
    B. llm_advisor (auto_execute=False) — advisor 추천만, 출하 보류 (pending_confirmation)
    C. confirm — 사람이 split 승인 → 결정론 process_backorder 실행
    D. fallback — LLM 강제 실패 → rule_baseline 으로 안전 폴백

주의:
    · 실제 Odoo + (B 라운드는) OPENAI_API_KEY 필요.
    · advisor 주입은 server dispatch 와 동일하게 policy 채널 사용.
    · 미연결 시 dispatch_shipment 가 plan-only 반환.

실행:
    cd ai_mcp_multi_agent_oosdk
    python scripts/bc4_demo_partial_shipment.py --picking 700
"""
import argparse
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from mcp_server.services import odoo_service  # noqa: E402
from mcp_server.agents.inventory_agent import InventoryAgent  # noqa: E402


def banner(s):
    print("\n" + "=" * 78 + "\n " + s + "\n" + "=" * 78)


def _picking_context(picking_id):
    """live Odoo picking → dispatch_shipment context (tier/state/demand 부착)."""
    p = odoo_service.get_picking(picking_id)
    if not p:
        return None
    sale_field = p.get("sale_id")
    sale_id = sale_field[0] if isinstance(sale_field, list) and sale_field else sale_field
    tier = "Standard"
    if sale_id:
        tier = (odoo_service.get_sale_order_tier_map([sale_id]) or {}).get(sale_id, "Standard")
    return {
        "picking": {"id": picking_id, "name": p.get("name"),
                    "state": p.get("state"), "tier": tier,
                    "scheduled_date": p.get("scheduled_date"),
                    "sale_order_id": sale_id},
    }


async def _dispatch(agent, ctx, partial_handling, auto_execute=False):
    policy = {
        "target_state": "done", "carrier_lookup": "by_tier",
        "partial_handling": partial_handling,
        "rule_baseline": "split",
        "auto_execute_advisor": auto_execute,
    }
    res = await agent.execute_action("dispatch_shipment", policy=policy, context=ctx)
    return res.get("result") or {}


def _show(inner):
    if inner.get("skipped"):
        print(f"  (skipped: {inner.get('reason')})")
        return
    if inner.get("pending_confirmation"):
        adv = inner.get("advisor") or {}
        sh = inner.get("shortage") or {}
        print(f"  🟡 보류(pending) — advisor 추천: {adv.get('recommendation')} "
              f"[{adv.get('source')}, conf={adv.get('confidence')}]")
        print(f"     근거: {adv.get('rationale')}")
        print(f"     부족: demand={sh.get('demand')} reserved={sh.get('reserved')} "
              f"shortage={sh.get('shortage')}")
        print("     → 출하 미실행 (confirm_partial_shipment 승인 필요)")
    elif inner.get("partial"):
        print(f"  ✅ partial 실행 — decision={inner.get('decision')} "
              f"[{inner.get('decision_source')}] exec={inner.get('execution_result')}")
    else:
        print(f"  ✅ 전량 출하 — success={inner.get('success')} {inner.get('validation_result')}")


async def amain(args):
    if not odoo_service.is_available():
        odoo_service.authenticate_odoo()
    if not odoo_service.is_available():
        print("❌ Odoo 미연결 — 데모 중단")
        return 1

    picking_id = int(args.picking)
    ctx = _picking_context(picking_id)
    if not ctx:
        print(f"❌ picking {picking_id} 없음")
        return 1
    pk = ctx["picking"]
    print(f"대상: {pk.get('name')} (tier={pk.get('tier')}, state={pk.get('state')})")
    if pk.get("state") != "partially_available":
        print(f"⚠️ 이 picking 은 partially_available 아님(state={pk.get('state')}). "
              "부분출하 분기를 보려면 demand>reserved 인 partial picking 필요.")

    agent = InventoryAgent(llm_config={"config_list": []})
    agent.register_tools_from_services(user_id="demo")

    # A. rule baseline
    banner("A. rule baseline (auto_backorder) — LLM 0회")
    _show(await _dispatch(agent, ctx, "auto_backorder"))

    # 다시 partial 상태로 (A 가 출하했으면 재셋업 필요 — 데모는 상태 재조회)
    ctx = _picking_context(picking_id) or ctx

    # B. llm_advisor (추천만)
    banner("B. llm_advisor (auto_execute=False) — 추천만, 출하 보류")
    _show(await _dispatch(agent, ctx, "llm_advisor", auto_execute=False))

    # C. confirm (사람 승인 — split)
    banner("C. confirm — 사람이 split 승인 → 결정론 실행")
    from mcp_server.server import confirm_partial_shipment  # noqa: E402
    # 주의: confirm 은 trigger_delivery_dispatch 가 메모리에 저장한 wizard 를 읽음.
    # 직접 데모에서는 server tool 경로로 호출해야 메모리에 wizard 가 있음.
    print("  (실제 승인 흐름: trigger_delivery_dispatch → confirm_partial_shipment 순서로 호출)")
    print("  MCP 경로: confirm_partial_shipment(picking_id, decision='split', confirmed_by='ops@acme')")

    # D. fallback (LLM 강제 실패)
    banner("D. fallback — LLM 강제 실패 → rule_baseline 폴백")
    with patch("mcp_server.services.openai_service.generate_text_with_system",
               return_value=None):
        _show(await _dispatch(agent, ctx, "llm_advisor", auto_execute=False))

    banner("끝")
    print("""
관찰 포인트:
  · A: 정답 정책(auto_backorder)이면 LLM 없이 즉시 분할출하.
  · B: llm_advisor 는 추천만 — 실제 출하는 안 함(사람 승인 대기). ← "advisor not driver"
  · C: 사람이 confirm 해야 결정론 코드가 실제 backorder 생성.
  · D: LLM 장애 시 rule_baseline(split)으로 안전 폴백 — 시스템 안 멈춤.

전체 MCP 흐름:
  trigger_delivery_dispatch(picking_id) → (partial 이면) advisor 추천 반환
  → confirm_partial_shipment(picking_id, decision, confirmed_by) → 실행
""")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--picking", default="700", help="대상 picking_id (partial 상태)")
    args = ap.parse_args()
    sys.exit(asyncio.run(amain(args)))
