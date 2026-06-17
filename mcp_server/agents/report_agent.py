# mcp_server/agents/report_agent.py
"""
Report Agent: 로그 분석 + 시스템 모니터링 전담
- 도구 호출 로그 조회/분석
- 사용 통계 + 성능 메트릭
- 에러 현황 리포팅
- (RAG 기능은 CS Agent, Helpdesk Agent로 분리됨)
"""
import sys
from datetime import datetime, timedelta
from .base_agent import BaseAgent


class ReportAgent(BaseAgent):
    """로그 분석 및 시스템 모니터링 전문 Agent"""

    def __init__(self, llm_config: dict, service_manager=None):
        super().__init__(
            name="Report Agent",
            description="시스템 로그를 분석하고, 사용 통계와 성능 메트릭을 리포팅합니다. "
                       "도구별 호출 빈도, 평균 응답시간, 에러율 등을 분석하여 보고서를 작성합니다. "
                       "관리자용 모니터링 Agent입니다.",
            llm_config=llm_config,
        )
        self.service_manager = service_manager

    def register_tools_from_services(self, user_id: str = None):
        """로그 분석 도구 등록"""

        # 로그 검색
        async def query_logs(start_time: str = None, end_time: str = None,
                            tool_name: str = None, source: str = None,
                            success: bool = None, keyword: str = None,
                            limit: int = 50):
            """도구 호출 로그를 검색합니다"""
            from ..logging_middleware import LogDatabase
            db = LogDatabase()
            return db.query_logs(
                start_time=start_time, end_time=end_time,
                tool_name=tool_name, source=source,
                success=success, keyword=keyword, limit=limit,
            )

        # 사용 통계
        async def get_stats(start_time: str = None, end_time: str = None,
                           source: str = None, period: str = None):
            """사용 통계를 조회합니다 (period: today/week/month 또는 start_time/end_time으로 기간 지정)"""
            if period and not start_time:
                now = datetime.now()
                if period == "today":
                    start_time = now.strftime("%Y-%m-%d 00:00:00")
                elif period == "week":
                    start_time = (now - timedelta(days=7)).strftime("%Y-%m-%d 00:00:00")
                elif period == "month":
                    start_time = (now - timedelta(days=30)).strftime("%Y-%m-%d 00:00:00")
            from ..logging_middleware import LogDatabase
            db = LogDatabase()
            return db.get_stats(
                start_time=start_time, end_time=end_time, source=source
            )

        # 에러 목록
        async def get_errors(limit: int = 20):
            """최근 에러 목록을 조회합니다"""
            from ..logging_middleware import LogDatabase
            db = LogDatabase()
            return db.get_recent_errors(limit=limit)

        # 느린 호출
        async def get_slow_tools(threshold_ms: int = 5000, limit: int = 10):
            """응답이 느린 도구 호출을 조회합니다"""
            from ..logging_middleware import LogDatabase
            db = LogDatabase()
            return db.get_slow_queries(threshold_ms=threshold_ms, limit=limit)

        # 도구 등록
        self.register_tool('query_logs', query_logs,
                          '도구 호출 로그를 검색합니다 (start_time, end_time, tool_name, source 등)')
        self.register_tool('get_stats', get_stats,
                          '사용 통계를 조회합니다 (기간별, 소스별)')
        self.register_tool('get_errors', get_errors,
                          '최근 에러 목록을 조회합니다 (limit)')
        self.register_tool('get_slow_tools', get_slow_tools,
                          '느린 도구 호출을 조회합니다 (threshold_ms, limit)')

        print(f"[Report Agent] {len(self._tools)} tools registered for user: {user_id}", file=sys.stderr)
