# mcp_server/services/__init__.py
"""
서비스 패키지

eager import 를 제거 — 각 서비스가 자기 외부 의존성 (google, openai,
simple-salesforce 등) 을 가지고 있어서, 한 의존성 미설치로 전체 패키지가 죽지
않게 한다. 호출 측은 그대로 `from ..services import odoo_service` 식으로 사용
가능 (Python 이 submodule 을 직접 찾아간다).
"""

__all__ = [
    'gmail_service',
    'openai_service',
    'salesforce_service',
    'vectordb_service',
    'calendar_service',
    'odoo_service',
    'service_manager',
]
