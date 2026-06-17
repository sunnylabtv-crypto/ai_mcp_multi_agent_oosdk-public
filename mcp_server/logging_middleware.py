# mcp_server/logging_middleware.py
"""
MCP 도구 호출 로깅 미들웨어
- 모든 도구 호출을 자동으로 기록
- SQLite DB + JSON Lines 파일에 저장
- 대시보드 및 검색을 위한 데이터 수집
"""
import sys
import time
import json
import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Any
from contextlib import contextmanager

from fastmcp.server.middleware import Middleware, MiddlewareContext

logger = logging.getLogger(__name__)


# ============================================================
# 로그 저장소 설정
# ============================================================

# 로그 파일 폴더 (프로젝트 내부, 배포 시 초기화됨)
LOGS_DIR = Path(__file__).parent.parent / "logs"
LOGS_DIR.mkdir(exist_ok=True)
JSONL_PATH = LOGS_DIR / "mcp_tools.jsonl"

# DB 파일 (영구 저장, 배포 시에도 유지됨)
# Docker: /app/data/multi/db 폴더 (Multi-Agent 전용, Single Agent와 분리)
DB_DIR = Path("/app/data/multi/db") if Path("/app/data/multi/db").exists() else Path(__file__).parent.parent / "data" / "db"
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "mcp_logs.db"


# ============================================================
# SQLite 데이터베이스 관리
# ============================================================

class LogDatabase:
    """SQLite 로그 데이터베이스 관리"""
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._init_db()
    
    def _init_db(self):
        """데이터베이스 및 테이블 초기화"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # 메인 로그 테이블
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tool_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'remote',
                    client_type TEXT DEFAULT 'mcp',
                    user_id TEXT,
                    tool_name TEXT NOT NULL,
                    parameters TEXT,
                    success INTEGER NOT NULL,
                    error_message TEXT,
                    duration_ms REAL,
                    result_summary TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 기존 DB에 client_type 컬럼이 없으면 추가 (마이그레이션)
            try:
                cursor.execute("SELECT client_type FROM tool_logs LIMIT 1")
            except sqlite3.OperationalError:
                cursor.execute("ALTER TABLE tool_logs ADD COLUMN client_type TEXT DEFAULT 'mcp'")
                logger.info("📦 DB 마이그레이션: client_type 컬럼 추가 완료")

            # 인덱스 생성 (검색 성능 향상)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON tool_logs(timestamp)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tool_name ON tool_logs(tool_name)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_success ON tool_logs(success)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_source ON tool_logs(source)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_id ON tool_logs(user_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_client_type ON tool_logs(client_type)")
            
            # 일별 집계 테이블 (보관주기용)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS daily_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    source TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    total_calls INTEGER DEFAULT 0,
                    success_count INTEGER DEFAULT 0,
                    error_count INTEGER DEFAULT 0,
                    avg_duration_ms REAL,
                    min_duration_ms REAL,
                    max_duration_ms REAL,
                    UNIQUE(date, source, tool_name)
                )
            """)
            
            conn.commit()
            logger.info(f"✅ 로그 DB 초기화 완료: {DB_PATH}")
    
    @contextmanager
    def _get_connection(self):
        """DB 연결 컨텍스트 매니저"""
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
    
    def insert_log(self, log_data: dict):
        """로그 레코드 삽입"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO tool_logs
                (timestamp, source, client_type, user_id, tool_name, parameters, success, error_message, duration_ms, result_summary)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                log_data['timestamp'],
                log_data.get('source', 'remote'),
                log_data.get('client_type', 'mcp'),
                log_data.get('user_id'),
                log_data['tool_name'],
                json.dumps(log_data.get('parameters', {}), ensure_ascii=False),
                1 if log_data['success'] else 0,
                log_data.get('error_message'),
                log_data.get('duration_ms'),
                log_data.get('result_summary')
            ))
            conn.commit()
            return cursor.lastrowid

    def insert_logs_bulk(self, logs: list):
        """여러 로그 일괄 삽입 (Local 로그 수신용)"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.executemany("""
                INSERT INTO tool_logs
                (timestamp, source, client_type, user_id, tool_name, parameters, success, error_message, duration_ms, result_summary)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [(
                log['timestamp'],
                log.get('source', 'local'),
                log.get('client_type', 'local'),
                log.get('user_id'),
                log['tool_name'],
                json.dumps(log.get('parameters', {}), ensure_ascii=False),
                1 if log.get('success', True) else 0,
                log.get('error_message'),
                log.get('duration_ms'),
                log.get('result_summary')
            ) for log in logs])
            conn.commit()
            return len(logs)
    
    def query_logs(
        self,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        tool_name: Optional[str] = None,
        source: Optional[str] = None,
        client_type: Optional[str] = None,
        success: Optional[bool] = None,
        user_id: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 100,
        offset: int = 0
    ) -> list:
        """로그 검색"""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            query = "SELECT * FROM tool_logs WHERE 1=1"
            params = []

            if start_time:
                query += " AND timestamp >= ?"
                params.append(start_time)
            if end_time:
                query += " AND timestamp <= ?"
                params.append(end_time)
            if tool_name:
                query += " AND tool_name LIKE ?"
                params.append(f"%{tool_name}%")
            if source:
                query += " AND source = ?"
                params.append(source)
            if client_type:
                query += " AND client_type = ?"
                params.append(client_type)
            if success is not None:
                query += " AND success = ?"
                params.append(1 if success else 0)
            if user_id:
                query += " AND user_id = ?"
                params.append(user_id)
            if keyword:
                query += " AND (parameters LIKE ? OR error_message LIKE ? OR result_summary LIKE ?)"
                params.extend([f"%{keyword}%"] * 3)

            query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
    
    def get_stats(
        self,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        source: Optional[str] = None
    ) -> dict:
        """통계 조회"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # 기본 조건
            where_clause = "WHERE 1=1"
            params = []
            
            if start_time:
                where_clause += " AND timestamp >= ?"
                params.append(start_time)
            if end_time:
                where_clause += " AND timestamp <= ?"
                params.append(end_time)
            if source:
                where_clause += " AND source = ?"
                params.append(source)
            
            # 전체 통계
            cursor.execute(f"""
                SELECT 
                    COUNT(*) as total_calls,
                    SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success_count,
                    SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as error_count,
                    AVG(duration_ms) as avg_duration_ms,
                    MIN(duration_ms) as min_duration_ms,
                    MAX(duration_ms) as max_duration_ms
                FROM tool_logs {where_clause}
            """, params)
            overall = dict(cursor.fetchone())
            
            # 도구별 통계
            cursor.execute(f"""
                SELECT 
                    tool_name,
                    COUNT(*) as calls,
                    SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success,
                    AVG(duration_ms) as avg_duration_ms
                FROM tool_logs {where_clause}
                GROUP BY tool_name
                ORDER BY calls DESC
            """, params)
            by_tool = [dict(row) for row in cursor.fetchall()]
            
            # 소스별 통계
            cursor.execute(f"""
                SELECT 
                    source,
                    COUNT(*) as calls,
                    SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success
                FROM tool_logs {where_clause}
                GROUP BY source
            """, params)
            by_source = [dict(row) for row in cursor.fetchall()]
            
            return {
                "overall": overall,
                "by_tool": by_tool,
                "by_source": by_source
            }
    
    def get_recent_errors(self, limit: int = 10) -> list:
        """최근 에러 조회"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM tool_logs 
                WHERE success = 0 
                ORDER BY timestamp DESC 
                LIMIT ?
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]
    
    def get_slow_queries(self, threshold_ms: float = 5000, limit: int = 10) -> list:
        """느린 쿼리 조회"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM tool_logs 
                WHERE duration_ms > ? 
                ORDER BY duration_ms DESC 
                LIMIT ?
            """, (threshold_ms, limit))
            return [dict(row) for row in cursor.fetchall()]


# 전역 DB 인스턴스
log_db = LogDatabase()


# ============================================================
# JSON Lines 파일 로깅
# ============================================================

def write_jsonl(log_data: dict):
    """JSON Lines 파일에 로그 추가"""
    try:
        with open(JSONL_PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps(log_data, ensure_ascii=False, default=str) + '\n')
    except Exception as e:
        logger.warning(f"JSONL 쓰기 실패: {e}")


# ============================================================
# 결과 요약 생성
# ============================================================

def summarize_result(result: Any, max_length: int = 200) -> str:
    """결과를 요약 문자열로 변환"""
    try:
        if result is None:
            return "null"
        if isinstance(result, str):
            return result[:max_length] + "..." if len(result) > max_length else result
        if isinstance(result, dict):
            # 주요 키만 요약
            summary_keys = ['status', 'count', 'total', 'message', 'error']
            summary = {k: v for k, v in result.items() if k in summary_keys}
            if not summary:
                summary = {"keys": list(result.keys())[:5]}
            return json.dumps(summary, ensure_ascii=False)[:max_length]
        if isinstance(result, list):
            return f"[{len(result)} items]"
        return str(result)[:max_length]
    except:
        return "unknown"


# ============================================================
# 로깅 미들웨어
# ============================================================

class LoggingMiddleware(Middleware):
    """
    모든 MCP 도구 호출을 자동으로 로깅하는 미들웨어

    기록 내용:
    - 타임스탬프, 도구명, 파라미터
    - 실행 시간 (밀리초)
    - 성공/실패 상태
    - 에러 메시지 (실패 시)
    - 결과 요약
    """

    async def on_message(self, context: MiddlewareContext, call_next):
        """모든 메시지 디버그 로깅"""
        print(f"[MIDDLEWARE DEBUG] on_message: method={context.method}, type={context.type}", file=sys.stderr)
        return await call_next(context)

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        """도구 호출 시 로깅"""
        print(f"[MIDDLEWARE DEBUG] on_call_tool 진입: {context.message.name}", file=sys.stderr)

        tool_name = context.message.name
        parameters = context.message.arguments or {}

        # 사용자 ID 및 클라이언트 타입 가져오기 (UserIdentificationMiddleware에서 설정)
        user_id = None
        client_type = "mcp"
        try:
            if context.fastmcp_context:
                user_id = await context.fastmcp_context.get_state("user_id")
                client_type = await context.fastmcp_context.get_state("client_type") or "mcp"
        except Exception as e:
            print(f"[MIDDLEWARE DEBUG] user_id/client_type 가져오기 실패: {e}", file=sys.stderr)

        # source = 도구 실행 위치 (remote: GCP 서버 / local: PC 로컬)
        # client_type = 호출 진입점 (claude_desktop / cursor / adk / mcp)
        # → 이 두 축은 독립적. 여기서는 항상 remote (서버에서 실행되므로)

        start_time = time.time()
        timestamp = datetime.utcnow().isoformat() + "Z"

        log_data = {
            "timestamp": timestamp,
            "source": "remote",
            "client_type": client_type,
            "user_id": user_id,
            "tool_name": tool_name,
            "parameters": parameters,
            "success": True,
            "error_message": None,
            "duration_ms": None,
            "result_summary": None
        }

        try:
            # 실제 도구 실행
            result = await call_next(context)

            # 성공 로그
            log_data["duration_ms"] = (time.time() - start_time) * 1000
            log_data["result_summary"] = summarize_result(result)

            print(f"[MIDDLEWARE DEBUG] ✅ {tool_name} 완료 ({log_data['duration_ms']:.0f}ms)", file=sys.stderr)

            return result

        except Exception as e:
            # 실패 로그
            log_data["success"] = False
            log_data["error_message"] = str(e)
            log_data["duration_ms"] = (time.time() - start_time) * 1000

            print(f"[MIDDLEWARE DEBUG] ❌ {tool_name} 실패: {e}", file=sys.stderr)

            raise

        finally:
            # DB 및 파일에 저장
            try:
                log_db.insert_log(log_data)
                write_jsonl(log_data)
                print(f"[MIDDLEWARE DEBUG] 로그 저장 완료: {tool_name}", file=sys.stderr)
            except Exception as e:
                print(f"[MIDDLEWARE DEBUG] 로그 저장 실패: {e}", file=sys.stderr)
                logger.error(f"로그 저장 실패: {e}")


# ============================================================
# 로그 조회 도구 (MCP 도구로 등록할 함수들)
# ============================================================

def query_tool_logs(
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
    """
    try:
        logs = log_db.query_logs(
            start_time=start_time,
            end_time=end_time,
            tool_name=tool_name,
            source=source,
            success=success,
            keyword=keyword,
            limit=limit
        )
        return {"status": "success", "count": len(logs), "logs": logs}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def get_log_stats(
    start_time: str = None,
    end_time: str = None,
    source: str = None
) -> dict:
    """
    로그 통계를 조회합니다.
    
    Args:
        start_time: 시작 시간 (ISO 포맷)
        end_time: 종료 시간 (ISO 포맷)
        source: 소스 필터 ('remote' 또는 'local')
    
    Returns:
        전체 통계, 도구별 통계, 소스별 통계
    """
    try:
        stats = log_db.get_stats(
            start_time=start_time,
            end_time=end_time,
            source=source
        )
        return {"status": "success", "stats": stats}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def get_recent_errors(limit: int = 10) -> dict:
    """
    최근 에러 로그를 조회합니다.
    
    Args:
        limit: 최대 결과 수 (기본 10)
    
    Returns:
        최근 에러 목록
    """
    try:
        errors = log_db.get_recent_errors(limit=limit)
        return {"status": "success", "count": len(errors), "errors": errors}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def get_slow_queries(threshold_ms: float = 5000, limit: int = 10) -> dict:
    """
    느린 도구 호출을 조회합니다.
    
    Args:
        threshold_ms: 기준 시간 (밀리초, 기본 5000ms)
        limit: 최대 결과 수 (기본 10)
    
    Returns:
        느린 호출 목록
    """
    try:
        slow = log_db.get_slow_queries(threshold_ms=threshold_ms, limit=limit)
        return {"status": "success", "count": len(slow), "slow_queries": slow}
    except Exception as e:
        return {"status": "error", "message": str(e)}
