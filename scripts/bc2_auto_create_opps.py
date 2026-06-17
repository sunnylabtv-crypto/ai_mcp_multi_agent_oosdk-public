"""
BC2 자동화 검증 스크립트 (self-contained)
==========================================
mcp_server 패키지 import 없이 SFDC REST API를 직접 호출.
필요한 모듈: requests, pyjwt, python-dotenv (보통 다 설치돼 있음)

- VIP Tech (CustomerPriority=VIP) → Opp_VIP RecordType + VIP_Sales_Process (5 stage)
- Standard Tech (CustomerPriority=Standard) → Opp_Standard RecordType + Standard_Sales_Process (4 stage)

실행:
    cd C:\\Users\\deploy\\Dev\\projects\\ai_mcp_multi_agent_oosdk
    python scripts\\bc2_auto_create_opps.py
"""
import os
import sys
import time
import jwt
import requests
from datetime import date, timedelta
from dotenv import load_dotenv

# 프로젝트 루트 = scripts/의 부모
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

# JWT key는 .env가 Docker 경로(/app/...)일 수 있어서 로컬 경로 우선 사용
LOCAL_KEY = os.path.join(PROJECT_ROOT, "credentials", "sf_new.key")
KEY_PATH = LOCAL_KEY if os.path.exists(LOCAL_KEY) else os.getenv("SF_JWT_KEY")

CONSUMER_KEY = os.getenv("SF_CLIENT_ID")
USERNAME = os.getenv("SF_USERNAME")
LOGIN_URL = os.getenv("SF_LOGIN_URL", "https://login.salesforce.com")
API_VERSION = "v60.0"


# ============================================================
# 1. JWT 인증
# ============================================================
def authenticate():
    print(f"[AUTH] username={USERNAME}")
    print(f"[AUTH] login_url={LOGIN_URL}")
    print(f"[AUTH] jwt_key_path={KEY_PATH}")

    if not KEY_PATH or not os.path.exists(KEY_PATH):
        print(f"❌ JWT 키 파일 없음: {KEY_PATH}")
        sys.exit(1)

    with open(KEY_PATH, "r", encoding="utf-8") as f:
        private_key = f.read().strip()

    now = int(time.time())
    payload = {
        "iss": CONSUMER_KEY,
        "sub": USERNAME,
        "aud": LOGIN_URL,
        "iat": now,
        "exp": now + 180,
    }
    assertion = jwt.encode(payload, private_key, algorithm="RS256")
    if isinstance(assertion, bytes):
        assertion = assertion.decode("utf-8")

    resp = requests.post(
        f"{LOGIN_URL}/services/oauth2/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    if resp.status_code != 200:
        print(f"❌ 인증 실패: {resp.status_code} {resp.text}")
        sys.exit(1)

    data = resp.json()
    print(f"✅ 인증 성공 — instance={data['instance_url']}\n")
    return data["access_token"], data["instance_url"]


# ============================================================
# 2. SFDC 호출 헬퍼
# ============================================================
def soql(token, instance, q):
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(
        f"{instance}/services/data/{API_VERSION}/query/",
        headers=headers,
        params={"q": q},
        timeout=20,
    )
    if r.status_code != 200:
        print(f"  [SOQL ERROR] {r.status_code} {r.text[:200]}")
        return None
    return r.json()


def post_sobject(token, instance, sobject, payload):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    r = requests.post(
        f"{instance}/services/data/{API_VERSION}/sobjects/{sobject}/",
        headers=headers,
        json=payload,
        timeout=30,
    )
    if r.status_code != 201:
        print(f"  [POST ERROR] {r.status_code} {r.text[:300]}")
        return None
    return r.json()


# ============================================================
# 3. Account 조회
# ============================================================
def find_account(token, instance, name):
    safe = name.replace("'", "\\'")
    q = (
        "SELECT Id, Name, CustomerPriority__c, AnnualRevenue, OwnerId "
        f"FROM Account WHERE Name = '{safe}' LIMIT 1"
    )
    result = soql(token, instance, q)
    if not result or not result.get("records"):
        return None
    return result["records"][0]


# ============================================================
# 4. RecordType Id 조회
# ============================================================
def get_record_type_id(token, instance, developer_name):
    safe = developer_name.replace("'", "\\'")
    q = (
        "SELECT Id, DeveloperName, Name FROM RecordType "
        f"WHERE SobjectType='Opportunity' AND DeveloperName='{safe}' AND IsActive=true LIMIT 1"
    )
    result = soql(token, instance, q)
    if not result or not result.get("records"):
        return None
    return result["records"][0]["Id"]


# ============================================================
# 5. Opportunity 생성
# ============================================================
def create_opp(token, instance, account, opp_name, stage, record_type_dev_name, amount):
    print("─" * 70)
    print(f"▶ {account['Name']}  (CustomerPriority={account.get('CustomerPriority__c')})")
    print("─" * 70)

    rt_id = get_record_type_id(token, instance, record_type_dev_name)
    if not rt_id:
        print(f"  ❌ RecordType '{record_type_dev_name}' 미발견. 스킵.")
        return None
    print(f"  · Account Id      : {account['Id']}")
    print(f"  · RecordType      : {record_type_dev_name} ({rt_id})")
    print(f"  · Stage           : {stage}")
    print(f"  · Amount          : ${amount:,}")

    close_date = (date.today() + timedelta(days=60)).isoformat()
    payload = {
        "Name": opp_name,
        "AccountId": account["Id"],
        "StageName": stage,
        "CloseDate": close_date,
        "RecordTypeId": rt_id,
        "Amount": amount,
    }
    result = post_sobject(token, instance, "Opportunity", payload)
    if not result:
        return None

    opp_id = result["id"]
    print(f"  ✅ 생성 성공 — Opportunity Id = {opp_id}")
    print(f"  🔗 {instance}/lightning/r/Opportunity/{opp_id}/view")
    return opp_id


# ============================================================
# 6. Opportunity 검증 (RecordType / Stage 확인)
# ============================================================
def verify(token, instance, opp_id):
    q = (
        "SELECT Id, Name, AccountId, Account.Name, Account.CustomerPriority__c, "
        "StageName, RecordType.DeveloperName, RecordType.Name, Amount "
        f"FROM Opportunity WHERE Id = '{opp_id}'"
    )
    result = soql(token, instance, q)
    if not result or not result.get("records"):
        return None
    opp = result["records"][0]
    print(f"  · 검증 RecordType : {opp['RecordType']['DeveloperName']}")
    print(f"  · 검증 Stage      : {opp['StageName']}")
    print(f"  · 검증 Account    : {opp['Account']['Name']} "
          f"(CustomerPriority={opp['Account']['CustomerPriority__c']})")
    return opp


# ============================================================
# Main
# ============================================================
def main():
    print("=" * 70)
    print("BC2 자동화 검증 — Account.CustomerPriority 기반 Opportunity 생성")
    print("=" * 70)
    print()

    token, instance = authenticate()

    # VIP Tech 처리
    vip_acct = find_account(token, instance, "VIP Tech")
    if not vip_acct:
        print("❌ Account 'VIP Tech' 미발견 — SFDC에 먼저 생성하세요.")
    else:
        vip_opp_id = create_opp(
            token, instance, vip_acct,
            opp_name="VIP Tech - Module X 도입 검토 (BC2 자동 생성)",
            stage="Qualification",
            record_type_dev_name="Opp_VIP",
            amount=120000,
        )
        if vip_opp_id:
            verify(token, instance, vip_opp_id)
        print()

    # Standard Tech 처리
    std_acct = find_account(token, instance, "Standard Tech")
    if not std_acct:
        print("❌ Account 'Standard Tech' 미발견 — SFDC에 먼저 생성하세요.")
    else:
        std_opp_id = create_opp(
            token, instance, std_acct,
            opp_name="Standard Tech - Pro 플랜 견적 (BC2 자동 생성)",
            stage="Prospecting",
            record_type_dev_name="Opp_Standard",
            amount=25000,
        )
        if std_opp_id:
            verify(token, instance, std_opp_id)
        print()

    print("=" * 70)
    print("완료 — URL 클릭해서 Stage 드롭다운 확인하세요.")
    print("  · VIP Tech Opp     → 6개 stage (Qualification → Closed Lost)")
    print("  · Standard Tech Opp → 5개 stage (Prospecting → Closed Lost)")
    print("=" * 70)


if __name__ == "__main__":
    main()
