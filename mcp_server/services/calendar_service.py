# mcp_server/services/calendar_service.py
"""
Google Calendar API 서비스 (멀티유저 지원)
"""
import os
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

# ============================================================
# 단일 사용자 모드 (기존 호환)
# ============================================================

_calendar_service = None
_user_email = None


def authenticate_calendar(config: dict) -> bool:
    """Calendar API 인증 (단일 사용자)"""
    global _calendar_service, _user_email
    
    logger.info("Google Calendar 서비스 인증 시작...")
    
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
                    logger.info("Calendar 토큰 새로고침 성공")
                except Exception as e:
                    logger.warning(f"Calendar 토큰 새로고침 실패: {e}")
                    creds = None
            
            if not creds:
                logger.warning("Calendar 인증 필요 - Gmail 토큰에 Calendar scope가 없습니다.")
                return False
            
            try:
                with open(token_path, 'w') as token:
                    token.write(creds.to_json())
            except OSError:
                pass

        _calendar_service = build('calendar', 'v3', credentials=creds)
        
        try:
            calendar_list = _calendar_service.calendarList().get(calendarId='primary').execute()
            _user_email = calendar_list.get('id', 'unknown')
        except Exception:
            _user_email = 'unknown'
        
        logger.info(f"✅ Google Calendar 인증 성공! 계정: {_user_email}")
        return True
        
    except Exception as e:
        logger.error(f"Google Calendar 인증 실패: {e}", exc_info=True)
        return False


def get_service_status() -> Dict:
    """Calendar 서비스 상태 (단일 사용자)"""
    global _calendar_service, _user_email
    return {
        'authenticated': _calendar_service is not None,
        'user_email': _user_email,
        'service_available': _calendar_service is not None
    }


# ============================================================
# 멀티유저 모드
# ============================================================

_user_calendar_services: Dict[str, dict] = {}


def authenticate_calendar_for_user(user_id: str, config: dict) -> bool:
    """사용자별 Calendar 인증"""
    global _user_calendar_services
    
    logger.info(f"Calendar 서비스 인증 시작 (사용자: {user_id})...")
    
    try:
        scopes = config['SCOPES']
        token_path = config['TOKEN_FILE']
        
        if not os.path.exists(token_path):
            logger.error(f"❌ Token 파일 없음: {token_path}")
            return False
        
        creds = Credentials.from_authorized_user_file(token_path, scopes)
        
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    logger.info(f"Calendar 토큰 새로고침 성공 (사용자: {user_id})")
                except Exception as e:
                    logger.error(f"Calendar 토큰 새로고침 실패: {e}")
                    return False
            else:
                logger.error(f"❌ 유효하지 않은 Calendar 토큰 (사용자: {user_id})")
                return False
        
        service = build('calendar', 'v3', credentials=creds)
        
        try:
            calendar_list = service.calendarList().get(calendarId='primary').execute()
            user_email = calendar_list.get('id', 'unknown')
        except Exception:
            user_email = 'unknown'
        
        _user_calendar_services[user_id] = {
            'service': service,
            'email': user_email
        }
        
        logger.info(f"✅ Calendar 인증 성공! 사용자: {user_id}, 계정: {user_email}")
        return True
        
    except Exception as e:
        logger.error(f"Calendar 인증 실패 (사용자: {user_id}): {e}", exc_info=True)
        return False


def get_user_service_status(user_id: str) -> Dict:
    """사용자별 Calendar 상태"""
    if user_id in _user_calendar_services:
        user_data = _user_calendar_services[user_id]
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


def _get_calendar_service(user_id: str = None):
    """현재 컨텍스트의 Calendar 서비스 반환"""
    if user_id and user_id in _user_calendar_services:
        return _user_calendar_services[user_id]['service']
    return _calendar_service


# ============================================================
# Calendar 기능 함수들
# ============================================================

def create_event(
    title: str,
    start_datetime: str,
    end_datetime: str,
    description: str = "",
    location: str = "",
    timezone: str = "Asia/Seoul",
    user_id: str = None
) -> Optional[Dict]:
    """캘린더에 새 일정 생성"""
    service = _get_calendar_service(user_id)
    
    if not service:
        logger.error("❌ Calendar 서비스가 초기화되지 않았습니다.")
        return None
    
    try:
        try:
            start_dt = datetime.strptime(start_datetime, '%Y-%m-%d %H:%M')
        except ValueError:
            try:
                start_dt = datetime.fromisoformat(start_datetime.replace('Z', '+00:00'))
            except:
                start_dt = datetime.strptime(start_datetime, '%Y-%m-%dT%H:%M:%S')
        
        try:
            end_dt = datetime.strptime(end_datetime, '%Y-%m-%d %H:%M')
        except ValueError:
            try:
                end_dt = datetime.fromisoformat(end_datetime.replace('Z', '+00:00'))
            except:
                end_dt = datetime.strptime(end_datetime, '%Y-%m-%dT%H:%M:%S')
        
        event = {
            'summary': title,
            'description': description,
            'location': location,
            'start': {'dateTime': start_dt.isoformat(), 'timeZone': timezone},
            'end': {'dateTime': end_dt.isoformat(), 'timeZone': timezone},
        }
        
        result = service.events().insert(calendarId='primary', body=event).execute()
        
        logger.info(f"✅ 일정 생성 성공: {title}")
        
        return {
            'id': result.get('id'),
            'title': result.get('summary'),
            'start': result.get('start', {}).get('dateTime'),
            'end': result.get('end', {}).get('dateTime'),
            'html_link': result.get('htmlLink'),
            'status': 'created'
        }
        
    except Exception as e:
        logger.error(f"❌ 일정 생성 실패: {e}", exc_info=True)
        return None


def get_events(
    days: int = 7,
    max_results: int = 10,
    time_min: str = None,
    user_id: str = None
) -> List[Dict]:
    """일정 목록 조회"""
    service = _get_calendar_service(user_id)
    
    if not service:
        logger.error("❌ Calendar 서비스가 초기화되지 않았습니다.")
        return []
    
    try:
        if time_min:
            try:
                start_time = datetime.fromisoformat(time_min.replace('Z', '+00:00'))
            except:
                start_time = datetime.now()
        else:
            start_time = datetime.now()
        
        end_time = start_time + timedelta(days=days)
        
        time_min_str = start_time.strftime('%Y-%m-%dT%H:%M:%S') + 'Z'
        time_max_str = end_time.strftime('%Y-%m-%dT%H:%M:%S') + 'Z'
        
        events_result = service.events().list(
            calendarId='primary',
            timeMin=time_min_str,
            timeMax=time_max_str,
            maxResults=max_results,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        
        events = events_result.get('items', [])
        
        result = []
        for event in events:
            start = event.get('start', {})
            end = event.get('end', {})
            
            result.append({
                'id': event.get('id'),
                'title': event.get('summary', '(제목 없음)'),
                'start': start.get('dateTime', start.get('date')),
                'end': end.get('dateTime', end.get('date')),
                'description': event.get('description', ''),
                'location': event.get('location', ''),
                'html_link': event.get('htmlLink'),
                'status': event.get('status')
            })
        
        logger.info(f"✅ {len(result)}개의 일정 조회 완료")
        return result
        
    except Exception as e:
        logger.error(f"❌ 일정 조회 실패: {e}", exc_info=True)
        return []


def update_event(
    event_id: str,
    title: str = None,
    start_datetime: str = None,
    end_datetime: str = None,
    description: str = None,
    location: str = None,
    timezone: str = "Asia/Seoul",
    user_id: str = None
) -> Optional[Dict]:
    """기존 일정 수정"""
    service = _get_calendar_service(user_id)
    
    if not service:
        logger.error("❌ Calendar 서비스가 초기화되지 않았습니다.")
        return None
    
    try:
        event = service.events().get(calendarId='primary', eventId=event_id).execute()
        
        if title is not None:
            event['summary'] = title
        if description is not None:
            event['description'] = description
        if location is not None:
            event['location'] = location
        if start_datetime is not None:
            try:
                start_dt = datetime.strptime(start_datetime, '%Y-%m-%d %H:%M')
            except ValueError:
                start_dt = datetime.fromisoformat(start_datetime.replace('Z', '+00:00'))
            event['start'] = {'dateTime': start_dt.isoformat(), 'timeZone': timezone}
        if end_datetime is not None:
            try:
                end_dt = datetime.strptime(end_datetime, '%Y-%m-%d %H:%M')
            except ValueError:
                end_dt = datetime.fromisoformat(end_datetime.replace('Z', '+00:00'))
            event['end'] = {'dateTime': end_dt.isoformat(), 'timeZone': timezone}
        
        updated_event = service.events().update(
            calendarId='primary',
            eventId=event_id,
            body=event
        ).execute()
        
        logger.info(f"✅ 일정 수정 성공: {updated_event.get('summary')}")
        
        return {
            'id': updated_event.get('id'),
            'title': updated_event.get('summary'),
            'start': updated_event.get('start', {}).get('dateTime'),
            'end': updated_event.get('end', {}).get('dateTime'),
            'html_link': updated_event.get('htmlLink'),
            'status': 'updated'
        }
        
    except Exception as e:
        logger.error(f"❌ 일정 수정 실패: {e}", exc_info=True)
        return None


def delete_event(event_id: str, user_id: str = None) -> bool:
    """일정 삭제"""
    service = _get_calendar_service(user_id)
    
    if not service:
        logger.error("❌ Calendar 서비스가 초기화되지 않았습니다.")
        return False
    
    try:
        service.events().delete(calendarId='primary', eventId=event_id).execute()
        logger.info(f"✅ 일정 삭제 성공: {event_id}")
        return True
    except Exception as e:
        logger.error(f"❌ 일정 삭제 실패: {e}", exc_info=True)
        return False


def search_events(query: str, days: int = 30, max_results: int = 10, user_id: str = None) -> List[Dict]:
    """일정 검색"""
    service = _get_calendar_service(user_id)
    
    if not service:
        logger.error("❌ Calendar 서비스가 초기화되지 않았습니다.")
        return []
    
    try:
        now = datetime.now()
        end_time = now + timedelta(days=days)
        
        time_min_str = now.strftime('%Y-%m-%dT%H:%M:%S') + 'Z'
        time_max_str = end_time.strftime('%Y-%m-%dT%H:%M:%S') + 'Z'
        
        events_result = service.events().list(
            calendarId='primary',
            timeMin=time_min_str,
            timeMax=time_max_str,
            maxResults=max_results,
            singleEvents=True,
            orderBy='startTime',
            q=query
        ).execute()
        
        events = events_result.get('items', [])
        
        result = []
        for event in events:
            start = event.get('start', {})
            end = event.get('end', {})
            
            result.append({
                'id': event.get('id'),
                'title': event.get('summary', '(제목 없음)'),
                'start': start.get('dateTime', start.get('date')),
                'end': end.get('dateTime', end.get('date')),
                'description': event.get('description', ''),
                'location': event.get('location', ''),
                'html_link': event.get('htmlLink')
            })
        
        logger.info(f"✅ '{query}' 검색 결과: {len(result)}개")
        return result
        
    except Exception as e:
        logger.error(f"❌ 일정 검색 실패: {e}", exc_info=True)
        return []
