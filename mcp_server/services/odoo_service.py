# mcp_server/services/odoo_service.py
"""
Odoo ERP 서비스 — XML-RPC 기반 (단일/멀티유저 대응)
─────────────────────────────────────────────────────────────────────
설계 의도:
  · services/ 는 외부 시스템 연결을 전담한다 (auth, session cache, raw API).
  · 이전에 erp_agent.py 안에 module-level 로 흩어져 있던 _odoo_* 헬퍼들을
    하나의 서비스로 모은다. 같은 패턴: salesforce_service.py 와 정렬.
  · BC3 (inventory_agent) 가 같은 세션을 공유한다 — 세션 캐시는 모듈 글로벌.

호출 규약:
  · 모든 함수는 동기 (XML-RPC 자체가 동기). async agent 에서는
    asyncio.to_thread(odoo_service.<func>, ...) 로 래핑할 것.
  · 인증 실패 / 미설정은 raise 가 아니라 None 또는 (False, reason) 형태로 반환.
    agent 가 정책 검증 모드로 흐름을 계속할 수 있도록.

환경변수:
  · ODOO_URL, ODOO_DB, ODOO_USERNAME, ODOO_API_KEY  (필수)
  · API key 가 없으면 service.is_available() == False — agent 는 plan only 반환.

⚠️ 보안:
  · ODOO_API_KEY 는 절대 소스에 default 로 박지 않는다. 누락 시 명시적 fail.
"""
import os
import sys
import logging
import xmlrpc.client
from typing import Dict, Any, Optional, List, Tuple

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# 모듈 상태 (싱글톤 세션 캐시)
# ════════════════════════════════════════════════════════════════
_odoo_config: Optional[Dict[str, str]] = None
_session: Dict[str, Any] = {
    "uid": None,
    "models": None,
    "url": None,
    "db": None,
    "api_key": None,
    "username": None,
    "common": None,
}
_last_auth_error: Optional[str] = None


# ════════════════════════════════════════════════════════════════
# Config / Authentication
# ════════════════════════════════════════════════════════════════
def _load_config_from_env() -> Dict[str, str]:
    """환경변수에서 Odoo 설정 로드. 누락 시 빈 string 으로 두고 호출 측이 판단."""
    return {
        "url": os.getenv("ODOO_URL", "").strip(),
        "db": os.getenv("ODOO_DB", "").strip(),
        "username": os.getenv("ODOO_USERNAME", "").strip(),
        "api_key": os.getenv("ODOO_API_KEY", "").strip(),
    }


def authenticate_odoo(config: Optional[Dict[str, str]] = None) -> bool:
    """
    Odoo XML-RPC 인증. 성공 시 모듈 세션 캐시에 uid/models 저장.
    config 미제공 시 환경변수에서 로드.

    Returns:
        True = 인증 성공, False = 실패 (_last_auth_error 에 사유)
    """
    global _odoo_config, _session, _last_auth_error
    _odoo_config = config or _load_config_from_env()

    missing = [k for k in ("url", "db", "username", "api_key") if not _odoo_config.get(k)]
    if missing:
        _last_auth_error = f"missing config: {missing}"
        logger.warning(f"[odoo_service] 설정 누락 — {_last_auth_error}")
        return False

    try:
        common = xmlrpc.client.ServerProxy(
            f"{_odoo_config['url']}/xmlrpc/2/common", allow_none=True
        )
        uid = common.authenticate(
            _odoo_config["db"],
            _odoo_config["username"],
            _odoo_config["api_key"],
            {},
        )
        if not uid:
            _last_auth_error = "authentication failed (invalid credentials)"
            logger.error(f"[odoo_service] {_last_auth_error}")
            return False

        models = xmlrpc.client.ServerProxy(
            f"{_odoo_config['url']}/xmlrpc/2/object", allow_none=True
        )
        _session.update({
            "uid": uid,
            "models": models,
            "common": common,
            **_odoo_config,
        })
        _last_auth_error = None
        logger.info(f"[odoo_service] 인증 성공 — uid={uid}")
        return True
    except Exception as e:
        _last_auth_error = f"connection error: {e}"
        logger.error(f"[odoo_service] {_last_auth_error}")
        return False


def is_available() -> bool:
    """현재 세션이 유효한지 체크 (재인증 시도 포함)."""
    if _session.get("uid") and _session.get("models"):
        return True
    # lazy init — 환경변수가 채워져 있으면 자동 인증 시도
    return authenticate_odoo()


def get_service_status() -> Dict[str, Any]:
    """대시보드용 상태 — auth 시도 없이 현재 캐시 상태만 반환."""
    if _session.get("uid"):
        return {
            "connected": True,
            "uid": _session["uid"],
            "url": _session["url"],
            "db": _session["db"],
        }
    return {
        "connected": False,
        "reason": _last_auth_error or "not authenticated yet",
    }


# ════════════════════════════════════════════════════════════════
# Core RPC
# ════════════════════════════════════════════════════════════════
def call(model: str, method: str, *args, **kwargs) -> Any:
    """
    Odoo execute_kw 호출. 동기 XML-RPC.
    async 환경에서는 asyncio.to_thread(odoo_service.call, ...) 로 래핑.

    Raises:
        RuntimeError: 세션 없거나 인증 실패 시
    """
    if not is_available():
        raise RuntimeError(
            f"Odoo 서비스 미사용 가능: {_last_auth_error or 'unknown'}"
        )
    return _session["models"].execute_kw(
        _session["db"],
        _session["uid"],
        _session["api_key"],
        model,
        method,
        list(args),
        kwargs,
    )


# ════════════════════════════════════════════════════════════════
# 도메인 헬퍼 — Partner / Product / Sales Order
# (이전 erp_agent.py 의 _find_or_create_* 가 여기로 이동)
# ════════════════════════════════════════════════════════════════
def find_or_create_partner(
    account_name: str,
    tier: str = "Standard",
    comment_extra: str = "",
) -> Optional[int]:
    """
    SFDC Account 이름 → Odoo res.partner.
    이름 매칭 우선, 없으면 신규 생성. tier 는 comment 에 기록.
    """
    if not account_name:
        return None
    partner_ids = call("res.partner", "search", [("name", "=", account_name)])
    if partner_ids:
        return partner_ids[0]
    email_local = account_name.lower().replace(" ", "").replace(".", "")
    comment = f"Tier: {tier}"
    if comment_extra:
        comment += f" | {comment_extra}"
    partner_id = call("res.partner", "create", {
        "name": account_name,
        "email": f"contact@{email_local}.com",
        "is_company": True,
        "customer_rank": 1,
        "comment": comment,
    })
    logger.info(f"[odoo_service] Partner 신규 생성 — Id={partner_id}, name={account_name}")
    return partner_id


# Odoo 19.2 에서 product.template.type 의 valid selection 은 ["consu","service","combo"] 로
# 축소됐고, storable 여부는 별도 boolean is_storable ("Track Inventory") 로 분리됨.
# 따라서 "storable"/"product" 입력은 type='consu' + is_storable=True 로 매핑한다.
PRODUCT_TYPE_MAP = {
    "service": "service",
    "consu": "consu",
    "consumable": "consu",
    "storable": "consu",
    "product": "consu",
}
# storable 매핑: PRODUCT_TYPE_MAP 의 key 중 is_storable=True 로 등록해야 하는 것들.
_STORABLE_INPUT_TYPES = {"storable", "product"}


def find_or_create_product(
    name: str = "Module X",
    default_price: float = 500,
    product_type: str = "service",
) -> Optional[int]:
    """
    제품 이름 매칭 → 없으면 생성.
    product_type 은 ontology policy 로부터 전달되어 service / consu / storable 분기.
    Odoo 19.2: storable 은 type='consu' + is_storable=True 조합으로 표현된다.
    """
    product_ids = call("product.product", "search", [("name", "=", name)])
    if product_ids:
        return product_ids[0]
    odoo_type = PRODUCT_TYPE_MAP.get(product_type, "service")
    vals: Dict[str, Any] = {
        "name": name,
        "list_price": default_price,
        "type": odoo_type,
        "sale_ok": True,
    }
    if product_type in _STORABLE_INPUT_TYPES:
        vals["is_storable"] = True
    return call("product.product", "create", vals)


def find_existing_sales_order(client_order_ref: str) -> Optional[Dict[str, Any]]:
    """SFDC Opp 이름 (client_order_ref) 로 기존 SO 조회 (멱등성 체크)."""
    if not client_order_ref:
        return None
    ids = call("sale.order", "search", [("client_order_ref", "=", client_order_ref)])
    if not ids:
        return None
    orders = call(
        "sale.order", "read", [ids[0]],
        fields=["name", "state", "amount_total", "partner_id", "client_order_ref"],
    )
    return orders[0] if orders else None


def find_currency_id(currency_name: str) -> Optional[int]:
    """통화명 (예: USD, KRW) → res.currency.id. 없으면 None."""
    if not currency_name:
        return None
    try:
        ids = call("res.currency", "search", [("name", "=", currency_name)])
        return ids[0] if ids else None
    except Exception as e:
        logger.warning(f"[odoo_service] currency lookup 실패 ({currency_name}): {e}")
        return None


def create_sales_order(
    partner_id: int,
    order_lines: List[Tuple],
    client_order_ref: str = "",
    note: str = "",
    currency_id: Optional[int] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """
    Sales Order 생성 (+ 옵션으로 즉시 확정).
    order_lines 는 Odoo many2many command 형식: [(0, 0, {...}), ...]

    Returns:
        {"order_id": int, "order": {...}, "url": str}
    """
    so_data: Dict[str, Any] = {
        "partner_id": partner_id,
        "order_line": order_lines,
    }
    if client_order_ref:
        so_data["client_order_ref"] = client_order_ref
    if note:
        so_data["note"] = note
    if currency_id:
        so_data["currency_id"] = currency_id

    order_id = call("sale.order", "create", so_data)

    if confirm:
        call("sale.order", "action_confirm", [order_id])

    order = call(
        "sale.order", "read", [order_id],
        fields=["name", "state", "amount_total", "partner_id", "client_order_ref"],
    )[0]

    return {
        "order_id": order_id,
        "order": order,
        "url": f"{_session['url']}/odoo/sales/{order_id}",
    }


# ════════════════════════════════════════════════════════════════
# BC3 — Inventory / Picking (Delivery Order, stock.move, stock.quant)
# ════════════════════════════════════════════════════════════════
# 사용 흐름:
#   1. SO confirmed → list_pickings_for_order() 로 자동 생성된 DO 들 조회
#   2. 각 DO 의 line → get_product_quants() 로 가용재고 확인 → get_inventory_state() 로 정책 입력
#   3. VIP 선점 필요 시 → list_open_moves_for_product() → unreserve_move() → reserve_move()
#   4. 입고 시 → register_stock_receipt() → 다시 reserve_move() 로 backorder 채움
#   5. 전 라인 assigned → validate_picking() 로 출고 확정

def list_pickings_for_order(sale_order_id: int) -> List[Dict[str, Any]]:
    """
    SO 에 연결된 stock.picking (Delivery Order) 목록.
    SO 가 storable 라인을 가지면 Odoo 가 자동으로 picking 을 생성한다.
    """
    picking_ids = call("stock.picking", "search", [("sale_id", "=", sale_order_id)])
    if not picking_ids:
        return []
    return call(
        "stock.picking", "read", picking_ids,
        fields=["name", "state", "scheduled_date", "origin", "partner_id",
                "move_ids", "sale_id"],
    )


def get_picking(picking_id: int) -> Optional[Dict[str, Any]]:
    """단일 picking 조회 (state 확인용)."""
    rec = call("stock.picking", "read", [picking_id],
               fields=["name", "state", "scheduled_date", "origin", "partner_id",
                       "move_ids", "sale_id"])
    return rec[0] if rec else None


def get_sale_order_tier_map(sale_order_ids: List[int]) -> Dict[int, str]:
    """
    sale_order_id → tier(VIP/Standard/Bronze) 매핑.

    BC3 MED #M1 봉합: replenish_priority_queue 가 origin 문자열에서 'VIP' 를
    찾던 휴리스틱을 대체. SO 의 partner_id.commercial_partner_id.category_id 를
    한 번에 읽어 tier 를 정확히 추론.

    Tier 결정 룰:
      1. partner 의 category_id 중 'VIP' / 'Standard' / 'Bronze' 가 있으면 그 값.
      2. 없으면 'Standard' (안전 default — VIP 오인식 방지).

    Returns:
        {sale_order_id: tier_string}. SO 조회 실패한 항목은 제외.
    """
    if not sale_order_ids:
        return {}
    sale_order_ids = [int(x) for x in sale_order_ids if x]
    out: Dict[int, str] = {}
    try:
        sos = call(
            "sale.order", "read", sale_order_ids,
            fields=["partner_id"],
        )
    except Exception as e:
        logger.warning(f"[odoo_service.get_sale_order_tier_map] sale.order read 실패: {e}")
        return out

    partner_to_so: Dict[int, List[int]] = {}
    for so in sos:
        so_id = so.get("id")
        partner_field = so.get("partner_id")
        partner_id = None
        if isinstance(partner_field, list) and partner_field:
            partner_id = partner_field[0]
        elif isinstance(partner_field, int):
            partner_id = partner_field
        if partner_id and so_id:
            partner_to_so.setdefault(partner_id, []).append(so_id)

    if not partner_to_so:
        return out
    try:
        partners = call(
            "res.partner", "read", list(partner_to_so.keys()),
            fields=["category_id", "commercial_partner_id"],
        )
    except Exception as e:
        logger.warning(f"[odoo_service.get_sale_order_tier_map] res.partner read 실패: {e}")
        return out

    # category_id 는 many2many — id 목록 형식. 각 category 이름을 별도 read.
    all_cat_ids: set = set()
    partner_cats: Dict[int, List[int]] = {}
    for p in partners:
        pid = p.get("id")
        cats = p.get("category_id") or []
        if isinstance(cats, list):
            partner_cats[pid] = cats
            all_cat_ids.update(cats)

    cat_names: Dict[int, str] = {}
    if all_cat_ids:
        try:
            cats = call("res.partner.category", "read", list(all_cat_ids), fields=["name"])
            for c in cats:
                cat_names[c.get("id")] = (c.get("name") or "").strip()
        except Exception as e:
            logger.warning(f"[odoo_service.get_sale_order_tier_map] category read 실패: {e}")

    tier_priority_order = ["VIP", "Standard", "Bronze"]
    for partner_id, so_ids in partner_to_so.items():
        partner_cat_names = [cat_names.get(cid, "") for cid in partner_cats.get(partner_id, [])]
        tier = "Standard"  # default
        for candidate in tier_priority_order:
            if any(candidate == n for n in partner_cat_names):
                tier = candidate
                break
        for so_id in so_ids:
            out[so_id] = tier
    return out


def _attach_sale_order_id(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    stock.move read 결과에 sale_order_id 를 attach.

    Odoo 의 stock.move 는 sale_line_id (many2one) 를 통해 sale.order 와 연결되지만,
    여기서 한 번 더 sale.order.line.read 를 쳐서 order_id 를 surface 한다.
    BC3 HIGH #5: allocate_with_preemption 이 자기 SO 의 다른 picking 을 회수
    후보에서 제외하려면 move 단에 sale_order_id 가 필요.

    sale_line_id 가 없는 move (예: 내부 이동) 는 sale_order_id=None 그대로.
    """
    if not records:
        return records
    line_ids = []
    for r in records:
        sl = r.get("sale_line_id")
        if isinstance(sl, list) and sl:
            line_ids.append(sl[0])
        elif isinstance(sl, int) and sl > 0:
            line_ids.append(sl)
    line_ids = list(set(line_ids))
    line_to_order: Dict[int, int] = {}
    if line_ids:
        try:
            lines = call("sale.order.line", "read", line_ids, fields=["order_id"])
            for ln in lines:
                lid = ln.get("id")
                oid_field = ln.get("order_id")
                if isinstance(oid_field, list) and oid_field:
                    line_to_order[lid] = oid_field[0]
                elif isinstance(oid_field, int):
                    line_to_order[lid] = oid_field
        except Exception as e:
            logger.warning(f"[odoo_service] sale.order.line 조회 실패 — sale_order_id 미부착: {e}")
    for r in records:
        sl = r.get("sale_line_id")
        if isinstance(sl, list) and sl:
            sl = sl[0]
        if isinstance(sl, int):
            r["sale_order_id"] = line_to_order.get(sl)
        else:
            r["sale_order_id"] = None
    return records


def _normalize_move_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    stock.move read 결과 normalize — `id` 키 보장 + Odoo 19.2 필드 alias.

    Odoo read() 는 항상 `id` 를 포함하지만, mock/test 응답이나 비정상 환경에서
    누락될 수 있다. id 가 없는 row 는 caller 가 picking_id many2one ([id, name]) 등
    잘못된 fallback 으로 흘러갈 위험이 있어 (BC3 CRIT #1), 여기서 명시적으로
    skip + warn 한다.

    또 Odoo 19.2 는 stock.move 의 reserved_availability 필드를 quantity 로 통합했다.
    기존 inventory_agent / test 가 reserved_availability key 로 접근하므로,
    여기서 quantity → reserved_availability alias 를 부여해 hot path 호환을 유지한다.
    """
    out: List[Dict[str, Any]] = []
    for r in records:
        mid = r.get("id")
        if not isinstance(mid, int) or mid <= 0:
            logger.warning(
                "[odoo_service] stock.move record missing valid 'id' — skipped: %r", r,
            )
            continue
        if "quantity" in r and "reserved_availability" not in r:
            r["reserved_availability"] = r["quantity"]
        out.append(r)
    return out


def get_picking_moves(picking_id: int) -> List[Dict[str, Any]]:
    """picking 내부의 stock.move 라인들 (라인별 reservation 확인용)."""
    move_ids = call("stock.move", "search", [("picking_id", "=", picking_id)])
    if not move_ids:
        return []
    records = call(
        "stock.move", "read", move_ids,
        fields=["product_id", "product_uom_qty", "quantity",
                "state", "date", "picking_id"],
    )
    return _normalize_move_records(records)


def get_move(move_id: int) -> Optional[Dict[str, Any]]:
    """
    단일 stock.move 의 최신 상태 조회.
    BC3 HIGH #8: allocate_fifo 가 reserve_move 직후 reserved_availability 를
    재확인하기 위해 사용. read() 응답이 비면 None.
    """
    try:
        recs = call(
            "stock.move", "read", [move_id],
            fields=["picking_id", "product_id", "product_uom_qty",
                    "quantity", "state", "date"],
        )
    except Exception as e:
        logger.warning(f"[odoo_service.get_move] {move_id} 실패: {e}")
        return None
    if not recs:
        return None
    return recs[0]


def get_product_quants(
    product_id: int,
    location_id: Optional[int] = None,
    usage: Optional[str] = "internal",
) -> List[Dict[str, Any]]:
    """
    제품의 현재 stock.quant (location 별 재고 분포).

    Args:
        product_id: 조회 대상 제품
        location_id: 특정 location 만 보기. 미지정 시 usage 필터 적용.
        usage: location.usage 필터. **default 'internal'** — 진짜 창고 재고만.
               None 으로 명시하면 모든 usage (supplier / customer / inventory / transit
               포함) 합산. audit / advanced 케이스 외에는 default 권장.

    BC3 CRITICAL fix (2026-05-26):
        이전엔 domain 에 location 필터 없어 supplier 의 음수 quant (-1000) 와
        WH/Stock 의 양수 quant (+1000) 가 상쇄되어 on_hand=0 silent bug.
        validate 직후 dashboard / inventory_agent 가 모두 0 으로 잘못 표시되는
        증상으로 발견. 모든 callers 가 사실상 internal 만 원하므로 default 변경이
        안전 — 명시적으로 None 을 전달해야만 옛 동작 (전체 합산).
    """
    domain = [("product_id", "=", product_id)]
    if location_id:
        domain.append(("location_id", "=", location_id))
    elif usage:
        domain.append(("location_id.usage", "=", usage))
    quant_ids = call("stock.quant", "search", domain)
    if not quant_ids:
        return []
    return call(
        "stock.quant", "read", quant_ids,
        fields=["product_id", "location_id", "quantity", "reserved_quantity", "available_quantity"],
    )


def list_open_moves_for_product(
    product_id: int,
    states: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    제품에 대한 미완료 stock.move.
    BC3 의 VIP 선점 후보 검색 / replenish 큐 조회에 사용.

    states: 기본 ["waiting", "confirmed", "partially_available", "assigned"]
            VIP 선점 시 보통 ["assigned"] (이미 할당됐는데 picking 안 된 것)
            Waiting 큐 조회 시 ["waiting", "confirmed"]
    """
    states = states or ["waiting", "confirmed", "partially_available", "assigned"]
    move_ids = call("stock.move", "search", [
        ("product_id", "=", product_id),
        ("state", "in", states),
    ])
    if not move_ids:
        return []
    # sale_line_id 가 있으면 그 order_id 를 surface — BC3 HIGH #5 봉합 용도.
    # allocate_with_preemption 의 candidate 필터가 자기 SO 의 다른 picking move 를
    # 회수 후보에서 제외할 때 sale_order_id 비교가 필요. picking_id 비교만으로는
    # split 된 같은 SO 의 다른 picking 을 걸러내지 못한다.
    records = call(
        "stock.move", "read", move_ids,
        # Odoo 19.2: stock.move.group_id 제거됨 (procurement_values json 으로 통합).
        # preemption 로직은 group_id 를 직접 쓰지 않으므로 fields list 에서만 제거.
        fields=["picking_id", "product_id", "product_uom_qty", "quantity",
                "state", "date", "origin", "partner_id", "sale_line_id"],
    )
    return _normalize_move_records(_attach_sale_order_id(records))


def get_inventory_state(product_id: int) -> Dict[str, Any]:
    """
    Odoo 의 raw stock.quant 를 ontology InventoryState 형태로 압축.
    여러 location 의 합산. incoming 은 별도 호출 (get_pending_receipts).

    Returns:
        { product_id, product_name, on_hand, reserved, available }
    """
    quants = get_product_quants(product_id)
    on_hand = sum(q.get("quantity", 0) for q in quants)
    reserved = sum(q.get("reserved_quantity", 0) for q in quants)
    available = on_hand - reserved

    product_name = ""
    try:
        prods = call("product.product", "read", [product_id], fields=["name"])
        product_name = prods[0]["name"] if prods else ""
    except Exception:
        pass

    return {
        "product_id": product_id,
        "product_name": product_name,
        "on_hand": on_hand,
        "reserved": reserved,
        "available": available,
    }


def get_pending_receipts(product_id: int, by_date_iso: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    제품에 대한 입고 예정 stock.move (incoming picking_type).
    by_date_iso 지정 시 그 날짜까지만 합산.
    """
    domain = [
        ("product_id", "=", product_id),
        ("state", "in", ["waiting", "confirmed", "partially_available", "assigned"]),
        ("picking_type_id.code", "=", "incoming"),
    ]
    if by_date_iso:
        domain.append(("date", "<=", by_date_iso))
    move_ids = call("stock.move", "search", domain)
    if not move_ids:
        return []
    return call(
        "stock.move", "read", move_ids,
        fields=["product_id", "product_uom_qty", "state", "date", "picking_id"],
    )


def reserve_move(move_id: int) -> bool:
    """
    stock.move 의 reservation 시도. 이미 reserved 면 noop, 재고 부족이면 partial.

    BC4 fix: move 의 `_action_assign` 은 private 라 XML-RPC 로 원격 호출 불가
    (saas-19.2: "Private methods cannot be called remotely"). 대신 move 가 속한
    picking 의 **공개 메서드** `stock.picking.action_assign` 으로 reserve 한다.
    (우리 도메인은 SO 당 1 outgoing picking·1 라인이므로 picking 단위 reserve =
    해당 move reserve 와 동치. 다중 라인이면 같은 picking 의 형제 move 도 함께
    reserve 되지만, Odoo 가 가용분만 잡으므로 안전.)
    """
    try:
        rec = call("stock.move", "read", [move_id], fields=["picking_id"])
        picking_id = None
        if rec:
            pf = rec[0].get("picking_id")
            picking_id = pf[0] if isinstance(pf, list) and pf else pf
        if picking_id:
            call("stock.picking", "action_assign", [picking_id])
            return True
        logger.warning(f"[odoo_service.reserve_move] {move_id} picking 없음 — reserve 불가")
        return False
    except Exception as e:
        logger.warning(f"[odoo_service.reserve_move] {move_id} 실패: {e}")
        return False


def unreserve_move(move_id: int) -> bool:
    """
    이미 reserved 된 stock.move 의 할당 해제. VIP 선점 시 Standard move 회수에 사용.
    """
    try:
        call("stock.move", "_do_unreserve", [move_id])
        return True
    except Exception as e:
        logger.warning(f"[odoo_service.unreserve_move] {move_id} 실패: {e}")
        return False


def validate_picking(picking_id: int) -> Dict[str, Any]:
    """
    stock.picking 을 'done' 상태로 진행 (button_validate).

    BC4 S1: button_validate 의 반환값을 해석한다.
      · True / {} / None        → 전량 검증 완료 (done) → {"validated": True}
      · act_window dict          → wizard 필요 (대부분 stock.backorder.confirmation)
                                   → {"validated": False, "wizard": {...}}  (결정 보류)
    이전 구현은 반환값을 버리고 무조건 validated:True 였다 — partial 상황에서
    wizard 가 떠도 "성공"으로 오인했다(BC3 gap). 이제 호출자(dispatch_shipment)가
    wizard 를 받아 partial_handling 정책(auto_backorder / cancel_remainder / llm_advisor)
    으로 분기한다.
    """
    try:
        res = call("stock.picking", "button_validate", [picking_id])
        # Odoo 가 wizard(act_window)를 띄워야 하면 res_model 을 가진 dict 를 반환.
        # BC4 S1: backorder 처리 코드(process/cancel_backorder)는 stock.backorder.confirmation
        # 모델 전용이므로, 그 wizard 일 때만 partial 분기로 보낸다. 다른 wizard
        # (stock.immediate.transfer 등)는 보수적으로 미검증 처리 + 로깅.
        if isinstance(res, dict) and res.get("res_model"):
            res_model = res.get("res_model")
            if res_model == "stock.backorder.confirmation":
                return {
                    "picking_id": picking_id,
                    "validated": False,
                    "wizard": {
                        "res_model": res_model,
                        "res_id": res.get("res_id"),
                        "context": res.get("context") or {},
                    },
                }
            logger.warning(
                f"[odoo_service.validate_picking] {picking_id} 예상외 wizard "
                f"res_model={res_model!r} — partial 분기 안 함 (수동 처리 필요)"
            )
            return {
                "picking_id": picking_id, "validated": False,
                "unexpected_wizard": res_model,
            }
        # truthy/{}/None — state 재조회로 done 확정 (안전 확인)
        latest = get_picking(picking_id)
        state = (latest or {}).get("state")
        return {
            "picking_id": picking_id,
            "validated": bool(state and str(state).lower() == "done"),
            "state": state,
        }
    except Exception as e:
        logger.warning(f"[odoo_service.validate_picking] {picking_id} 실패: {e}")
        return {"picking_id": picking_id, "validated": False, "error": str(e)}


def process_backorder(wizard_res_id: Optional[int], wizard_context: Dict[str, Any]) -> Dict[str, Any]:
    """
    stock.backorder.confirmation.process — 가용분 즉시 출하 + 부족분 backorder picking 생성.

    wizard_context 는 button_validate 가 돌려준 context 를 그대로 넘긴다
    (button_validate_picking_ids 등이 들어있어야 backorder 가 올바른 picking 에 묶임).
    res_id 가 None 이면(버전차로 wizard record 미반환) context 만으로 create 후 process 폴백.
    """
    try:
        rid = wizard_res_id
        if not rid:
            rid = call("stock.backorder.confirmation", "create", [{}],
                       context=wizard_context)
            if isinstance(rid, list):
                rid = rid[0]
        call("stock.backorder.confirmation", "process", [rid], context=wizard_context)
        return {"action": "backorder_created", "ok": True, "wizard_res_id": rid}
    except Exception as e:
        logger.warning(f"[odoo_service.process_backorder] {wizard_res_id} 실패: {e}")
        return {"action": "backorder_created", "ok": False, "error": str(e)}


def cancel_backorder(wizard_res_id: Optional[int], wizard_context: Dict[str, Any]) -> Dict[str, Any]:
    """
    stock.backorder.confirmation.process_cancel_backorder — 가용분만 출하, 부족분 취소.
    (주문 일부 취소는 파괴적 — 명시적 비즈니스 결정에만 사용.)
    """
    try:
        rid = wizard_res_id
        if not rid:
            rid = call("stock.backorder.confirmation", "create", [{}],
                       context=wizard_context)
            if isinstance(rid, list):
                rid = rid[0]
        call("stock.backorder.confirmation", "process_cancel_backorder", [rid],
             context=wizard_context)
        return {"action": "remainder_cancelled", "ok": True, "wizard_res_id": rid}
    except Exception as e:
        logger.warning(f"[odoo_service.cancel_backorder] {wizard_res_id} 실패: {e}")
        return {"action": "remainder_cancelled", "ok": False, "error": str(e)}


def get_picking_shortage(picking_id: int) -> Dict[str, Any]:
    """
    picking 의 demand vs reserved 집계 (partial advisor 입력용).
    quantity = 현재 reserve 된 수량, product_uom_qty = 주문 수요.
    """
    moves = get_picking_moves(picking_id)
    demand = sum(float(m.get("product_uom_qty") or 0) for m in moves)
    reserved = sum(float(m.get("quantity") or 0) for m in moves)
    return {
        "demand": demand,
        "reserved": reserved,
        "shortage": max(demand - reserved, 0.0),
    }


def register_stock_receipt(
    product_id: int,
    qty: float,
    location_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    수동 입고 등록 — 테스트 / 시연용. 실제 환경은 PO 흐름.
    """
    if not location_id:
        loc_ids = call("stock.location", "search",
                       [("usage", "=", "internal")], limit=1)
        if not loc_ids:
            raise RuntimeError("internal location 없음")
        location_id = loc_ids[0]

    existing = call("stock.quant", "search",
                    [("product_id", "=", product_id), ("location_id", "=", location_id)],
                    limit=1)
    if existing:
        curr = call("stock.quant", "read", [existing[0]], fields=["quantity"])[0]
        new_qty = (curr.get("quantity") or 0) + qty
        call("stock.quant", "write", [existing[0]], {"quantity": new_qty})
        return {"product_id": product_id, "location_id": location_id,
                "added_qty": qty, "new_total": new_qty, "method": "merged"}
    else:
        quant_id = call("stock.quant", "create", {
            "product_id": product_id,
            "location_id": location_id,
            "quantity": qty,
        })
        return {"product_id": product_id, "location_id": location_id,
                "added_qty": qty, "quant_id": quant_id, "method": "created"}


# ════════════════════════════════════════════════════════════════
# BC4 — Invoicing (출고분 기준 account.move 발행)
# ════════════════════════════════════════════════════════════════
# 청구 정책(invoice_policy)이 라인별로 갈린다:
#   · service(라이선스/컨설팅) = 'order'    → 주문 수량 전량 즉시 청구
#   · consu(USB 재고)          = 'delivery' → 인도(done)된 수량만 청구
# _create_invoices 는 "현재 청구 가능한(qty_to_invoice>0)" 모든 라인을 1장의
# account.move 초안으로 만든다 → 부분출고면 USB 출고분만 자동 반영.
def create_invoice_for_sale_order(
    sale_order_id: int, post: bool = True, dry_run: bool = False,
) -> Dict[str, Any]:
    """
    SO 의 현재 청구 가능분으로 고객 청구서(account.move) 생성 + (옵션) 확정(post).

    멱등/안전:
      · invoice_status != 'to invoice' 면 생성 안 함 (이미 청구됨/청구할 것 없음).
      · 생성 전후 sale_order.invoice_ids 차집합으로 '새로 생긴' 청구서만 식별.
      · dry_run=True 면 라인별 qty_to_invoice 만 집계해 미리보기 (쓰기 없음).
    """
    so = call("sale.order", "read", [sale_order_id],
              fields=["name", "invoice_status", "invoice_ids"])
    if not so:
        return {"ok": False, "error": f"sale.order {sale_order_id} 없음"}
    so = so[0]
    status = so.get("invoice_status")

    # 미리보기: 라인별 청구 예정 수량
    lines = call("sale.order.line", "search_read",
                 [("order_id", "=", sale_order_id), ("qty_to_invoice", "!=", 0)],
                 fields=["product_id", "qty_to_invoice", "qty_delivered", "price_unit"])
    preview = [{"product": (l["product_id"][1] if isinstance(l.get("product_id"), list) else None),
                "qty_to_invoice": l.get("qty_to_invoice"),
                "qty_delivered": l.get("qty_delivered")} for l in lines]

    if status != "to invoice":
        return {"ok": True, "created": False, "reason": f"invoice_status={status!r} (청구할 것 없음)",
                "preview": preview}
    if dry_run:
        return {"ok": True, "created": False, "dry_run": True, "preview": preview}

    before = set(so.get("invoice_ids") or [])
    # _create_invoices 는 private(원격 호출 차단) → 표준 마법사 사용.
    # advance_payment_method='delivered' = 인도/청구가능분으로 일반 청구서 생성.
    # 주의: create_invoices 는 ir.actions.act_window(None 포함) 를 반환 → trial 의
    #   XMLRPC 마shaller(allow_none=False)가 *응답 직렬화*에서 실패한다. 그러나
    #   청구서는 이미 커밋된다 → 예외를 흡수하고 invoice_ids 차집합으로 판정.
    wiz_ctx = {"active_model": "sale.order",
               "active_ids": [sale_order_id], "active_id": sale_order_id}
    try:
        wiz_id = call("sale.advance.payment.inv", "create",
                      {"advance_payment_method": "delivered"}, context=wiz_ctx)
        if isinstance(wiz_id, list):
            wiz_id = wiz_id[0]
        call("sale.advance.payment.inv", "create_invoices", [wiz_id], context=wiz_ctx)
    except Exception as e:
        logger.info(f"[create_invoice_for_sale_order] create_invoices 반환 직렬화 경고"
                    f"(무해 — 청구서는 생성됨): {type(e).__name__}")

    after_so = call("sale.order", "read", [sale_order_id], fields=["invoice_ids"])[0]
    new_ids = [i for i in (after_so.get("invoice_ids") or []) if i not in before]
    if not new_ids:
        return {"ok": False, "error": "청구서가 생성되지 않음 (new invoice 없음)", "preview": preview}

    if post:
        try:
            call("account.move", "action_post", new_ids)
        except Exception as e:
            logger.warning(f"[create_invoice_for_sale_order] action_post 실패(초안 유지): {e}")

    invs = call("account.move", "read", new_ids,
                fields=["name", "state", "amount_total", "invoice_origin"])
    return {"ok": True, "created": True, "invoice_ids": new_ids,
            "invoices": invs, "preview": preview}


# ════════════════════════════════════════════════════════════════
# BC5 — 수금 (연체 감지 + 입금 등록 = 실제 ERP 조작)
# ════════════════════════════════════════════════════════════════
def list_overdue_invoices(as_of_iso: Optional[str] = None) -> List[Dict[str, Any]]:
    """미수(미납/부분) + 만기 경과 고객 청구서 조회 (연체 감지, 결정론).

    as_of_iso 미지정 시 today 기준. account_followup 모듈 없이 account.move 로 감지.
    """
    import datetime as _dt
    asof = as_of_iso or _dt.date.today().isoformat()
    domain = [
        ("move_type", "=", "out_invoice"),
        ("state", "=", "posted"),
        ("payment_state", "in", ["not_paid", "partial"]),
        ("invoice_date_due", "<", asof),
    ]
    ids = call("account.move", "search", domain)
    if not ids:
        return []
    rows = call("account.move", "read", ids,
                fields=["name", "partner_id", "invoice_date_due",
                        "amount_total", "amount_residual", "payment_state"])
    # tier + 고객명 부착 (dunning advisor 입력용) — partner category 기준
    pids = list({r["partner_id"][0] for r in rows if isinstance(r.get("partner_id"), list)})
    tier_by_partner: Dict[int, str] = {}
    if pids:
        try:
            partners = call("res.partner", "read", pids, fields=["category_id"])
            cat_ids = set()
            for p in partners:
                cat_ids.update(p.get("category_id") or [])
            cat_names = {}
            if cat_ids:
                for c in call("res.partner.category", "read", list(cat_ids), fields=["name"]):
                    cat_names[c["id"]] = c.get("name") or ""
            for p in partners:
                names = [cat_names.get(cid, "") for cid in (p.get("category_id") or [])]
                tier_by_partner[p["id"]] = next(
                    (t for t in ("VIP", "Standard", "Bronze")
                     if any(t in n for n in names)), "Standard")
        except Exception as e:
            logger.warning(f"[list_overdue_invoices] tier 조회 실패: {e}")
    for r in rows:
        pf = r.get("partner_id")
        pid = pf[0] if isinstance(pf, list) else pf
        r["customer"] = pf[1] if isinstance(pf, list) else ""
        r["tier"] = tier_by_partner.get(pid, "Standard")
        try:
            due = _dt.date.fromisoformat(str(r.get("invoice_date_due")))
            r["days_overdue"] = (_dt.date.fromisoformat(asof) - due).days
        except Exception:
            r["days_overdue"] = None
    return rows


def register_invoice_payment(
    invoice_id: int, journal_id: Optional[int] = None, amount: Optional[float] = None,
) -> Dict[str, Any]:
    """입금 등록 (account.payment.register 마법사) → 인보이스 payment_state=paid.

    이것이 '수금' 의 실제 ERP 조작. amount 미지정 시 미수 전액. journal 미지정 시
    Bank/Cash 저널 자동. (create_invoices 처럼 마법사 반환 action 의 None 직렬화는
    무해 — 결과는 payment_state 재조회로 판정.)
    """
    if not journal_id:
        js = call("account.journal", "search",
                  [("type", "in", ["bank", "cash"])], limit=1)
        journal_id = js[0] if js else None

    before = call("account.move", "read", [invoice_id],
                  fields=["name", "payment_state", "amount_residual"])
    if not before:
        return {"ok": False, "error": f"invoice {invoice_id} 없음"}
    before = before[0]

    ctx = {"active_model": "account.move",
           "active_ids": [invoice_id], "active_id": invoice_id}
    vals: Dict[str, Any] = {}
    if journal_id:
        vals["journal_id"] = journal_id
    if amount is not None:
        vals["amount"] = amount
    try:
        wiz = call("account.payment.register", "create", vals, context=ctx)
        if isinstance(wiz, list):
            wiz = wiz[0]
        call("account.payment.register", "action_create_payments", [wiz], context=ctx)
    except Exception as e:
        logger.info(f"[register_invoice_payment] action 반환 직렬화 경고"
                    f"(무해 — 입금은 등록됨): {type(e).__name__}")

    after = call("account.move", "read", [invoice_id],
                 fields=["name", "payment_state", "amount_residual"])[0]
    return {"ok": after.get("payment_state") in ("paid", "in_payment"),
            "before": before, "after": after, "journal_id": journal_id}


def log_dunning_on_invoice(invoice_id: int, message: str) -> Dict[str, Any]:
    """독촉 발송 사실을 인보이스 chatter(mail.message)에 기록 → ERP 감사선."""
    try:
        call("account.move", "message_post", [invoice_id],
             body=message, subject="수금 독촉 발송")
        return {"ok": True, "invoice_id": invoice_id}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def list_pickings_by_state(states: List[str]) -> List[Dict[str, Any]]:
    """
    지정된 state 들의 모든 outgoing stock.picking 조회.
    replenish 시 waiting / confirmed 인 DO 들을 한 번에.
    """
    domain = [
        ("state", "in", states),
        ("picking_type_id.code", "=", "outgoing"),
    ]
    pids = call("stock.picking", "search", domain)
    if not pids:
        return []
    return call(
        "stock.picking", "read", pids,
        fields=["name", "state", "scheduled_date", "origin", "partner_id", "sale_id"],
    )


# ════════════════════════════════════════════════════════════════
# BC5 — Replenishment (보충 발주 = incoming picking 생성 + 미충족 수요 집계)
# ════════════════════════════════════════════════════════════════
# Odoo 제약: 이 SaaS trial 에는 'purchase' 모듈이 없어 purchase.order 사용 불가.
# 대신 incoming stock.picking 을 직접 create 한다 (scripts/bc3_create_po.py 패턴).
# 결과 동일 — Odoo 재고관리 → 입고 화면에 표시되고, 검증 시 stock.quant 증가.
# 그 검증은 trigger_stock_received 가 처리(루프 닫힘) → rule 400 재할당.

# bc3_create_po.py 와 동일한 Odoo Online 상수 — 동적 lookup 실패 시 폴백.
_INCOMING_PICKING_TYPE_ID_FALLBACK = 1   # "입고": source=Vendors → dest=WH/재고
_SUPPLIER_LOCATION_ID_FALLBACK = 1       # "Vendors"
_INTERNAL_LOCATION_ID_FALLBACK = 5       # "WH/재고"


def _resolve_incoming_picking_type() -> Dict[str, Any]:
    """incoming picking_type 와 default src/dest location 을 동적 조회.

    실패 시 bc3_create_po.py 의 하드코딩 상수로 폴백 (Odoo Online 환경 호환).
    Returns: {picking_type_id, location_id, location_dest_id}
    """
    try:
        pt_ids = call("stock.picking.type", "search",
                      [("code", "=", "incoming")], limit=1)
        if pt_ids:
            pt = call("stock.picking.type", "read", [pt_ids[0]],
                      fields=["default_location_src_id", "default_location_dest_id"])[0]

            def _m2o(v):
                return v[0] if isinstance(v, list) and v else v

            src = _m2o(pt.get("default_location_src_id"))
            dest = _m2o(pt.get("default_location_dest_id"))
            # default_location_src 가 비면(공급처 미지정) supplier usage location 탐색
            if not src:
                sup = call("stock.location", "search",
                           [("usage", "=", "supplier")], limit=1)
                src = sup[0] if sup else _SUPPLIER_LOCATION_ID_FALLBACK
            if not dest:
                intl = call("stock.location", "search",
                            [("usage", "=", "internal")], limit=1)
                dest = intl[0] if intl else _INTERNAL_LOCATION_ID_FALLBACK
            return {
                "picking_type_id": pt_ids[0],
                "location_id": src,
                "location_dest_id": dest,
            }
    except Exception as e:
        logger.warning(f"[odoo_service._resolve_incoming_picking_type] 동적 조회 실패, 상수 폴백: {e}")
    return {
        "picking_type_id": _INCOMING_PICKING_TYPE_ID_FALLBACK,
        "location_id": _SUPPLIER_LOCATION_ID_FALLBACK,
        "location_dest_id": _INTERNAL_LOCATION_ID_FALLBACK,
    }


def find_or_create_vendor(vendor_name: str) -> Optional[int]:
    """공급처(res.partner, supplier_rank>0) 조회 — 없으면 생성."""
    if not vendor_name:
        return None
    ids = call("res.partner", "search", [("name", "=", vendor_name)])
    if ids:
        return ids[0]
    return call("res.partner", "create", {
        "name": vendor_name,
        "is_company": True,
        "supplier_rank": 1,
        "comment": "BC5 auto-created replenishment vendor",
    })


def find_existing_incoming_picking(origin: str) -> Optional[Dict[str, Any]]:
    """동일 origin 의 미검증(assigned/confirmed/waiting/draft) incoming picking 조회.

    BC5 보충 발주 멱등성 — 같은 부족 상황으로 두 번 호출돼도 중복 발주 안 함.
    """
    if not origin:
        return None
    ids = call("stock.picking", "search", [
        ("origin", "=", origin),
        ("picking_type_id.code", "=", "incoming"),
        ("state", "in", ["draft", "waiting", "confirmed", "assigned"]),
    ], limit=1)
    if not ids:
        return None
    rec = call("stock.picking", "read", [ids[0]],
               fields=["name", "state", "scheduled_date", "origin"])
    return rec[0] if rec else None


def create_incoming_picking(
    product_id: int,
    qty: float,
    vendor_name: str = "TechSupply Co",
    origin: str = "",
    confirm: bool = True,
) -> Dict[str, Any]:
    """보충 입고 stock.picking 생성 (+ 옵션으로 action_confirm).

    BC5: agent 의 자율 보충 발주가 호출. purchase.order 없이 incoming picking 직접 생성.

    Returns:
        {"picking_id", "picking_name", "state", "scheduled_date", "origin",
         "qty", "skipped"?, "existing"?}
    """
    if not product_id or qty <= 0:
        raise RuntimeError(f"잘못된 인자 — product_id={product_id}, qty={qty}")

    # 멱등 키는 안정적이어야 한다 — qty/urgency 등 변동값을 origin 에 넣으면 advisor 출력이
    # 달라질 때마다 매칭이 깨져 중복 picking 이 생긴다. product 기준 안정 키 사용
    # (제품당 미검증 보충건 1개 유지). 수량은 move 에, 설명은 호출부 결과에 담는다.
    origin = origin or f"BC5 auto-replenish product_id={product_id}"

    # 멱등성 — 동일 origin 의 미검증 incoming picking 이 있으면 재생성 skip.
    existing = find_existing_incoming_picking(origin)
    if existing:
        return {
            "picking_id": None,
            "picking_name": existing.get("name"),
            "state": existing.get("state"),
            "scheduled_date": existing.get("scheduled_date"),
            "origin": origin,
            "qty": qty,
            "skipped": True,
            "reason": "동일 origin 의 미검증 incoming picking 이 이미 존재 (멱등성)",
            "existing": existing,
        }

    loc = _resolve_incoming_picking_type()
    vendor_id = find_or_create_vendor(vendor_name)

    # product 의 uom_id (Odoo 19.2: stock.move 의 uom_id 필수)
    prod = call("product.product", "read", [product_id], fields=["name", "uom_id"])[0]
    uom_id = prod["uom_id"][0] if isinstance(prod.get("uom_id"), list) else prod.get("uom_id")
    product_name = prod.get("name")

    vals: Dict[str, Any] = {
        "picking_type_id": loc["picking_type_id"],
        "location_id": loc["location_id"],
        "location_dest_id": loc["location_dest_id"],
        "origin": origin,
        "move_ids": [(0, 0, {
            "product_id": product_id,
            "product_uom_qty": qty,
            "uom_id": uom_id,
            "location_id": loc["location_id"],
            "location_dest_id": loc["location_dest_id"],
        })],
    }
    if vendor_id:
        vals["partner_id"] = vendor_id

    raw = call("stock.picking", "create", [vals])
    picking_id = raw[0] if isinstance(raw, list) and raw else raw

    confirm_error = None
    if confirm:
        try:
            call("stock.picking", "action_confirm", [picking_id])
        except Exception as e:
            # confirm 실패 시 picking 은 'draft' 로 남아 입고 큐(assigned/confirmed/waiting)
            # 검색에 안 잡힌다 → 루프가 조용히 끊김. success 로 위장하지 말고 표면화.
            confirm_error = f"{type(e).__name__}: {e}"
            logger.warning(f"[odoo_service.create_incoming_picking] action_confirm 실패: {e}")

    rec = call("stock.picking", "read", [picking_id],
               fields=["name", "state", "scheduled_date", "origin"])[0]
    return {
        "picking_id": picking_id,
        "picking_name": rec.get("name"),
        "state": rec.get("state"),
        "scheduled_date": rec.get("scheduled_date"),
        "origin": origin,
        "qty": qty,
        "product_name": product_name,
        "vendor_id": vendor_id,
        "confirm_error": confirm_error,
        "skipped": False,
    }


def get_open_demand_for_product(product_id: int) -> Dict[str, Any]:
    """제품의 미충족 outgoing 수요 집계 (보충 발주 판단 입력).

    waiting/confirmed/partially_available 인 outgoing stock.move 를 모아
    SO 별로 묶고 tier·미충족수량을 계산. get_inventory_state / get_pending_receipts
    와 합쳐 "지금 부족분이 얼마이고, 어떤 주문이 블록됐는가" 를 만든다.

    Returns:
        {
          product_id, product_name, on_hand, reserved, available, incoming,
          total_demand, total_shortage, unmet_qty,   # unmet = max(total_shortage - available - incoming, 0)
          blocked_orders: [
            {sale_order_id, so_name, account_name, tier, demand, reserved, shortage}, ...
          ]  # tier desc, 그 다음 납기 asc 로 정렬
        }
    """
    inv = get_inventory_state(product_id)
    available = float(inv.get("available") or 0)

    # incoming 예정량
    try:
        incoming_moves = get_pending_receipts(product_id)
        incoming = sum(float(m.get("product_uom_qty") or 0) for m in incoming_moves)
    except Exception as e:
        logger.warning(f"[get_open_demand_for_product] incoming 조회 실패: {e}")
        incoming = 0.0

    # 미충족 outgoing move 조회 (assigned 제외 — 이미 잡힌 건 충족된 것)
    moves = list_open_moves_for_product(
        product_id, ["waiting", "confirmed", "partially_available"],
    )
    # outgoing 판정: 이 도메인에서 "고객 수요" = SO 에 연결된(sale_order_id 있는) move.
    # list_open_moves_for_product 는 picking_type 필터가 없지만, incoming/내부이동 move 는
    # sale_order_id 가 없어 아래 가드로 자연 제외된다. (MTO/dropship 처럼 sale_line_id 를
    # 가진 비-outgoing move 가 생기는 환경이면 picking_type code 필터를 추가해야 함.)
    by_so: Dict[int, Dict[str, Any]] = {}
    for m in moves:
        so_id = m.get("sale_order_id")
        if not so_id:
            continue  # SO 에 연결 안 된 move (내부이동/입고) 제외
        demand = float(m.get("product_uom_qty") or 0)
        reserved = float(m.get("quantity") or m.get("reserved_availability") or 0)
        shortage = max(demand - reserved, 0.0)
        if so_id not in by_so:
            by_so[so_id] = {"sale_order_id": so_id, "demand": 0.0,
                            "reserved": 0.0, "shortage": 0.0}
        by_so[so_id]["demand"] += demand
        by_so[so_id]["reserved"] += reserved
        by_so[so_id]["shortage"] += shortage

    # SO 메타(이름/tier/partner) 보강
    so_ids = list(by_so.keys())
    tier_map = get_sale_order_tier_map(so_ids) if so_ids else {}
    so_meta: Dict[int, Dict[str, Any]] = {}
    if so_ids:
        try:
            recs = call("sale.order", "read", so_ids,
                        fields=["name", "partner_id"])
            for r in recs:
                pf = r.get("partner_id")
                acc = pf[1] if isinstance(pf, list) and len(pf) > 1 else None
                so_meta[r.get("id")] = {"name": r.get("name"), "account_name": acc}
        except Exception as e:
            logger.warning(f"[get_open_demand_for_product] sale.order read 실패: {e}")

    # 표시 정렬용 순서(VIP 먼저). 정책 가중치(100/50/25)를 데이터 계층에 두지 않는다 —
    # 실제 우선순위 '정책'은 ontology/agent 소유. 여기선 tier 이름의 표시 순서만 안다.
    _tier_display_order = {"VIP": 0, "Standard": 1, "Bronze": 2}
    blocked = []
    for so_id, agg in by_so.items():
        if agg["shortage"] <= 0:
            continue
        meta = so_meta.get(so_id, {})
        blocked.append({
            "sale_order_id": so_id,
            "so_name": meta.get("name"),
            "account_name": meta.get("account_name"),
            "tier": tier_map.get(so_id, "Standard"),
            "demand": agg["demand"],
            "reserved": agg["reserved"],
            "shortage": agg["shortage"],
        })
    blocked.sort(key=lambda b: (_tier_display_order.get(b["tier"], 9), b["so_name"] or ""))

    total_demand = sum(b["demand"] for b in blocked)
    total_shortage = sum(b["shortage"] for b in blocked)
    # 미충족 = 부족분 - (아직 reserve 안 된 free 재고) - (입고 예정분).
    # reservation_method='manual' 환경에서는 available(미예약 on-hand)가 양수일 수 있어
    # 명시적으로 차감하지 않으면 그만큼 과다 발주된다. demo 클린 케이스(available=0,
    # incoming=0)에서는 unmet == total_shortage.
    unmet_qty = max(total_shortage - available - incoming, 0.0)

    return {
        "product_id": product_id,
        "product_name": inv.get("product_name"),
        "on_hand": inv.get("on_hand"),
        "reserved": inv.get("reserved"),
        "available": available,
        "incoming": incoming,
        "total_demand": total_demand,
        "total_shortage": total_shortage,
        "unmet_qty": unmet_qty,
        "blocked_orders": blocked,
    }


# ════════════════════════════════════════════════════════════════
# Reset / Test helper
# ════════════════════════════════════════════════════════════════
def reset_session_for_tests():
    """테스트 격리용 — 세션 캐시 초기화."""
    global _session, _last_auth_error
    _session = {
        "uid": None, "models": None, "url": None,
        "db": None, "api_key": None, "username": None, "common": None,
    }
    _last_auth_error = None
