# mcp_server/services/gmail_service.py
"""
Gmail API 서비스 (멀티유저 지원)
"""
import os
import base64
import logging
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from typing import List, Dict, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

# ============================================================
# 단일 사용자 모드 (기존 호환)
# ============================================================

_gmail_service = None
_user_email = None


def authenticate_gmail(config: dict) -> bool:
    """Gmail API 인증 (단일 사용자 모드)"""
    global _gmail_service, _user_email
    
    logger.info("Gmail 서비스 인증 시작...")
    
    try:
        scopes = config['SCOPES']
        token_path = config['TOKEN_FILE']
        credentials_path = config['CREDENTIALS_FILE']
        
        creds = None
        
        if os.path.exists(token_path):
            creds = Credentials.from_authorized_user_file(token_path, scopes)
        
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    logger.info("토큰 새로고침 성공")
                except Exception as e:
                    logger.warning(f"토큰 새로고침 실패: {e}")
                    creds = None
            
            if not creds:
                try:
                    flow = InstalledAppFlow.from_client_secrets_file(credentials_path, scopes)
                    creds = flow.run_local_server(port=0)
                except Exception as e:
                    logger.error(f"새 인증 흐름 실패: {e}", exc_info=True)
                    return False
            
            try:
                with open(token_path, 'w') as token:
                    token.write(creds.to_json())
            except OSError:
                logger.info("토큰 저장 스킵 (읽기 전용 환경)")

        _gmail_service = build('gmail', 'v1', credentials=creds)
        
        profile = _gmail_service.users().getProfile(userId='me').execute()
        _user_email = profile['emailAddress']
        
        logger.info(f"✅ Gmail 인증 성공! 계정: {_user_email}")
        return True
        
    except Exception as e:
        logger.error(f"Gmail 인증 실패: {e}", exc_info=True)
        return False


def get_service_status() -> Dict:
    """Gmail 서비스 상태 확인 (단일 사용자)"""
    global _gmail_service, _user_email
    return {
        'authenticated': _gmail_service is not None,
        'user_email': _user_email,
        'service_available': _gmail_service is not None
    }


# ============================================================
# 멀티유저 모드
# ============================================================

_user_gmail_services: Dict[str, dict] = {}


def authenticate_gmail_for_user(user_id: str, config: dict) -> bool:
    """사용자별 Gmail 인증"""
    global _user_gmail_services
    
    logger.info(f"Gmail 서비스 인증 시작 (사용자: {user_id})...")
    
    try:
        scopes = config['SCOPES']
        token_path = config['TOKEN_FILE']
        credentials_path = config['CREDENTIALS_FILE']
        
        logger.info(f"  Token: {token_path}")
        logger.info(f"  Credentials: {credentials_path}")
        
        if not os.path.exists(token_path):
            logger.error(f"❌ Token 파일 없음: {token_path}")
            return False
        
        if not os.path.exists(credentials_path):
            logger.error(f"❌ Credentials 파일 없음: {credentials_path}")
            return False
        
        creds = Credentials.from_authorized_user_file(token_path, scopes)
        
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    logger.info(f"토큰 새로고침 성공 (사용자: {user_id})")
                    
                    try:
                        with open(token_path, 'w') as token:
                            token.write(creds.to_json())
                    except OSError:
                        pass
                except Exception as e:
                    logger.error(f"토큰 새로고침 실패: {e}")
                    return False
            else:
                logger.error(f"❌ 유효하지 않은 토큰 (사용자: {user_id})")
                return False
        
        service = build('gmail', 'v1', credentials=creds)
        profile = service.users().getProfile(userId='me').execute()
        user_email = profile['emailAddress']
        
        _user_gmail_services[user_id] = {
            'service': service,
            'email': user_email,
            'credentials': creds
        }
        
        logger.info(f"✅ Gmail 인증 성공! 사용자: {user_id}, 계정: {user_email}")
        return True
        
    except Exception as e:
        logger.error(f"Gmail 인증 실패 (사용자: {user_id}): {e}", exc_info=True)
        return False


def get_user_service_status(user_id: str) -> Dict:
    """사용자별 Gmail 상태 확인"""
    if user_id in _user_gmail_services:
        user_data = _user_gmail_services[user_id]
        return {
            'authenticated': True,
            'user_email': user_data['email'],
            'service_available': True
        }
    return {
        'authenticated': False,
        'user_email': None,
        'service_available': False
    }


def _get_gmail_service(user_id: str = None):
    """현재 컨텍스트의 Gmail 서비스 반환"""
    if user_id and user_id in _user_gmail_services:
        return _user_gmail_services[user_id]['service'], _user_gmail_services[user_id]['email']
    return _gmail_service, _user_email


# ============================================================
# Gmail 기능 함수들
# ============================================================

def get_recent_emails(minutes_ago: int = 10, max_results: int = 10, user_id: str = None) -> List[Dict]:
    """
    최근 이메일 조회 — 시간 필터는 클라이언트에서 수행.

    Gmail API 의 `q="after:<ts>"` 검색 필터는 자체 검색 인덱스를 사용하기 때문에
    새 메일이 도착해도 30초~2분 정도 인덱싱 지연이 발생합니다.
    그동안에는 메일이 받은편지함에 있어도 검색에 안 잡힙니다 (실제 데모에서 0건 반환됨).

    이를 우회하기 위해:
      1. q 필터에서 `after:` 제거 — `in:inbox -from:me` 만 적용
      2. messages.list 로 최신 N (max_results 의 3배까지) 가져옴
      3. 각 메일의 internalDate 헤더로 클라이언트에서 시간 필터링
      4. 결과 중 max_results 개만 반환

    이렇게 하면 인덱스 lag 영향 없이 새 메일이 즉시 잡힙니다.
    """
    service, user_email = _get_gmail_service(user_id)

    if not service:
        logger.error("❌ Gmail 서비스가 초기화되지 않았습니다.")
        return []

    try:
        cutoff_ms = int((datetime.now() - timedelta(minutes=minutes_ago)).timestamp() * 1000)

        # q 에서 after: 제거 — 인덱스 lag 우회
        # 시간 필터는 아래에서 internalDate 로 수행
        query = '-from:me in:inbox'

        # max_results 의 3배 (최대 50) 까지 가져와서 시간 필터 후 잘라냄
        list_limit = min(max(max_results * 3, 10), 50)
        results = service.users().messages().list(
            userId='me',
            q=query,
            maxResults=list_limit,
        ).execute()

        messages = results.get('messages', [])
        emails = []
        skipped_old = 0

        for msg in messages:
            if len(emails) >= max_results:
                break
            try:
                email_data = service.users().messages().get(
                    userId='me',
                    id=msg['id'],
                    format='full',
                ).execute()

                # internalDate 는 ms epoch 문자열 — 시간 필터 (인덱스 lag 우회 핵심)
                internal_date_ms = int(email_data.get('internalDate', '0') or 0)
                if internal_date_ms and internal_date_ms < cutoff_ms:
                    skipped_old += 1
                    continue

                headers = email_data['payload']['headers']
                sender = next((h['value'] for h in headers if h['name'].lower() == 'from'), '')
                subject = next((h['value'] for h in headers if h['name'].lower() == 'subject'), '')

                if user_email and user_email.lower() in sender.lower():
                    continue

                content = ""
                payload = email_data['payload']

                if 'parts' in payload:
                    for part in payload['parts']:
                        if part['mimeType'] == 'text/plain' and 'data' in part['body']:
                            content = base64.urlsafe_b64decode(
                                part['body']['data']
                            ).decode('utf-8', errors='ignore')
                            break
                elif 'body' in payload and 'data' in payload['body']:
                    content = base64.urlsafe_b64decode(
                        payload['body']['data']
                    ).decode('utf-8', errors='ignore')

                emails.append({
                    'id': msg['id'],
                    'sender': sender,
                    'subject': subject,
                    'content': content.strip(),
                })

            except Exception as e:
                logger.warning(f"개별 이메일 파싱 실패: {e}")
                continue

        if emails:
            logger.info(f"✅ {len(emails)}개의 새 이메일 발견 (cutoff={minutes_ago}분, scanned={len(messages)}, skipped_old={skipped_old})")
        else:
            logger.info(f"📭 새 이메일 없음 (cutoff={minutes_ago}분, scanned={len(messages)}, skipped_old={skipped_old})")

        return emails

    except Exception as e:
        logger.error(f"이메일 조회 실패: {e}", exc_info=True)
        return []


def send_reply(to_email: str, subject: str, content: str, 
               original_email_id: Optional[str] = None,
               attachment_path: Optional[str] = None,
               user_id: str = None) -> bool:
    """이메일 발송"""
    service, user_email = _get_gmail_service(user_id)
    
    if not service:
        logger.error("❌ Gmail 서비스가 초기화되지 않았습니다.")
        return False
    
    try:
        message = MIMEMultipart()
        message['to'] = to_email
        message['from'] = user_email
        message['subject'] = subject
        
        message.attach(MIMEText(content, 'plain', 'utf-8'))
        
        if attachment_path and os.path.exists(attachment_path):
            filename = os.path.basename(attachment_path)
            with open(attachment_path, 'rb') as f:
                part = MIMEBase('application', 'octet-stream')
                part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header('Content-Disposition', f'attachment; filename="{filename}"')
                message.attach(part)
        
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')
        
        service.users().messages().send(
            userId='me', 
            body={'raw': raw_message}
        ).execute()
        
        logger.info(f"✅ 이메일 발송 성공: {to_email}")
        return True
        
    except Exception as e:
        logger.error(f"이메일 발송 실패: {e}", exc_info=True)
        return False


def send_reply_with_base64_attachment(
    to_email: str, 
    subject: str, 
    content: str,
    attachment_base64: Optional[str] = None,
    attachment_filename: Optional[str] = None,
    original_email_id: Optional[str] = None,
    user_id: str = None
) -> bool:
    """이메일 발송 (Base64 첨부파일)"""
    service, user_email = _get_gmail_service(user_id)
    
    if not service:
        logger.error("❌ Gmail 서비스가 초기화되지 않았습니다.")
        return False
    
    try:
        message = MIMEMultipart()
        message['to'] = to_email
        message['from'] = user_email
        message['subject'] = subject
        
        message.attach(MIMEText(content, 'plain', 'utf-8'))
        
        if attachment_base64 and attachment_filename:
            try:
                file_data = base64.b64decode(attachment_base64)
                part = MIMEBase('application', 'octet-stream')
                part.set_payload(file_data)
                encoders.encode_base64(part)
                part.add_header('Content-Disposition', f'attachment; filename="{attachment_filename}"')
                message.attach(part)
            except Exception as e:
                logger.error(f"❌ 첨부파일 처리 실패: {e}")
                return False
        
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')
        
        service.users().messages().send(
            userId='me', 
            body={'raw': raw_message}
        ).execute()
        
        logger.info(f"✅ 이메일 발송 성공: {to_email}")
        return True
        
    except Exception as e:
        logger.error(f"이메일 발송 실패: {e}", exc_info=True)
        return False
