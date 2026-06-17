# mcp_server/ontology_engine/adapters/__init__.py
"""
Source Adapters — 외부 데이터 소스 추상화

엔터프라이즈 정당성:
- 같은 Customer 타입이라도 회사마다 소스가 다름 (SFDC / SAP / Workday / etc.)
- 마이그레이션 중 (예: SAP → SFDC) 에 어댑터 교체로 대응
- 테스트 시 fake adapter 주입
- SFDC 장애 시 local_json 으로 graceful fallback

신규 어댑터 추가 시:
1. base.SourceAdapter 상속
2. fetch_one / fetch_batch / health_check 구현
3. engine.py 의 _load_adapters() 디스패치에 추가
"""
from .base import SourceAdapter
from .salesforce import SalesforceAdapter
from .local_json import LocalJsonAdapter

__all__ = ["SourceAdapter", "SalesforceAdapter", "LocalJsonAdapter"]
