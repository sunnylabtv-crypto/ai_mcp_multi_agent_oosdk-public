# mcp_server/log_receiver.py
"""
Local MCP 로그 수신 API
- Local PC에서 전송한 로그를 수신
- FastAPI 엔드포인트로 구현
"""
import logging
from datetime import datetime
from typing import Any, List, Optional
from pydantic import BaseModel

from fastapi import APIRouter, HTTPException, Header

from mcp_server.logging_middleware import log_db

logger = logging.getLogger(__name__)

# API Router
router = APIRouter(prefix="/logs", tags=["logs"])


# ============================================================
# Request/Response 모델
# ============================================================

class LogEntry(BaseModel):
    """단일 로그 항목"""
    timestamp: str
    tool_name: str
    parameters: Any = {}
    success: bool = True
    error_message: Optional[str] = None
    duration_ms: Optional[float] = None
    result_summary: Optional[str] = None
    server_name: Optional[str] = None
    user_id: Optional[str] = None


class LogUploadRequest(BaseModel):
    """로그 업로드 요청"""
    logs: List[LogEntry]
    source: str = "local"  # 기본값: local


class LogUploadResponse(BaseModel):
    """로그 업로드 응답"""
    status: str
    uploaded_count: int
    message: Optional[str] = None


class LogQueryRequest(BaseModel):
    """로그 검색 요청"""
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    tool_name: Optional[str] = None
    source: Optional[str] = None
    success: Optional[bool] = None
    keyword: Optional[str] = None
    limit: int = 50
    offset: int = 0


class StatsResponse(BaseModel):
    """통계 응답"""
    status: str
    stats: dict


# ============================================================
# API Key 검증 (간단한 보안)
# ============================================================

# 환경변수나 config에서 가져오기
import os
API_KEY = os.getenv("LOG_API_KEY", "")


def verify_api_key(x_api_key: str = Header(None)):
    """API Key 검증"""
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API Key")
    return True


# ============================================================
# API 엔드포인트
# ============================================================

@router.post("/upload", response_model=LogUploadResponse)
async def upload_logs(
    request: LogUploadRequest,
    x_api_key: str = Header(None)
):
    """
    Local MCP 로그 업로드
    
    Local PC의 log_uploader.py에서 호출
    """
    verify_api_key(x_api_key)
    
    try:
        logs_data = []
        for log in request.logs:
            logs_data.append({
                "timestamp": log.timestamp,
                "source": request.source,
                "user_id": log.user_id,
                "tool_name": log.tool_name,
                "parameters": log.parameters,
                "success": log.success,
                "error_message": log.error_message,
                "duration_ms": log.duration_ms,
                "result_summary": log.result_summary
            })
        
        count = log_db.insert_logs_bulk(logs_data)
        
        logger.info(f"✅ Local 로그 수신 완료: {count}건")
        
        return LogUploadResponse(
            status="success",
            uploaded_count=count,
            message=f"{count}개 로그 저장 완료"
        )
        
    except Exception as e:
        logger.error(f"❌ 로그 업로드 실패: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/query")
async def query_logs(request: LogQueryRequest):
    """
    로그 검색 API
    """
    try:
        logs = log_db.query_logs(
            start_time=request.start_time,
            end_time=request.end_time,
            tool_name=request.tool_name,
            source=request.source,
            success=request.success,
            keyword=request.keyword,
            limit=request.limit,
            offset=request.offset
        )
        
        return {
            "status": "success",
            "count": len(logs),
            "logs": logs
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stats")
async def get_stats(
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    source: Optional[str] = None
):
    """
    로그 통계 API
    """
    try:
        stats = log_db.get_stats(
            start_time=start_time,
            end_time=end_time,
            source=source
        )
        
        return {
            "status": "success",
            "stats": stats
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/errors")
async def get_recent_errors(limit: int = 10):
    """
    최근 에러 API
    """
    try:
        errors = log_db.get_recent_errors(limit=limit)
        return {
            "status": "success",
            "count": len(errors),
            "errors": errors
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/slow")
async def get_slow_queries(threshold_ms: float = 5000, limit: int = 10):
    """
    느린 쿼리 API
    """
    try:
        slow = log_db.get_slow_queries(threshold_ms=threshold_ms, limit=limit)
        return {
            "status": "success",
            "count": len(slow),
            "slow_queries": slow
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health")
async def health_check():
    """
    헬스체크 API
    """
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "service": "MCP Log Receiver"
    }
