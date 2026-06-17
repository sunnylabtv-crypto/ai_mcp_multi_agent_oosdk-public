# mcp_server/tools/calendar_tools.py
"""
Google Calendar MCP 도구들 (멀티유저 지원)
Claude Desktop이 호출할 수 있는 Calendar 관련 함수들

일정비서 기능:
- 자연어로 일정 추가 ("오늘 2시 회의", "내일 3시 치과예약")
- 일정 조회, 수정, 삭제
"""
import json
import logging
from datetime import datetime
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

# 서비스 함수들을 import
from ..services import calendar_service
from ..services.service_manager import get_current_user


def register_calendar_tools(mcp):
    """Calendar 도구들을 MCP 서버에 등록"""
    
    @mcp.tool()
    def add_calendar_event(
        title: str,
        start_datetime: str,
        end_datetime: str = None,
        description: str = "",
        location: str = ""
    ) -> Dict:
        """
        Google Calendar에 새 일정을 추가합니다.
        
        Args:
            title: 일정 제목 (예: "팀 회의", "치과 예약")
            start_datetime: 시작 시간 (형식: "YYYY-MM-DD HH:MM", 예: "2025-01-15 14:00")
            end_datetime: 종료 시간 (형식: "YYYY-MM-DD HH:MM", 기본값: 시작시간 + 1시간)
            description: 일정 설명 (선택)
            location: 장소 (선택)
            
        Returns:
            생성된 일정 정보
            {
                "status": str,
                "event": {
                    "id": str,
                    "title": str,
                    "start": str,
                    "end": str,
                    "html_link": str
                }
            }
            
        Example:
            add_calendar_event(
                title="팀 회의",
                start_datetime="2025-01-15 14:00",
                end_datetime="2025-01-15 15:00",
                description="주간 업무 회의",
                location="회의실 A"
            )
        """
        current_user = get_current_user()
        logger.info(f"📅 일정 추가 요청: {title} @ {start_datetime} (user: {current_user})")
        
        try:
            # 종료 시간이 없으면 시작 시간 + 1시간
            if not end_datetime:
                try:
                    start_dt = datetime.strptime(start_datetime, '%Y-%m-%d %H:%M')
                    from datetime import timedelta
                    end_dt = start_dt + timedelta(hours=1)
                    end_datetime = end_dt.strftime('%Y-%m-%d %H:%M')
                except:
                    end_datetime = start_datetime  # 파싱 실패시 같은 시간
            
            # 멀티유저 모드: user_id 전달
            result = calendar_service.create_event(
                title=title,
                start_datetime=start_datetime,
                end_datetime=end_datetime,
                description=description,
                location=location,
                user_id=current_user
            )
            
            if result:
                logger.info(f"✅ 일정 추가 성공: {title} (user: {current_user})")
                return {
                    "status": "success",
                    "message": f"일정 '{title}'이(가) 추가되었습니다.",
                    "user": current_user,
                    "event": result
                }
            else:
                logger.error(f"❌ 일정 추가 실패: {title}")
                return {
                    "status": "error",
                    "message": "일정 추가에 실패했습니다. Calendar 서비스 상태를 확인해주세요.",
                    "user": current_user
                }
                
        except Exception as e:
            logger.error(f"❌ 일정 추가 중 오류: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"일정 추가 중 오류 발생: {str(e)}",
                "user": current_user
            }
    
    
    @mcp.tool()
    def get_calendar_events(days: int = 7, max_results: int = 10) -> Dict:
        """
        Google Calendar에서 예정된 일정을 조회합니다.
        
        Args:
            days: 조회할 기간 (일 단위, 기본값: 7일)
            max_results: 최대 결과 수 (기본값: 10개)
            
        Returns:
            일정 목록
            {
                "status": str,
                "count": int,
                "events": [
                    {
                        "id": str,
                        "title": str,
                        "start": str,
                        "end": str,
                        "location": str,
                        "html_link": str
                    }
                ]
            }
            
        Example:
            get_calendar_events(days=7, max_results=10)
        """
        current_user = get_current_user()
        logger.info(f"📅 일정 조회 요청: 향후 {days}일, 최대 {max_results}개 (user: {current_user})")
        
        try:
            # 멀티유저 모드: user_id 전달
            events = calendar_service.get_events(
                days=days, 
                max_results=max_results,
                user_id=current_user
            )
            
            if events:
                logger.info(f"✅ {len(events)}개의 일정 조회 완료 (user: {current_user})")
                return {
                    "status": "success",
                    "message": f"{len(events)}개의 일정을 찾았습니다.",
                    "user": current_user,
                    "count": len(events),
                    "events": events
                }
            else:
                return {
                    "status": "success",
                    "message": "예정된 일정이 없습니다.",
                    "user": current_user,
                    "count": 0,
                    "events": []
                }
                
        except Exception as e:
            logger.error(f"❌ 일정 조회 중 오류: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"일정 조회 중 오류 발생: {str(e)}",
                "user": current_user,
                "events": []
            }
    
    
    @mcp.tool()
    def update_calendar_event(
        event_id: str,
        title: str = None,
        start_datetime: str = None,
        end_datetime: str = None,
        description: str = None,
        location: str = None
    ) -> Dict:
        """
        기존 일정을 수정합니다.
        
        Args:
            event_id: 수정할 이벤트 ID (get_calendar_events로 조회 가능)
            title: 새 제목 (변경하지 않으려면 None)
            start_datetime: 새 시작 시간 (형식: "YYYY-MM-DD HH:MM")
            end_datetime: 새 종료 시간 (형식: "YYYY-MM-DD HH:MM")
            description: 새 설명
            location: 새 장소
            
        Returns:
            수정된 일정 정보
            
        Example:
            update_calendar_event(
                event_id="abc123xyz",
                title="팀 회의 (변경)",
                start_datetime="2025-01-15 15:00"
            )
        """
        current_user = get_current_user()
        logger.info(f"📅 일정 수정 요청: {event_id} (user: {current_user})")
        
        try:
            # 멀티유저 모드: user_id 전달
            result = calendar_service.update_event(
                event_id=event_id,
                title=title,
                start_datetime=start_datetime,
                end_datetime=end_datetime,
                description=description,
                location=location,
                user_id=current_user
            )
            
            if result:
                logger.info(f"✅ 일정 수정 성공: {result.get('title')} (user: {current_user})")
                return {
                    "status": "success",
                    "message": f"일정이 수정되었습니다.",
                    "user": current_user,
                    "event": result
                }
            else:
                return {
                    "status": "error",
                    "message": "일정 수정에 실패했습니다. 이벤트 ID를 확인해주세요.",
                    "user": current_user
                }
                
        except Exception as e:
            logger.error(f"❌ 일정 수정 중 오류: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"일정 수정 중 오류 발생: {str(e)}",
                "user": current_user
            }
    
    
    @mcp.tool()
    def delete_calendar_event(event_id: str) -> Dict:
        """
        일정을 삭제(취소)합니다.
        
        Args:
            event_id: 삭제할 이벤트 ID (get_calendar_events로 조회 가능)
            
        Returns:
            삭제 결과
            
        Example:
            delete_calendar_event(event_id="abc123xyz")
        """
        current_user = get_current_user()
        logger.info(f"📅 일정 삭제 요청: {event_id} (user: {current_user})")
        
        try:
            # 멀티유저 모드: user_id 전달
            success = calendar_service.delete_event(
                event_id=event_id,
                user_id=current_user
            )
            
            if success:
                logger.info(f"✅ 일정 삭제 성공: {event_id} (user: {current_user})")
                return {
                    "status": "success",
                    "message": "일정이 삭제되었습니다.",
                    "user": current_user,
                    "deleted_event_id": event_id
                }
            else:
                return {
                    "status": "error",
                    "message": "일정 삭제에 실패했습니다. 이벤트 ID를 확인해주세요.",
                    "user": current_user
                }
                
        except Exception as e:
            logger.error(f"❌ 일정 삭제 중 오류: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"일정 삭제 중 오류 발생: {str(e)}",
                "user": current_user
            }
    
    
    @mcp.tool()
    def search_calendar_events(query: str, days: int = 30) -> Dict:
        """
        일정을 검색합니다 (제목, 설명에서 키워드 검색).
        
        Args:
            query: 검색어 (예: "회의", "치과", "미팅")
            days: 검색 기간 (일 단위, 기본값: 30일)
            
        Returns:
            검색된 일정 목록
            
        Example:
            search_calendar_events(query="회의", days=14)
        """
        current_user = get_current_user()
        logger.info(f"📅 일정 검색 요청: '{query}' (향후 {days}일) (user: {current_user})")
        
        try:
            # 멀티유저 모드: user_id 전달
            events = calendar_service.search_events(
                query=query, 
                days=days,
                user_id=current_user
            )
            
            if events:
                logger.info(f"✅ '{query}' 검색 결과: {len(events)}개 (user: {current_user})")
                return {
                    "status": "success",
                    "message": f"'{query}' 검색 결과 {len(events)}개를 찾았습니다.",
                    "user": current_user,
                    "query": query,
                    "count": len(events),
                    "events": events
                }
            else:
                return {
                    "status": "success",
                    "message": f"'{query}'에 해당하는 일정이 없습니다.",
                    "user": current_user,
                    "query": query,
                    "count": 0,
                    "events": []
                }
                
        except Exception as e:
            logger.error(f"❌ 일정 검색 중 오류: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"일정 검색 중 오류 발생: {str(e)}",
                "user": current_user,
                "events": []
            }
    
    
    @mcp.tool()
    def get_calendar_status() -> Dict:
        """
        Google Calendar 서비스 상태를 확인합니다.
        
        Returns:
            상태 정보 {"authenticated": bool, "user_email": str}
            
        Example:
            get_calendar_status()
        """
        current_user = get_current_user()
        logger.info(f"📊 Calendar 상태 확인 요청 (user: {current_user})")
        
        try:
            # 멀티유저 모드: 현재 사용자의 Calendar 상태 확인
            if current_user:
                status = calendar_service.get_user_service_status(current_user)
            else:
                status = calendar_service.get_service_status()
            
            if status['authenticated']:
                logger.info(f"✅ Calendar 인증됨: {status['user_email']} (user: {current_user})")
            else:
                logger.warning(f"⚠️ Calendar 미인증 상태 (user: {current_user})")
            
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
    
    logger.info("✅ Calendar 도구 등록 완료")
