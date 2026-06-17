# mcp_server/agents/__init__.py
"""
Enterprise AI Multi-Agent 시스템 (8개 전문 Agent)
- Orchestrator: 사용자 요청을 분석하여 적절한 Agent에게 위임
- Email Agent: Gmail + AI 분석 전담
- CRM Agent: Salesforce 전담 (BC2: Opp 생성, Re-engage Task 포함)
- Calendar Agent: Google Calendar 전담 (BC2: Kickoff 미팅 포함)
- CS Agent: 고객 서비스 전담 (product_docs Collection)
- Helpdesk Agent: 내부 헬프데스크 전담 (internal_docs Collection)
- Report Agent: 로그 분석 + 시스템 모니터링 전담
- ERP Agent (BC2 신규): Closed Won → Odoo Sales Order 자동 생성
- Analytics Agent (BC2 신규): Closed Lost 사유 분석 + 누적 패턴
"""

from .base_agent import BaseAgent
from .orchestrator import Orchestrator
from .email_agent import EmailAgent
from .crm_agent import CRMAgent
from .calendar_agent import CalendarAgent
from .cs_agent import CSAgent
from .helpdesk_agent import HelpdeskAgent
from .report_agent import ReportAgent
from .erp_agent import ERPAgent
from .analytics_agent import AnalyticsAgent
from .inventory_agent import InventoryAgent

__all__ = [
    'BaseAgent',
    'Orchestrator',
    'EmailAgent',
    'CRMAgent',
    'CalendarAgent',
    'CSAgent',
    'HelpdeskAgent',
    'ReportAgent',
    'ERPAgent',
    'AnalyticsAgent',
    'InventoryAgent',
]
