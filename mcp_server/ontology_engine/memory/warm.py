# mcp_server/ontology_engine/memory/warm.py
"""
SqliteWarm — Phase 1 warm tier (SQLite, 30일 보관)

특징:
- 영속성 (프로세스 재시작해도 살아있음)
- TTL 기반 만료 (sweep_expired 로 청소)
- JSON 직렬화 값 저장

Phase 2 교체 후보: PostgresWarm (multi-replica 공유)
"""
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional, List, Dict

from .base import MemoryTier


class SqliteWarm(MemoryTier):
    def __init__(self, db_path: str = "./data/memory/warm.db", default_ttl_sec: int = 2592000):
        self.db_path = Path(db_path)
        self.default_ttl = default_ttl_sec
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _conn(self):
        # 매 호출마다 새 connection (multi-thread safe 하게)
        return sqlite3.connect(str(self.db_path))

    def _init_schema(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS warm_memory (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    expires_at REAL,
                    stored_at REAL NOT NULL
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_expires ON warm_memory(expires_at)")

    def put(self, key: str, value: Any, ttl_sec: Optional[int] = None) -> None:
        ttl = ttl_sec if ttl_sec is not None else self.default_ttl
        expires = time.time() + ttl if ttl else None
        serialized = json.dumps(value, ensure_ascii=False, default=str)
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO warm_memory (key, value, expires_at, stored_at) "
                "VALUES (?, ?, ?, ?)",
                (key, serialized, expires, time.time()),
            )

    def get(self, key: str) -> Optional[Any]:
        with self._conn() as c:
            row = c.execute(
                "SELECT value, expires_at FROM warm_memory WHERE key = ?", (key,)
            ).fetchone()
        if not row:
            return None
        value_json, expires = row
        if expires is not None and time.time() > expires:
            self.delete(key)
            return None
        try:
            return json.loads(value_json)
        except json.JSONDecodeError:
            return None

    def delete(self, key: str) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM warm_memory WHERE key = ?", (key,))
            return cur.rowcount > 0

    def size(self) -> int:
        with self._conn() as c:
            row = c.execute("SELECT COUNT(*) FROM warm_memory").fetchone()
        return row[0] if row else 0

    def list_keys(self, limit: int = 100) -> List[str]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT key FROM warm_memory ORDER BY stored_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [r[0] for r in rows]

    def sweep_expired(self) -> int:
        now = time.time()
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM warm_memory WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,),
            )
            return cur.rowcount

    def stats(self) -> Dict[str, Any]:
        base = super().stats()
        base.update({
            "db_path": str(self.db_path),
            "default_ttl_sec": self.default_ttl,
        })
        return base
