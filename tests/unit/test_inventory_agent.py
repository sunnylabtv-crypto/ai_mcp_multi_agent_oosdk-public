# tests/unit/test_inventory_agent.py
"""
BC3 — inventory_agent 단위 테스트.

다음 sprint 의 CRIT/HIGH/MED gap 수정이 의도대로 동작함을 보장한다.
spec: docs/BC3_WEEK1_SPEC_v1_inventory_allocation.md (Section 9.2 의 T1~T6).

  T1. Mixed SO (storable + service) → split 의 service_plan 에
      email_agent.send_license_activation 진입점이 포함됨.    [CRIT #3 의 표면]
  T2. allocate_with_preemption 이 자기 SO 의 다른 picking move 를
      preempt 후보에서 제외 (sale_order_id 비교).             [HIGH #5]
  T3. replenish_priority_queue → replenished=[] 일 때
      send_allocation_notice 가 skip 됨.                      [HIGH #6]
  T4. 405 vs 410 yaml mutex: VIP picking 이 partially_available
      이고 inventory.available < qty_demand 일 때 410 만 매칭. [CRIT #2]
  T5. list_open_moves 가 'id' 키 없는 row 를 반환해도
      unreserve_move 가 호출되지 않음.                        [CRIT #1]
  T6. allocate_fifo: reserve_move 후 reserved_availability 가
      demand 와 동일하면 moves_reserved 에 분류 (stale 값 사용 X).[HIGH #8]

실행:
    cd ai_mcp_multi_agent_oosdk
    python -m unittest tests.unit.test_inventory_agent
혹은 (pytest 설치 시):
    python -m pytest tests/unit/test_inventory_agent.py -v
"""
import asyncio
import os
import sys
import tempfile
import types
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ════════════════════════════════════════════════════════════════
# 외부 의존성 stub — google/openai 미설치 환경에서도 테스트가 돌도록.
# EmailAgent 가 import 시점에 google.auth 를 끌어들이므로 미리 stub.
# (scripts/bc3_inventory_demo.py 와 동일 패턴.)
# ════════════════════════════════════════════════════════════════
def _ensure_stub(mod_name: str, attrs: Optional[Dict[str, Any]] = None) -> None:
    if mod_name in sys.modules:
        return
    parts = mod_name.split(".")
    for i in range(1, len(parts) + 1):
        sub = ".".join(parts[:i])
        if sub not in sys.modules:
            sys.modules[sub] = types.ModuleType(sub)
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
    _ensure_stub("google.oauth2")
    _ensure_stub("google.oauth2.credentials", {"Credentials": object})
    _ensure_stub("google_auth_oauthlib")
    _ensure_stub("google_auth_oauthlib.flow", {"InstalledAppFlow": object})
    _ensure_stub("googleapiclient")
    _ensure_stub("googleapiclient.discovery", {"build": lambda *a, **kw: None})
    _ensure_stub("googleapiclient.errors", {"HttpError": Exception})

try:
    import openai  # noqa: F401
except ImportError:
    _ensure_stub("openai", {
        "OpenAI": object,
        "AsyncOpenAI": object,
        "OpenAIError": Exception,
    })

try:
    import simple_salesforce  # noqa: F401
except ImportError:
    _ensure_stub("simple_salesforce", {
        "Salesforce": object,
        "SalesforceLogin": (lambda **kw: ("sid", "instance")),
        "SFType": object,
    })

from mcp_server.agents.inventory_agent import InventoryAgent  # noqa: E402
from mcp_server.ontology_engine.engine import OntologyEngine  # noqa: E402
from mcp_server.ontology_engine.memory.facade import ThreeTierMemory  # noqa: E402


# ════════════════════════════════════════════════════════════════
# in-memory Odoo mock — 각 테스트가 자기 fixture 로 초기화.
# ════════════════════════════════════════════════════════════════
class _OdooFixture:
    """
    odoo_service 의 surface 를 in-memory dict 로 흉내낸다.

    의도:
      · 모든 변경 (reserve / unreserve / validate) 이 fixture 의 mutable state 를
        업데이트. 멱등성 / 재조회 테스트가 정확히 가능.
      · 시그니처는 odoo_service 의 실제 함수와 동일 — patch 로 갈아끼우면 됨.
    """

    def __init__(self) -> None:
        self.available: bool = True
        self.pickings_by_so: Dict[int, List[Dict[str, Any]]] = {}
        self.pickings: Dict[int, Dict[str, Any]] = {}
        self.moves: Dict[int, Dict[str, Any]] = {}      # move_id → record
        self.reserve_calls: List[int] = []
        self.unreserve_calls: List[int] = []
        self.validate_calls: List[int] = []
        # T6: reserve_move 호출 시 reserved_availability 를 demand 만큼 끌어올린다.
        self.reserve_fills_to_demand: bool = True
        # T5: candidates 에 'id' 키 없는 row 를 섞을지.
        self.return_malformed: bool = False
        # BC4 S1: validate_picking 이 backorder wizard 를 반환할지 (partial 시뮬).
        self.wizard_on_validate: bool = False
        self.backorder_calls: List[int] = []
        self.cancel_calls: List[int] = []
        # BC5: 생성된 보충 입고 picking 기록.
        self.incoming_created: List[Dict[str, Any]] = []
        # BC5 안 C: agent 가 직접 조회할 부족분 (context.shortage 없을 때).
        self.open_demand: Dict[str, Any] = {
            "product_id": 77, "product_name": "USB SecureKey-100",
            "available": 0, "incoming": 0, "total_demand": 100,
            "total_shortage": 80, "unmet_qty": 80,
            "blocked_orders": [{
                "sale_order_id": 1, "so_name": "S00021", "account_name": "VIP Tech",
                "tier": "VIP", "demand": 100, "reserved": 20, "shortage": 80}],
        }
        # get_picking_shortage 가 반환할 값 (테스트가 셋업).
        self.shortage: Dict[str, Any] = {
            "demand": 1200, "reserved": 1000, "shortage": 200, "lines": 1,
        }

    # ─── 서비스 surface ───────────────────────────────────────────
    def is_available(self) -> bool:
        return self.available

    def get_service_status(self) -> Dict[str, Any]:
        return {"available": self.available, "reason": "fixture"}

    def list_pickings_for_order(self, so_id: int) -> List[Dict[str, Any]]:
        return deepcopy(self.pickings_by_so.get(so_id, []))

    def get_picking(self, picking_id: int) -> Optional[Dict[str, Any]]:
        return deepcopy(self.pickings.get(picking_id))

    def list_open_moves_for_product(
        self, product_id: int, states: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        states = states or [
            "waiting", "confirmed", "partially_available", "assigned",
        ]
        out: List[Dict[str, Any]] = []
        for m in self.moves.values():
            if m.get("product_id") != product_id:
                continue
            if m.get("state") not in states:
                continue
            row = deepcopy(m)
            out.append(row)
        if self.return_malformed:
            # T5 용: id 키 없는 row 1 개 추가.
            out.insert(0, {
                "picking_id": [9999, "WH/OUT/BAD"],
                "product_id": product_id,
                "product_uom_qty": 7,
                "reserved_availability": 7,
                "state": "assigned",
                "sale_order_id": None,
            })
        return out

    def get_picking_moves(self, picking_id: int) -> List[Dict[str, Any]]:
        return [
            deepcopy(m) for m in self.moves.values()
            if self._move_picking_id(m) == picking_id
        ]

    def get_move(self, move_id: int) -> Optional[Dict[str, Any]]:
        return deepcopy(self.moves.get(move_id))

    def reserve_move(self, move_id: int) -> bool:
        self.reserve_calls.append(move_id)
        m = self.moves.get(move_id)
        if not m:
            return False
        if self.reserve_fills_to_demand:
            m["reserved_availability"] = m.get("product_uom_qty") or 0
            m["state"] = "assigned"
        return True

    def unreserve_move(self, move_id: int) -> bool:
        self.unreserve_calls.append(move_id)
        m = self.moves.get(move_id)
        if not m:
            return False
        m["reserved_availability"] = 0
        m["state"] = "waiting"
        return True

    def validate_picking(self, picking_id: int) -> Dict[str, Any]:
        self.validate_calls.append(picking_id)
        # BC4 S1: partial 시뮬 — wizard(backorder confirmation) 반환, done 으로 안 넘김.
        if self.wizard_on_validate:
            return {
                "picking_id": picking_id, "validated": False,
                "wizard": {"res_model": "stock.backorder.confirmation",
                           "res_id": 777, "context": {"button_validate_picking_ids": [picking_id]}},
            }
        p = self.pickings.get(picking_id)
        if p:
            p["state"] = "done"
        return {"picking_id": picking_id, "validated": True, "state": "done"}

    # ─── BC4 S1: backorder wizard 처리 + shortage ───────────────────
    def process_backorder(self, wizard_res_id, wizard_context) -> Dict[str, Any]:
        self.backorder_calls.append(wizard_res_id)
        return {"action": "backorder_created", "ok": True, "wizard_res_id": wizard_res_id}

    def cancel_backorder(self, wizard_res_id, wizard_context) -> Dict[str, Any]:
        self.cancel_calls.append(wizard_res_id)
        return {"action": "remainder_cancelled", "ok": True, "wizard_res_id": wizard_res_id}

    def get_picking_shortage(self, picking_id: int) -> Dict[str, Any]:
        return deepcopy(self.shortage)

    def get_pending_receipts(self, product_id, by_date_iso=None) -> List[Dict[str, Any]]:
        return []

    def get_inventory_state(self, product_id: int) -> Dict[str, Any]:
        return {
            "product_id": product_id,
            "on_hand": 0, "reserved": 0, "available": 0,
        }

    def get_sale_order_tier_map(self, so_ids: List[int]) -> Dict[int, str]:
        # 기본은 모두 Standard — VIP 케이스는 테스트별로 override.
        return {sid: "Standard" for sid in so_ids}

    # ─── BC5: 보충 발주 (incoming picking 생성) ──────────────────────
    # 테스트가 셋업하는 시뮬 플래그:
    incoming_state = "assigned"      # "draft" 면 confirm 실패 시뮬
    incoming_confirm_error = None    # confirm 실패 메시지 시뮬
    incoming_skip = False            # 멱등 재사용(이미 존재) 시뮬

    def get_open_demand_for_product(self, product_id) -> Dict[str, Any]:
        # BC5 안 C — agent 가 context.shortage 없을 때 직접 조회하는 경로.
        return deepcopy(self.open_demand)

    def create_incoming_picking(self, product_id, qty, vendor_name="TechSupply Co",
                                origin="", confirm=True) -> Dict[str, Any]:
        self.incoming_created.append({
            "product_id": product_id, "qty": qty,
            "vendor_name": vendor_name, "origin": origin,
        })
        pid = 70000 + len(self.incoming_created)
        return {
            "picking_id": None if self.incoming_skip else pid,
            "picking_name": f"WH/IN/{pid}",
            "state": self.incoming_state,
            "scheduled_date": None,
            "origin": origin or f"BC5 auto-replenish product_id={product_id}",
            "qty": qty,
            "product_name": "USB SecureKey-100",
            "vendor_id": 42,
            "confirm_error": self.incoming_confirm_error,
            "skipped": self.incoming_skip,
        }

    @staticmethod
    def _move_picking_id(m: Dict[str, Any]) -> Optional[int]:
        pid = m.get("picking_id")
        if isinstance(pid, list) and pid:
            return pid[0]
        if isinstance(pid, int):
            return pid
        return None


def _install_fixture(fixture: _OdooFixture):
    """odoo_service 의 module-level 함수를 fixture 메서드로 monkey-patch."""
    from mcp_server.services import odoo_service as svc
    methods = [
        "is_available", "get_service_status",
        "list_pickings_for_order", "get_picking",
        "list_open_moves_for_product", "get_picking_moves", "get_move",
        "reserve_move", "unreserve_move", "validate_picking",
        "get_pending_receipts", "get_inventory_state",
        "get_sale_order_tier_map",
        "process_backorder", "cancel_backorder", "get_picking_shortage",
        "create_incoming_picking", "get_open_demand_for_product",
    ]
    patchers = [
        patch.object(svc, name, getattr(fixture, name)) for name in methods
    ]
    for p in patchers:
        p.start()
    return patchers


def _stop_patchers(patchers):
    for p in patchers:
        p.stop()


def _build_agent() -> InventoryAgent:
    agent = InventoryAgent(llm_config={"config_list": []})
    agent.register_tools_from_services(user_id="test")
    return agent


def _run(coro):
    # Python 3.10+ 에서 메인스레드의 default event loop 가 사라졌다.
    # asyncio.run 은 매 호출마다 새 loop 를 만들고 정리해 준다.
    return asyncio.run(coro)


# ════════════════════════════════════════════════════════════════
# Test cases
# ════════════════════════════════════════════════════════════════
class TestInventoryAgentGaps(unittest.TestCase):
    """BC3 다음-sprint 의 CRIT/HIGH/MED 수정의 회귀 방지 테스트."""

    def setUp(self):
        self.fixture = _OdooFixture()
        self.patchers = _install_fixture(self.fixture)
        self.agent = _build_agent()

    def tearDown(self):
        _stop_patchers(self.patchers)

    # ─────────────────────────────────────────────────────────────
    # T1 — mixed SO: split 이 service_plan 에 send_license_activation 진입점을 채움
    # ─────────────────────────────────────────────────────────────
    def test_T1_mixed_so_service_plan_includes_license_activation(self):
        so = {
            "id": 1001,
            "name": "SO-1001",
            "tier": "VIP",
            "has_storable_lines": True,
            "has_service_lines": True,
            "target_delivery_date": "2026-06-01",
            "account_name": "VIP Tech",
        }
        # storable picking 1건 등록 (Odoo 가 자동 생성한 셈)
        self.fixture.pickings_by_so[1001] = [{
            "id": 5001, "name": "WH/OUT/5001",
            "state": "confirmed", "scheduled_date": "2026-06-01",
        }]
        self.fixture.pickings[5001] = self.fixture.pickings_by_so[1001][0]

        res = _run(self.agent.execute_action(
            "split_fulfillment_path",
            policy={
                "service_path": {"activation": "license_auto",
                                 "consulting_kickoff_for_tier": ["VIP"]},
                "storable_path": {"spawn_event": "delivery_ready_check",
                                  "tier_priority_score": {"VIP": 100}},
            },
            context={"sales_order": so},
        ))
        inner = res.get("result") or {}
        actions = [s.get("action") for s in (inner.get("service_plan") or [])]
        self.assertIn(
            "send_license_activation", actions,
            f"mixed SO 의 service_plan 에 send_license_activation 누락 (실제: {actions})",
        )
        spawn = inner.get("spawn_events") or []
        self.assertTrue(
            any(ev.get("entity") == "delivery_ready_check" for ev in spawn),
            f"storable 라인의 spawn_event(delivery_ready_check) 누락 (실제: {spawn})",
        )
        # 추가 검증: VIP 일 때 consulting kickoff 도 포함
        self.assertIn("book_kickoff_meeting", actions)

    # ─────────────────────────────────────────────────────────────
    # T2 — preempt candidates: 자기 SO 의 다른 picking move 가 후보에서 빠짐
    # ─────────────────────────────────────────────────────────────
    def test_T2_preempt_excludes_own_sale_order_other_picking(self):
        # 같은 SO 의 picking A (VIP), B (다른 라인) + 다른 SO 의 picking C (Standard)
        self.fixture.moves = {
            201: {"id": 201, "picking_id": [9001, "WH/OUT/A"],
                  "product_id": 77, "product_uom_qty": 5,
                  "reserved_availability": 5, "state": "assigned",
                  "sale_order_id": 8888},   # 자기 SO 의 다른 picking
            202: {"id": 202, "picking_id": [9002, "WH/OUT/C"],
                  "product_id": 77, "product_uom_qty": 5,
                  "reserved_availability": 5, "state": "assigned",
                  "sale_order_id": 7777},   # 다른 SO — preempt OK
        }

        res = _run(self.agent.execute_action(
            "allocate_with_preemption",
            policy={"tier": "VIP", "preempt_mode": "soft",
                    "preempt_target_states": ["assigned"],
                    "preempt_exclude_states": ["partially_available", "done"],
                    "backorder_against_incoming": False,
                    "notify_account_owner": True},
            context={
                "picking": {
                    "id": 9000, "tier": "VIP", "product_id": 77,
                    "qty_demand": 10, "sale_order_id": 8888,
                },
                "inventory": {"product_id": 77, "available": 0},
            },
        ))
        inner = res.get("result") or {}
        preempted_ids = [pm.get("move_id") for pm in (inner.get("preempted_moves") or [])]
        self.assertNotIn(
            201, preempted_ids,
            "자기 SO(8888) 의 다른 picking move 201 이 회수 후보로 잡힘 (HIGH #5 회귀)",
        )
        self.assertIn(
            202, preempted_ids,
            "다른 SO(7777) 의 move 는 회수 후보여야 함",
        )
        # odoo unreserve_move 호출도 자기 SO move 에 대해선 한 번도 없어야
        self.assertNotIn(201, self.fixture.unreserve_calls)

    # ─────────────────────────────────────────────────────────────
    # T3 — replenish: empty replenished + send_allocation_notice skip
    # ─────────────────────────────────────────────────────────────
    def test_T3_replenish_empty_then_email_skipped(self):
        # 큐가 비어있다 — 같은 receipt 가 두 번 들어와도 reserve 할 게 없는 상황
        self.fixture.moves = {}
        replen = _run(self.agent.execute_action(
            "replenish_priority_queue",
            policy={"ordering": ["tier"],
                    "tier_priority": {"VIP": 100, "Standard": 50},
                    "consume_all_for_vip_first": True},
            context={"receipt": {"id": 9100, "product_id": 77, "qty": 50}},
        ))
        replen_inner = replen.get("result") or {}
        self.assertEqual(replen_inner.get("replenished"), [])

        # email_agent.send_allocation_notice 를 모방 — context.agent_outputs 에
        # replenish_priority_queue 결과가 attach 된 상태로 호출되어야 skip.
        from mcp_server.agents.email_agent import EmailAgent
        email_agent = EmailAgent(llm_config={"config_list": []})
        email_agent.register_tools_from_services(user_id="test")

        notice = _run(email_agent.execute_action(
            "send_allocation_notice",
            policy={"tone": "professional", "template": "stock_replenished",
                    "audience": "affected_owners"},
            context={
                "customer": {"name": "Owner Co", "email": "ops@owner.com"},
                "agent_outputs": {"replenish_priority_queue": replen_inner},
            },
        ))
        notice_inner = notice.get("result") or {}
        self.assertTrue(
            notice_inner.get("skipped"),
            f"replenished 가 비었는데 알림 메일이 발송됨 (HIGH #6 회귀). 결과: {notice_inner}",
        )

    # ─────────────────────────────────────────────────────────────
    # T4 — 405 vs 410: VIP partially_available + shortage → 410 만 매칭
    # ─────────────────────────────────────────────────────────────
    def test_T4_rule_routing_partially_available_does_not_fire_405(self):
        tmpdir = tempfile.mkdtemp(prefix="bc3_t4_")
        try:
            memory = ThreeTierMemory({
                "hot":  {"backend": "in_memory", "ttl_sec": 60, "max_size": 50},
                "warm": {"backend": "sqlite", "ttl_sec": 60,
                         "path": os.path.join(tmpdir, "warm.db")},
                "cold": {"backend": "jsonl", "path": os.path.join(tmpdir, "cold/")},
            })
            engine = OntologyEngine(
                str(PROJECT_ROOT / "ontology" / "ontology.yaml"),
                memory=memory,
            )
            ctx = engine.resolve_links(
                "delivery_ready_check",
                {"picking": {"id": 1, "tier": "VIP", "qty_demand": 10,
                             "state": "partially_available"},
                 "inventory": {"available": 5}},
            )
            action = engine.check_rules(ctx)
            self.assertIsNotNone(action,
                "rule 매칭이 None — 410 의 picking.state != 'assigned' 가드 회귀 의심")
            self.assertEqual(
                (action or {}).get("rule_name"),
                "inventory_allocate_vip_preempt",
                "405 (delivery_ready_to_ship_vip) 가 partially_available 에서 발화됨 "
                "(CRIT #2 회귀)",
            )
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    # ─────────────────────────────────────────────────────────────
    # T5 — list_open_moves 가 'id' 없는 row 를 섞어 줘도 unreserve 호출 0
    # ─────────────────────────────────────────────────────────────
    def test_T5_missing_id_does_not_trigger_unreserve(self):
        self.fixture.return_malformed = True
        # 정상 move 도 같이 등록 — malformed 만 skip, 정상은 처리.
        self.fixture.moves = {
            301: {"id": 301, "picking_id": [9101, "WH/OUT/X"],
                  "product_id": 77, "product_uom_qty": 4,
                  "reserved_availability": 4, "state": "assigned",
                  "sale_order_id": 5555},
        }
        res = _run(self.agent.execute_action(
            "allocate_with_preemption",
            policy={"tier": "VIP", "preempt_mode": "soft",
                    "preempt_target_states": ["assigned"],
                    "preempt_exclude_states": ["partially_available", "done"],
                    "backorder_against_incoming": False},
            context={
                "picking": {"id": 9200, "tier": "VIP", "product_id": 77,
                            "qty_demand": 10, "sale_order_id": 6666},
                "inventory": {"product_id": 77, "available": 0},
            },
        ))
        self.assertTrue(res.get("success"))
        # malformed row 의 picking_id (9999) 는 unreserve_move 에 절대 안 들어가야
        for called in self.fixture.unreserve_calls:
            self.assertNotEqual(called, 9999,
                "malformed 후보의 picking_id 가 move_id 로 unreserve 호출됨 "
                "(CRIT #1 회귀)")
        # 정상 candidate 는 처리됐어야
        self.assertIn(301, self.fixture.unreserve_calls)

    # ─────────────────────────────────────────────────────────────
    # T6 — allocate_fifo 가 reserve_move 후의 최신 reserved 값으로 분류
    # ─────────────────────────────────────────────────────────────
    def test_T6_allocate_fifo_uses_fresh_reserved_after_assign(self):
        # 처음엔 reserved_availability=0 (잡히기 전 stale 값) 이지만
        # reserve_move 가 demand 만큼 채워줘 (reserve_fills_to_demand=True),
        # get_move 재조회가 그 갱신값을 보여야 reserved 로 분류됨.
        self.fixture.moves = {
            401: {"id": 401, "picking_id": [9300, "WH/OUT/F"],
                  "product_id": 88, "product_uom_qty": 3,
                  "reserved_availability": 0, "state": "confirmed"},
        }
        # picking 자체 등록
        self.fixture.pickings[9300] = {
            "id": 9300, "name": "WH/OUT/F",
            "state": "confirmed", "scheduled_date": None,
        }
        res = _run(self.agent.execute_action(
            "allocate_fifo",
            policy={"tier": "Standard", "wait_for_incoming": True},
            context={"picking": {"id": 9300}},
        ))
        inner = res.get("result") or {}
        self.assertIn(
            401, inner.get("moves_reserved") or [],
            "reserve_move 후 reserved_availability 가 demand 와 동일한데도 "
            "moves_waiting 으로 오분류됨 (HIGH #8 회귀). 결과: "
            f"reserved={inner.get('moves_reserved')}, waiting={inner.get('moves_waiting')}",
        )


# ════════════════════════════════════════════════════════════════
# BC4 S0 — Priority Override (Case A) 단위 테스트
# ════════════════════════════════════════════════════════════════
# S0-1. override 미지정 → 기존 VIP-first 동작 불변 (회귀).
# S0-2. equal_vip override → Standard 가 VIP 동급으로 올라, 더 이른 date 로 먼저 reserve.
# S0-3. above_vip override → Standard 가 더 늦은 date 라도 score 우위로 먼저 reserve.
# S0-4. 만료(expires_at 과거) override → 무시, VIP-first 폴백.
# S0-5. override SO 가 큐에 없음 → no-op, 에러 없음.
# S0-6. _apply_override_score 순수 함수 (equal_vip=max(base,VIP), above_vip=VIP+boost).
class TestInventoryAgentS0Override(unittest.TestCase):
    """BC4 S0 priority override 의 결정론 동작 + 가드(만료) 회귀 방지."""

    def setUp(self):
        self.fixture = _OdooFixture()
        self.patchers = _install_fixture(self.fixture)
        self.agent = _build_agent()

    def tearDown(self):
        _stop_patchers(self.patchers)

    # VIP(so=1) / Standard(so=2) 각 1건. received_qty 가 한 건만 채우므로
    # "누가 먼저 reserve 됐나" = "정렬 1순위가 누구인가" 를 직접 관측.
    def _setup_two(self, vip_date: str, std_date: str):
        self.fixture.moves = {
            101: {"id": 101, "picking_id": [9001, "WH/OUT/VIP"],
                  "product_id": 77, "product_uom_qty": 5,
                  "reserved_availability": 0, "state": "confirmed",
                  "sale_order_id": 1, "date": vip_date},
            102: {"id": 102, "picking_id": [9002, "WH/OUT/STD"],
                  "product_id": 77, "product_uom_qty": 5,
                  "reserved_availability": 0, "state": "confirmed",
                  "sale_order_id": 2, "date": std_date},
        }

    def _run_replenish(self, override=None, qty: float = 5.0):
        policy = {
            "ordering": ["tier", "target_delivery_date", "sale_order_id"],
            "tier_priority": {"VIP": 100, "Standard": 50, "Bronze": 25},
            "consume_all_for_vip_first": True,
        }
        if override is not None:
            policy["priority_override_runtime"] = override
        context = {
            "receipt": {"product_id": 77, "qty": qty},
            "tier_lookup": {1: "VIP", 2: "Standard"},
        }
        res = _run(self.agent.execute_action(
            "replenish_priority_queue", policy=policy, context=context,
        ))
        return res.get("result") or {}

    # ─── S0-1: override 없음 → VIP 먼저 (Standard date 가 더 일러도) ───
    def test_S0_1_no_override_vip_first_regression(self):
        # Standard 가 더 이른 date 임에도, override 없으면 score 우위로 VIP 먼저.
        self._setup_two(vip_date="2026-06-10", std_date="2026-06-01")
        inner = self._run_replenish(override=None)
        self.assertEqual(
            self.fixture.reserve_calls, [101],
            f"override 없이 VIP(move 101) 가 먼저 reserve 돼야 함 (실제: {self.fixture.reserve_calls})",
        )
        self.assertIsNone(inner.get("override"), "override 미지정인데 override 요약이 채워짐")

    # ─── S0-2: equal_vip → Standard 가 VIP 동급 → 더 이른 date 로 먼저 ───
    def test_S0_2_equal_vip_standard_wins_by_earlier_date(self):
        self._setup_two(vip_date="2026-06-10", std_date="2026-06-01")
        override = {"so_ids": [2], "cfg": {"mode": "equal_vip"}}
        inner = self._run_replenish(override=override)
        self.assertEqual(
            self.fixture.reserve_calls, [102],
            f"equal_vip override 로 Standard(move 102) 가 먼저여야 함 (실제: {self.fixture.reserve_calls})",
        )
        self.assertTrue(inner["override"]["applied"])
        self.assertEqual(inner["override"]["reserved_so_ids"], [2])
        self.assertEqual(inner["override"]["mode"], "equal_vip")

    # ─── S0-3: above_vip → Standard 가 더 늦은 date 라도 먼저 (score 우위) ───
    def test_S0_3_above_vip_standard_wins_despite_later_date(self):
        self._setup_two(vip_date="2026-06-01", std_date="2026-06-30")
        override = {"so_ids": [2], "cfg": {"mode": "above_vip", "boost_score": 1000}}
        self._run_replenish(override=override)
        self.assertEqual(
            self.fixture.reserve_calls, [102],
            f"above_vip override 로 Standard(move 102) 가 date 늦어도 먼저여야 함 "
            f"(실제: {self.fixture.reserve_calls})",
        )

    # ─── S0-4: 만료된 override → 무시, VIP-first 폴백 ───
    def test_S0_4_expired_override_ignored(self):
        self._setup_two(vip_date="2026-06-10", std_date="2026-06-01")
        override = {"so_ids": [2], "cfg": {"mode": "above_vip"},
                    "expires_at": "2000-01-01T00:00:00Z"}   # 과거 → 만료
        inner = self._run_replenish(override=override)
        self.assertEqual(
            self.fixture.reserve_calls, [101],
            f"만료 override 는 무시되고 VIP 먼저여야 함 (실제: {self.fixture.reserve_calls})",
        )
        self.assertIsNone(inner.get("override"), "만료 override 가 적용된 것으로 표시됨")

    # ─── S0-5: override SO 가 큐에 없음 → no-op ───
    def test_S0_5_override_so_not_in_queue_noop(self):
        self._setup_two(vip_date="2026-06-10", std_date="2026-06-01")
        override = {"so_ids": [999], "cfg": {"mode": "equal_vip"}}   # 큐에 없는 SO
        inner = self._run_replenish(override=override)
        self.assertEqual(
            self.fixture.reserve_calls, [101],
            "override SO 가 큐에 없으면 기존 VIP-first 그대로여야 함",
        )
        self.assertEqual(inner["override"]["reserved_so_ids"], [],
                         "매칭된 move 가 없으므로 reserved_so_ids 는 비어야 함")

    # ─── S0-6: _apply_override_score 순수 함수 ───
    def test_S0_6_apply_override_score_pure(self):
        f = InventoryAgent._apply_override_score
        table = {"VIP": 100, "Standard": 50}
        # equal_vip: Standard(50) → VIP(100) 동급
        self.assertEqual(f(50, {"cfg": {"mode": "equal_vip"}}, table), 100)
        # equal_vip: 이미 VIP 보다 높으면(예: 120) 낮추지 않음
        self.assertEqual(f(120, {"cfg": {"mode": "equal_vip"}}, table), 120)
        # above_vip: VIP + boost
        self.assertEqual(
            f(50, {"cfg": {"mode": "above_vip", "boost_score": 1000}}, table), 1100)
        # default(mode 미지정) = equal_vip
        self.assertEqual(f(50, {"cfg": {}}, table), 100)


# ════════════════════════════════════════════════════════════════
# BC4 S1 — Partial Shipment Advisor 단위 테스트
# ════════════════════════════════════════════════════════════════
# S1-1. auto_backorder → wizard 시 process_backorder 호출 (rule baseline).
# S1-2. llm_advisor + auto_execute=False → 추천만, 출하 미실행 (안전 핵심 가드).
# S1-3. LLM 실패 → rule_baseline 폴백 (fail-safe).
# S1-4. wizard 없음(전량) → 기존 경로 불변 (회귀).
# S1-5. auto_execute=True + split 추천 → 실행됨 (데모 모드).
# S1-6. auto_execute=True + wait 추천 → 실행 안 함.
# S1-7. _parse_advisor_json 순수 함수 (코드펜스 스트립, None→예외).
_ADVISOR_FN = "mcp_server.services.openai_service.generate_text_with_system"


class TestInventoryAgentS1PartialAdvisor(unittest.TestCase):
    """BC4 S1 — 부분출하 advisor: 추천/실행 분리 + fail-safe 폴백 + 회귀."""

    def setUp(self):
        self.fixture = _OdooFixture()
        self.patchers = _install_fixture(self.fixture)
        self.agent = _build_agent()

    def tearDown(self):
        _stop_patchers(self.patchers)

    def _run_dispatch(self, policy, state="partially_available"):
        self.fixture.pickings[700] = {"id": 700, "state": state}
        context = {"picking": {"id": 700, "tier": "VIP", "state": state}}
        res = _run(self.agent.execute_action(
            "dispatch_shipment", policy=policy, context=context))
        return res.get("result") or {}

    # ─── S1-1: auto_backorder → process_backorder 호출 ───
    def test_S1_1_auto_backorder_executes_split(self):
        self.fixture.wizard_on_validate = True
        inner = self._run_dispatch({"partial_handling": "auto_backorder"})
        self.assertTrue(inner.get("partial"))
        self.assertEqual(inner.get("decision"), "split")
        self.assertEqual(inner.get("decision_source"), "rule_baseline")
        self.assertEqual(self.fixture.backorder_calls, [777])

    # ─── S1-2: llm_advisor + auto_execute=False → 추천만, 출하 미실행 ───
    def test_S1_2_advisor_pending_does_not_execute(self):
        self.fixture.wizard_on_validate = True
        with patch(_ADVISOR_FN,
                   return_value='{"recommendation":"split","rationale":"빨리","confidence":0.8}'):
            inner = self._run_dispatch({
                "partial_handling": "llm_advisor",
                "auto_execute_advisor": False, "rule_baseline": "split"})
        self.assertTrue(inner.get("pending_confirmation"))
        self.assertEqual(inner["advisor"]["recommendation"], "split")
        self.assertEqual(inner["advisor"]["source"], "llm")
        # 핵심 안전 가드: 추천만 했을 뿐 실제 출하(process_backorder)는 안 일어남
        self.assertEqual(self.fixture.backorder_calls, [],
                         "advisor 추천 단계에서 출하가 실행됨 (안전 원칙 위반)")

    # ─── S1-3: LLM 실패 → rule_baseline 폴백 ───
    def test_S1_3_advisor_fallback_on_llm_failure(self):
        self.fixture.wizard_on_validate = True
        with patch(_ADVISOR_FN, return_value=None):   # openai 미초기화 모사
            inner = self._run_dispatch({
                "partial_handling": "llm_advisor",
                "auto_execute_advisor": False, "rule_baseline": "split"})
        self.assertEqual(inner["advisor"]["source"], "fallback_rule")
        self.assertEqual(inner["advisor"]["recommendation"], "split")
        self.assertEqual(self.fixture.backorder_calls, [])   # 여전히 미실행

    # ─── S1-4: 전량(wizard 없음) → 기존 경로 불변 (회귀) ───
    def test_S1_4_full_shipment_regression(self):
        self.fixture.wizard_on_validate = False
        inner = self._run_dispatch({}, state="assigned")
        self.assertTrue(inner.get("success"))
        self.assertIsNone(inner.get("partial"), "전량 출하인데 partial 분기 진입")
        self.assertEqual(self.fixture.backorder_calls, [])

    # ─── S1-5: auto_execute=True + split → 실행 (데모 모드) ───
    def test_S1_5_auto_execute_split_runs(self):
        self.fixture.wizard_on_validate = True
        with patch(_ADVISOR_FN,
                   return_value='{"recommendation":"split","rationale":"x","confidence":0.9}'):
            inner = self._run_dispatch({
                "partial_handling": "llm_advisor", "auto_execute_advisor": True})
        self.assertEqual(inner.get("decision"), "split")
        self.assertEqual(self.fixture.backorder_calls, [777])

    # ─── S1-6: auto_execute=True + wait → 실행 안 함 ───
    def test_S1_6_auto_execute_wait_holds(self):
        self.fixture.wizard_on_validate = True
        with patch(_ADVISOR_FN,
                   return_value='{"recommendation":"wait","rationale":"곧 입고","confidence":0.7}'):
            inner = self._run_dispatch({
                "partial_handling": "llm_advisor", "auto_execute_advisor": True})
        self.assertEqual(inner.get("decision"), "wait")
        self.assertEqual(self.fixture.backorder_calls, [],
                         "wait 추천인데 backorder 가 생성됨")

    # ─── S1-7: _parse_advisor_json 순수 함수 ───
    def test_S1_7_parse_advisor_json(self):
        f = InventoryAgent._parse_advisor_json
        self.assertEqual(f('{"recommendation":"split"}'), {"recommendation": "split"})
        self.assertEqual(f('```json\n{"recommendation":"wait"}\n```'),
                         {"recommendation": "wait"})
        with self.assertRaises(Exception):
            f(None)


# ════════════════════════════════════════════════════════════════
# BC5 — 충족 불가 → 자율 보충 발주 + 담당자 브리핑 단위 테스트
# ════════════════════════════════════════════════════════════════
# B5-1. LLM advisor 정상 → recommended_qty(LLM) 로 incoming picking 생성.
# B5-2. LLM 실패 → rule 폴백(unmet + safety_buffer) qty 로 발주.
# B5-3. dry_run=True → 추천만, picking 미생성 (안전 토글).
# B5-4. unmet_qty=0 → skip (보충 불필요).
# B5-5. Odoo 미연결 → plan-only (picking 미생성).
# B5-6. advisor qty 가 unmet 보다 작으면 unmet 으로 하한 보정.
# B5-7. email send_replenishment_alert: notify_to 있고 LLM 실패 → 템플릿 폴백으로 발송.
# B5-8. email: notify_to 없음 → draft 만 (skip).
_ADVISOR_FN_B5 = "mcp_server.services.openai_service.generate_text_with_system"


class TestInventoryAgentBC5Replenishment(unittest.TestCase):
    """BC5 — 자율 보충 발주: LLM 발주량 advisor + fail-safe 폴백 + 안전 토글."""

    def setUp(self):
        self.fixture = _OdooFixture()
        self.patchers = _install_fixture(self.fixture)
        self.agent = _build_agent()

    def tearDown(self):
        _stop_patchers(self.patchers)

    def _shortage(self, unmet=80.0, vip=True):
        blocked = [
            {"sale_order_id": 1, "so_name": "S00021", "account_name": "VIP Tech",
             "tier": "VIP" if vip else "Standard", "demand": 100,
             "reserved": 20, "shortage": 80},
        ]
        return {
            "product_id": 77, "product_name": "USB SecureKey-100",
            "available": 0, "incoming": 0,
            "total_demand": 100, "total_shortage": unmet, "unmet_qty": unmet,
            "blocked_orders": blocked,
        }

    def _run_po(self, policy, shortage=None):
        res = _run(self.agent.execute_action(
            "create_replenishment_po", policy=policy,
            context={"shortage": shortage or self._shortage()},
        ))
        return res.get("result") or {}

    # ─── B5-1: LLM advisor 정상 → 그 qty 로 picking 생성 ───
    def test_B5_1_llm_advisor_creates_picking(self):
        with patch(_ADVISOR_FN_B5,
                   return_value='{"recommended_qty":100,"urgency":"HIGH",'
                                '"rationale":"VIP 블록","confidence":0.9}'):
            inner = self._run_po({"auto_create_po": True, "vendor_name": "TechSupply Co"})
        self.assertTrue(inner.get("success"))
        self.assertEqual(inner.get("recommended_qty"), 100)
        self.assertEqual(inner["advisor"]["source"], "llm")
        self.assertEqual(inner["advisor"]["urgency"], "HIGH")
        self.assertEqual(len(self.fixture.incoming_created), 1)
        self.assertEqual(self.fixture.incoming_created[0]["qty"], 100)
        self.assertEqual(inner["po"]["picking_name"], "WH/IN/70001")

    # ─── B5-2: LLM 실패 → rule 폴백 (unmet + safety_buffer) ───
    def test_B5_2_fallback_qty_on_llm_failure(self):
        with patch(_ADVISOR_FN_B5, return_value=None):
            inner = self._run_po({"auto_create_po": True, "safety_buffer_units": 20})
        self.assertEqual(inner["advisor"]["source"], "fallback_rule")
        # unmet(80) + safety(20) = 100
        self.assertEqual(inner.get("recommended_qty"), 100)
        self.assertEqual(inner["advisor"]["urgency"], "HIGH")  # VIP 블록 → HIGH
        self.assertEqual(self.fixture.incoming_created[0]["qty"], 100)

    # ─── B5-3: dry_run → 추천만, picking 미생성 ───
    def test_B5_3_dry_run_holds_creation(self):
        with patch(_ADVISOR_FN_B5,
                   return_value='{"recommended_qty":90,"urgency":"MEDIUM",'
                                '"rationale":"x","confidence":0.5}'):
            inner = self._run_po({"auto_create_po": True, "dry_run": True})
        self.assertTrue(inner.get("pending_confirmation"))
        self.assertIsNone(inner.get("po"))
        self.assertEqual(self.fixture.incoming_created, [],
                         "dry_run 인데 incoming picking 이 생성됨 (안전 토글 위반)")

    # ─── B5-4: unmet=0 → skip ───
    def test_B5_4_no_unmet_skips(self):
        inner = self._run_po({"auto_create_po": True}, shortage=self._shortage(unmet=0))
        self.assertTrue(inner.get("skipped"))
        self.assertEqual(self.fixture.incoming_created, [])

    # ─── B5-5: Odoo 미연결 → plan-only ───
    def test_B5_5_odoo_unavailable_plan_only(self):
        self.fixture.available = False
        with patch(_ADVISOR_FN_B5, return_value=None):
            inner = self._run_po({"auto_create_po": True})
        self.assertTrue(inner.get("skipped"))
        self.assertIn("intended_plan", inner)
        self.assertEqual(self.fixture.incoming_created, [])

    # ─── B5-6: advisor qty < unmet → unmet 으로 하한 보정 ───
    def test_B5_6_qty_floored_to_unmet(self):
        with patch(_ADVISOR_FN_B5,
                   return_value='{"recommended_qty":10,"urgency":"LOW",'
                                '"rationale":"x","confidence":0.3}'):
            inner = self._run_po({"auto_create_po": True})  # unmet=80
        self.assertGreaterEqual(inner.get("recommended_qty"), 80,
                                "LLM 추천이 부족분보다 작은데 하한 보정 안 됨")

    # ─── B5-7: email 브리핑 — notify_to 있고 LLM 실패 → 템플릿 폴백 발송 ───
    def test_B5_7_email_template_fallback_sends(self):
        from mcp_server.agents.email_agent import EmailAgent
        from mcp_server.services import gmail_service
        email_agent = EmailAgent(llm_config={"config_list": []})
        email_agent.register_tools_from_services(user_id="test")

        repl_result = {
            "product_name": "USB SecureKey-100", "unmet_qty": 80,
            "recommended_qty": 100,
            "advisor": {"urgency": "HIGH", "rationale": "VIP 블록", "source": "fallback_rule"},
            "po": {"picking_name": "WH/IN/70001"},
            "blocked_orders": [
                {"so_name": "S00021", "tier": "VIP", "account_name": "VIP Tech",
                 "shortage": 80},
            ],
            "vip_blocked_count": 1,
        }
        with patch(_ADVISOR_FN_B5, return_value=None), \
                patch.object(gmail_service, "send_reply", return_value=True) as send:
            res = _run(email_agent.execute_action(
                "send_replenishment_alert",
                policy={"auto_send": True, "notify_to": "ops@acme.com", "language": "ko"},
                context={"agent_outputs": {"create_replenishment_po": repl_result}},
            ))
        inner = res.get("result") or {}
        self.assertTrue(inner.get("success"))
        self.assertEqual(inner.get("to"), "ops@acme.com")
        self.assertEqual(inner["policy_applied"]["generated_via"], "template")
        send.assert_called_once()
        # 본문에 권장 발주량 + 입고건 + VIP 블록이 포함돼야 (의사결정용 브리핑)
        body = inner["draft"]["body"]
        self.assertIn("100", body)
        self.assertIn("WH/IN/70001", body)

    # ─── B5-11 (안 C): context.shortage 없이 product_id 만 → agent 가 직접 부족분 조회 ───
    def test_B5_11_agent_self_fetches_shortage(self):
        with patch(_ADVISOR_FN_B5, return_value=None):
            res = _run(self.agent.execute_action(
                "create_replenishment_po",
                policy={"auto_create_po": True},
                context={"product_id": 77}))   # shortage 미주입
        inner = res.get("result") or {}
        self.assertTrue(inner.get("success"),
                        "agent 가 shortage 를 직접 조회하지 못함 (안 C 회귀)")
        self.assertEqual(inner.get("unmet_qty"), 80)
        self.assertEqual(inner.get("product_name"), "USB SecureKey-100")
        self.assertEqual(len(self.fixture.incoming_created), 1)
        # 결과에 shortage 스냅샷 포함 (trigger audit 용)
        self.assertEqual((inner.get("shortage") or {}).get("unmet_qty"), 80)

    # ─── B5-12 (안 C): 조회한 부족분이 0 → skip (email 도 skip 해야) ───
    def test_B5_12_self_fetch_no_unmet_skips(self):
        self.fixture.open_demand = {**self.fixture.open_demand,
                                    "unmet_qty": 0, "total_shortage": 0,
                                    "blocked_orders": []}
        with patch(_ADVISOR_FN_B5, return_value=None):
            res = _run(self.agent.execute_action(
                "create_replenishment_po",
                policy={"auto_create_po": True},
                context={"product_id": 77}))
        inner = res.get("result") or {}
        self.assertTrue(inner.get("skipped"))
        self.assertEqual(self.fixture.incoming_created, [])

    # ─── B5-9: confirm 실패(draft) → success=False + 경고 note (성공 위장 금지) ───
    def test_B5_9_confirm_failure_not_success(self):
        self.fixture.incoming_state = "draft"
        self.fixture.incoming_confirm_error = "Fault: action_confirm 거부"
        with patch(_ADVISOR_FN_B5, return_value=None):
            inner = self._run_po({"auto_create_po": True})
        self.assertFalse(inner.get("success"),
                         "confirm 실패(draft)인데 success=True 로 위장됨 (루프 침묵 끊김)")
        self.assertIn("confirm 실패", inner.get("note", ""))
        self.assertEqual(inner["po"]["state"], "draft")

    # ─── B5-10: 멱등 재사용(이미 존재) → 중복 발주 안 함 note ───
    def test_B5_10_idempotent_reuse_note(self):
        self.fixture.incoming_skip = True
        with patch(_ADVISOR_FN_B5, return_value=None):
            inner = self._run_po({"auto_create_po": True})
        self.assertTrue(inner.get("success"))
        self.assertIn("멱등", inner.get("note", ""))

    # ─── B5-8: email — notify_to 없음 → draft 만 (skip) ───
    def test_B5_8_email_no_recipient_draft_only(self):
        from mcp_server.agents.email_agent import EmailAgent
        email_agent = EmailAgent(llm_config={"config_list": []})
        email_agent.register_tools_from_services(user_id="test")
        with patch(_ADVISOR_FN_B5, return_value=None):
            res = _run(email_agent.execute_action(
                "send_replenishment_alert",
                policy={"auto_send": True, "language": "ko"},
                context={"agent_outputs": {"create_replenishment_po": {
                    "product_name": "USB", "unmet_qty": 80, "recommended_qty": 80,
                    "advisor": {"urgency": "MEDIUM"}, "blocked_orders": []}}},
            ))
        inner = res.get("result") or {}
        self.assertTrue(inner.get("skipped"))
        self.assertIn("draft", inner)


if __name__ == "__main__":
    unittest.main()
