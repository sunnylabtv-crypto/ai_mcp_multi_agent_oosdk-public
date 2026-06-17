# mcp_server/tools/logging_tools.py
"""
MCP 로그 조회 도구
- Claude에서 직접 로그 검색/통계 조회 가능
"""
import logging
from mcp_server.logging_middleware import (
    query_tool_logs,
    get_log_stats,
    get_recent_errors,
    get_slow_queries,
    log_db
)

logger = logging.getLogger(__name__)


def register_logging_tools(mcp):
    """로그 조회 도구 등록"""
    
    @mcp.tool()
    def query_logs(
        start_time: str = None,
        end_time: str = None,
        tool_name: str = None,
        source: str = None,
        success: bool = None,
        keyword: str = None,
        limit: int = 50
    ) -> dict:
        """
        도구 호출 로그를 검색합니다.
        
        Args:
            start_time: 시작 시간 (ISO 포맷, 예: 2026-02-06T00:00:00Z)
            end_time: 종료 시간 (ISO 포맷)
            tool_name: 도구 이름 필터 (부분 일치)
            source: 소스 필터 ('remote' 또는 'local')
            success: 성공 여부 필터 (True/False)
            keyword: 키워드 검색 (파라미터, 에러메시지, 결과에서)
            limit: 최대 결과 수 (기본 50)
        
        Returns:
            검색 결과 로그 목록
        
        Example:
            query_logs(tool_name="send_email", success=False, limit=10)
        """
        logger.info(f"🔍 로그 검색: tool={tool_name}, source={source}, success={success}")
        return query_tool_logs(
            start_time=start_time,
            end_time=end_time,
            tool_name=tool_name,
            source=source,
            success=success,
            keyword=keyword,
            limit=limit
        )
    
    @mcp.tool()
    def get_stats(
        start_time: str = None,
        end_time: str = None,
        source: str = None
    ) -> dict:
        """
        로그 통계를 조회합니다.
        
        Args:
            start_time: 시작 시간 (ISO 포맷, 예: 2026-02-06T00:00:00Z)
            end_time: 종료 시간 (ISO 포맷)
            source: 소스 필터 ('remote' 또는 'local')
        
        Returns:
            전체 통계, 도구별 통계, 소스별 통계
        
        Example:
            get_stats()  # 전체 통계
            get_stats(source="remote")  # Remote MCP만
            get_stats(start_time="2026-02-06T00:00:00Z")  # 오늘부터
        """
        logger.info(f"📊 통계 조회: source={source}")
        return get_log_stats(
            start_time=start_time,
            end_time=end_time,
            source=source
        )
    
    @mcp.tool()
    def get_errors(limit: int = 10) -> dict:
        """
        최근 에러 로그를 조회합니다.
        
        Args:
            limit: 최대 결과 수 (기본 10)
        
        Returns:
            최근 에러 목록 (타임스탬프, 도구명, 에러메시지 포함)
        
        Example:
            get_errors()  # 최근 10개 에러
            get_errors(limit=5)  # 최근 5개 에러
        """
        logger.info(f"❌ 에러 조회: limit={limit}")
        return get_recent_errors(limit=limit)
    
    @mcp.tool()
    def get_slow_tools(threshold_ms: float = 5000, limit: int = 10) -> dict:
        """
        느린 도구 호출을 조회합니다.
        
        Args:
            threshold_ms: 기준 시간 (밀리초, 기본 5000ms = 5초)
            limit: 최대 결과 수 (기본 10)
        
        Returns:
            느린 호출 목록 (응답시간 기준 내림차순)
        
        Example:
            get_slow_tools()  # 5초 이상 걸린 호출
            get_slow_tools(threshold_ms=3000)  # 3초 이상 걸린 호출
        """
        logger.info(f"🐢 느린 쿼리 조회: threshold={threshold_ms}ms, limit={limit}")
        return get_slow_queries(threshold_ms=threshold_ms, limit=limit)
    
    @mcp.tool()
    def upload_local_logs(logs: list) -> dict:
        """
        Local MCP 로그를 업로드합니다. (Local PC에서 전송용)
        
        Args:
            logs: 로그 목록 (각 항목은 timestamp, tool_name, success 등 포함)
        
        Returns:
            업로드 결과
        
        Example:
            upload_local_logs([
                {"timestamp": "2026-02-06T10:00:00Z", "tool_name": "read_file", "success": True, "duration_ms": 100}
            ])
        """
        logger.info(f"📤 Local 로그 업로드: {len(logs)}건")
        try:
            count = log_db.insert_logs_bulk(logs)
            return {"status": "success", "uploaded_count": count}
        except Exception as e:
            logger.error(f"❌ Local 로그 업로드 실패: {e}")
            return {"status": "error", "message": str(e)}
    
    logger.info("✅ 로그 조회 도구 등록 완료")
