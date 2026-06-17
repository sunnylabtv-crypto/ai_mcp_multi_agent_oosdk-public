# scripts/bc5_demo_replenishment.py
"""
BC5 — 충족 불가 → 자율 보충 발주 + 담당자 브리핑 시연.

상황 (규칙으로 못 푸는 예외):
    VIP 주문이 들어왔는데 재고도 0, 선점할 Standard reservation 도 없어
    어떤 결정론 규칙(VIP 선점/입고 재할당/override)으로도 채울 수 없다.
    → agent 가 감지 → 판단①(LLM: 발주량/긴급도) → 발주(incoming picking 생성)
      → 판단②(LLM: 담당자 브리핑) → 통보.

이 스크립트가 보여주는 흐름:
    1. 미충족 수요 집계 (get_open_demand_for_product) — 블록된 주문 + 부족분
    2. inventory_agent.create_replenishment_po
       · 발주량 advisor(LLM) → recommended_qty + urgency  (LLM 실패 시 rule 폴백)
       · auto_create_po=True → Odoo incoming stock.picking 생성 (purchase 모듈 없음)
    3. email_agent.send_replenishment_alert
       · 담당자 브리핑 메일 LLM 작성 (블록 주문·임팩트·권장발주량·긴급도)
       · auto_send + notify_to → 발송, 아니면 draft
    4. (안내) 그 picking 을 trigger_stock_received 로 입고하면 rule 400 이
       VIP backorder 부터 자동 충족 → 루프 닫힘.

주의:
    · 실제 Odoo (your-tenant.odoo.com) 연결 필요. 미연결 시 agent 가 plan-only 반환.
    · 발주량/브리핑 LLM 은 OPENAI_API_KEY 있을 때만. 없으면 rule/템플릿 폴백.
    · 메일 발송은 --notify-to 와 Gmail 인증이 있을 때만 실제 전송. 기본은 draft 출력.

실행:
    cd ai_mcp_multi_agent_oosdk
    python scripts/bc5_demo_replenishment.py                       # USB, draft만
    python scripts/bc5_demo_replenishment.py --notify-to ops@acme.com --send
    python scripts/bc5_demo_replenishment.py --product "USB SecureKey-100" --dry-run
    python scripts/bc5_demo_replenishment.py --reset                # USB 예약 해제(동일 출발선)
"""
import argparse
import asyncio
import sys
from pathlib import Path

# Windows 콘솔(cp949)에서 한글·em-dash·LLM 출력이 깨지거나 UnicodeEncodeError 나는 것 방지.
for _stream in ("stdout", "stderr"):
    try:
        getattr(sys, _stream).reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from mcp_server.services import odoo_service  # noqa: E402
from mcp_server.agents.inventory_agent import InventoryAgent  # noqa: E402
from mcp_server.agents.email_agent import EmailAgent  # noqa: E402

USB_PRODUCT_NAME = "USB SecureKey-100"


def banner(s):
    print("\n" + "=" * 78 + "\n " + s + "\n" + "=" * 78)


def _maybe_init_openai():
    """OPENAI_API_KEY 있으면 openai_service 초기화 (LLM advisor/브리핑 활성화)."""
    try:
        from mcp_server.services import openai_service
        from mcp_server import config as cfg
        oc = getattr(cfg, "OPENAI_CONFIG", None)
        if oc and oc.get("API_KEY"):
            openai_service.initialize_openai(oc)
            return True
    except Exception as e:
        print(f"  (openai 초기화 생략: {e})")
    return False


def _product_id(name):
    pids = odoo_service.call("product.product", "search", [("name", "=", name)])
    return int(pids[0]) if pids else None


def _reset(product_id):
    """USB outgoing move 중 assigned/partially 인 것을 unreserve → 동일 출발선."""
    moves = odoo_service.list_open_moves_for_product(
        product_id, ["assigned", "partially_available"])
    n = 0
    for m in moves:
        try:
            odoo_service.call("stock.move", "_do_unreserve", [m["id"]])
            n += 1
        except Exception:
            pass
    print(f"  unreserve {n} move(s) → 동일 출발선")


def _show_shortage(sh):
    print(f"  제품: {sh.get('product_name')} (id={sh.get('product_id')})")
    print(f"  on_hand={sh.get('on_hand')} reserved={sh.get('reserved')} "
          f"available={sh.get('available')} incoming={sh.get('incoming')}")
    print(f"  총 수요={sh.get('total_demand')} 총 부족={sh.get('total_shortage')} "
          f"미충족(unmet)={sh.get('unmet_qty')}")
    blocked = sh.get("blocked_orders") or []
    print(f"  블록된 주문 {len(blocked)}건:")
    for b in blocked:
        print(f"    · {b.get('so_name')} [{b.get('tier')}] "
              f"{b.get('account_name') or ''} — 부족 {int(b.get('shortage') or 0)}개")


def _show_po(inner):
    if inner.get("skipped"):
        print(f"  (skipped: {inner.get('reason')})")
    adv = inner.get("advisor") or {}
    print(f"  🤖 발주량 advisor [{adv.get('source')}]: "
          f"권장 {inner.get('recommended_qty')}개, urgency={adv.get('urgency')}, "
          f"conf={adv.get('confidence')}")
    print(f"     근거: {adv.get('rationale')}")
    po = inner.get("po")
    if po:
        print(f"  📦 입고건 생성: {po.get('picking_name')} (state={po.get('state')}, "
              f"qty={po.get('qty')})")
        print(f"     → 이 건을 입고 처리하면 rule 400 이 VIP backorder 부터 자동 충족.")
    elif inner.get("pending_confirmation"):
        print("  🟡 발주 보류 (dry_run/auto_create_po=False) — 사람 승인 대기")


def _show_mail(inner):
    if inner.get("skipped"):
        print(f"  (메일 skip: {inner.get('reason')})")
    via = (inner.get("policy_applied") or {}).get("generated_via")
    print(f"  ✉️  담당자 브리핑 [{via}] → {inner.get('to')}")
    draft = inner.get("draft") or {}
    print(f"     제목: {draft.get('subject')}")
    body = draft.get("body") or ""
    for line in body.splitlines():
        print(f"     | {line}")


async def main_async(args):
    if not odoo_service.is_available():
        odoo_service.authenticate_odoo()
    has_llm = _maybe_init_openai()
    print(f"  LLM(OpenAI) advisor/브리핑: {'활성' if has_llm else '비활성 → rule/템플릿 폴백'}")

    product_id = _product_id(args.product)
    if not product_id:
        print(f"❌ 제품 못 찾음: {args.product!r}")
        return 1

    if args.reset:
        banner("RESET — USB 예약 해제")
        _reset(product_id)
        return 0

    # 1. 미충족 수요 집계
    banner("1. 미충족 수요 집계 (get_open_demand_for_product)")
    shortage = odoo_service.get_open_demand_for_product(product_id)
    _show_shortage(shortage)
    if float(shortage.get("unmet_qty") or 0) <= 0:
        print("\n  ✅ 미충족 수요 없음 — 보충 발주 불필요. (재고 0 + 블록 주문이 있어야 시연됨)")
        return 0

    # agents
    inv = InventoryAgent(llm_config={"config_list": []})
    inv.register_tools_from_services(user_id="demo")
    email = EmailAgent(llm_config={"config_list": []})
    email.register_tools_from_services(user_id="demo")

    # 2. 자율 보충 발주 (판단① + 입고 picking 생성)
    banner("2. inventory_agent.create_replenishment_po (판단① 발주량 + 발주)")
    po_policy = {
        "auto_create_po": not args.dry_run,
        "vendor_name": args.vendor,
        "safety_buffer_units": args.safety_buffer,
        "lead_time_days": 3,
        "dry_run": args.dry_run,
    }
    po_res = await inv.execute_action(
        "create_replenishment_po", policy=po_policy,
        context={"shortage": shortage})
    po_inner = po_res.get("result") or {}
    _show_po(po_inner)

    # 3. 담당자 브리핑 (판단② + 통보)
    banner("3. email_agent.send_replenishment_alert (판단② 브리핑 + 통보)")
    mail_policy = {
        "tone": "professional", "language": "ko",
        "auto_send": bool(args.send and args.notify_to and not args.dry_run),
        "notify_to": args.notify_to,
    }
    mail_res = await email.execute_action(
        "send_replenishment_alert", policy=mail_policy,
        context={"shortage": shortage,
                 "agent_outputs": {"create_replenishment_po": po_inner}})
    _show_mail(mail_res.get("result") or {})

    # 4. 루프 닫힘 안내
    po = po_inner.get("po") or {}
    if po.get("picking_name"):
        banner("4. 루프 닫힘 (다음 단계)")
        print(f"  생성된 입고건 {po.get('picking_name')} (id={po.get('picking_id')}) 을 입고 처리하면:")
        print(f"    trigger_stock_received(product_name={args.product!r}, "
              f"qty={po_inner.get('recommended_qty')}, "
              f"incoming_picking_id={po.get('picking_id')})")
        print(f"  → 그 picking 을 결정적으로 검증 → rule 400(stock_received_replenish) 이 "
              f"VIP backorder 부터 자동 충족.")
    return 0


def main():
    ap = argparse.ArgumentParser(description="BC5 자율 보충 발주 + 담당자 브리핑 시연")
    ap.add_argument("--product", default=USB_PRODUCT_NAME, help="보충 대상 제품명")
    ap.add_argument("--vendor", default="TechSupply Co", help="보충 공급처")
    ap.add_argument("--notify-to", dest="notify_to", default="",
                    help="담당자 이메일 (브리핑 수신자). 없으면 draft만")
    ap.add_argument("--send", action="store_true",
                    help="담당자 메일 실제 발송 (--notify-to 필요, Gmail 인증 필요)")
    ap.add_argument("--safety-buffer", dest="safety_buffer", type=float, default=0,
                    help="rule 폴백 시 부족분에 더할 안전버퍼")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="발주·메일 모두 보류(추천/draft만) — 라이브 시연 안전토글")
    ap.add_argument("--reset", action="store_true",
                    help="USB 예약 해제 후 종료 (동일 출발선)")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
