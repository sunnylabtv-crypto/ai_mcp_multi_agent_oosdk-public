# mcp_server/ontology_engine/adapters/base.py
"""
SourceAdapter 추상 인터페이스

모든 데이터 소스는 이 인터페이스를 구현해야 함.
이로써 OntologyEngine 은 소스 종류와 무관하게 동작.
"""
from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any


class SourceAdapter(ABC):
    """모든 데이터 소스 어댑터의 베이스 클래스"""

    def __init__(self, source_config: Dict[str, Any], connections: Optional[Dict] = None):
        """
        Args:
            source_config: yaml 의 object_types.<Type>.source 블록
            connections:   yaml 의 connections 블록 (어댑터가 connection_ref 로 lookup)
        """
        self.config = source_config
        self.connections = connections or {}
        # 마지막 조회의 에러 사유 (Engine trace 에서 surface 하기 위함)
        # 정상 = None, 인증 실패/HTTP 에러/no_match 등 케이스마다 문자열 코드 세팅
        self.last_error: Optional[str] = None

    @abstractmethod
    def fetch_one(self, lookup_value: str) -> Optional[Dict[str, Any]]:
        """
        단건 조회 — 이메일 1통 처리 시 사용.

        Args:
            lookup_value: lookup.by 필드의 값 (예: email_domain 값)

        Returns:
            dict (인스턴스 데이터) 또는 None (없을 때)
        """
        pass

    @abstractmethod
    def fetch_batch(self, lookup_values: List[str]) -> List[Optional[Dict[str, Any]]]:
        """
        일괄 조회 — 100통 배치 처리 시 사용 (rate limit 회피).

        Returns:
            len(lookup_values) 와 같은 길이의 리스트.
            각 원소: dict (찾았을 때) 또는 None (없을 때).
        """
        pass

    @abstractmethod
    def health_check(self) -> bool:
        """
        연결 상태 확인 — circuit breaker 용.

        Returns:
            True = 정상, False = 장애
        """
        pass

    def _apply_field_map(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """
        외부 시스템 필드명 → 온톨로지 필드명 매핑.

        예: SFDC 의 'Customer_Tier__c' → 온톨로지의 'tier'
        """
        field_map = self.config.get("field_map", {})
        if not field_map:
            return raw
        return {
            ontology_field: raw.get(external_field)
            for ontology_field, external_field in field_map.items()
        }
