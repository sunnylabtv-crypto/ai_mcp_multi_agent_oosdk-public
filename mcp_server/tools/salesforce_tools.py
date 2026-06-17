# mcp_server/tools/salesforce_tools.py
"""
Salesforce MCP 도구들
Claude Desktop이 호출할 수 있는 Salesforce CRM 관련 함수들
"""
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# 서비스 함수들을 import
from ..services import salesforce_service


def register_salesforce_tools(mcp):
    """Salesforce 도구들을 MCP 서버에 등록"""
    
    @mcp.tool()
    def create_salesforce_lead(
        customer_name: str,
        customer_company: str,
        customer_email: str,
        customer_title: str = "",
        customer_phone: str = ""
    ) -> Dict:
        """
        Salesforce에 새로운 Lead를 생성합니다.
        
        Args:
            customer_name: 고객 이름 (필수)
            customer_company: 회사명 (필수)
            customer_email: 이메일 (필수)
            customer_title: 직급 (선택)
            customer_phone: 전화번호 (선택)
            
        Returns:
            생성 결과 {"status": str, "lead_id": str, "lead_url": str}
            
        Example:
            create_salesforce_lead(
                customer_name="성춘향",
                customer_company="춘향서비스",
                customer_email="chunhyang@example.com",
                customer_title="과장",
                customer_phone="010-1234-5678"
            )
        """
        logger.info(f"📊 Salesforce Lead 생성 요청: {customer_name} ({customer_company})")
        
        try:
            # 고객 정보 딕셔너리 구성
            customer_info = {
                "name": customer_name,
                "company": customer_company,
                "email": customer_email,
                "title": customer_title,
                "phone": customer_phone
            }
            
            lead_id = salesforce_service.create_lead(customer_info)
            
            if lead_id:
                lead_url = salesforce_service.get_lead_url(lead_id)
                
                logger.info(f"✅ Lead 생성 성공: {lead_id}")
                
                return {
                    "status": "success",
                    "message": f"Lead가 성공적으로 생성되었습니다.",
                    "lead_id": lead_id,
                    "lead_url": lead_url,
                    "customer_name": customer_name,
                    "customer_company": customer_company
                }
            else:
                logger.error("❌ Lead 생성 실패")
                return {
                    "status": "error",
                    "message": "Lead 생성에 실패했습니다.",
                    "lead_id": None
                }
                
        except Exception as e:
            logger.error(f"❌ Lead 생성 중 오류: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"Lead 생성 중 오류 발생: {str(e)}",
                "lead_id": None
            }
    
    
    @mcp.tool()
    def verify_salesforce_lead(lead_id: str) -> Dict:
        """
        Salesforce에서 Lead 정보를 확인합니다.
        
        Args:
            lead_id: Lead ID
            
        Returns:
            Lead 정보
            
        Example:
            verify_salesforce_lead(lead_id="00Q...")
        """
        logger.info(f"🔍 Salesforce Lead 확인 요청: {lead_id}")
        
        try:
            lead_info = salesforce_service.verify_lead(lead_id)
            
            if lead_info:
                logger.info(f"✅ Lead 확인 성공: {lead_id}")
                
                return {
                    "status": "success",
                    "message": "Lead 정보를 성공적으로 조회했습니다.",
                    "lead_id": lead_id,
                    "data": {
                        "Name": f"{lead_info.get('FirstName', '')} {lead_info.get('LastName', '')}".strip(),
                        "Company": lead_info.get('Company'),
                        "Email": lead_info.get('Email'),
                        "Phone": lead_info.get('Phone'),
                        "Title": lead_info.get('Title'),
                        "Status": lead_info.get('Status'),
                        "LeadSource": lead_info.get('LeadSource')
                    }
                }
            else:
                logger.error(f"❌ Lead 확인 실패: {lead_id}")
                return {
                    "status": "error",
                    "message": "Lead 정보를 찾을 수 없습니다.",
                    "lead_id": lead_id
                }
                
        except Exception as e:
            logger.error(f"❌ Lead 확인 중 오류: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"Lead 확인 중 오류 발생: {str(e)}",
                "lead_id": lead_id
            }
    
    
    @mcp.tool()
    def get_salesforce_status() -> Dict:
        """
        Salesforce 서비스 상태를 확인합니다.
        
        Returns:
            상태 정보
            
        Example:
            get_salesforce_status()
        """
        logger.info("📊 Salesforce 상태 확인 요청")
        
        try:
            status = salesforce_service.get_service_status()
            
            if status['authenticated']:
                logger.info(f"✅ Salesforce 인증됨: {status['username']}")
            else:
                logger.warning("⚠️ Salesforce 미인증 상태")
            
            return {
                "status": "success",
                "data": status
            }
            
        except Exception as e:
            logger.error(f"❌ 상태 확인 실패: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"상태 확인 중 오류 발생: {str(e)}"
            }
    
    logger.info("✅ Salesforce 도구 등록 완료")