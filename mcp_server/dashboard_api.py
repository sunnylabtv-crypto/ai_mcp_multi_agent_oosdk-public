# mcp_server/dashboard_api.py
"""
Dashboard 전용 HTTP API 라우터.

Why this exists
---------------
Dashboard (Streamlit) 와 MCP server 는 별개 프로세스이므로 in-memory engine 인스턴스를
공유할 수 없고, SQLite 파일을 fs 공유로 묶는 방식은 "단일 프로세스 데모" 가정에서만
동작합니다. Phase 2 enterprise 확장 (수평 scale, 멀티 컨테이너, 멀티 테넌트, 관리형 DB)
시점에 즉시 깨집니다.

근본 해결 (Option A): Dashboard 는 stateless viewer, MCP server 는 데이터 owner.
이 라우터가 그 경계의 backend 면입니다.

Endpoint 그룹
-------------
/api/dashboard/ontology/*  — OntologyEngine 메모리 / 의사결정
/api/dashboard/logs/*      — log_db (도구 호출 로그) 통계/검색
/api/dashboard/health      — 헬스체크

모두 GET (읽기 전용). 쓰기는 기존 /api/logs/upload (local 로그 push) 만 유지.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

from mcp_server.logging_middleware import log_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


# ============================================================
# OntologyEngine 접근 — server.py 의 싱글톤 재사용
# ============================================================

def _get_engine():
    """server.py 의 get_or_create_ontology_engine() 싱글톤을 lazy import.
    순환 import 회피용 — module-level import 하면 server.py 로드 중에 깨짐.
    """
    from mcp_server.server import get_or_create_ontology_engine
    return get_or_create_ontology_engine()


# ============================================================
# /ontology — OntologyEngine 메모리 / 결정 이력
# ============================================================

@router.get("/ontology/decisions")
def list_ontology_decisions(
    limit: int = Query(default=10, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """
    최근 OOSDK 의사결정 이력 (warm + hot tier 합산, ts 역순).

    Returns:
        {
          "ok": true,
          "count": N,
          "decisions": [
            {"key", "ts", "ts_iso", "tier", "email": {...}, "customer": ..., "matched_rule", "plan": [...]},
            ...
          ]
        }

    Why warm + hot 합산: 이전 구현은 "warm 비어있으면 hot 으로 폴백" 이라 warm 에
    NewProspect 1건만 있어도 hot 의 VIP/Standard 가 영원히 안 보였음.
    """
    try:
        engine = _get_engine()
    except Exception as e:
        logger.error(f"engine init 실패: {e}", exc_info=True)
        raise HTTPException(status_code=503, detail=f"ontology engine unavailable: {e}")

    rows: List[Dict[str, Any]] = []
    # 두 tier 모두에서 ontology_decision:* 키 수집
    # 페이지네이션을 위해 충분히 가져오기 (offset + limit + 여유분)
    fetch_size = max((offset + limit) * 4, 200)
    for tier in ("hot", "warm"):
        try:
            keys = engine.memory.list_keys(tier=tier, limit=fetch_size) or []
        except Exception as e:
            logger.warning(f"list_keys({tier}) 실패: {e}")
            continue

        for k in keys:
            if not str(k).startswith("ontology_decision:"):
                continue
            try:
                v = engine.memory.get(k, tier=tier) or {}
            except Exception:
                continue
            ts = v.get("ts")
            ts_iso = None
            if ts:
                try:
                    from datetime import datetime as _dt
                    ts_iso = _dt.utcfromtimestamp(ts).isoformat() + "Z"
                except Exception:
                    pass
            # customer_tier: 신규 records 는 "customer_tier", 구 records 는 "tier" 에 저장됨.
            # "tier" 가 VIP/Standard 면 customer tier 로 인정, 아니면 None.
            legacy_tier = v.get("tier")
            cust_tier = v.get("customer_tier") or (
                legacy_tier if legacy_tier in ("VIP", "Standard") else None
            )
            rows.append({
                "key": k,
                "ts": ts,
                "ts_iso": ts_iso,
                # memory tier (hot/warm/cold) — dashboard 의 'memory_tier' 컬럼용
                "memory_tier": tier,
                "tier": tier,   # backward compat (구 dashboard 가 d.get("tier") 로 memory tier 읽음)
                # BC1 email entity 필드
                "email": v.get("email"),
                "customer": v.get("customer"),
                # BC2 sales entity 필드 (신규)
                "entity": v.get("entity"),
                "event": v.get("event"),
                "stage": v.get("stage"),
                "account_name": v.get("account_name"),
                "account_id": v.get("account_id"),
                "opportunity_id": v.get("opportunity_id"),
                "opportunity_name": v.get("opportunity_name"),
                "customer_tier": cust_tier,  # VIP / Standard / None
                # 공통
                "matched_rule": v.get("matched_rule"),
                "plan": v.get("plan") or [],
                # BC4 S0: priority override (있을 때만 — 누가/왜/어느 SO + gate 결과)
                "override": v.get("override"),
            })

    # 중복 키 제거 (동일 key 가 hot/warm 모두에 있을 가능성 — hot 우선)
    dedup: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        key = r["key"]
        if key not in dedup or (r["tier"] == "hot" and dedup[key]["tier"] != "hot"):
            dedup[key] = r

    all_sorted = sorted(dedup.values(), key=lambda r: r.get("ts") or 0, reverse=True)
    total = len(all_sorted)
    decisions = all_sorted[offset:offset + limit]

    return {
        "ok": True,
        "count": len(decisions),
        "total": total,           # 전체 누적 결정 건수 (페이지 수 계산용)
        "offset": offset,
        "limit": limit,
        "decisions": decisions,
    }


@router.get("/ontology/memory_stats")
def get_memory_stats():
    """
    3-Tier Memory 통계 (size + backend per tier).

    Returns:
        {
          "ok": true,
          "stats": {
            "hot":  {"size": N, "backend": "in_memory", ...},
            "warm": {...},
            "cold": {...}
          }
        }
    """
    try:
        engine = _get_engine()
        stats = engine.memory.stats()
        return {"ok": True, "stats": stats}
    except Exception as e:
        logger.error(f"memory_stats 실패: {e}", exc_info=True)
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/ontology/recent_keys")
def get_recent_keys(tier: str = Query(default="warm"), limit: int = Query(default=10, ge=1, le=200)):
    """
    특정 tier 의 최근 key 목록 (모든 prefix 포함).
    Dashboard 의 "Recent keys" expander 가 사용.
    """
    if tier not in ("hot", "warm", "cold"):
        raise HTTPException(status_code=400, detail="tier must be one of hot/warm/cold")
    try:
        engine = _get_engine()
        keys = engine.memory.list_keys(tier=tier, limit=limit) or []
        return {"ok": True, "tier": tier, "count": len(keys), "keys": keys}
    except Exception as e:
        logger.error(f"recent_keys 실패: {e}", exc_info=True)
        raise HTTPException(status_code=503, detail=str(e))



@router.post("/ontology/reset_keys")
def reset_keys(payload: Dict[str, Any]):
    """
    BC2 데모 반복 시 prefix 매칭 key 를 일괄 삭제.

    Body:
        {"prefixes": ["ontology_decision:sales_", "lost_analysis:"],
         "tiers":    ["hot", "warm"]}

    BC1 의 chat_* 결정은 prefix 매칭이 아니라 보존됨.
    """
    prefixes = payload.get("prefixes") or []
    tiers = payload.get("tiers") or ["hot", "warm"]
    if not prefixes or not isinstance(prefixes, list):
        raise HTTPException(status_code=400, detail="prefixes (list) required")
    for t in tiers:
        if t not in ("hot", "warm", "cold"):
            raise HTTPException(status_code=400, detail=f"invalid tier: {t}")

    try:
        engine = _get_engine()
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))

    by_tier: Dict[str, int] = {}
    total = 0
    for tier in tiers:
        try:
            keys = engine.memory.list_keys(tier=tier, limit=10000) or []
        except Exception as e:
            logger.warning(f"reset_keys: list_keys({tier}) 실패: {e}")
            by_tier[tier] = 0
            continue
        deleted = 0
        for k in keys:
            if any(str(k).startswith(p) for p in prefixes):
                try:
                    if engine.memory.delete(k, tier=tier):
                        deleted += 1
                except Exception as e:
                    logger.warning(f"reset_keys: delete({tier},{k}) 실패: {e}")
        by_tier[tier] = deleted
        total += deleted

    logger.info(f"[reset_keys] prefixes={prefixes} tiers={tiers} -> deleted={total}")
    return {"ok": True, "deleted": total, "by_tier": by_tier,
            "prefixes": prefixes, "tiers": tiers}

@router.get("/ontology/yaml")
def get_active_yaml():
    """
    현재 MCP server 가 로드한 ontology.yaml 의 raw text.
    Dashboard 가 직접 file read 대신 server 가 실제로 보고 있는 yaml 을 반환 → 일치 보장.
    """
    try:
        yaml_path = Path(__file__).parent.parent / "ontology" / "ontology.yaml"
        if not yaml_path.exists():
            raise HTTPException(status_code=404, detail=f"ontology.yaml not found: {yaml_path}")
        text = yaml_path.read_text(encoding="utf-8")
        return {"ok": True, "path": str(yaml_path), "size": len(text), "content": text}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"yaml 읽기 실패: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# /logs — log_db 통계 / 검색
# ============================================================
# (기존 /api/logs/* 와 의도적으로 별도 prefix /api/dashboard/logs/* 로 분리:
#  log_uploader 가 쓰는 채널 (upload, query, stats, errors, slow) 와
#  dashboard 가 쓰는 채널 (필터 차원이 더 풍부) 는 책임이 다름)

# UI 라벨로 흘러들어올 수 있는 "전체 의미" 값. 클라이언트 측에서 거를 게 정석이지만
# server 도 방어적으로 무시 (서로 다른 dashboard 가 다른 라벨을 쓰므로).
_NO_FILTER = {None, "", "전체", "All"}


def _build_where(start_time, end_time, user_id, source, client_type) -> tuple[str, list]:
    """공통 WHERE 절 빌더 — None / "전체" / "All" 은 필터 미적용."""
    where = "WHERE 1=1"
    params: list = []
    if start_time:
        where += " AND timestamp >= ?"
        params.append(start_time)
    if end_time:
        where += " AND timestamp <= ?"
        params.append(end_time)
    if user_id not in _NO_FILTER:
        where += " AND user_id = ?"
        params.append(user_id)
    if source not in _NO_FILTER:
        where += " AND source = ?"
        params.append(source)
    if client_type not in _NO_FILTER:
        where += " AND client_type = ?"
        params.append(client_type)
    return where, params


@router.get("/logs/overview")
def get_logs_overview(
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    user_id: Optional[str] = None,
    source: Optional[str] = None,
    client_type: Optional[str] = None,
):
    """
    Dashboard 요약 카드 (총호출 / 성공 / 실패 / 평균응답) + 도구별 통계.
    기존 dashboard.get_stats() 가 직접 sqlite3 로 하던 일을 HTTP 로 노출.
    """
    try:
        where, params = _build_where(start_time, end_time, user_id, source, client_type)
        with log_db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                SELECT
                    COUNT(*) as total_calls,
                    SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success_count,
                    SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as error_count,
                    AVG(duration_ms) as avg_duration_ms
                FROM tool_logs {where}
            """, params)
            overall = dict(cursor.fetchone())

            cursor.execute(f"""
                SELECT
                    tool_name,
                    COUNT(*) as calls,
                    SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success,
                    AVG(duration_ms) as avg_duration
                FROM tool_logs {where}
                GROUP BY tool_name
                ORDER BY calls DESC
            """, params)
            by_tool = [dict(row) for row in cursor.fetchall()]

        return {"ok": True, "overall": overall, "by_tool": by_tool}
    except Exception as e:
        logger.error(f"logs/overview 실패: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/logs/client_type_stats")
def get_client_type_stats(
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    user_id: Optional[str] = None,
):
    """클라이언트(client_type)별 통계."""
    try:
        where, params = _build_where(start_time, end_time, user_id, None, None)
        with log_db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                SELECT
                    COALESCE(client_type, 'mcp') as client_type,
                    COUNT(*) as calls,
                    SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success,
                    SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as errors,
                    AVG(duration_ms) as avg_duration
                FROM tool_logs {where}
                GROUP BY client_type
                ORDER BY calls DESC
            """, params)
            rows = [dict(row) for row in cursor.fetchall()]
        return {"ok": True, "rows": rows}
    except Exception as e:
        logger.error(f"client_type_stats 실패: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/logs/hourly_calls")
def get_hourly_calls(
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    user_id: Optional[str] = None,
    source: Optional[str] = None,
    client_type: Optional[str] = None,
):
    """시간대별 (hour bucket) 호출 수."""
    try:
        where, params = _build_where(start_time, end_time, user_id, source, client_type)
        with log_db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                SELECT
                    strftime('%Y-%m-%d %H:00', timestamp) as hour,
                    COUNT(*) as calls,
                    SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success,
                    SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as errors
                FROM tool_logs {where}
                GROUP BY hour
                ORDER BY hour
            """, params)
            rows = [dict(row) for row in cursor.fetchall()]
        return {"ok": True, "rows": rows}
    except Exception as e:
        logger.error(f"hourly_calls 실패: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/logs/agent_stats")
def get_agent_stats(payload: Dict[str, Any]):
    """
    Agent 별 통계. agent_tools 매핑은 클라이언트가 보냄
    (dashboard.AGENT_TOOLS — 도구명이 dashboard 측 표시 정의에 종속이라 server 에 두지 않음).

    Request body:
        {
          "agent_tools": {"email_agent": ["run_email_agent", ...], ...},
          "filters": {"start_time": "...", "end_time": "...", ...}  // optional
        }

    Returns:
        {"ok": true, "by_agent": {"email_agent": {calls, success, errors, avg_duration}, ...}}
    """
    try:
        agent_tools: Dict[str, List[str]] = payload.get("agent_tools") or {}
        filters: Dict[str, Any] = payload.get("filters") or {}
        where, base_params = _build_where(
            filters.get("start_time"),
            filters.get("end_time"),
            filters.get("user_id"),
            filters.get("source"),
            filters.get("client_type"),
        )

        results: Dict[str, Dict[str, Any]] = {}
        with log_db._get_connection() as conn:
            cursor = conn.cursor()
            for agent_key, tools in agent_tools.items():
                if not tools:
                    continue
                placeholders = ",".join(["?"] * len(tools))
                # ─── prefix 매칭 보강 ───
                # BaseAgent.execute_action / execute_tool 이 직접 기록하는 내부 호출은
                # tool_name 이 "agent_action:<agent>.<action>" / "agent_tool:<agent>.<tool>"
                # 형태라 dashboard.AGENT_TOOLS 의 정적 리스트와 정확 매칭이 안 된다.
                # → agent_key 의 prefix LIKE 패턴을 OR 조건으로 추가해서
                #   dispatch 안에서 호출된 action/tool 까지 같은 agent 로 집계.
                action_prefix = f"agent_action:{agent_key}.%"
                tool_prefix = f"agent_tool:{agent_key}.%"
                cursor.execute(f"""
                    SELECT
                        COUNT(*) as calls,
                        SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success,
                        SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as errors,
                        AVG(duration_ms) as avg_duration
                    FROM tool_logs
                    {where}
                      AND (
                        tool_name IN ({placeholders})
                        OR tool_name LIKE ?
                        OR tool_name LIKE ?
                      )
                """, base_params + tools + [action_prefix, tool_prefix])
                row = dict(cursor.fetchone())
                results[agent_key] = row
        return {"ok": True, "by_agent": results}
    except Exception as e:
        logger.error(f"agent_stats 실패: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/logs/user_ids")
def get_user_ids():
    """DB 에 등장한 distinct user_id 목록 (필터 dropdown 용)."""
    try:
        with log_db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT DISTINCT user_id FROM tool_logs
                WHERE user_id IS NOT NULL ORDER BY user_id
            """)
            user_ids = [row["user_id"] for row in cursor.fetchall()]
        return {"ok": True, "user_ids": user_ids}
    except Exception as e:
        logger.error(f"user_ids 실패: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/logs/query")
def query_logs(payload: Dict[str, Any]):
    """
    로그 검색 — dashboard 의 다축 필터 (agent + tool + user + source + client + keyword).

    Request body (모두 optional):
        {
          "start_time", "end_time",
          "tool_name",        // LIKE %tool_name%
          "agent_tools": [...],  // tool_name IN (...)  (dashboard 가 AGENT_TOOLS[agent] 를 보냄)
          "user_id", "source", "client_type",
          "success": true/false,
          "keyword",
          "limit": 100
        }
    """
    try:
        f = payload or {}
        where, params = _build_where(
            f.get("start_time"), f.get("end_time"),
            f.get("user_id"), f.get("source"), f.get("client_type"),
        )
        if f.get("tool_name"):
            where += " AND tool_name LIKE ?"
            params.append(f"%{f['tool_name']}%")
        if f.get("agent_tools"):
            tools = f["agent_tools"]
            placeholders = ",".join(["?"] * len(tools))
            # agent_action:* / agent_tool:* prefix 패턴도 같은 agent 로 매칭.
            # agent_key 는 client 가 보내준 첫 tool 이름의 "run_<agent_key>" 형태에서
            # 추론 (dashboard.AGENT_TOOLS 컨벤션). 못 찾으면 prefix 매칭 생략.
            agent_key = None
            for t in tools:
                if t.startswith("run_") and t.endswith("_agent"):
                    agent_key = t[len("run_"):]  # "run_email_agent" → "email_agent"
                    break
            if agent_key:
                where += (
                    f" AND ("
                    f"tool_name IN ({placeholders}) "
                    f"OR tool_name LIKE ? OR tool_name LIKE ?"
                    f")"
                )
                params.extend(tools)
                params.append(f"agent_action:{agent_key}.%")
                params.append(f"agent_tool:{agent_key}.%")
            else:
                where += f" AND tool_name IN ({placeholders})"
                params.extend(tools)
        success = f.get("success")
        if success is not None and success != "전체":
            # "성공"/"실패" 한국어 라벨도 허용
            if success in (True, "성공", "success", "true", 1):
                where += " AND success = 1"
            elif success in (False, "실패", "fail", "false", 0):
                where += " AND success = 0"
        if f.get("keyword"):
            where += " AND (parameters LIKE ? OR error_message LIKE ?)"
            params.extend([f"%{f['keyword']}%"] * 2)

        limit = int(f.get("limit") or 100)
        with log_db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"SELECT * FROM tool_logs {where} ORDER BY timestamp DESC LIMIT ?",
                           params + [limit])
            rows = [dict(r) for r in cursor.fetchall()]
        return {"ok": True, "count": len(rows), "logs": rows}
    except Exception as e:
        logger.error(f"logs/query 실패: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health")
def dashboard_api_health():
    """헬스체크 — dashboard 가 server reachable 한지 확인용."""
    try:
        engine = _get_engine()
        engine_ok = engine is not None
    except Exception:
        engine_ok = False
    return {
        "ok": True,
        "engine_loaded": engine_ok,
        "service": "dashboard_api",
    }


# ============================================================
# /inventory — SO 라인 4-state 재고 (BC3 Section 2.1 모델)
# ============================================================
#
# 왜 dashboard_api 에 두는가:
#   Streamlit dashboard 는 stateless viewer 원칙. Odoo XML-RPC 호출은 무겁고
#   (라인 1개 → quants + pickings + moves + pending receipts 4 hop) 인증 세션을
#   유지해야 하므로 dashboard 프로세스가 직접 치면 매 새로고침마다 reauthenticate.
#   MCP server 안에서 odoo_service 세션을 공유 (BC3 PREP refactor 의 결과) 하므로
#   여기서 한 번에 묶어 dashboard 에 dict 로 내려준다.
#
# Spec 매핑 (docs/BC3_WEEK1_SPEC_v2_inventory_allocation.md §2.1):
#   on_hand        = stock.quant 합
#   reserved       = stock.move WHERE state='assigned' 합 (전 site)
#   available      = on_hand - reserved
#   incoming       = SO.commitment_date 까지 incoming PO 의 product_uom_qty 합
#   projected_avail= available + incoming
#   assigned (이 SO) = list_pickings_for_order(so).moves WHERE state='assigned' 의 quantity 합
#   delivered (이 SO) = sale.order.line.qty_delivered

@router.get("/inventory/so_lines")
def get_so_inventory_lines(
    so_name: Optional[str] = Query(default=None, description="예: S00009"),
    so_id: Optional[int] = Query(default=None, description="sale.order.id 직접 지정"),
):
    """
    SO 의 라인별 4-state 재고 + 이 SO 의 assigned/delivered 수량.

    Returns:
        {
          "ok": true,
          "so": {"id", "name", "state", "partner", "tier", "commitment_date",
                 "pickings": [{"id","name","state","scheduled_date"}, ...]},
          "lines": [
            {
              "line_id", "product_id", "product_name", "product_type",
              "qty_ordered",                     # SO 라인 주문량
              "qty_on_hand", "qty_available",    # 전사 재고 (모든 location 합)
              "qty_incoming", "qty_projected",   # commitment_date 까지 입고예정 + 예상가용
              "qty_assigned_for_so",             # 이 SO 의 picking 중 assigned 인 move 합
              "qty_delivered",                   # 표준 sale.order.line.qty_delivered
            },
            ...
          ]
        }
    """
    if not so_name and not so_id:
        # 본 엔드포인트는 panel 별 graceful-degrade 정책 (아래 794 줄 주석 참조).
        # HTTPException 대신 ok:False 로 통일 — dashboard 가 빨간 에러 박스를 깔끔히 표시.
        return {"ok": False, "error": "so_name 또는 so_id 중 하나는 필요합니다."}

    # odoo_service 는 module-level import 시 google/sf 의존성 까지 끌고 들어가지
    # 않도록 lazy import (services/__init__.py 의 lazy 패턴과 정합)
    try:
        from mcp_server.services import odoo_service
    except Exception as e:
        logger.error(f"odoo_service import 실패: {e}", exc_info=True)
        return {"ok": False, "error": f"odoo_service unavailable: {e}"}

    if not odoo_service.is_available():
        # 미인증 상태면 한 번 시도. 실패 시 graceful error.
        try:
            ok = odoo_service.authenticate_odoo()
        except Exception as e:
            return {"ok": False, "error": f"Odoo auth 예외: {e}"}
        if not ok:
            return {
                "ok": False,
                "error": "Odoo 미연결 — 환경변수 / credentials 확인 필요",
                "service_status": odoo_service.get_service_status(),
            }

    try:
        # 1) SO 헤더 — name 으로 검색하거나 id 직접 사용
        if so_id is None:
            ids = odoo_service.call("sale.order", "search", [("name", "=", so_name)])
            if not ids:
                return {"ok": False, "error": f"SO {so_name!r} 못 찾음"}
            so_id = int(ids[0])

        so_rows = odoo_service.call(
            "sale.order", "read", [so_id],
            fields=["name", "state", "partner_id", "commitment_date", "order_line"],
        )
        if not so_rows:
            return {"ok": False, "error": f"SO id={so_id} read 결과 없음"}
        so = so_rows[0]
        commitment_iso = so.get("commitment_date") or None
        partner_field = so.get("partner_id") or [None, ""]
        partner_label = partner_field[1] if isinstance(partner_field, list) and len(partner_field) > 1 else ""

        # 2) tier — BC3 MED #M1 봉합된 helper 재사용
        tier_map = odoo_service.get_sale_order_tier_map([so_id])
        tier = tier_map.get(so_id, "Standard")

        # 3) pickings + 이 SO 의 **라인 별** assigned 합 사전 집계
        #
        # 왜 line-id 키 인가 (vs product-id 키)
        # ────────────────────────────────────
        # 같은 product 가 여러 SO 라인에 등장할 수 있다 (예: 동일 부품을 2개 라인으로
        # 나눈 quote). product-id 로 합산하면 모든 동일-product 라인이 동일 합계를
        # 받아 부족분 ⚠️/✅ 표시가 깨진다. stock.move.sale_line_id 가
        # sale.order.line 으로의 정확한 역방향 링크이므로 그걸로 키한다.
        # get_picking_moves 는 sale_line_id 를 fields 에 포함하지 않으므로
        # 여기서 직접 read 호출 (helper 시그니처 변경은 다른 caller 회귀 위험).
        pickings = odoo_service.list_pickings_for_order(so_id)
        assigned_by_line: Dict[int, float] = {}
        picking_summary = []
        for p in pickings:
            pid = p.get("id")
            picking_summary.append({
                "id": pid,
                "name": p.get("name"),
                "state": p.get("state"),
                "scheduled_date": p.get("scheduled_date"),
            })
            try:
                move_ids = odoo_service.call(
                    "stock.move", "search", [("picking_id", "=", pid)],
                )
                moves = odoo_service.call(
                    "stock.move", "read", move_ids,
                    fields=["product_id", "quantity", "state", "sale_line_id"],
                ) if move_ids else []
            except Exception as e:
                logger.warning(f"[inventory/so_lines] picking {pid} moves 실패: {e}")
                continue
            for m in moves:
                # state 가 'assigned' 또는 'partially_available' 둘 다 reserve 잡힌 것.
                # · assigned             — 전 demand 가 reserve 완료
                # · partially_available  — 일부만 reserve (예: VIP preempt 가 가용 1000 다
                #                          잡아갔지만 demand 1200 이라 200 short)
                # quantity field 가 실제 reserve qty (Odoo 19.2). 양쪽 state 모두 인정해야
                # VIP preempt 결과가 dashboard 에 올바르게 표시됨 (code-review #7).
                if m.get("state") not in ("assigned", "partially_available"):
                    continue
                sl_field = m.get("sale_line_id")
                # sale_line_id 가 비어 있으면 SO 라인과 연결할 수 없는 move
                # (예: 내부 이동 / 수동 픽). 합산 skip — 그 product 는 0 으로 표시.
                line_id = sl_field[0] if isinstance(sl_field, list) and sl_field else None
                if not line_id:
                    continue
                qty = float(m.get("quantity") or 0.0)
                assigned_by_line[line_id] = assigned_by_line.get(line_id, 0.0) + qty

        # 4) SO line 별 — product type / on_hand / available / incoming / delivered
        line_ids = so.get("order_line") or []
        lines_out: List[Dict[str, Any]] = []
        if line_ids:
            lines = odoo_service.call(
                "sale.order.line", "read", line_ids,
                fields=["product_id", "product_uom_qty", "qty_delivered", "name",
                        "product_template_id"],
            )
            # product.template.type 한 번에 lookup — service 라인은 재고 계산 skip
            tpl_ids = []
            for ln in lines:
                tpl = ln.get("product_template_id")
                if isinstance(tpl, list) and tpl:
                    tpl_ids.append(tpl[0])
            tpl_types: Dict[int, str] = {}
            if tpl_ids:
                try:
                    tpls = odoo_service.call(
                        "product.template", "read", list(set(tpl_ids)),
                        fields=["type"],
                    )
                    tpl_types = {t.get("id"): t.get("type") for t in tpls}
                except Exception as e:
                    logger.warning(f"[inventory/so_lines] template read 실패: {e}")

            for ln in lines:
                prod_field = ln.get("product_id") or []
                product_id = prod_field[0] if isinstance(prod_field, list) and prod_field else None
                product_name = prod_field[1] if isinstance(prod_field, list) and len(prod_field) > 1 else ln.get("name", "")
                tpl_field = ln.get("product_template_id") or []
                tpl_id = tpl_field[0] if isinstance(tpl_field, list) and tpl_field else None
                ptype = tpl_types.get(tpl_id, "")

                qty_ordered = float(ln.get("product_uom_qty") or 0.0)
                qty_delivered = float(ln.get("qty_delivered") or 0.0)

                # service / consu 라인은 재고 4-state 가 의미 없음 → 0 으로 채우고 표시 단에서 "—"
                if not product_id or ptype == "service":
                    lines_out.append({
                        "line_id": ln.get("id"),
                        "product_id": product_id,
                        "product_name": product_name,
                        "product_type": ptype or "service",
                        "qty_ordered": qty_ordered,
                        "qty_on_hand": None,
                        "qty_available": None,
                        "qty_incoming": None,
                        "qty_incoming_total": None,
                        "qty_projected": None,
                        "qty_assigned_for_so": None,
                        "qty_delivered": qty_delivered,
                    })
                    continue

                # 4-state — storable 만
                try:
                    inv = odoo_service.get_inventory_state(product_id)
                except Exception as e:
                    logger.warning(f"[inventory/so_lines] inventory_state 실패 product={product_id}: {e}")
                    inv = {"on_hand": 0.0, "available": 0.0}

                on_hand = float(inv.get("on_hand") or 0.0)
                available = float(inv.get("available") or 0.0)

                # ── 입고예정 — 2 가지 의미 동시 노출 (code-review #4 보완) ──
                #
                # qty_incoming (spec 정의)
                #   BC3 §2.1 — commitment_date 까지 도착할 PO 만 합산.
                #   "이 SO 의 약속 시점 안에 채워질 양". commitment 가 None / 과거 인
                #   경우 0 으로 나올 수 있어 UX 상 혼동 가능.
                #
                # qty_incoming_total (UX 보완)
                #   commitment 무관 — 모든 미수령 incoming PO 합. "당장은 못 받았지만
                #   곧 들어올 양". 두 값이 다르면 사용자가 'commitment 이후 도착 PO 가
                #   있다' 는 사실을 즉시 인지.
                #
                # RPC 1번만 — filter off 로 받아 Python 에서 분류 (commitment 비교).
                try:
                    all_receipts = odoo_service.get_pending_receipts(product_id, by_date_iso=None)
                except Exception as e:
                    logger.warning(f"[inventory/so_lines] pending_receipts 실패 product={product_id}: {e}")
                    all_receipts = []
                incoming_total = sum(float(r.get("product_uom_qty") or 0.0) for r in all_receipts)
                if commitment_iso:
                    incoming_within = sum(
                        float(r.get("product_uom_qty") or 0.0)
                        for r in all_receipts
                        if r.get("date") and r.get("date") <= commitment_iso
                    )
                else:
                    # commitment 미설정 → spec 상 incoming 의미가 약함. UX 적으로 전체와 동일.
                    incoming_within = incoming_total
                projected = available + incoming_within

                lines_out.append({
                    "line_id": ln.get("id"),
                    "product_id": product_id,
                    "product_name": product_name,
                    "product_type": ptype or "product",
                    "qty_ordered": qty_ordered,
                    "qty_on_hand": on_hand,
                    "qty_available": available,
                    "qty_incoming": incoming_within,        # spec 정의 (≤ commitment)
                    "qty_incoming_total": incoming_total,   # commitment 무관 전체
                    "qty_projected": projected,
                    # sale_line_id 기준 — 동일 product 가 여러 라인에 있어도 라인별 정확
                    "qty_assigned_for_so": assigned_by_line.get(ln.get("id"), 0.0),
                    "qty_delivered": qty_delivered,
                })

        return {
            "ok": True,
            "so": {
                "id": so_id,
                "name": so.get("name"),
                "state": so.get("state"),
                "partner": partner_label,
                "tier": tier,
                "commitment_date": commitment_iso,
                "pickings": picking_summary,
            },
            "lines": lines_out,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"inventory/so_lines 실패: {e}", exc_info=True)
        # raise HTTPException 대신 ok:False 로 — dashboard 가 panel 별 graceful degrade
        return {"ok": False, "error": str(e)}
