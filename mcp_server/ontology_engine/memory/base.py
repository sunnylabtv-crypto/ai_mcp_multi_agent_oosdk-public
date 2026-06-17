# mcp_server/ontology_engine/memory/base.py
"""
MemoryTier 추상 인터페이스

모든 tier (hot/warm/cold) 는 이 인터페이스를 구현.
backend 가 dict/SQLite/JSONL 이든 Redis/Postgres/S3 든 동일 API.
"""
from abc import ABC, abstractmethod
from typing import Any, Optional, List, Dict


class MemoryTier(ABC):
    """단일 메모리 계층 (hot 또는 warm 또는 cold) 의 베이스"""

    @abstractmethod
    def put(self, key: str, value: Any, ttl_sec: Optional[int] = None) -> None:
        """
        값 저장.

        Args:
            key: 고유 키
            value: 저장할 값 (JSON-직렬화 가능해야 함)
            ttl_sec: 이 항목만의 TTL 오버라이드. None 이면 tier 의 default.
        """
        pass

    @abstractmethod
    def get(self, key: str) -> Optional[Any]:
        """값 조회. 없거나 만료면 None."""
        pass

    @abstractmethod
    def delete(self, key: str) -> bool:
        """값 삭제. 삭제됐으면 True."""
        pass

    @abstractmethod
    def size(self) -> int:
        """현재 저장된 항목 수."""
        pass

    @abstractmethod
    def list_keys(self, limit: int = 100) -> List[str]:
        """디버깅/대시보드용. 최근 키 일부."""
        pass

    @abstractmethod
    def sweep_expired(self) -> int:
        """
        TTL 지난 항목 청소 (백그라운드 호출용).

        Returns:
            삭제한 항목 수.
        """
        pass

    def stats(self) -> Dict[str, Any]:
        """
        대시보드 표시용 기본 통계.
        서브클래스가 오버라이드해서 추가 정보 넣을 수 있음.
        """
        return {
            "size": self.size(),
            "backend": self.__class__.__name__,
        }
