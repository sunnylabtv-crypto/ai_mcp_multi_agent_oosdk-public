# scripts/bc4_demo_scenario_ABC.py
"""
BC4 통합 시연 — VIP 우선(A) / VIP backorder(B) / Priority Override S0(C).

★ 이 시연의 모든 배정은 결정론(rule + S0 override)이다. LLM 판단 없음.
  (LLM advisor 는 별도 시나리오 S1 — 여기엔 없음.)

시나리오
────────
[리셋] 기존 SO 전부 취소·삭제 + USB 재고 0 + 예약 해제 → 깨끗한 출발선.

[A] 입고 100 + 주문 3건                  기대 결과
    · Std-5   (Standard, USB 5)            0   (대기)
    · Std-10  (Standard, USB 10)           0   (대기)
    · VIP-100 (VIP,      USB 100)         100  ← VIP 먼저 선점
    입고승인 = trigger replenish(qty=100)

[B] 입고 50 + VIP 주문 1건
    · VIP-50  (VIP, USB 50)               50  ← VIP backorder 우선
    · Std-5 / Std-10                       0   (여전히 대기)

[C] 입고 50 + 주문 2건 + 비즈니스 영향도 승인(override)
    · Std-30 (Standard, USB 30) ★override 30  ← 사람이 승인한 우선
    · VIP-30 (VIP, USB 30)                 20  ← 남은 20만 (부분)
    · Std-5 / Std-10                       0   (여전히 대기)
    입고승인 = trigger replenish(qty=50, priority_override_so_ids=[Std-30],
                                 requested_by="ops.manager", reason="비즈니스 영향도")

주의
────
· 라이브 Odoo 를 실제로 변경한다(주문 삭제·재고 조정). 안전을 위해 --yes 필수.
· 배정은 inventory_agent.replenish_priority_queue 를 직접 호출(=server dispatch 와
  동일 정책 채널). server MCP tool 의 발행 게이트/audit 는 생략(데모 단순화).
· USB 는 manual reservation 전제(SO 확정해도 자동 reserve 안 됨) — 확인됨.

실행
────
    python scripts/bc4_demo_scenario_ABC.py            # dry — 계획만 출력, 변경 X
    python scripts/bc4_demo_scenario_ABC.py --yes      # 실제 실행
"""
import argparse
import asyncio
import sys
from pathlib import Path

# Windows 콘솔(cp949)에서 한글/em-dash 출력 깨짐 방지.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
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

USB_NAME = "USB SecureKey-100"
TIER_TABLE = {"VIP": 100, "Standard": 50, "Bronze": 25}


def banner(s):
    print("\n" + "=" * 78 + "\n " + s + "\n" + "=" * 78)


_INTERACTIVE = {"on": False}


def pause(msg):
    """--interactive 일 때만 Enter 대기 (발표 중 화면 전환·설명용)."""
    if _INTERACTIVE["on"]:
        try:
            input(f"\n  ⏸  {msg}\n     (Enter ▶) ")
        except EOFError:
            pass


def _nid(raw):
    """Odoo create 반환 정규화 ([id] 또는 id → int)."""
    return int(raw[0]) if isinstance(raw, list) and raw else int(raw)


# ── 조회 헬퍼 ────────────────────────────────────────────────────────
def _usb_id():
    pids = odoo_service.call("product.product", "search", [("name", "=", USB_NAME)])
    return int(pids[0]) if pids else None


def _partner_by_tier(tier):
    cat_ids = odoo_service.call("res.partner.category", "search", [("name", "=", tier)])
    if not cat_ids:
        return None
    pids = odoo_service.call("res.partner", "search", [("category_id", "in", cat_ids)])
    return int(pids[0]) if pids else None


def _inv(usb_id):
    return odoo_service.get_inventory_state(usb_id)


def _so_reserved(so_id, usb_id):
    """SO 의 USB outgoing move 의 demand / reserved 집계."""
    picks = odoo_service.call(
        "stock.picking", "search",
        [("sale_id", "=", so_id), ("picking_type_id.code", "=", "outgoing")])
    if not picks:
        return 0.0, 0.0
    moves = odoo_service.call(
        "stock.move", "search_read",
        [("picking_id", "in", picks), ("product_id", "=", usb_id)],
        fields=["product_uom_qty", "quantity", "state"])
    demand = sum(float(m.get("product_uom_qty") or 0) for m in moves)
    reserved = sum(float(m.get("quantity") or 0) for m in moves
                   if m.get("state") in ("assigned", "partially_available"))
    return demand, reserved


# ── 변경(파괴적) 작업 ────────────────────────────────────────────────
def reset_all(usb_id, do_it):
    banner("[리셋] 기존 SO 취소·삭제 + 재고 0 + 예약 해제")
    so_ids = odoo_service.call("sale.order", "search",
                               [("state", "in", ["draft", "sent", "sale"])])
    print(f"  기존 SO {len(so_ids)} 건: {so_ids}")
    if do_it:
        for sid in so_ids:
            try:
                odoo_service.call("sale.order", "action_cancel", [sid])
            except Exception as e:
                print(f"    ⚠️ cancel {sid}: {e}")
        for sid in so_ids:
            try:
                odoo_service.call("sale.order", "unlink", [sid])
            except Exception as e:
                print(f"    ⚠️ unlink {sid} (cancel 상태로 남김): {e}")
        # 남은 outgoing 예약 해제
        rpicks = odoo_service.call(
            "stock.picking", "search",
            [("picking_type_id.code", "=", "outgoing"),
             ("state", "in", ["assigned", "partially_available"])])
        for pid in rpicks:
            try:
                odoo_service.call("stock.picking", "do_unreserve", [pid])
            except Exception as e:
                print(f"    ⚠️ unreserve {pid}: {e}")
        # USB 재고 0 으로 조정 (internal location quants)
        quant_ids = odoo_service.call(
            "stock.quant", "search",
            [("product_id", "=", usb_id), ("location_id.usage", "=", "internal")])
        for qid in quant_ids:
            try:
                # call(model, method, *args): write(ids, vals) → ids 와 vals 를 별도 위치인자로.
                odoo_service.call("stock.quant", "write", [qid], {"inventory_quantity": 0})
            except Exception as e:
                print(f"    ⚠️ quant write {qid}: {str(e)[:120]}")
        if quant_ids:
            try:
                # action_apply_inventory(self): recordset = quant_ids (평탄 리스트)
                odoo_service.call("stock.quant", "action_apply_inventory", quant_ids)
            except Exception as e:
                print(f"    ⚠️ action_apply_inventory: {str(e)[:120]}")
        inv = _inv(usb_id)
        print(f"  → USB on_hand={inv['on_hand']:.0f} reserved={inv['reserved']:.0f} "
              f"available={inv['available']:.0f}")
    else:
        print("  (dry-run — 변경 안 함)")


def add_stock(usb_id, qty, do_it):
    print(f"  · 입고 +{qty:.0f}")
    if do_it:
        odoo_service.register_stock_receipt(product_id=usb_id, qty=qty)


def create_so(usb_id, qty, tier, do_it):
    """USB qty 짜리 SO 생성 + 확정. (so_id, name) 반환."""
    if not do_it:
        print(f"  · (dry) {tier} USB x{qty} SO 생성 예정")
        return None, f"(dry-{tier}-{qty})"
    partner_id = _partner_by_tier(tier)
    if not partner_id:
        print(f"  ❌ {tier} partner 없음")
        return None, None
    price = (odoo_service.call("product.product", "read", [usb_id],
                               fields=["list_price"])[0].get("list_price") or 0)
    raw = odoo_service.call("sale.order", "create", [{
        "partner_id": partner_id,
        "order_line": [(0, 0, {"product_id": usb_id, "product_uom_qty": qty,
                               "price_unit": price})],
    }])
    so_id = _nid(raw)
    odoo_service.call("sale.order", "action_confirm", [so_id])
    name = odoo_service.call("sale.order", "read", [so_id], fields=["name"])[0]["name"]
    print(f"  · {name} = {tier} USB x{qty} 생성·확정")
    return so_id, name


async def bc5_replenish_check(agent, usb_id, notify_to, do_it):
    """[D] BC5 — 충족 불가 남은 수요 → 자율 보충 발주(LLM 발주량) + 담당자 브리핑(LLM).

    A/B/C(결정론) 와 대비되는 'AI 개입' 라운드. do_it=False 면 dry_run(추천/draft만).
    EmailAgent 는 lazy import (google 미설치 로컬에서도 inventory 단계는 동작)."""
    banner("[D] BC5 — 남은 충족 불가 수요 → 자율 보충 발주 + 담당자 브리핑 (AI 개입)")
    shortage = odoo_service.get_open_demand_for_product(usb_id)
    unmet = float(shortage.get("unmet_qty") or 0)
    print(f"  미충족(unmet)={unmet:.0f}, 블록 주문 {len(shortage.get('blocked_orders') or [])}건")
    for b in shortage.get("blocked_orders") or []:
        print(f"    · {b.get('so_name')} [{b.get('tier')}] 부족 {int(b.get('shortage') or 0)}")
    if unmet <= 0:
        print("  ✅ 미충족 없음 — 보충 발주 불필요 (앞 단계에서 모두 충족됨).")
        return

    # 판단① — 발주량 advisor (LLM) + 입고 picking 생성 (do_it 일 때만 실제 생성)
    po_res = await agent.execute_action(
        "create_replenishment_po",
        policy={"auto_create_po": do_it, "dry_run": not do_it,
                "vendor_name": "TechSupply Co", "safety_buffer_units": 0},
        context={"shortage": shortage})
    po = po_res.get("result") or {}
    adv = po.get("advisor") or {}
    print(f"\n  🤖 발주량 advisor [{adv.get('source')}]: 권장 {po.get('recommended_qty')}개 "
          f"(urgency={adv.get('urgency')})  근거: {adv.get('rationale')}")
    if po.get("po"):
        print(f"  📦 입고건: {po['po'].get('picking_name')} (state={po['po'].get('state')})")
    elif po.get("pending_confirmation"):
        print("  🟡 (dry-run) 발주 보류 — 추천만")

    # 판단② — 담당자 브리핑 (LLM). EmailAgent lazy import.
    try:
        from mcp_server.agents.email_agent import EmailAgent
        email = EmailAgent(llm_config={"config_list": []})
        email.register_tools_from_services(user_id="demo")
        mail_res = await email.execute_action(
            "send_replenishment_alert",
            policy={"auto_send": bool(do_it and notify_to), "notify_to": notify_to,
                    "language": "ko", "tone": "professional"},
            context={"shortage": shortage,
                     "agent_outputs": {"create_replenishment_po": po}})
        m = mail_res.get("result") or {}
        d = m.get("draft") or {}
        sent = "발송" if (m.get("success") and not m.get("skipped") and notify_to) else "draft"
        print(f"\n  ✉️  담당자 브리핑 [{(m.get('policy_applied') or {}).get('generated_via')}, {sent}] "
              f"→ {m.get('to')}")
        print(f"     제목: {d.get('subject')}")
    except ModuleNotFoundError as e:
        print(f"\n  ✉️  (브리핑 생략 — EmailAgent 의존성 미설치 로컬: {e}. VM 에선 동작.)")


async def replenish(agent, usb_id, qty, tier_lookup, override=None):
    """server dispatch 와 동일 정책으로 replenish_priority_queue 호출."""
    policy = {
        "ordering": ["tier", "target_delivery_date", "sale_order_id"],
        "tier_priority": dict(TIER_TABLE),
        "consume_all_for_vip_first": True,
    }
    if override:
        policy["priority_override_runtime"] = override
    context = {"receipt": {"product_id": usb_id, "qty": qty}, "tier_lookup": tier_lookup}
    res = await agent.execute_action("replenish_priority_queue", policy=policy, context=context)
    return res.get("result") or {}


def report(label, tracked, usb_id):
    """tracked = [(so_id, name, tier, demand)] → reserved 현황 테이블."""
    banner(f"[{label}] 배정 현황")
    inv = _inv(usb_id)
    print(f"  재고: on_hand={inv['on_hand']:.0f} reserved={inv['reserved']:.0f} "
          f"available={inv['available']:.0f}\n")
    print(f"  {'SO':10s} {'tier':9s} {'주문':>5s} {'배정':>5s}  상태")
    for so_id, name, tier, demand in tracked:
        if so_id is None:
            continue
        d, r = _so_reserved(so_id, usb_id)
        if r >= demand and demand > 0:
            st = "✅ 전량"
        elif r > 0:
            st = "🟡 부분"
        else:
            st = "⬜ 대기"
        print(f"  {name:10s} {tier:9s} {demand:>5.0f} {r:>5.0f}  {st}")


async def amain(args):
    do_it = args.yes
    if not odoo_service.is_available():
        odoo_service.authenticate_odoo()
    if not odoo_service.is_available():
        print("❌ Odoo 미연결")
        return 1
    usb_id = _usb_id()
    if not usb_id:
        print(f"❌ {USB_NAME} 없음")
        return 1

    if not do_it:
        print("\n*** DRY-RUN — 아무것도 변경하지 않습니다. 실제 실행은 --yes ***")

    agent = InventoryAgent(llm_config={"config_list": []})
    agent.register_tools_from_services(user_id="demo")

    # ── --setup-only: 리셋 + 6 SO 생성(할당·입고 X). Claude Desktop 자연어 시연용. ──
    if args.setup_only:
        reset_all(usb_id, do_it)
        banner("[세팅] 6 SO 생성 (입고·할당 전 — 모두 대기)")
        orders = [("Standard", 5), ("Standard", 10), ("VIP", 100),
                  ("VIP", 50), ("Standard", 30), ("VIP", 30)]
        created = []
        for tier, qty in orders:
            sid, name = create_so(usb_id, qty, tier, do_it)
            created.append((sid, name, tier, qty))
        std30 = next((c for c in created if c[2] == "Standard" and c[3] == 30), None)
        std30_id = std30[0] if std30 else "<Std30_id>"
        std30_nm = std30[1] if std30 else "<S00xxx>"
        banner("[런북] Claude Desktop 에 순서대로 입력 (VM MCP 트리거)")
        print(f"""
생성된 주문:
  {chr(10).join(f'  · {c[1]}  {c[2]:8s} USB x{c[3]}' for c in created if c[0])}

각 단계 후 대시보드(http://REDACTED_VM_IP:9601) → SO재고탭 + Recent Decisions 확인.

  [A] ▶ Claude 에 입력:
      "USB SecureKey-100 100개 입고됐어. 재고 배정해줘."
      → VIP100 이 100 선점, Standard 는 대기

  [B] ▶ Claude 에 입력:
      "USB SecureKey-100 50개 더 입고됐어. 배정해줘."
      → VIP50 이 50 배정, Standard 계속 대기

  [C] ▶ Claude 에 입력 (override — 반드시 '내가 승인' 포함):
      "USB SecureKey-100 50개 입고됐어. 비즈니스 사정상 {std30_nm}(주문번호 {std30_id})
       주문을 VIP보다 먼저 배정하도록 내가(ops.manager) 승인할게. 배정해줘."
      → {std30_nm}(Standard) 가 30 먼저, VIP30 은 남은 20 만 (부분)

  [D] ▶ Claude 에 입력 (BC5 — AI 개입):
      "USB SecureKey-100 아직 부족해서 주문들이 막혀 있어. 입고요청(보충 발주)하고
       구매담당자(ops@acme.com)한테 상황 알려줘."
      → AI 가 발주량 판단(LLM) + incoming picking 생성 + 담당자 브리핑 메일(LLM)
      → 그 입고건을 다시 입고 처리하면 rule 400 이 남은 VIP backorder 부터 충족 (루프 닫힘)

주의:
  · [C] 는 requested_by(승인자)가 있어야 override 발효 — '내가 승인' 문구 필수.
  · [D] 가 A/B/C(결정론)와 대비되는 'AI 판단' 포인트 — 발주량·브리핑을 LLM 이 생성.
""")
        return 0

    reset_all(usb_id, do_it)
    tier_lookup = {}
    tracked = []   # (so_id, name, tier, demand)

    # ── A ──
    banner("[A] 입고 100 + Std5 / Std10 / VIP100 → VIP 먼저 선점")
    add_stock(usb_id, 100, do_it)
    a_orders = [("Standard", 5), ("Standard", 10), ("VIP", 100)]
    a_tracked = []
    for tier, qty in a_orders:
        sid, name = create_so(usb_id, qty, tier, do_it)
        if sid:
            tier_lookup[sid] = tier
        a_tracked.append((sid, name, tier, qty))
    tracked += a_tracked
    if do_it:
        inner = await replenish(agent, usb_id, 100, tier_lookup)
        print(f"  replenish: {inner.get('note')}")
    report("A", tracked, usb_id)

    # ── B ──
    banner("[B] 입고 50 + VIP50 → VIP backorder 우선 (Std 는 계속 대기)")
    add_stock(usb_id, 50, do_it)
    sid, name = create_so(usb_id, 50, "VIP", do_it)
    if sid:
        tier_lookup[sid] = "VIP"
    b_tracked = [(sid, name, "VIP", 50)]
    tracked += b_tracked
    if do_it:
        inner = await replenish(agent, usb_id, 50, tier_lookup)
        print(f"  replenish: {inner.get('note')}")
    report("B", tracked, usb_id)

    # ── C ──
    banner("[C] 입고 50 + Std30 / VIP30 + 비즈니스 영향도 승인(override) → Std30 우선")
    add_stock(usb_id, 50, do_it)
    std30_id, std30_name = create_so(usb_id, 30, "Standard", do_it)
    vip30_id, vip30_name = create_so(usb_id, 30, "VIP", do_it)
    if std30_id:
        tier_lookup[std30_id] = "Standard"
    if vip30_id:
        tier_lookup[vip30_id] = "VIP"
    c_tracked = [(std30_id, std30_name, "Standard", 30), (vip30_id, vip30_name, "VIP", 30)]
    tracked += c_tracked
    if do_it:
        override = {
            "so_ids": [std30_id],
            "requested_by": "ops.manager@acme",
            "reason": "고객요청 + 회사 비즈니스 영향도 — Std-30 우선 승인",
            "cfg": {"mode": "equal_vip", "boost_score": 1000,
                    "requires_authorization": True},
        }
        print(f"  🔧 override: Std-30(id={std30_id}) by {override['requested_by']}")
        inner = await replenish(agent, usb_id, 50, tier_lookup, override=override)
        ov = inner.get("override") or {}
        print(f"  replenish: override.applied={ov.get('applied')} "
              f"reserved_so_ids={ov.get('reserved_so_ids')}")
    report("C", tracked, usb_id)

    # ── D (BC5) ── A/B/C(결정론) 와 대비되는 AI 개입 라운드.
    if not args.no_bc5:
        pause("[D] BC5 — 자율 보충 발주 + 담당자 브리핑")
        await bc5_replenish_check(agent, usb_id, args.notify_to, do_it)

    banner("끝")
    print("""
핵심 관찰:
  · A·B: rule(VIP-first) 만으로 결정론 배정 — LLM 0.
  · C: 사람이 승인한 override(S0) 로 Std-30 이 VIP-30 보다 먼저 — 여전히 LLM 0.
  · A·B·C = "온톨로지 정책 + agent 결정론 실행" (규칙으로 풀리는 정상 흐름).
  · D (BC5): 규칙으로 못 푸는 충족 불가 → AI 가 발주량 판단(LLM) + 담당자 브리핑(LLM)
            → 자율 보충 발주 + 통보. 여기서 비로소 'AI 개입'이 드러난다.
Dashboard: http://REDACTED_VM_IP:9601 → SO 재고 탭 / Recent Decisions
""")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="실제 Odoo 변경 실행 (없으면 dry-run)")
    ap.add_argument("--setup-only", action="store_true",
                    help="리셋 + 6 SO 생성만 (Claude Desktop 자연어 시연용 런북 출력)")
    ap.add_argument("--no-bc5", action="store_true",
                    help="[D] BC5 자율 보충 발주 라운드 생략 (A/B/C 만)")
    ap.add_argument("--notify-to", dest="notify_to", default="",
                    help="[D] 보충 브리핑 담당자 이메일 (없으면 draft 만)")
    args = ap.parse_args()
    sys.exit(asyncio.run(amain(args)))
