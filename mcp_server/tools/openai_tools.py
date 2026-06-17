# mcp_server/tools/openai_tools.py
"""
OpenAI MCP 도구들
Claude Desktop이 호출할 수 있는 AI 분석 관련 함수들
"""
import logging
from typing import Dict

logger = logging.getLogger(__name__)

# 서비스 함수들을 import
from ..services import openai_service


def register_openai_tools(mcp):
    """OpenAI 도구들을 MCP 서버에 등록"""
    
    @mcp.tool()
    def analyze_email_with_ai(
        email_content: str,
        sender_email: str
    ) -> Dict:
        """
        AI를 사용하여 이메일에서 고객 정보를 추출합니다.
        
        Args:
            email_content: 이메일 본문
            sender_email: 발신자 이메일 주소
            
        Returns:
            추출된 고객 정보
            {
                "has_all_info": bool,
                "name": str,
                "company": str,
                "title": str,
                "phone": str,
                "email": str,
                "missing_fields": list
            }
            
        Example:
            analyze_email_with_ai(
                email_content="안녕하세요. 저는 춘향서비스의 성춘향 과장입니다...",
                sender_email="chunhyang@example.com"
            )
        """
        logger.info(f"🤖 AI 이메일 분석 요청: {sender_email}")
        
        try:
            customer_info = openai_service.extract_customer_info(
                email_content=email_content,
                sender_email=sender_email
            )
            
            if customer_info['has_all_info']:
                logger.info(f"✅ 고객 정보 완전 추출: {customer_info['name']} ({customer_info['company']})")
            else:
                logger.warning(f"⚠️ 일부 정보 누락: {customer_info['missing_fields']}")
            
            return {
                "status": "success",
                "data": customer_info
            }
            
        except Exception as e:
            logger.error(f"❌ AI 분석 실패: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"AI 분석 중 오류 발생: {str(e)}",
                "data": {
                    "has_all_info": False,
                    "email": sender_email,
                    "missing_fields": ["name", "company", "title", "phone"]
                }
            }
    
    
    @mcp.tool()
    def generate_email_reply(
        customer_name: str = None,
        customer_company: str = None,
        customer_title: str = None,
        customer_phone: str = None,
        customer_email: str = None,
        original_subject: str = "고객 문의",
        has_all_info: bool = False,
        missing_fields: list = None
    ) -> Dict:
        """
        고객 정보를 바탕으로 AI가 답변 이메일을 생성합니다.
        
        Args:
            customer_name: 고객 이름
            customer_company: 회사명
            customer_title: 직급
            customer_phone: 전화번호
            customer_email: 이메일
            original_subject: 원본 이메일 제목
            has_all_info: 모든 정보가 있는지 여부
            missing_fields: 누락된 필드 목록
            
        Returns:
            생성된 답변 {"subject": str, "body": str}
            
        Example:
            generate_email_reply(
                customer_name="성춘향",
                customer_company="춘향서비스",
                customer_title="과장",
                customer_phone="010-1234-5678",
                customer_email="chunhyang@example.com",
                original_subject="제품 문의",
                has_all_info=True
            )
        """
        logger.info(f"✍️ AI 답변 생성 요청: {customer_name or customer_email}")
        
        try:
            # 고객 정보 딕셔너리 구성
            customer_info = {
                "name": customer_name,
                "company": customer_company,
                "title": customer_title,
                "phone": customer_phone,
                "email": customer_email,
                "has_all_info": has_all_info,
                "missing_fields": missing_fields or []
            }
            
            reply = openai_service.generate_reply(
                customer_info=customer_info,
                original_subject=original_subject
            )
            
            logger.info(f"✅ 답변 생성 완료: {reply['subject']}")
            
            return {
                "status": "success",
                "data": reply
            }
            
        except Exception as e:
            logger.error(f"❌ 답변 생성 실패: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"답변 생성 중 오류 발생: {str(e)}",
                "data": {
                    "subject": f"Re: {original_subject}",
                    "body": "문의 주셔서 감사합니다. 빠른 시일 내에 답변 드리겠습니다."
                }
            }
    
    
    @mcp.tool()
    def get_openai_status() -> Dict:
        """
        OpenAI 서비스 상태를 확인합니다.
        
        Returns:
            상태 정보
            
        Example:
            get_openai_status()
        """
        logger.info("📊 OpenAI 상태 확인 요청")
        
        try:
            status = openai_service.get_service_status()
            
            if status['initialized'] and status['api_key_configured']:
                logger.info("✅ OpenAI 정상 작동 중")
            else:
                logger.warning("⚠️ OpenAI 미설정 상태")
            
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
    
    logger.info("✅ OpenAI 도구 등록 완료")