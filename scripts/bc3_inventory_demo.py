# scripts/bc3_inventory_demo.py
"""
BC3 — Inventory Allocation 시연 스크립트
============================================================
"BC2 Closed Won → Odoo SO" 까지 끝낸 상태에서, 이제 SO 가 confirmed 된 직후의
fulfillment 분기를 ontology 정책으로 제어하는 흐름을 보여준다.

핵심 가설 (시연으로 검증):
  · "Odoo (ERP) 가 transaction 을 다 한다.
     그 위에 ontology 정책 (VIP 선점, target_delivery 매칭, 입고 backorder 우선) 이 얹힌다."
  · yaml 한 줄로 정책이 바뀌면, 같은 코드가 다른 분기로 흐른다.

시나리오 (yu-ai-38 시리즈 — VIP Tech vs Standard Tech):
  1) [Setup] VIP Tech 의 SO 와 Standard Tech 의 SO 가 모두 동일 USB 제품에 의존.
  2) [재고 부족 가정] 가용재고 < VIP 수요. Standard 가 먼저 reserve 한 상태.
  3) [VIP SO confirmed] → order_split_by_line_type (420) 발화
        · service 라인 (Module X 라이선스) → send_license_activation
        · storable 라인 (USB) → delivery_ready_check 스폰
  4) [delivery_ready_check, VIP] → inventory_allocate_vip_preempt (410) 발화
        · Standard 의 'assigned' move 회수 (soft preempt)
        · VIP move 에 재할당
        · 부족분은 incoming PO 에 backorder
  5) [delivery_ready_check, Standard] → inventory_allocate_standard (390) 발화
        · 회수당한 Standard 는 Waiting 큐로 강등
  6) [stock_received] → stock_received_replenish (400) 발화
        · 큐를 tier+date 로 정렬해 VIP backorder 먼저 채움
        · 남은 양으로 Standard Waiting 충족
  7) [VIP picking state='assigned'] → delivery_ready_to_ship_vip (405) 발화
        · validate_picking → state='done'
        · send_shipping_notification + update_delivery_milestone

실행 모드:
  · 기본: Odoo 미연결도 OK (각 action 이 plan-only 반환). 정책 분기 검증용.
  · ODOO_* 환경변수 설정 시 실제 Odoo 작업 수행.
  · LIVE_GMAIL=1 설정 시 이메일 실발송. 기본은 draft 만 반환.

사용:
  python scripts/bc3_inventory_demo.py
"""
import asyncio
import json
import os
import sys
import types
from pathlib import Path
from datetime import datetime, timedelta

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass


# ════════════════════════════════════════════════════════════════
# 외부 의존성 stub — google/openai/simple-salesforce 미설치 환경에서도
# 데모가 돌도록. production 환경 (requirements.txt 설치) 에서는 stub 이
# 절대 활성화되지 않음 (실제 모듈이 먼저 로드됨).
# ════════════════════════════════════════════════════════════════
def _ensure_stub(mod_name: str, attrs: dict = None):
    """sys.modules 에 가짜 모듈을 등록 (이미 있으면 skip)."""
    if mod_name in sys.modules:
        return
    parts = mod_name.split(".")
    for i in range(1, len(parts) + 1):
        sub = ".".join(parts[:i])
        if sub not in sys.modules:
            mod = types.ModuleType(sub)
            sys.modules[sub] = mod
    if attrs:
        for k, v in attrs.items():
            setattr(sys.modules[mod_name], k, v)


try:
    import google.auth  # noqa: F401
except ImportError:
    _ensure_stub("google")
    _ensure_stub("google.auth")
    _ensure_stub("google.auth.transport")
    _ensure_stub("google.auth.transport.requests", {"Request": object})
    _ensure_stub("google.oauth2", {"credentials": types.ModuleType("credentials")})
    _ensure_stub("google.oauth2.credentials", {"Credentials": object})
    _ensure_stub("google_auth_oauthlib", {"flow": types.ModuleType("flow")})
    _ensure_stub("google_auth_oauthlib.flow", {"InstalledAppFlow": object})
    _ensure_stub("googleapiclient", {"discovery": types.ModuleType("discovery"),
                                       "errors": types.ModuleType("errors")})
    _ensure_stub("googleapiclient.discovery", {"build": lambda *a, **k: None})
    _ensure_stub("googleapiclient.errors", {"HttpError": Exception})

try:
    import simple_salesforce  # noqa: F401
except ImportError:
    _ensure_stub("simple_salesforce", {"Salesforce": object,
                                        "SalesforceAuthenticationFailed": Exception,
                                        "SalesforceError": Exception})

try:
    import chromadb  # noqa: F401
except ImportError:
    _ensure_stub("chromadb", {"PersistentClient": object,
                               "Client": object})


# Ontology + agents
from mcp_server.ontology_engine.engine import OntologyEngine  # noqa: E402
from mcp_server.agents.inventory_agent import InventoryAgent  # noqa: E402
from mcp_server.agents.email_agent import EmailAgent  # noqa: E402
from mcp_server.agents.crm_agent import CRMAgent  # noqa: E402
from mcp_server.agents.calendar_agent import CalendarAgent  # noqa: E402
from mcp_server.agents.erp_agent import ERPAgent  # noqa: E402


# ════════════════════════════════════════════════════════════════
# 시연용 datafixtures
# ════════════════════════════════════════════════════════════════
TARGET_DELIVERY_VIP = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")
TARGET_DELIVERY_STD = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")

# SFDC Closed Won → Odoo SO 이후 상태로 합성.
# 실제 환경에서는 odoo_service.find_existing_sales_order() 로 가져옴.
VIP_SO = {
    "id": 1001,                                # Odoo sale.order.id (시연용)
    "name": "S00101",
    "state": "sale",
    "client_order_ref": "Opp-VIP-Tech-2026Q2", # = SFDC Opp.Name
    "tier": "VIP",
    "account_name": "VIP Tech",
    "amount_total": 142_000,
    "target_delivery_date": TARGET_DELIVERY_VIP,
    "has_storable_lines": True,                # USB / Appliance / Edge
    "has_service_lines": True,                 # Module X license + Consulting
}

STANDARD_SO = {
    "id": 1002,
    "name": "S00102",
    "state": "sale",
    "client_order_ref": "Opp-Standard-Tech-2026Q2",
    "tier": "Standard",
    "account_name": "Standard Tech",
    "amount_total": 12_500,
    "target_delivery_date": TARGET_DELIVERY_STD,
    "has_storable_lines": True,                # USB 5개
    "has_service_lines": False,
}

# 재고 부족 가정 — VIP 수요 1200 > 가용 5
INVENTORY_USB_BEFORE = {
    "product_id": 501,
    "product_name": "Encrypted USB 128GB",
    "on_hand": 5,
    "reserved": 5,                             # Standard 가 이미 5개 잡음
    "available": 0,
    "incoming": 2000,                          # 다음 주 PO 도착 예정
    "projected_avail": 2000,
}

# VIP 의 picking 정보 (Odoo 가 SO confirm 시 자동 생성)
VIP_PICKING = {
    "id": 5001,
    "name": "WH/OUT/00101",
    "state": "confirmed",                      # 가용재고 0 이라 confirmed (자동 reserve 실패)
    "scheduled_date": TARGET_DELIVERY_VIP,
    "sale_order_id": VIP_SO["id"],
    "tier": "VIP",
    "qty_demand": 1200,
    "priority_score": 100,
    "product_id": 501,
    "account_name": "VIP Tech",
}

STD_PICKING = {
    "id": 5002,
    "name": "WH/OUT/00102",
    "state": "assigned",                       # Standard 가 먼저 reserve 했다고 가정
    "scheduled_date": TARGET_DELIVERY_STD,
    "sale_order_id": STANDARD_SO["id"],
    "tier": "Standard",
    "qty_demand": 5,
    "priority_score": 50,
    "product_id": 501,
    "account_name": "Standard Tech",
}

# 입고 이벤트
STOCK_RECEIPT = {
    "product_id": 501,
    "product_name": "Encrypted USB 128GB",
    "qty": 2000,                               # 다음 주 PO 도착
    "received_at": datetime.now().isoformat(),
    "source_po_id": 7001,
}


# ════════════════════════════════════════════════════════════════
# 출력 헬퍼
# ════════════════════════════════════════════════════════════════
def line(char: str = "═", length: int = 78) -> str:
    return char * length


def banner(text: str, char: str = "═"):
    print(line(char))
    print(f" {text}")
    print(line(char))


def step_header(num: int, title: str):
    print()
    print(line("─"))
    print(f" Step {num}: {title}")
    print(line("─"))


def show_inventory(state: dict, label: str = "Inventory"):
    print(f"\n[{label}] {state.get('product_name')} (Id={state.get('product_id')})")
    print(f"  · on_hand          = {state.get('on_hand')}")
    print(f"  · reserved         = {state.get('reserved')}")
    print(f"  · available        = {state.get('available')}")
    print(f"  · incoming (≤target) = {state.get('incoming')}")
    print(f"  · projected_avail  = {state.get('projected_avail')}")


def show_step_result(rule_name: str, plan: list, result_summaries: list):
    print(f"\n  ✦ rule matched : {rule_name}")
    print(f"  ✦ delegations  : {len(plan)} agent(s)")
    for i, step in enumerate(plan):
        agent = step.get("agent")
        action = step.get("action")
        print(f"      [{i}] {agent}.{action}")
    print(f"  ✦ outcomes     :")
    for s in result_summaries:
        print(f"      · {s}")


def summarize(res: dict, max_chars: int = 220) -> str:
    """action 결과를 한 줄 요약 (demo 가독성용)."""
    if not isinstance(res, dict):
        return str(res)[:max_chars]
    inner = res.get("result") if "result" in res else res
    if isinstance(inner, dict):
        action = inner.get("action") or "(action)"
        success = inner.get("success")
        note = inner.get("note") or ""
        skipped = inner.get("skipped")
        prefix = f"{action} (ok={success}{' SKIP' if skipped else ''})"
        extras = []
        for k in ("subject", "tier", "preempted_moves", "shortage",
                  "remaining_after_replenish", "vip_first_count",
                  "validation_result", "milestone", "draft"):
            if k in inner:
                v = inner[k]
                if isinstance(v, (list, dict)):
                    if isinstance(v, list):
                        extras.append(f"{k}={len(v)}건")
                    elif "subject" in v:
                        extras.append(f"draft.subject={v['subject'][:60]!r}")
                    else:
                        extras.append(f"{k}={json.dumps(v, ensure_ascii=False)[:80]}")
                else:
                    extras.append(f"{k}={str(v)[:60]}")
        return f"{prefix} {' '.join(extras)} — {note[:80]}"[:max_chars]
    return str(res)[:max_chars]


# ════════════════════════════════════════════════════════════════
# Agent 라이트 부팅 — server.py 없이 데모에서도 동일 dispatch 흐름
# ════════════════════════════════════════════════════════════════
def boot_agents(user_id: str = "demo") -> dict:
    """server.py 의 get_or_create_orchestrator 를 흉내내어 agent 사전 등록."""
    AGENT_LLM_CONFIG = {"temperature": 0.2, "max_tokens": 500}

    inventory_agent = InventoryAgent(llm_config=AGENT_LLM_CONFIG)
    inventory_agent.register_tools_from_services(user_id=user_id)

    email_agent = EmailAgent(llm_config=AGENT_LLM_CONFIG)
    email_agent.register_tools_from_services(user_id=user_id)

    crm_agent = CRMAgent(llm_config=AGENT_LLM_CONFIG)
    crm_agent.register_tools_from_services(user_id=user_id)

    calendar_agent = CalendarAgent(llm_config=AGENT_LLM_CONFIG)
    calendar_agent.register_tools_from_services(user_id=user_id)

    erp_agent = ERPAgent(llm_config=AGENT_LLM_CONFIG)
    erp_agent.register_tools_from_services(user_id=user_id)

    return {
        "inventory_agent": inventory_agent,
        "email_agent": email_agent,
        "crm_agent": crm_agent,
        "calendar_agent": calendar_agent,
        "erp_agent": erp_agent,
    }


async def run_dispatch(engine: OntologyEngine, agents: dict,
                       entity: str, payload: dict) -> dict:
    """
    server.process_with_ontology 의 핵심만 발췌:
      resolve_links → check_rules → trigger_events → execute_action 루프
    """
    ctx = engine.resolve_links(entity, payload)
    action = engine.check_rules(ctx)
    if not action:
        return {"matched": None, "ctx": ctx, "plan": [], "results": []}

    rule_name = action.get("rule_name")
    plan = engine.trigger_events(action, ctx)

    # 공통 context — 각 step 이 같이 받음 (server.py 와 동일 구조)
    base_context = {
        **{k: v for k, v in ctx.items() if k != "entity"},
        "entity": entity,
        "payload": payload,
    }
    agent_outputs = {}
    step_results = []
    for idx, step in enumerate(plan):
        if step.get("kind") != "delegate":
            continue
        agent_id = step["agent"]
        action_name = step["action"]
        policy = step.get("policy", {}) or {}
        agent_obj = agents.get(agent_id)
        if agent_obj is None:
            step_results.append({"agent": agent_id, "action": action_name,
                                 "error": "agent 미등록"})
            continue
        step_ctx = {**base_context, "agent_outputs": dict(agent_outputs)}
        res = await agent_obj.execute_action(action_name, policy=policy, context=step_ctx)
        inner = res.get("result") if isinstance(res, dict) else None
        if isinstance(inner, dict):
            agent_outputs[action_name] = inner
            agent_outputs[agent_obj.name] = inner
        step_results.append({"agent": agent_id, "action": action_name, "result": res})

    return {"matched": rule_name, "ctx": ctx, "plan": plan, "results": step_results,
            "agent_outputs": agent_outputs}


# ════════════════════════════════════════════════════════════════
# 메인 시나리오
# ════════════════════════════════════════════════════════════════
async def main():
    banner("BC3 — Inventory Allocation 시연 (VIP Tech vs Standard Tech)")
    print()
    print("가설: 'Odoo 가 fulfillment transaction 을 다 한다.")
    print("       그 위에 ontology 정책 (VIP 선점, target_delivery, 입고 backorder) 이 얹힌다.'")
    print()
    print("Odoo 환경변수 미설정 시 — 각 action 은 plan-only 결과를 반환합니다.")
    print("ODOO_URL / ODOO_DB / ODOO_USERNAME / ODOO_API_KEY 가 세팅되면 실제 작업 수행.")

    # 0) Engine + agents 부팅
    engine = OntologyEngine(str(PROJECT_ROOT / "ontology" / "ontology.yaml"))
    agents = boot_agents(user_id=os.getenv("DEMO_USER_ID", "demo"))

    # ─── Step 1: 재고 상태 사전 점검 (Standard 가 이미 잡고 있음) ───
    step_header(1, "재고 사전 상태 — Standard 가 5개 reserve, VIP 1200개 demand 발생 임박")
    show_inventory(INVENTORY_USB_BEFORE, "Inventory @t0")
    print("\n  → VIP 의 1200 demand vs available 0  → shortage 1200 발생 예상")
    print("    → 정책 결정 포인트: VIP 가 Standard 의 'assigned' move 를 회수할 것인가? (soft)")

    # ─── Step 2: VIP SO Confirmed → order_split_by_line_type 발화 ───
    step_header(2, "VIP SO Confirmed → order_split_by_line_type (priority 420)")
    print("  payload: { sales_order: VIP_SO (has_storable=True, has_service=True) }")
    out = await run_dispatch(engine, agents, "sale_order_confirmed",
                              {"sales_order": VIP_SO})
    show_step_result(
        out["matched"], out["plan"],
        [summarize(r["result"]) for r in out["results"]],
    )

    # split 결과에서 spawn_events 회수 (실제 환경에선 dispatcher 가 자동 fanout)
    split_inner = (out["results"][0]["result"].get("result")
                   if out["results"] else {}) or {}
    spawn_events_vip = split_inner.get("spawn_events", []) or []
    service_plan_vip = split_inner.get("service_plan", []) or []
    print(f"\n  ★ split 결과 spawn_events: {len(spawn_events_vip)}건 (delivery_ready_check)")
    print(f"  ★ split 결과 service_plan : {len(service_plan_vip)}건 "
          f"({[s['action'] for s in service_plan_vip]})")

    # service_plan 직접 fanout — 데모 편의 (실전은 dispatcher 가 처리)
    for sp in service_plan_vip:
        ag = agents.get(sp["agent"])
        if not ag:
            continue
        sp_ctx = {"sales_order": VIP_SO, "customer": {"tier": "VIP", "name": "VIP Tech"}}
        res = await ag.execute_action(sp["action"], policy=sp["policy"], context=sp_ctx)
        print(f"  · fanout {sp['agent']}.{sp['action']}: {summarize(res)}")

    # ─── Step 3: Standard SO Confirmed (역시 storable) → 같은 룰 발화, 다른 스폰 ───
    step_header(3, "Standard SO Confirmed → order_split_by_line_type 또 발화 (Standard tier)")
    out_std = await run_dispatch(engine, agents, "sale_order_confirmed",
                                  {"sales_order": STANDARD_SO})
    show_step_result(
        out_std["matched"], out_std["plan"],
        [summarize(r["result"]) for r in out_std["results"]],
    )
    split_std_inner = (out_std["results"][0]["result"].get("result")
                       if out_std["results"] else {}) or {}
    spawn_events_std = split_std_inner.get("spawn_events", []) or []
    print(f"\n  ★ Standard spawn_events: {len(spawn_events_std)}건")

    # ─── Step 4: VIP delivery_ready_check → 재고 부족 → VIP 선점 (soft) ───
    step_header(4, "VIP delivery_ready_check → inventory_allocate_vip_preempt (410)")
    print("  → 'Standard 가 먼저 잡은' assigned move 를 회수해 VIP 에 재할당하는 분기.")
    out_vip_check = await run_dispatch(engine, agents, "delivery_ready_check",
                                        {"picking": VIP_PICKING,
                                         "inventory": INVENTORY_USB_BEFORE})
    show_step_result(
        out_vip_check["matched"], out_vip_check["plan"],
        [summarize(r["result"]) for r in out_vip_check["results"]],
    )

    # ─── Step 5: Standard delivery_ready_check → Standard FIFO (390) ───
    step_header(5, "Standard delivery_ready_check → inventory_allocate_standard (390)")
    print("  → 회수당한 Standard 는 가용 0 이므로 Waiting 으로 강등될 것.")
    # 회수 후 Standard 의 시각상 available = 0 (시연용 합성)
    std_inventory_after_preempt = {**INVENTORY_USB_BEFORE,
                                   "reserved": 1200,   # VIP 가 다 잡았다고 가정
                                   "available": 0}
    out_std_check = await run_dispatch(engine, agents, "delivery_ready_check",
                                        {"picking": STD_PICKING,
                                         "inventory": std_inventory_after_preempt})
    show_step_result(
        out_std_check["matched"], out_std_check["plan"],
        [summarize(r["result"]) for r in out_std_check["results"]],
    )

    # ─── Step 6: stock_received → 입고 보충 → VIP backorder 먼저, Standard 그 다음 ───
    step_header(6, "stock_received → stock_received_replenish (400)")
    print(f"  입고: {STOCK_RECEIPT['qty']}개 (USB)")
    out_recv = await run_dispatch(engine, agents, "stock_received",
                                   {"receipt": STOCK_RECEIPT})
    show_step_result(
        out_recv["matched"], out_recv["plan"],
        [summarize(r["result"]) for r in out_recv["results"]],
    )

    # ─── Step 7: VIP picking 이 'assigned' 됐다고 가정 → 출고 ───
    # BC3 CRIT #2 봉합 이후: 410 의 if 에 `picking.state != 'assigned'` 가드가 들어가서
    #   inventory payload 를 같이 보내도 405 만 매칭된다 (410 은 state 가드로 막힘).
    # 따라서 inventory payload 누락 hack 이 더 이상 필요 없다 — 정상 payload 로 405 발화.
    step_header(7, "VIP picking state='assigned' → delivery_ready_to_ship_vip (405)")
    vip_picking_assigned = {**VIP_PICKING, "state": "assigned"}
    out_ship = await run_dispatch(engine, agents, "delivery_ready_check",
                                   {"picking": vip_picking_assigned,
                                    "inventory": INVENTORY_USB_BEFORE,
                                    "sales_order": VIP_SO})
    show_step_result(
        out_ship["matched"], out_ship["plan"],
        [summarize(r["result"]) for r in out_ship["results"]],
    )

    # ─── 마무리 요약 ───
    banner("Demo 종료 — 정책 분기 매트릭스 요약", "═")
    print()
    print("  rule (priority)                   trigger entity            발화 결과")
    print("  ─────────────────────────────────────────────────────────────────────")
    print("  order_split_by_line_type (420)    sale_order_confirmed      VIP & Std 둘 다 분기")
    print("  inventory_allocate_vip_preempt    delivery_ready_check      VIP soft preempt")
    print("  delivery_ready_to_ship_vip (405)  delivery_ready_check      VIP assigned → ship")
    print("  stock_received_replenish (400)    stock_received            VIP backorder 우선")
    print("  inventory_allocate_standard (390) delivery_ready_check      Standard FIFO/Waiting")
    print()
    print("  정책 변경 = yaml 한 줄 수정.  코드 재배포 X.")
    print("  Ontology = '어떻게 → 누가, 어디서, 언제' 의 매뉴얼.  ERP = 실제 트랜잭션 실행자.")
    print()


if __name__ == "__main__":
    asyncio.run(main())
