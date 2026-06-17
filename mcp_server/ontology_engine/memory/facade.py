# mcp_server/ontology_engine/memory/facade.py
"""
ThreeTierMemory — hot/warm/cold 를 묶는 facade

OntologyEngine 은 이 facade 만 알면 됨. 내부 backend 가 무엇인지 신경 안 씀.
yaml 의 memory 섹션을 받아 각 tier 인스턴스화.
"""
from typing import Any, Optional, Dict

from .base import MemoryTier
from .hot import InMemoryHot
from .warm import SqliteWarm
from .cold import JsonlCold


class ThreeTierMemory:
    """3-tier 메모리 facade"""

    def __init__(self, memory_config: Optional[Dict] = None):
        """
        Args:
            memory_config: yaml 의 memory 섹션. None 이면 기본값.
        """
        cfg = memory_config or {}
        self.hot: MemoryTier = self._build_hot(cfg.get("hot", {}))
        self.warm: MemoryTier = self._build_warm(cfg.get("warm", {}))
        self.cold: MemoryTier = self._build_cold(cfg.get("cold", {}))

    # ---------------------------------------------------------------
    # Public API — engine 이 부름
    # ---------------------------------------------------------------
    def put(self, key: str, value: Any, tier: str = "hot", ttl_sec: Optional[int] = None) -> None:
        self._tier(tier).put(key, value, ttl_sec=ttl_sec)

    def get(self, key: str, tier: str = "hot") -> Optional[Any]:
        return self._tier(tier).get(key)

    def delete(self, key: str, tier: str = "hot") -> bool:
        return self._tier(tier).delete(key)

    def stats(self) -> Dict[str, Any]:
        """대시보드용 — 3 tier 의 상태 한꺼번에"""
        return {
            "hot":  self.hot.stats(),
            "warm": self.warm.stats(),
            "cold": self.cold.stats(),
        }

    def list_keys(self, tier: str = "hot", limit: int = 100):
        return self._tier(tier).list_keys(limit=limit)

    def sweep_all(self) -> Dict[str, int]:
        """백그라운드 작업용 — 모든 tier 의 만료 청소"""
        return {
            "hot":  self.hot.sweep_expired(),
            "warm": self.warm.sweep_expired(),
            "cold": self.cold.sweep_expired(),
        }

    # ---------------------------------------------------------------
    # internals
    # ---------------------------------------------------------------
    def _tier(self, name: str) -> MemoryTier:
        if name == "hot":  return self.hot
        if name == "warm": return self.warm
        if name == "cold": return self.cold
        raise ValueError(f"Unknown tier: {name}")

    @staticmethod
    def _build_hot(cfg: Dict) -> MemoryTier:
        backend = cfg.get("backend", "in_memory")
        if backend == "in_memory":
            return InMemoryHot(
                max_size=cfg.get("max_size", 1000),
                default_ttl_sec=cfg.get("ttl_sec", 86400),
            )
        # Phase 2 분기 (placeholder)
        # if backend == "redis": return RedisHot(...)
        raise ValueError(f"Unknown hot backend: {backend}")

    @staticmethod
    def _build_warm(cfg: Dict) -> MemoryTier:
        backend = cfg.get("backend", "sqlite")
        if backend == "sqlite":
            return SqliteWarm(
                db_path=cfg.get("path", "./data/memory/warm.db"),
                default_ttl_sec=cfg.get("ttl_sec", 2592000),
            )
        # Phase 2 분기 (placeholder)
        # if backend == "postgres": return PostgresWarm(...)
        raise ValueError(f"Unknown warm backend: {backend}")

    @staticmethod
    def _build_cold(cfg: Dict) -> MemoryTier:
        backend = cfg.get("backend", "jsonl")
        if backend == "jsonl":
            return JsonlCold(base_dir=cfg.get("path", "./data/memory/cold/"))
        # Phase 2 분기 (placeholder)
        # if backend == "s3": return S3ArchiveCold(...)
        raise ValueError(f"Unknown cold backend: {backend}")
