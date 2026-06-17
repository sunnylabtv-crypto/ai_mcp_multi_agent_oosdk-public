# mcp_server/ontology_engine/memory/__init__.py
"""
3-Tier Memory

엔터프라이즈 정당성:
- multi-replica 배포 시 단일 dict 는 깨짐 → backend 추상화 필요
- Phase 1 = in-memory + SQLite + JSONL
- Phase 2 = Redis + Postgres + S3 (interface 같음, 구현만 교체)
"""
from .base import MemoryTier
from .hot import InMemoryHot
from .warm import SqliteWarm
from .cold import JsonlCold
from .facade import ThreeTierMemory

__all__ = ["MemoryTier", "InMemoryHot", "SqliteWarm", "JsonlCold", "ThreeTierMemory"]
