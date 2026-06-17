# scripts/bc5_demo_scenario12.py
"""
시연 시나리오 1 & 2 전용 셋업 + 런북.

★ 제품 구성 (반드시 명심):
  · 이 시나리오의 **메인 제품 = 모듈(Module X ~, service 타입, 재고 추적 X).**
  · USB SecureKey-100 은 **재고 관리를 보여주려고 끼워넣은 storable 품목.**
  · 그래서 모든 주문 = [기본 모듈(service) × N] + [USB(storable) × N].  (N = USB 수량)
  · 재고 이야기는 USB 수량 기준으로 함. (모듈은 재고 무관 — BC3 split_fulfillment_path
    가 service 라인=라이선스 활성화 / storable 라인=재고 할당 으로 자동 분기.)

전제: USB picking 은 manual reservation (SO 확정해도 자동 reserve 안 됨 → 전부 waiting).

────────────────────────────────────────────────────────────────────
시나리오 1 — VIP 재고 선점 (Ontology 결정론)
  · 재고: USB 200 이 "입고 대기(incoming, 미검증)" 상태로 존재
  · 고객 4명 / 주문 4건 (생성순 = id순, 모두 waiting). 메인 모듈은 골고루:
        S01 Customer A (Standard)  Module X 10           + USB 10
        S02 Customer B (Standard)  SmartBox Pro 1TB 5    + USB 5
        S03 Customer C (VIP)       SecureGate Software 200 + USB 200   ← S04 보다 먼저 생성
        S04 Customer D (VIP)       SmartBox Pro 5TB 300  + USB 300
  · Claude 에 "USB 200 입고됐어. 입고 처리하고 배정해줘."
        → trigger_stock_received 가 입고 picking 검증(button_validate) +
          replenish_priority_queue 로 VIP 우선 배정 (사용자 수동 승인 불필요)
  · 결과: VIP S03(Customer C) 가 USB 200 전량 Assigned. S01·S02·S04 대기.

시나리오 2 — 부족 → 자율 보충 발주 + 담당자 브리핑 (BC5, Inventory Agent)
  · 시나리오1 직후, S01·S02·S04 부족 (USB 미충족 합 = 10+5+300 = 315)
  · Claude 에 "아직 부족한 주문들 입고요청(보충 발주)하고 담당자한테 메일로 알려줘."
        → trigger_replenishment_check → 발주량 advisor(LLM) + incoming picking +
          담당자 브리핑 메일 (notify_to = finance@example.com)

────────────────────────────────────────────────────────────────────
실행:
    python scripts/bc5_demo_scenario12.py            # dry — 계획만 출력
    python scripts/bc5_demo_scenario12.py --yes      # 실제 Odoo 셋업 + 런북 출력
    python scripts/bc5_demo_scenario12.py --reset     # 리셋만
"""
import argparse
import asyncio
import sys
from pathlib import Path

for _s in ("stdout", "stderr"):
    try:
        getattr(sys, _s).reconfigure(encoding="utf-8")
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

USB_NAME = "USB SecureKey-100"          # storable — 재고 관리 시연용 품목
NOTIFY_TO = "finance@example.com"     # 데모 담당자(공개 채널 메일)
INCOMING_QTY = 200
INCOMING_ORIGIN = "DEMO-S1 USB 200 incoming (manual receipt)"

# (코드, 고객명, tier, USB수량, 메인모듈(service)). 고객 A/B/C/D 1:1.
# S03(C,VIP200) < S04(D,VIP300) 순 생성. 메인 모듈은 골고루(Odoo 실제 service 제품) —
# 재고는 USB 기준이라 모듈명은 시연 디테일. 각 주문은 [모듈 + USB] 두 라인, 같은 수량.
CUSTOMERS = [
    ("S01", "Customer A", "Standard", 10,  "Module X"),
    ("S02", "Customer B", "Standard", 5,   "SmartBox Pro 1TB"),
    ("S03", "Customer C", "VIP",      200, "SecureGate Software"),
    ("S04", "Customer D", "VIP",      300, "SmartBox Pro 5TB"),
]


def banner(s):
    print("\n" + "=" * 78 + "\n " + s + "\n" + "=" * 78)


def _nid(raw):
    return int(raw[0]) if isinstance(raw, list) and raw else int(raw)


def _usb_id():
    pids = odoo_service.call("product.product", "search", [("name", "=", USB_NAME)])
    return int(pids[0]) if pids else None


def _ensure_category(name):
    ids = odoo_service.call("res.partner.category", "search", [("name", "=", name)])
    if ids:
        return int(ids[0])
    return _nid(odoo_service.call("res.partner.category", "create", [{"name": name}]))


def _ensure_customer(name, tier):
    """이름으로 파트너 조회/생성 + tier 카테고리 태그 보장 (get_sale_order_tier_map 이 읽음)."""
    cat = _ensure_category(tier)
    ids = odoo_service.call("res.partner", "search", [("name", "=", name)])
    if ids:
        pid = int(ids[0])
        try:
            odoo_service.call("res.partner", "write", [pid], {"category_id": [(4, cat)]})
        except Exception as e:
            print(f"    ⚠️ {name} category 태그 실패: {str(e)[:80]}")
        return pid
    return _nid(odoo_service.call("res.partner", "create", [{
        "name": name, "is_company": True, "customer_rank": 1,
        "category_id": [(6, 0, [cat])],
        "comment": f"DEMO {tier} customer",
    }]))


def _ensure_module(name):
    """메인 모듈(service 타입, 재고 추적 X) 조회/생성. Odoo 에 이미 있으면 그걸 사용."""
    return odoo_service.find_or_create_product(name, 500, "service")


def _price(product_id):
    rec = odoo_service.call("product.product", "read", [product_id], fields=["list_price"])
    return (rec[0].get("list_price") if rec else 0) or 0


def _inv(usb_id):
    return odoo_service.get_inventory_state(usb_id)


def reset_all(usb_id, do_it):
    banner("[리셋] 기존 SO 취소·삭제 + 재고 0 + 예약 해제 + 미검증 입고 정리")
    so_ids = odoo_service.call("sale.order", "search",
                               [("state", "in", ["draft", "sent", "sale"])])
    print(f"  기존 SO {len(so_ids)} 건: {so_ids}")
    if not do_it:
        print("  (dry-run — 변경 안 함)")
        return
    for sid in so_ids:
        try:
            odoo_service.call("sale.order", "action_cancel", [sid])
        except Exception as e:
            print(f"    ⚠️ cancel {sid}: {str(e)[:80]}")
    # picking 레코드 완전 삭제 — 취소만 하면 'cancel' 상태로 목록에 남으므로 unlink 까지.
    # done(실제 출고완료) 은 보존. 그 외(대기/준비/취소 등)는 취소 후 삭제.
    for code, label in (("outgoing", "출고(WH/OUT)"), ("incoming", "입고(WH/IN)")):
        pids = odoo_service.call(
            "stock.picking", "search",
            [("picking_type_id.code", "=", code), ("state", "!=", "done")])
        if pids:
            print(f"  {label} picking {len(pids)} 건 취소·삭제: {pids}")
        else:
            print(f"  {label} picking 0 건")
        for pid in pids:
            try:
                odoo_service.call("stock.picking", "action_cancel", [pid])
            except Exception as e:
                print(f"    ⚠️ {label} cancel {pid}: {str(e)[:80]}")
        for pid in pids:
            try:
                odoo_service.call("stock.picking", "unlink", [pid])
            except Exception as e:
                print(f"    ⚠️ {label} unlink {pid} (cancel 상태로 남음): {str(e)[:80]}")
    # SO 삭제 (picking 정리 후라야 깔끔히 unlink)
    for sid in so_ids:
        try:
            odoo_service.call("sale.order", "unlink", [sid])
        except Exception as e:
            print(f"    ⚠️ SO unlink {sid} (cancel 상태로 남김): {str(e)[:80]}")
    quant_ids = odoo_service.call(
        "stock.quant", "search",
        [("product_id", "=", usb_id), ("location_id.usage", "=", "internal")])
    for qid in quant_ids:
        try:
            odoo_service.call("stock.quant", "write", [qid], {"inventory_quantity": 0})
        except Exception as e:
            print(f"    ⚠️ quant write {qid}: {str(e)[:80]}")
    if quant_ids:
        try:
            odoo_service.call("stock.quant", "action_apply_inventory", quant_ids)
        except Exception as e:
            print(f"    ⚠️ action_apply_inventory: {str(e)[:80]}")
    inv = _inv(usb_id)
    print(f"  → USB on_hand={inv['on_hand']:.0f} reserved={inv['reserved']:.0f} "
          f"available={inv['available']:.0f}")


def create_incoming(usb_id, do_it):
    banner(f"[입고 준비] USB {INCOMING_QTY} 입고 대기(incoming, 미검증) 생성")
    if not do_it:
        print(f"  · (dry) USB {INCOMING_QTY} incoming picking 생성 예정")
        return None
    res = odoo_service.create_incoming_picking(
        usb_id, INCOMING_QTY, vendor_name="TechSupply Co",
        origin=INCOMING_ORIGIN, confirm=True)
    print(f"  · {res.get('picking_name')} state={res.get('state')} "
          f"(검증 전 — Claude 가 trigger_stock_received 로 검증+배정)")
    return res


def create_orders(usb_id, do_it):
    banner("[주문] S01~S04 — 고객 A/B/C/D 1:1, 각 주문 = 메인 모듈(service) + USB(재고)")
    usb_price = _price(usb_id)
    created = []
    for code, cust, tier, qty, module_name in CUSTOMERS:
        if not do_it:
            print(f"  · (dry) {code} {cust}({tier})  {module_name} x{qty} + USB x{qty}")
            created.append((code, cust, tier, qty, module_name, None, f"(dry-{code})"))
            continue
        module_id = _ensure_module(module_name)
        mod_price = _price(module_id)
        partner_id = _ensure_customer(cust, tier)
        raw = odoo_service.call("sale.order", "create", [{
            "partner_id": partner_id,
            "client_order_ref": f"DEMO-{code}",
            "order_line": [
                # 메인 모듈(service) — 재고 무관, 반드시 포함
                (0, 0, {"product_id": module_id, "product_uom_qty": qty,
                        "price_unit": mod_price}),
                # USB(storable) — 재고 할당 대상
                (0, 0, {"product_id": usb_id, "product_uom_qty": qty,
                        "price_unit": usb_price}),
            ],
        }])
        so_id = _nid(raw)
        odoo_service.call("sale.order", "action_confirm", [so_id])
        name = odoo_service.call("sale.order", "read", [so_id], fields=["name"])[0]["name"]
        print(f"  · {code} → {name} = {cust}({tier})  {module_name} x{qty} + USB x{qty} (id={so_id})")
        created.append((code, cust, tier, qty, module_name, so_id, name))
    return created


def runbook(created):
    banner("[런북] Claude Desktop 에 순서대로 입력 (VM MCP 트리거)")
    s03 = next((c for c in created if c[0] == "S03"), None)
    s03_name = s03[6] if s03 else "S0xx"
    s03_cust = s03[1] if s03 else "Customer C"
    print(f"""
사전 (Odoo 화면으로 보여주기):
  · 재고관리 → 입고: {INCOMING_ORIGIN!r} = USB {INCOMING_QTY} (검증 전 '대기')
  · 판매: S01(A·Std) S02(B·Std) S03(C·VIP) S04(D·VIP) — 각 주문에 메인 모듈(service) + USB,
    USB 라인 모두 '대기(waiting)'
  · ※ 메인 제품은 모듈(Module X / SmartBox / SecureGate, 서비스·재고無), USB 는 재고 시연용.
    재고 수량은 USB 기준.

──────────────────────────────────────────────────────────────────
[시나리오 1] VIP 재고 선점 — Ontology 결정론
  ▶ Claude 에 입력:
     "USB SecureKey-100 200개 입고됐어. 입고 처리하고 재고 배정해줘."
  → trigger_stock_received(product_name="USB SecureKey-100", qty=200)
     (Claude 가 입고 picking 검증 + VIP 우선 배정 — Odoo 수동 승인 불필요)
  ✅ 기대: VIP {s03_name}({s03_cust}) 의 USB 200 전량 Assigned. S01·S02·S04 대기.
  확인: Claude 응답 / Odoo({s03_name} 'assigned') / Dashboard Recent Decisions

  ※ 자동 검증이 안 되면 fallback:
     Odoo 에서 입고건 [검증] 클릭 → Claude 에 "배정만 해줘"
     → trigger_inventory_allocation_window (재고 안 더하고 tier 우선 배정)

──────────────────────────────────────────────────────────────────
[시나리오 2] 부족 → 자율 보충 발주 + 담당자 브리핑 — BC5 (Inventory Agent)
  ▶ Claude 에 입력:
     "USB SecureKey-100 아직 부족해서 막힌 주문들 있잖아. 부족분 입고요청(보충 발주)하고
      담당자한테 메일로 알려줘."
  → trigger_replenishment_check(product_name="USB SecureKey-100")
  ✅ 기대: 미충족 315개(S04 300 + S01 10 + S02 5) 감지 →
     AI 발주량 판단(LLM) + incoming picking 생성 + 담당자 브리핑 메일 → {NOTIFY_TO}
  확인: Claude 응답 / Odoo(새 incoming picking) / Gmail({NOTIFY_TO}) / Dashboard

──────────────────────────────────────────────────────────────────
메시지 포인트:
  · 시나리오1 = "흔한 VIP 선점" → Ontology 규칙만으로 결정론 (LLM 0).
  · 시나리오2 = "규칙으로 못 푸는 재고 부족" → Inventory Agent 가 발주량·브리핑을
    LLM 으로 판단·자율 대응. 여기서 'AI 개입'이 드러남.
Dashboard: http://REDACTED_VM_IP:9601 → SO재고 탭 / Recent Decisions
""")


async def amain(args):
    if not odoo_service.is_available():
        odoo_service.authenticate_odoo()
    if not odoo_service.is_available():
        print("❌ Odoo 미연결")
        return 1
    usb_id = _usb_id()
    if not usb_id:
        print(f"❌ {USB_NAME} 없음 (storable 제품 먼저 등록 필요)")
        return 1

    do_it = args.yes
    if not do_it and not args.reset:
        print("\n*** DRY-RUN — 변경 없음. 실제 셋업은 --yes ***")

    reset_all(usb_id, do_it or args.reset)
    if args.reset:
        print("\n리셋 완료.")
        return 0

    create_incoming(usb_id, do_it)
    created = create_orders(usb_id, do_it)
    runbook(created)
    if not do_it:
        print("\n(위는 dry-run 계획. 실제 생성은 --yes)")
    return 0


def main():
    ap = argparse.ArgumentParser(description="시연 시나리오 1·2 셋업 + 런북")
    ap.add_argument("--yes", action="store_true", help="실제 Odoo 셋업 실행 (없으면 dry-run)")
    ap.add_argument("--reset", action="store_true", help="리셋만 (동일 출발선)")
    args = ap.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
