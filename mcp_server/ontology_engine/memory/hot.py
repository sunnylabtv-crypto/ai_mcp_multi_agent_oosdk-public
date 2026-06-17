# mcp_server/ontology_engine/memory/hot.py
"""
InMemoryHot — Phase 1 hot tier (LRU dict + TTL)

특징:
- in-process dict (단일 프로세스 한정)
- LRU eviction (max_size 초과 시 가장 오래된 것부터 제거)
- 항목별 TTL (default 24h)

Phase 2 교체 후보: RedisHot (multi-replica 지원)
"""
import time
from collections import OrderedDict
from typing import Any, Optional, List, Dict

from .base import MemoryTier


class InMemoryHot(MemoryTier):
    def __init__(self, max_size: int = 1000, default_ttl_sec: int = 86400):
        self.max_size = max_size
        self.default_ttl = default_ttl_sec
        self._store: "OrderedDict[str, Dict]" = OrderedDict()

    def put(self, key: str, value: Any, ttl_sec: Optional[int] = None) -> None:
        ttl = ttl_sec if ttl_sec is not None else self.default_ttl
        expires = time.time() + ttl if ttl else None
        # 기존 키면 제거 후 재삽입 (LRU 갱신)
        if key in self._store:
            del self._store[key]
        self._store[key] = {"value": value, "expires": expires, "stored_at": time.time()}
        self._evict_if_full()

    def get(self, key: str) -> Optional[Any]:
        if key not in self._store:
            return None
        item = self._store[key]
        if item["expires"] is not None and time.time() > item["expires"]:
            del self._store[key]
            return None
        # LRU 갱신 — 최근 접근으로 끝으로 이동
        self._store.move_to_end(key)
        return item["value"]

    def delete(self, key: str) -> bool:
        if key in self._store:
            del self._store[key]
            return True
        return False

    def size(self) -> int:
        return len(self._store)

    def list_keys(self, limit: int = 100) -> List[str]:
        return list(self._store.keys())[-limit:]

    def sweep_expired(self) -> int:
        now = time.time()
        expired = [
            k for k, v in self._store.items()
            if v["expires"] is not None and now > v["expires"]
        ]
        for k in expired:
            del self._store[k]
        return len(expired)

    def _evict_if_full(self) -> None:
        while len(self._store) > self.max_size:
            self._store.popitem(last=False)  # FIFO eviction (LRU)

    def stats(self) -> Dict[str, Any]:
        base = super().stats()
        base.update({
            "max_size": self.max_size,
            "default_ttl_sec": self.default_ttl,
        })
        return base
