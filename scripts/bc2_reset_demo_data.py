"""
BC2 데모 데이터 리셋 스크립트
==============================
시연/영상 녹화 반복을 위한 cleanup. 매 테스트 사이클마다 호출.

리셋 대상 (3 영역):
  1. SFDC
     · Opportunity DELETE — Name LIKE '%BC2 자동 생성%' OR RecordType IN (Opp_VIP, Opp_Standard)
     · Lead.Status = "Closed - Converted" → "Open - Not Contacted" 복원
     · Lead.Description 비우기 (Convert 흔적 제거)

  2. Odoo
     · sale.order DELETE — client_order_ref 가 'VIP Tech%' 또는 'Standard Tech%'
     · cascade 로 sale.order.line 도 같이 삭제

  3. Ontology Memory (VM 의 warm.db / hot in-memory)
     · /api/dashboard/ontology/reset_keys 호출 (prefix=sales_, lost_analysis_)
     · BC1 의 chat_* 결정은 그대로 유지

보존:
  · Account: VIP Tech, Standard Tech
  · Lead: 레코드 자체 (Status / Description 만 reset)
  · Odoo Partner / Product
  · BC1 memory (chat_* prefix)

사용법:
  python scripts/bc2_reset_demo_data.py                        # 전체 리셋
  python scripts/bc2_reset_demo_data.py --dry-run              # 미리보기
  python scripts/bc2_reset_demo_data.py --skip-memory          # SFDC + Odoo 만
  python scripts/bc2_reset_demo_data.py --skip-odoo            # SFDC + Memory 만
  python scripts/bc2_reset_demo_data.py --leads kim.vip@x.com,park.std@y.com   # 특정 Lead 만
"""
import os
import sys
import argparse
import time
import jwt
import requests
import xmlrpc.client
from typing import List, Optional
from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

# ============================================================
# 자격증명 (bc2_to_odoo_handoff.py 와 동일 패턴)
# ============================================================
SFDC_CONSUMER_KEY = os.getenv("SF_CLIENT_ID")
SFDC_USERNAME = os.getenv("SF_USERNAME")
SFDC_LOGIN_URL = os.getenv("SF_LOGIN_URL", "https://login.salesforce.com")
SFDC_KEY_PATH = (
    os.path.join(PROJECT_ROOT, "credentials", "sf_new.key")
    if os.path.exists(os.path.join(PROJECT_ROOT, "credentials", "sf_new.key"))
    else os.getenv("SF_JWT_KEY")
)
SFDC_API_VERSION = "v60.0"

ODOO_URL = os.getenv("ODOO_URL", "https://your-tenant.odoo.com")
ODOO_DB = os.getenv("ODOO_DB", "your_db_name")
ODOO_USERNAME = os.getenv("ODOO_USERNAME", "admin@example.com")
ODOO_API_KEY = os.getenv("ODOO_API_KEY", "")

# Memory reset endpoint (배포된 MCP 서버)
MCP_DASHBOARD_API = os.getenv("MCP_DASHBOARD_API", "http://REDACTED_VM_IP:9101")


# ============================================================
# SFDC
# ============================================================
def sfdc_auth():
    print("[SFDC] JWT 인증...")
    with open(SFDC_KEY_PATH, "r", encoding="utf-8") as f:
        private_key = f.read().strip()
    now = int(time.time())
    payload = {
        "iss": SFDC_CONSUMER_KEY, "sub": SFDC_USERNAME,
        "aud": SFDC_LOGIN_URL, "iat": now, "exp": now + 180,
    }
    assertion = jwt.encode(payload, private_key, algorithm="RS256")
    if isinstance(assertion, bytes):
        assertion = assertion.decode("utf-8")
    resp = requests.post(
        f"{SFDC_LOGIN_URL}/services/oauth2/token",
        data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
              "assertion": assertion},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    if resp.status_code != 200:
        print(f"  ❌ SFDC 인증 실패: {resp.status_code} {resp.text}")
        sys.exit(1)
    data = resp.json()
    print(f"  ✅ instance={data['instance_url']}")
    return data["access_token"], data["instance_url"]


def sfdc_query(token, instance, soql):
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(
        f"{instance}/services/data/{SFDC_API_VERSION}/query/",
        headers=headers, params={"q": soql}, timeout=20,
    )
    return r.json().get("records", []) if r.status_code == 200 else []


def sfdc_delete(token, instance, sobject, record_id, dry_run=False):
    if dry_run:
        return True
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.delete(
        f"{instance}/services/data/{SFDC_API_VERSION}/sobjects/{sobject}/{record_id}",
        headers=headers, timeout=20,
    )
    return r.status_code == 204


def sfdc_patch(token, instance, sobject, record_id, fields, dry_run=False):
    if dry_run:
        return True
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    r = requests.patch(
        f"{instance}/services/data/{SFDC_API_VERSION}/sobjects/{sobject}/{record_id}",
        headers=headers, json=fields, timeout=20,
    )
    return r.status_code == 204


def reset_sfdc(token, instance, lead_emails: Optional[List[str]] = None,
               dry_run: bool = False):
    print()
    print("=" * 60)
    print("[1] SFDC 리셋")
    print("=" * 60)

    # 1-A) BC2 자동 생성 Opp 조회 + 삭제
    soql = (
        "SELECT Id, Name, Account.Name, StageName, Amount, "
        "RecordType.DeveloperName "
        "FROM Opportunity "
        "WHERE Name LIKE '%BC2 자동 생성%' "
        "   OR Name LIKE '%Module X 도입 검토%' "
        "   OR Name LIKE '%Pro 플랜 견적%' "
        "   OR RecordType.DeveloperName IN ('Opp_VIP', 'Opp_Standard')"
    )
    opps = sfdc_query(token, instance, soql)
    print(f"\n[Opp] BC2 자동 생성 / Opp_VIP / Opp_Standard 조회: {len(opps)}건")
    deleted_opps = 0
    for opp in opps:
        rt = (opp.get("RecordType") or {}).get("DeveloperName", "—")
        amount = opp.get("Amount")
        amount_str = f"${amount:,.0f}" if amount else "—"
        prefix = "  [DRY]" if dry_run else "  🗑️ "
        print(f"{prefix} {opp['Name']} | RT={rt} | {amount_str}")
        if sfdc_delete(token, instance, "Opportunity", opp["Id"], dry_run=dry_run):
            deleted_opps += 1
    print(f"  → 삭제 완료: {deleted_opps}/{len(opps)}")

    # 1-B) Lead 상태 복원
    # SFDC dev org 의 sample Leads (Stumuller/Young/Rogers 등) 은 진짜 Lead Convert 가
    # 돼있어 ConvertedAccountId 등이 채워짐 → Status PATCH 가 거부됨.
    # 우리는 BC1/BC2 가 만든 Lead 만 건드리자: ConvertedOpportunityId IS NULL 조건 추가.
    if lead_emails:
        email_filter = " OR ".join(f"Email = '{e}'" for e in lead_emails)
        where_clause = f"({email_filter})"
    else:
        # 기본: Closed - Converted 면서 ConvertedOpportunityId 가 비어있는 Lead 만
        # (= 우리 script 가 단순 PATCH 한 BC1/BC2 Lead. SFDC 샘플은 자동 제외)
        where_clause = (
            "Status = 'Closed - Converted' "
            "AND ConvertedOpportunityId = null"
        )

    soql_lead = (
        "SELECT Id, Name, Email, Status, Description, "
        "ConvertedOpportunityId, ConvertedAccountId "
        f"FROM Lead WHERE {where_clause}"
    )
    leads = sfdc_query(token, instance, soql_lead)
    print(f"\n[Lead] 복원 대상 조회: {len(leads)}건 (SFDC 샘플 제외)")
    reset_leads = 0
    failed_leads = []
    for lead in leads:
        prefix = "  [DRY]" if dry_run else "  🔄"
        ok = sfdc_patch(
            token, instance, "Lead", lead["Id"],
            {"Status": "Open - Not Contacted", "Description": ""},
            dry_run=dry_run,
        )
        status_icon = "✅" if ok else "❌"
        print(f"{prefix} {status_icon} {lead.get('Name')} ({lead.get('Email')})")
        if ok:
            reset_leads += 1
        else:
            failed_leads.append(lead.get('Email'))

    print(f"  → 복원 완료: {reset_leads}/{len(leads)}")
    if failed_leads:
        print(f"  ⚠️  실패: {failed_leads}")
        print(f"     (보통 ConvertedOpportunityId/AccountId 가 채워진 Lead — "
              f"SFDC UI 에서 'Lead 변환 취소' 해야 함)")


# ============================================================
# Odoo
# ============================================================
def odoo_connect():
    if not ODOO_API_KEY:
        print("  ⚠️  ODOO_API_KEY 미설정 — Odoo 단계 SKIP")
        return None
    print("\n[Odoo] XML-RPC 인증...")
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_API_KEY, {})
    if not uid:
        print("  ❌ Odoo 인증 실패")
        return None
    print(f"  ✅ uid={uid}")
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models


def reset_odoo(dry_run: bool = False):
    print()
    print("=" * 60)
    print("[2] Odoo 리셋")
    print("=" * 60)
    session = odoo_connect()
    if not session:
        return
    uid, models = session

    def call(model, method, *args, **kwargs):
        return models.execute_kw(ODOO_DB, uid, ODOO_API_KEY,
                                 model, method, list(args), kwargs)

    # client_order_ref 가 BC2 패턴 매칭하는 SO 조회
    domain = ['|', '|', '|',
              ('client_order_ref', 'like', 'VIP Tech%'),
              ('client_order_ref', 'like', 'Standard Tech%'),
              ('note', 'like', '%BC2 ontology dispatch%'),
              ('note', 'like', '%BC2 자동%')]
    so_ids = call("sale.order", "search", domain)
    print(f"\n[sale.order] BC2 패턴 매칭: {len(so_ids)}건")

    if not so_ids:
        print("  → 삭제 대상 없음")
        return

    so_records = call("sale.order", "read", so_ids,
                      fields=["name", "state", "client_order_ref", "amount_total"])
    for so in so_records:
        prefix = "  [DRY]" if dry_run else "  🗑️ "
        print(f"{prefix} {so['name']} | ref={so.get('client_order_ref')} | "
              f"state={so['state']} | ${so['amount_total']:,.0f}")

    if dry_run:
        return

    # 확정된 SO 는 cancel 먼저, 그 다음 unlink (Odoo 정책)
    confirmed = [so["id"] for so in so_records if so["state"] in ("sale", "done")]
    if confirmed:
        try:
            call("sale.order", "action_cancel", confirmed)
        except Exception as e:
            print(f"  ⚠️  action_cancel 일부 실패: {e}")

    deleted = 0
    for sid in so_ids:
        try:
            call("sale.order", "unlink", [sid])
            deleted += 1
        except Exception as e:
            print(f"  ⚠️  unlink({sid}) 실패: {e}")
    print(f"  → 삭제 완료: {deleted}/{len(so_ids)}")


# ============================================================
# Ontology Memory (VM warm.db + hot in-memory via dashboard API)
# ============================================================
def reset_memory(dry_run: bool = False):
    print()
    print("=" * 60)
    print("[3] Ontology Memory 리셋 (sales_* 결정 + lost_analysis_*)")
    print("=" * 60)

    # 옵션 A) MCP server 에 reset 엔드포인트가 없으면 SSH 로 sqlite 직접 정리하거나
    #         dashboard_api 에 신규 endpoint 추가 필요.
    # 일단 prefix 별 reset endpoint 가 있다고 가정한 호출 시도:
    url = f"{MCP_DASHBOARD_API}/api/dashboard/ontology/reset_keys"
    payload = {
        "prefixes": ["ontology_decision:sales_", "lost_analysis:"],
        "tiers": ["hot", "warm"],
    }
    if dry_run:
        print(f"  [DRY] POST {url}")
        print(f"        payload={payload}")
        return

    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            data = r.json()
            print(f"  ✅ 삭제: {data.get('deleted', '?')}건")
        elif r.status_code == 404:
            print(f"  ℹ️  reset_keys 엔드포인트 미구현 (404).")
            print(f"     → VM 에 SSH 후 직접 정리:")
            print(f"     ssh deploy@REDACTED_VM_IP")
            print(f"     sqlite3 ~/mcp_data/multi_oosdk/memory/warm.db \\")
            print(f"       \"DELETE FROM warm_memory WHERE key LIKE 'ontology_decision:sales_%' OR key LIKE 'lost_analysis:%';\"")
            print(f"     # hot tier 는 MCP 서버 재시작 시 자동 비워짐 (in-memory)")
        else:
            print(f"  ⚠️  reset 실패: HTTP {r.status_code} {r.text[:200]}")
    except requests.exceptions.RequestException as e:
        print(f"  ⚠️  Dashboard API 연결 실패: {e}")
        print(f"     → VM 의 MCP 서버 재시작이 가장 확실 (hot tier 자동 비움):")
        print(f"     ssh deploy@REDACTED_VM_IP 'docker restart mcp-server-multi-oosdk'")


# ============================================================
# Main
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="BC2 데모 데이터 리셋")
    parser.add_argument("--dry-run", action="store_true",
                        help="삭제 안 하고 미리보기만")
    parser.add_argument("--skip-sfdc", action="store_true")
    parser.add_argument("--skip-odoo", action="store_true")
    parser.add_argument("--skip-memory", action="store_true")
    parser.add_argument("--leads", default="",
                        help="특정 이메일 Lead 만 reset (콤마 구분)")
    args = parser.parse_args()

    print("=" * 60)
    print(f"BC2 Demo Reset {'(DRY RUN)' if args.dry_run else ''}")
    print("=" * 60)

    if not args.skip_sfdc:
        token, instance = sfdc_auth()
        lead_emails = [e.strip() for e in args.leads.split(",") if e.strip()] or None
        reset_sfdc(token, instance, lead_emails=lead_emails, dry_run=args.dry_run)

    if not args.skip_odoo:
        reset_odoo(dry_run=args.dry_run)

    if not args.skip_memory:
        reset_memory(dry_run=args.dry_run)

    print()
    print("=" * 60)
    print("✅ 리셋 완료 — BC2 시연 다시 시작 가능")
    print("=" * 60)
    print()


if __name__ == "__main__":
    main()
