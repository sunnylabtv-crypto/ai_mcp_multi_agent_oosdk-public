# dashboard_modules/api_client.py
"""
Dashboard → MCP server HTTP client.

Why
---
Dashboard (Streamlit) 와 MCP server 는 별개 프로세스이므로 in-memory engine /
SQLite 파일 직접 접근으로 데이터를 공유할 수 없음. 모든 데이터는 MCP server 의
Log API (port 9101) 를 통해 fetch.

Endpoint base
-------------
환경변수 OOSDK_MCP_API_BASE 로 override 가능. 기본값은 같은 VM 의 9101.

Error policy
------------
HTTP 실패 / 타임아웃 시 dict {"ok": False, "error": "..."} 를 반환 (raise 하지 않음).
호출 측이 ok=False 를 보고 에러 표시. 이렇게 하면 dashboard 가 MCP 서버 다운
시점에도 panel 별로 grace ful degradation.
"""
from __future__ import annotations

import os
import logging
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# ─── 환경 설정 ─────────────────────────────────────────────────
# OOSDK 기본값:
#   - VM 내부 (dashboard 가 VM 에서 streamlit 으로 띄워진 경우): localhost:9101
#   - Docker 내부 / 다른 VM: OOSDK_MCP_API_BASE 환경변수로 override
DEFAULT_BASE = "http://localhost:9101/api"
API_BASE = os.getenv("OOSDK_MCP_API_BASE", DEFAULT_BASE).rstrip("/")
TIMEOUT_SEC = float(os.getenv("OOSDK_MCP_API_TIMEOUT", "10"))


def _url(path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    return f"{API_BASE}{path}"


def _safe_get(path: str, params: Optional[Dict] = None) -> Dict[str, Any]:
    try:
        r = requests.get(_url(path), params=params, timeout=TIMEOUT_SEC)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        logger.warning(f"[api_client] GET {path} 실패: {e}")
        return {"ok": False, "error": str(e), "endpoint": path}
    except ValueError as e:
        logger.warning(f"[api_client] GET {path} JSON parse 실패: {e}")
        return {"ok": False, "error": f"invalid JSON: {e}", "endpoint": path}


def _safe_post(path: str, json: Optional[Dict] = None) -> Dict[str, Any]:
    try:
        r = requests.post(_url(path), json=json or {}, timeout=TIMEOUT_SEC)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        logger.warning(f"[api_client] POST {path} 실패: {e}")
        return {"ok": False, "error": str(e), "endpoint": path}
    except ValueError as e:
        logger.warning(f"[api_client] POST {path} JSON parse 실패: {e}")
        return {"ok": False, "error": f"invalid JSON: {e}", "endpoint": path}


# ============================================================
# Ontology
# ============================================================

def get_ontology_decisions(limit: int = 10, offset: int = 0) -> Dict[str, Any]:
    """최근 의사결정 (warm + hot 합산, ts 역순). offset 으로 페이지네이션 가능."""
    return _safe_get(
        "/dashboard/ontology/decisions",
        params={"limit": limit, "offset": offset},
    )


def get_memory_stats() -> Dict[str, Any]:
    """3-Tier Memory 통계."""
    return _safe_get("/dashboard/ontology/memory_stats")


def get_recent_keys(tier: str = "warm", limit: int = 10) -> Dict[str, Any]:
    """특정 tier 의 최근 key 목록."""
    return _safe_get("/dashboard/ontology/recent_keys", params={"tier": tier, "limit": limit})


def get_active_yaml() -> Dict[str, Any]:
    """현재 MCP server 가 로드한 ontology.yaml 의 raw text."""
    return _safe_get("/dashboard/ontology/yaml")


# ============================================================
# Logs
# ============================================================

def get_logs_overview(
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    user_id: Optional[str] = None,
    source: Optional[str] = None,
    client_type: Optional[str] = None,
) -> Dict[str, Any]:
    """요약 카드 + 도구별 통계."""
    params = _drop_none({
        "start_time": start_time, "end_time": end_time,
        "user_id": user_id, "source": source, "client_type": client_type,
    })
    return _safe_get("/dashboard/logs/overview", params=params)


def get_client_type_stats(
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    params = _drop_none({"start_time": start_time, "end_time": end_time, "user_id": user_id})
    return _safe_get("/dashboard/logs/client_type_stats", params=params)


def get_hourly_calls(
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    user_id: Optional[str] = None,
    source: Optional[str] = None,
    client_type: Optional[str] = None,
) -> Dict[str, Any]:
    params = _drop_none({
        "start_time": start_time, "end_time": end_time,
        "user_id": user_id, "source": source, "client_type": client_type,
    })
    return _safe_get("/dashboard/logs/hourly_calls", params=params)


def get_agent_stats(
    agent_tools: Dict[str, List[str]],
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    user_id: Optional[str] = None,
    source: Optional[str] = None,
    client_type: Optional[str] = None,
) -> Dict[str, Any]:
    body = {
        "agent_tools": agent_tools,
        "filters": _drop_none({
            "start_time": start_time, "end_time": end_time,
            "user_id": user_id, "source": source, "client_type": client_type,
        }),
    }
    return _safe_post("/dashboard/logs/agent_stats", json=body)


def get_user_ids() -> Dict[str, Any]:
    return _safe_get("/dashboard/logs/user_ids")


def query_logs(
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    tool_name: Optional[str] = None,
    agent_tools: Optional[List[str]] = None,
    user_id: Optional[str] = None,
    source: Optional[str] = None,
    client_type: Optional[str] = None,
    success: Optional[Any] = None,
    keyword: Optional[str] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    body = _drop_none({
        "start_time": start_time, "end_time": end_time,
        "tool_name": tool_name,
        "agent_tools": agent_tools,
        "user_id": user_id, "source": source, "client_type": client_type,
        "success": success, "keyword": keyword,
        "limit": limit,
    })
    return _safe_post("/dashboard/logs/query", json=body)


def health() -> Dict[str, Any]:
    return _safe_get("/dashboard/health")


# ============================================================
# Inventory (SO 4-state)
# ============================================================

def get_so_inventory(so_name: Optional[str] = None, so_id: Optional[int] = None) -> Dict[str, Any]:
    """SO 의 라인별 4-state 재고 + assigned/delivered. 둘 중 하나는 필수."""
    params = _drop_none({"so_name": so_name, "so_id": so_id})
    return _safe_get("/dashboard/inventory/so_lines", params=params)


# ============================================================
# helpers
# ============================================================

def _drop_none(d: Dict) -> Dict:
    """None / 한국어 "전체" / 영어 "All" 라벨은 필터 무시 의미 → 전송 제외."""
    skip = {None, "전체", "All"}
    return {k: v for k, v in d.items() if v not in skip}


def base_url() -> str:
    """현재 사용 중인 API base — 디버그/표시용."""
    return API_BASE
