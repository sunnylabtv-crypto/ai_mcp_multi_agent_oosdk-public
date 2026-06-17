# mcp_server/tools/gmail_tools.py
"""
Gmail MCP 도구들 (멀티유저 지원)
Claude Desktop이 호출할 수 있는 Gmail 관련 함수들
"""
import logging
from typing import List, Dict

logger = logging.getLogger(__name__)

# 서비스 함수들을 import
from ..services import gmail_service
from ..services.service_manager import get_current_user


def register_gmail_tools(mcp):
    """Gmail 도구들을 MCP 서버에 등록"""
    
    @mcp.tool()
    def fetch_unread_emails(minutes_ago: int = 10, max_results: int = 10) -> List[Dict]:
        """
        최근 받은 이메일을 조회합니다 (자신이 보낸 이메일 제외).
        
        Args:
            minutes_ago: 몇 분 전부터 조회할지 (기본값: 10분)
            max_results: 최대 결과 수 (기본값: 10개)
            
        Returns:
            이메일 목록 [{"id": str, "sender": str, "subject": str, "content": str}, ...]
            
        Example:
            최근 10분간 받은 이메일 조회:
            fetch_unread_emails(minutes_ago=10, max_results=10)
        """
        current_user = get_current_user()
        logger.info(f"📧 이메일 조회 요청: 최근 {minutes_ago}분, 최대 {max_results}개 (user: {current_user})")
        
        try:
            # 멀티유저 모드: user_id 전달
            emails = gmail_service.get_recent_emails(
                minutes_ago=minutes_ago,
                max_results=max_results,
                user_id=current_user
            )
            
            if not emails:
                return {
                    "status": "success",
                    "message": "새로운 이메일이 없습니다.",
                    "user": current_user,
                    "emails": []
                }
            
            logger.info(f"✅ {len(emails)}개의 이메일 조회 완료 (user: {current_user})")
            
            return {
                "status": "success",
                "message": f"{len(emails)}개의 이메일을 찾았습니다.",
                "user": current_user,
                "count": len(emails),
                "emails": emails
            }
            
        except Exception as e:
            logger.error(f"❌ 이메일 조회 실패: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"이메일 조회 중 오류 발생: {str(e)}",
                "user": current_user,
                "emails": []
            }
    
    
    @mcp.tool()
    def send_email_reply(
        to_email: str,
        subject: str,
        body: str,
        original_email_id: str = None,
        attachment_base64: str = None,
        attachment_filename: str = None
    ) -> Dict:
        """
        이메일을 발송합니다 (Base64 첨부파일 지원).
        
        Args:
            to_email: 수신자 이메일 주소
            subject: 이메일 제목
            body: 이메일 본문
            original_email_id: 원본 이메일 ID (선택사항)
            attachment_base64: Base64로 인코딩된 첨부파일 데이터 (선택사항)
            attachment_filename: 첨부파일 이름 (선택사항, 예: "report.pptx")
            
        Returns:
            발송 결과 {"status": str, "message": str}
            
        Example:
            일반 이메일 보내기:
            send_email_reply(
                to_email="customer@example.com",
                subject="안녕하세요",
                body="문의 감사합니다."
            )
            
            첨부파일과 함께 보내기:
            send_email_reply(
                to_email="customer@example.com",
                subject="월간 보고서",
                body="첨부파일을 확인해주세요.",
                attachment_base64="UEsDBBQAAAA...",
                attachment_filename="report.pptx"
            )
        """
        current_user = get_current_user()
        log_msg = f"📤 이메일 발송 요청: {to_email} (user: {current_user})"
        if attachment_filename:
            log_msg += f", 첨부: {attachment_filename}"
        logger.info(log_msg)
        
        try:
            # Base64 첨부파일이 있으면 새 함수 사용, 없으면 기존 함수 사용
            # 멀티유저 모드: user_id 전달
            if attachment_base64 and attachment_filename:
                success = gmail_service.send_reply_with_base64_attachment(
                    to_email=to_email,
                    subject=subject,
                    content=body,
                    attachment_base64=attachment_base64,
                    attachment_filename=attachment_filename,
                    original_email_id=original_email_id,
                    user_id=current_user
                )
            else:
                success = gmail_service.send_reply(
                    to_email=to_email,
                    subject=subject,
                    content=body,
                    original_email_id=original_email_id,
                    user_id=current_user
                )
            
            if success:
                logger.info(f"✅ 이메일 발송 성공: {to_email} (user: {current_user})")
                result = {
                    "status": "success",
                    "message": f"이메일을 성공적으로 발송했습니다: {to_email}",
                    "user": current_user,
                    "recipient": to_email,
                    "subject": subject
                }
                if attachment_filename:
                    result["attachment"] = attachment_filename
                return result
            else:
                logger.error(f"❌ 이메일 발송 실패: {to_email}")
                return {
                    "status": "error",
                    "message": "이메일 발송에 실패했습니다.",
                    "user": current_user,
                    "recipient": to_email
                }
                
        except Exception as e:
            logger.error(f"❌ 이메일 발송 중 오류: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"이메일 발송 중 오류 발생: {str(e)}",
                "user": current_user,
                "recipient": to_email
            }
    
    
    @mcp.tool()
    def get_gmail_status() -> Dict:
        """
        Gmail 서비스 상태를 확인합니다.
        
        Returns:
            상태 정보 {"authenticated": bool, "user_email": str}
            
        Example:
            get_gmail_status()
        """
        current_user = get_current_user()
        logger.info(f"📊 Gmail 상태 확인 요청 (user: {current_user})")
        
        try:
            # 멀티유저 모드: 현재 사용자의 Gmail 상태 확인
            if current_user:
                status = gmail_service.get_user_service_status(current_user)
            else:
                status = gmail_service.get_service_status()
            
            if status['authenticated']:
                logger.info(f"✅ Gmail 인증됨: {status['user_email']} (user: {current_user})")
            else:
                logger.warning(f"⚠️ Gmail 미인증 상태 (user: {current_user})")
            
            return {
                "status": "success",
                "user": current_user,
                "data": status
            }
            
        except Exception as e:
            logger.error(f"❌ 상태 확인 실패: {e}", exc_info=True)
            return {
                "status": "error",
                "user": current_user,
                "message": f"상태 확인 중 오류 발생: {str(e)}"
            }
    
    logger.info("✅ Gmail 도구 등록 완료")
