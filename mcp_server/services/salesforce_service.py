# mcp_server/services/salesforce_service.py
"""
Salesforce API 서비스 (멀티유저 지원, JWT Bearer Flow)
"""
import os
import time
import re
import logging
import jwt
import requests
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# ============================================================
# 단일 사용자 모드 (기존 호환)
# ============================================================

_salesforce_config = None
_access_token = None
_instance_url = None


def authenticate_salesforce(config: dict) -> bool:
    """Salesforce JWT Bearer Flow 인증 (단일 사용자)"""
    global _salesforce_config, _access_token, _instance_url
    
    _salesforce_config = config
    
    logger.info("Salesforce JWT 토큰 요청 중...")
    
    try:
        consumer_key = _salesforce_config.get('CONSUMER_KEY')
        username = _salesforce_config.get('USERNAME')
        login_url = _salesforce_config.get('LOGIN_URL')
        key_path = _salesforce_config.get('JWT_KEY_PATH')
        
        if not all([consumer_key, username, login_url, key_path]):
            logger.error("❌ Salesforce 설정이 완전하지 않습니다.")
            return False
        
        try:
            with open(key_path, "r", encoding="utf-8") as f:
                private_key = f.read().strip()
        except FileNotFoundError:
            logger.error(f"❌ 개인키 파일을 찾을 수 없습니다: {key_path}")
            return False
        
        now = int(time.time())
        payload = {
            "iss": consumer_key,
            "sub": username,
            "aud": login_url,
            "iat": now,
            "exp": now + 180
        }
        
        assertion = jwt.encode(payload, private_key, algorithm="RS256")
        if isinstance(assertion, bytes):
            assertion = assertion.decode("utf-8")
        
        token_url = f"{login_url}/services/oauth2/token"
        
        response = requests.post(
            token_url,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=20,
        )
        
        if response.status_code == 200:
            token_data = response.json()
            _access_token = token_data["access_token"]
            _instance_url = token_data["instance_url"]
            
            logger.info("✅ Salesforce JWT 토큰 획득 성공!")
            logger.info(f"   Instance URL: {_instance_url}")
            return True
        else:
            logger.error(f"❌ 토큰 요청 실패: {response.status_code}")
            logger.error(f"   응답: {response.text}")
            return False
            
    except Exception as e:
        logger.error(f"❌ Salesforce 인증 실패: {e}", exc_info=True)
        return False


def get_service_status() -> Dict:
    """Salesforce 서비스 상태 (단일 사용자)"""
    global _salesforce_config, _access_token, _instance_url
    return {
        'configured': _salesforce_config is not None,
        'authenticated': _access_token is not None,
        'instance_url': _instance_url,
        'username': _salesforce_config.get('USERNAME') if _salesforce_config else None
    }


# ============================================================
# 멀티유저 모드
# ============================================================

_user_sf_sessions: Dict[str, dict] = {}


def authenticate_salesforce_for_user(user_id: str, config: dict) -> bool:
    """사용자별 Salesforce 인증"""
    global _user_sf_sessions
    
    logger.info(f"Salesforce 인증 시작 (사용자: {user_id})...")
    
    try:
        consumer_key = config.get('CONSUMER_KEY')
        username = config.get('USERNAME')
        login_url = config.get('LOGIN_URL')
        key_path = config.get('JWT_KEY_PATH')
        
        if not all([consumer_key, username, login_url, key_path]):
            logger.error(f"❌ Salesforce 설정이 완전하지 않습니다 (사용자: {user_id})")
            return False
        
        if not os.path.exists(key_path):
            logger.error(f"❌ JWT Key 파일 없음: {key_path}")
            return False
        
        with open(key_path, "r", encoding="utf-8") as f:
            private_key = f.read().strip()
        
        now = int(time.time())
        payload = {
            "iss": consumer_key,
            "sub": username,
            "aud": login_url,
            "iat": now,
            "exp": now + 180
        }
        
        assertion = jwt.encode(payload, private_key, algorithm="RS256")
        if isinstance(assertion, bytes):
            assertion = assertion.decode("utf-8")
        
        token_url = f"{login_url}/services/oauth2/token"
        
        response = requests.post(
            token_url,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=20,
        )
        
        if response.status_code == 200:
            token_data = response.json()
            
            _user_sf_sessions[user_id] = {
                'config': config,
                'access_token': token_data["access_token"],
                'instance_url': token_data["instance_url"]
            }
            
            logger.info(f"✅ Salesforce 인증 성공! 사용자: {user_id}")
            logger.info(f"   Instance URL: {token_data['instance_url']}")
            return True
        else:
            logger.error(f"❌ 토큰 요청 실패 (사용자: {user_id}): {response.status_code}")
            return False
            
    except Exception as e:
        logger.error(f"❌ Salesforce 인증 실패 (사용자: {user_id}): {e}", exc_info=True)
        return False


def get_user_service_status(user_id: str) -> Dict:
    """사용자별 Salesforce 상태"""
    if user_id in _user_sf_sessions:
        session = _user_sf_sessions[user_id]
        return {
            'configured': True,
            'authenticated': True,
            'instance_url': session['instance_url'],
            'username': session['config'].get('USERNAME')
        }
    return {
        'configured': False,
        'authenticated': False,
        'instance_url': None,
        'username': None
    }


def _get_sf_session(user_id: str = None):
    """현재 컨텍스트의 Salesforce 세션 반환"""
    if user_id and user_id in _user_sf_sessions:
        session = _user_sf_sessions[user_id]
        return session['access_token'], session['instance_url']
    return _access_token, _instance_url


# ============================================================
# Salesforce 기능 함수들
# ============================================================

def create_lead(customer_info: Dict, user_id: str = None) -> Optional[str]:
    """Salesforce Lead 생성"""
    access_token, instance_url = _get_sf_session(user_id)
    
    if not access_token or not instance_url:
        logger.error("❌ Salesforce 인증이 필요합니다")
        return None
    
    try:
        name = customer_info.get('name', '')
        if name:
            name_parts = name.strip().split()
            if len(name_parts) >= 2:
                last_name = name_parts[0]
                first_name = ' '.join(name_parts[1:])
            else:
                last_name = name
                first_name = ''
        else:
            last_name = 'Unknown'
            first_name = ''
        
        email = customer_info.get('email', '')
        if email:
            email_match = re.search(r'<(.+?)>', email)
            if email_match:
                email = email_match.group(1)
            email = email.strip()
        
        lead_data = {
            "LastName": last_name,
            "FirstName": first_name,
            "Company": customer_info.get('company', 'Unknown'),
            "Title": customer_info.get('title', ''),
            "Phone": customer_info.get('phone', ''),
            "Email": email,
            "LeadSource": "Email Inquiry",
            "Status": "Open - Not Contacted",
            "Description": "자동 이메일 워크플로우를 통해 생성된 Lead"
        }
        
        lead_url = f"{instance_url}/services/data/v60.0/sobjects/Lead/"
        
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        
        response = requests.post(lead_url, headers=headers, json=lead_data, timeout=30)
        
        if response.status_code == 201:
            result = response.json()
            lead_id = result['id']
            logger.info(f"✅ Lead 생성 성공! ID: {lead_id}")
            return lead_id
        else:
            logger.error(f"❌ Lead 생성 실패: {response.status_code}")
            logger.error(f"   응답: {response.text}")
            return None
        
    except Exception as e:
        logger.error(f"❌ Lead 생성 중 오류: {e}", exc_info=True)
        return None


def verify_lead(lead_id: str, user_id: str = None) -> Optional[Dict]:
    """Lead 정보 확인"""
    access_token, instance_url = _get_sf_session(user_id)
    
    if not access_token or not instance_url:
        logger.error("❌ Salesforce 인증이 필요합니다")
        return None
    
    try:
        lead_url = f"{instance_url}/services/data/v60.0/sobjects/Lead/{lead_id}"
        
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        
        response = requests.get(lead_url, headers=headers, timeout=30)
        
        if response.status_code == 200:
            return response.json()
        else:
            logger.error(f"❌ Lead 정보 확인 실패: {response.status_code}")
            return None
            
    except Exception as e:
        logger.error(f"❌ Lead 확인 중 오류: {e}", exc_info=True)
        return None


def get_lead_url(lead_id: str, user_id: str = None) -> Optional[str]:
    """Lead 웹 URL 생성"""
    _, instance_url = _get_sf_session(user_id)

    if not instance_url:
        return None

    return f"{instance_url}/lightning/r/Lead/{lead_id}/view"


def search_leads_by_email(email: str, user_id: str = None) -> Optional[Dict]:
    """
    이메일 주소로 Lead 검색 (OOSDK Customer 해석에 사용)
    Returns:
        Lead dict (Id, Name, Email, Customer_Tier__c, Company, AnnualRevenue) or None
    """
    access_token, instance_url = _get_sf_session(user_id)

    if not access_token or not instance_url:
        logger.error("❌ Salesforce 인증이 필요합니다")
        return None

    if not email:
        return None

    # 'John <john@x.com>' 형태에서 순수 이메일만 추출
    email_match = re.search(r'<(.+?)>', email)
    if email_match:
        email = email_match.group(1)
    email = email.strip().lower()

    # 단순 SOQL 이스케이프
    safe_email = email.replace("'", "\\'")

    soql = (
        "SELECT Id, FirstName, LastName, Name, Email, Company, "
        "Customer_Tier__c, AnnualRevenue, Status, OwnerId "
        f"FROM Lead WHERE Email = '{safe_email}' LIMIT 1"
    )

    try:
        url = f"{instance_url}/services/data/v60.0/query/"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        response = requests.get(url, headers=headers, params={"q": soql}, timeout=20)

        if response.status_code != 200:
            logger.warning(f"[search_leads_by_email] status={response.status_code} body={response.text[:200]}")
            return None

        records = response.json().get("records", [])
        if not records:
            return None

        return records[0]

    except Exception as e:
        logger.error(f"❌ search_leads_by_email 오류: {e}", exc_info=True)
        return None


# ============================================================
# BC2 — Opportunity / Account / SOQL 함수
# ============================================================

def query_soql(soql: str, user_id: str = None) -> Optional[Dict]:
    """
    범용 SOQL 쿼리 실행. (BC2: Account/Opportunity/RecordType 조회용)

    Args:
        soql: 실행할 SOQL 쿼리 문자열
    Returns:
        {"totalSize": int, "records": [...]} 또는 None
    """
    access_token, instance_url = _get_sf_session(user_id)

    if not access_token or not instance_url:
        logger.error("❌ Salesforce 인증이 필요합니다")
        return None

    try:
        url = f"{instance_url}/services/data/v60.0/query/"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        response = requests.get(url, headers=headers, params={"q": soql}, timeout=30)

        if response.status_code != 200:
            logger.warning(f"[query_soql] status={response.status_code} body={response.text[:300]}")
            return None

        return response.json()

    except Exception as e:
        logger.error(f"❌ query_soql 오류: {e}", exc_info=True)
        return None


def search_account_by_name(name: str, user_id: str = None) -> Optional[Dict]:
    """
    이름으로 Account 조회 (BC2: 견적 인입 시 매칭용).

    Returns:
        Account dict (Id, Name, CustomerPriority__c, AnnualRevenue, OwnerId) or None
    """
    if not name:
        return None
    safe_name = name.replace("'", "\\'")
    soql = (
        "SELECT Id, Name, CustomerPriority__c, AnnualRevenue, OwnerId "
        f"FROM Account WHERE Name = '{safe_name}' LIMIT 1"
    )
    result = query_soql(soql, user_id=user_id)
    if not result or not result.get("records"):
        return None
    return result["records"][0]


def get_opportunity_record_type_id(developer_name: str, user_id: str = None) -> Optional[str]:
    """
    DeveloperName으로 Opportunity Record Type Id 조회 (BC2: Opp_VIP / Opp_Standard).

    Returns:
        RecordType Id 문자열 or None
    """
    if not developer_name:
        return None
    safe = developer_name.replace("'", "\\'")
    soql = (
        "SELECT Id, DeveloperName, Name "
        "FROM RecordType "
        f"WHERE SobjectType='Opportunity' AND DeveloperName='{safe}' AND IsActive=true LIMIT 1"
    )
    result = query_soql(soql, user_id=user_id)
    if not result or not result.get("records"):
        return None
    return result["records"][0]["Id"]


def create_opportunity(
    account_id: str,
    name: str,
    stage: str,
    record_type_dev_name: str = None,
    amount: float = None,
    close_date: str = None,
    extra_fields: Dict = None,
    user_id: str = None,
) -> Optional[Dict]:
    """
    Salesforce Opportunity 생성 (BC2 핵심 액션).

    Args:
        account_id: 부모 Account Id (필수)
        name: Opportunity 이름 (필수)
        stage: StageName — Sales Process에 정의된 stage 중 하나 (필수)
        record_type_dev_name: RecordType DeveloperName (예: 'Opp_VIP', 'Opp_Standard')
                              지정하지 않으면 SFDC default Record Type 사용
        amount: 금액 (선택)
        close_date: ISO 형식 'YYYY-MM-DD' (선택, 미지정 시 60일 후)
        extra_fields: 추가 필드 dict (선택)

    Returns:
        {"id": opp_id, "url": full_url, "record_type_dev_name": ..., "stage": ...} or None
    """
    access_token, instance_url = _get_sf_session(user_id)

    if not access_token or not instance_url:
        logger.error("❌ Salesforce 인증이 필요합니다")
        return None

    if not account_id or not name or not stage:
        logger.error("❌ create_opportunity: account_id, name, stage는 필수")
        return None

    # CloseDate default = 60일 후
    if not close_date:
        from datetime import date, timedelta
        close_date = (date.today() + timedelta(days=60)).isoformat()

    payload = {
        "Name": name,
        "AccountId": account_id,
        "StageName": stage,
        "CloseDate": close_date,
    }
    if amount is not None:
        payload["Amount"] = amount
    if extra_fields:
        payload.update(extra_fields)

    # RecordType 매핑 — DeveloperName → Id 변환
    if record_type_dev_name:
        rt_id = get_opportunity_record_type_id(record_type_dev_name, user_id=user_id)
        if rt_id:
            payload["RecordTypeId"] = rt_id
        else:
            logger.warning(f"[create_opportunity] RecordType '{record_type_dev_name}' 미발견, default 사용")

    try:
        url = f"{instance_url}/services/data/v60.0/sobjects/Opportunity/"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        response = requests.post(url, headers=headers, json=payload, timeout=30)

        if response.status_code != 201:
            logger.error(f"❌ Opportunity 생성 실패: {response.status_code} body={response.text[:300]}")
            return None

        opp_id = response.json()["id"]
        logger.info(f"✅ Opportunity 생성 성공! Id={opp_id}, RecordType={record_type_dev_name}, Stage={stage}")
        return {
            "id": opp_id,
            "url": f"{instance_url}/lightning/r/Opportunity/{opp_id}/view",
            "record_type_dev_name": record_type_dev_name,
            "stage": stage,
            "name": name,
            "account_id": account_id,
        }

    except Exception as e:
        logger.error(f"❌ create_opportunity 오류: {e}", exc_info=True)
        return None


def update_lead(lead_id: str, fields: Dict, user_id: str = None) -> bool:
    """
    SFDC Lead 필드 업데이트 (BC2: Lead Convert 시 Status 변경 등).

    Args:
        lead_id: Lead Id
        fields: 변경할 필드 dict (예: {"Status": "Closed - Converted"})

    Returns:
        True / False
    """
    access_token, instance_url = _get_sf_session(user_id)
    if not access_token or not instance_url or not lead_id or not fields:
        return False
    try:
        url = f"{instance_url}/services/data/v60.0/sobjects/Lead/{lead_id}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        response = requests.patch(url, headers=headers, json=fields, timeout=30)
        return response.status_code == 204
    except Exception as e:
        logger.error(f"❌ update_lead 오류: {e}", exc_info=True)
        return False


def update_opportunity(opp_id: str, fields: Dict, user_id: str = None) -> bool:
    """
    SFDC Opportunity 필드 업데이트 (Stage 변경, Loss Reason 입력 등).
    """
    access_token, instance_url = _get_sf_session(user_id)
    if not access_token or not instance_url or not opp_id or not fields:
        return False
    try:
        url = f"{instance_url}/services/data/v60.0/sobjects/Opportunity/{opp_id}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        response = requests.patch(url, headers=headers, json=fields, timeout=30)
        return response.status_code == 204
    except Exception as e:
        logger.error(f"❌ update_opportunity 오류: {e}", exc_info=True)
        return False


def get_lead(lead_id: str, user_id: str = None) -> Optional[Dict]:
    """
    SFDC Lead 상세 조회 (BC2: Customer_Tier__c 등 커스텀 필드 포함).

    Returns: dict {Id, Name, Company, Email, Status, Customer_Tier__c, ...}
    """
    if not lead_id:
        return None
    safe = lead_id.replace("'", "\\'")
    soql = (
        "SELECT Id, FirstName, LastName, Name, Company, Email, Status, "
        "Customer_Tier__c, ConvertedAccountId, ConvertedOpportunityId "
        f"FROM Lead WHERE Id = '{safe}' LIMIT 1"
    )
    result = query_soql(soql, user_id=user_id)
    if not result or not result.get("records"):
        return None
    return result["records"][0]


def verify_opportunity(opp_id: str, user_id: str = None) -> Optional[Dict]:
    """생성된 Opportunity 상세 조회 (RecordType, Stage, Account 등 검증용)"""
    if not opp_id:
        return None
    safe = opp_id.replace("'", "\\'")
    soql = (
        "SELECT Id, Name, AccountId, Account.Name, Account.CustomerPriority__c, "
        "StageName, RecordType.DeveloperName, RecordType.Name, "
        "Amount, CloseDate "
        f"FROM Opportunity WHERE Id = '{safe}' LIMIT 1"
    )
    result = query_soql(soql, user_id=user_id)
    if not result or not result.get("records"):
        return None
    return result["records"][0]
