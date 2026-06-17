# mcp_server/services/service_manager.py
"""
서비스 통합 관리자 (멀티유저 지원)
"""
import logging
from typing import Dict, Any, Optional

from . import gmail_service
from . import openai_service
from . import salesforce_service
from . import vectordb_service
from . import calendar_service
from . import odoo_service

logger = logging.getLogger(__name__)

# 현재 사용자 컨텍스트
_current_user: Optional[str] = None

# 사용자별 서비스 인스턴스 캐시
_user_services: Dict[str, Dict] = {}


def set_current_user(user_id: str):
    """현재 요청의 사용자 설정"""
    global _current_user
    _current_user = user_id
    logger.info(f"🔄 현재 사용자 설정: {user_id}")


def get_current_user() -> Optional[str]:
    """현재 사용자 반환"""
    return _current_user


# ============================================================
# 단일 사용자 모드 (기존 호환)
# ============================================================

def initialize_all_services(config: Dict[str, Any]) -> Dict[str, bool]:
    """모든 서비스 초기화 (단일 사용자 모드)"""
    logger.info("=" * 60)
    logger.info("🚀 전체 서비스 초기화 시작")
    logger.info("=" * 60)
    
    results = {
        'gmail': False,
        'openai': False,
        'salesforce': False,
        'vectordb': False,
        'calendar': False,
        'odoo': False,
    }

    # Gmail 인증
    logger.info("\n[1/5] Gmail 서비스 인증 중...")
    try:
        results['gmail'] = gmail_service.authenticate_gmail(config['GMAIL_CONFIG'])
        logger.info("✅ Gmail 인증 성공" if results['gmail'] else "❌ Gmail 인증 실패")
    except Exception as e:
        logger.error(f"❌ Gmail 인증 중 예외 발생: {e}", exc_info=True)
    
    # OpenAI 초기화
    logger.info("\n[2/5] OpenAI 서비스 인증 중...")
    try:
        results['openai'] = openai_service.authenticate_openai(config['OPENAI_CONFIG'])
        logger.info("✅ OpenAI 인증 성공" if results['openai'] else "❌ OpenAI 인증 실패")
    except Exception as e:
        logger.error(f"❌ OpenAI 인증 중 예외 발생: {e}", exc_info=True)
    
    # Salesforce 인증
    logger.info("\n[3/5] Salesforce 서비스 인증 중...")
    try:
        if config.get('SALESFORCE_CONFIG'):
            results['salesforce'] = salesforce_service.authenticate_salesforce(config['SALESFORCE_CONFIG'])
            logger.info("✅ Salesforce 인증 성공" if results['salesforce'] else "❌ Salesforce 인증 실패")
        else:
            logger.info("⏭️ Salesforce 설정 없음 - 건너뜀")
            results['salesforce'] = True
    except Exception as e:
        logger.error(f"❌ Salesforce 인증 중 예외 발생: {e}", exc_info=True)
    
    # VectorDB 초기화
    logger.info("\n[4/5] VectorDB (ChromaDB) 초기화 중...")
    try:
        vectordb_config = config.get('VECTORDB_CONFIG', {})
        results['vectordb'] = vectordb_service.initialize(vectordb_config)
        logger.info("✅ VectorDB 초기화 성공" if results['vectordb'] else "❌ VectorDB 초기화 실패")
    except Exception as e:
        logger.error(f"❌ VectorDB 초기화 중 예외 발생: {e}", exc_info=True)
    
    # Calendar 인증
    logger.info("\n[5/5] Google Calendar 서비스 인증 중...")
    try:
        results['calendar'] = calendar_service.authenticate_calendar(config['GMAIL_CONFIG'])
        logger.info("✅ Calendar 인증 성공" if results['calendar'] else "❌ Calendar 인증 실패")
    except Exception as e:
        logger.error(f"❌ Calendar 인증 중 예외 발생: {e}", exc_info=True)

    # Odoo 인증 (BC2+ — 환경변수 기반, 누락 시 자동 skip)
    logger.info("\n[+] Odoo ERP 서비스 인증 시도 (환경변수)")
    try:
        results['odoo'] = odoo_service.authenticate_odoo()
        if results['odoo']:
            logger.info("✅ Odoo 인증 성공")
        else:
            status = odoo_service.get_service_status()
            logger.info(f"⏭️ Odoo 비활성 ({status.get('reason', 'unknown')}) — agent 는 plan-only 모드로 동작")
    except Exception as e:
        logger.error(f"❌ Odoo 인증 중 예외 발생: {e}", exc_info=True)

    # 결과 요약
    _print_results_summary(results)

    return results


def get_all_service_status() -> Dict[str, Dict]:
    """모든 서비스의 상태 확인 (단일 사용자 모드)"""
    return {
        'gmail': gmail_service.get_service_status(),
        'openai': openai_service.get_service_status(),
        'salesforce': salesforce_service.get_service_status(),
        'vectordb': vectordb_service.get_status(),
        'calendar': calendar_service.get_service_status(),
        'odoo': odoo_service.get_service_status(),
    }


# ============================================================
# 멀티유저 모드
# ============================================================

def initialize_user_services(config: Dict[str, Any]) -> Dict[str, bool]:
    """사용자별 서비스 초기화"""
    user_id = config.get('user_id', 'unknown')
    
    logger.info("=" * 60)
    logger.info(f"🚀 사용자 '{user_id}' 서비스 초기화 시작")
    logger.info("=" * 60)
    
    results = {
        'gmail': False,
        'openai': False,
        'salesforce': False,
        'vectordb': False,
        'calendar': False
    }
    
    # Gmail 인증 (사용자별)
    logger.info(f"\n[1/5] Gmail 서비스 인증 중... ({config.get('gmail_account', 'unknown')})")
    try:
        gmail_result = gmail_service.authenticate_gmail_for_user(
            user_id, 
            config['GMAIL_CONFIG']
        )
        results['gmail'] = gmail_result
        logger.info("✅ Gmail 인증 성공" if gmail_result else "❌ Gmail 인증 실패")
    except Exception as e:
        logger.error(f"❌ Gmail 인증 중 예외 발생: {e}", exc_info=True)
    
    # OpenAI 초기화 (공통)
    logger.info("\n[2/5] OpenAI 서비스 초기화 중...")
    try:
        results['openai'] = openai_service.authenticate_openai(config['OPENAI_CONFIG'])
        logger.info("✅ OpenAI 초기화 성공" if results['openai'] else "❌ OpenAI 초기화 실패")
    except Exception as e:
        logger.error(f"❌ OpenAI 초기화 중 예외 발생: {e}", exc_info=True)
    
    # Salesforce 인증 (사용자별, 선택적)
    logger.info("\n[3/5] Salesforce 서비스 인증 중...")
    if config.get('sfdc_enabled') and config.get('SALESFORCE_CONFIG'):
        try:
            sf_result = salesforce_service.authenticate_salesforce_for_user(
                user_id,
                config['SALESFORCE_CONFIG']
            )
            results['salesforce'] = sf_result
            logger.info("✅ Salesforce 인증 성공" if sf_result else "❌ Salesforce 인증 실패")
        except Exception as e:
            logger.error(f"❌ Salesforce 인증 중 예외 발생: {e}", exc_info=True)
    else:
        logger.info(f"⏭️ Salesforce 비활성화 (사용자: {user_id})")
        results['salesforce'] = True
    
    # VectorDB 초기화 (공통)
    logger.info("\n[4/5] VectorDB 초기화 중...")
    try:
        vectordb_config = config.get('VECTORDB_CONFIG', {})
        results['vectordb'] = vectordb_service.initialize(vectordb_config)
        logger.info("✅ VectorDB 초기화 성공" if results['vectordb'] else "❌ VectorDB 초기화 실패")
    except Exception as e:
        logger.error(f"❌ VectorDB 초기화 중 예외 발생: {e}", exc_info=True)
    
    # Calendar 인증 (사용자별)
    logger.info("\n[5/5] Calendar 서비스 인증 중...")
    try:
        cal_result = calendar_service.authenticate_calendar_for_user(
            user_id,
            config['GMAIL_CONFIG']
        )
        results['calendar'] = cal_result
        logger.info("✅ Calendar 인증 성공" if cal_result else "❌ Calendar 인증 실패")
    except Exception as e:
        logger.error(f"❌ Calendar 인증 중 예외 발생: {e}", exc_info=True)
    
    # 캐시에 저장
    _user_services[user_id] = {
        'config': config,
        'results': results
    }
    
    _print_results_summary(results, user_id)
    
    return results


def get_user_service_status(user_id: str) -> Dict[str, Dict]:
    """사용자별 서비스 상태 확인"""
    return {
        'gmail': gmail_service.get_user_service_status(user_id),
        'openai': openai_service.get_service_status(),
        'salesforce': salesforce_service.get_user_service_status(user_id),
        'vectordb': vectordb_service.get_status(),
        'calendar': calendar_service.get_user_service_status(user_id)
    }


def _print_results_summary(results: Dict[str, bool], user_id: str = None):
    """결과 요약 출력"""
    logger.info("\n" + "=" * 60)
    if user_id:
        logger.info(f"📊 사용자 '{user_id}' 서비스 초기화 결과")
    else:
        logger.info("📊 서비스 초기화 결과 요약")
    logger.info("=" * 60)
    
    success_count = sum(results.values())
    total_count = len(results)
    
    for service_name, success in results.items():
        status = "✅ 성공" if success else "❌ 실패"
        logger.info(f"  {service_name.upper():12s}: {status}")
    
    logger.info("-" * 60)
    logger.info(f"  전체: {success_count}/{total_count} 서비스 초기화 완료")
    logger.info("=" * 60 + "\n")


# ============================================================
# 편의 함수
# ============================================================

def get_gmail_service():
    return gmail_service

def get_openai_service():
    return openai_service

def get_salesforce_service():
    return salesforce_service

def get_vectordb_service():
    return vectordb_service

def get_calendar_service():
    return calendar_service

def get_odoo_service():
    return odoo_service
