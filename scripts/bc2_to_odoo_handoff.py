"""
BC2 → Odoo 핸드오프 검증 스크립트 (v2 — odoo_service 재사용)
============================================================
SFDC Opportunity (VIP Tech / Standard Tech) → Odoo Sales Order 자동 생성.

v2 변경 (2026-05-18 refactor):
  · 이전 버전: 자체 _odoo_connect / _odoo_call / find_or_create_* 가 erp_agent 와 100% 중복.
  · 이번 버전: mcp_server.services.odoo_service 를 그대로 호출 — 단일 진실 소스.
  · 인증/세션/멱등성 로직이 한 군데에만 존재.

흐름:
  1. SFDC 인증 → BC2 demo Opp 조회 (VIP Tech, Standard Tech 계정)
  2. Odoo 인증 (odoo_service)
  3. 각 Opp 에 대해:
     · Partner 찾기/생성 (odoo_service.find_or_create_partner)
     · Product 찾기/생성 (odoo_service.find_or_create_product)
     · Sales Order 생성 + 확정 (odoo_service.create_sales_order)
  4. 결과 URL 출력
"""
import os
import sys
import time
import jwt
import requests
from pathlib import Path
from dotenv import load_dotenv

# 프로젝트 루트 등록 → mcp_server 모듈 import 가능하게
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

from mcp_server.services import odoo_service  # noqa: E402


# ============================================================
# SFDC 자격증명 (별도 인증 — 이건 SFDC 쪽이라 그대로 유지)
# ============================================================
SFDC_CONSUMER_KEY = os.getenv("SF_CLIENT_ID")
SFDC_USERNAME = os.getenv("SF_USERNAME")
SFDC_LOGIN_URL = os.getenv("SF_LOGIN_URL", "https://login.salesforce.com")
SFDC_KEY_PATH = (
    PROJECT_ROOT / "credentials" / "sf_new.key"
    if (PROJECT_ROOT / "credentials" / "sf_new.key").exists()
    else os.getenv("SF_JWT_KEY")
)
SFDC_API_VERSION = "v60.0"


# ============================================================
# SFDC 인증 + 쿼리
# ============================================================
def sfdc_authenticate():
    print("[SFDC] JWT 인증 중...")
    with open(SFDC_KEY_PATH, "r", encoding="utf-8") as f:
        private_key = f.read().strip()
    now = int(time.time())
    payload = {
        "iss": SFDC_CONSUMER_KEY,
        "sub": SFDC_USERNAME,
        "aud": SFDC_LOGIN_URL,
        "iat": now,
        "exp": now + 180,
    }
    assertion = jwt.encode(payload, private_key, algorithm="RS256")
    if isinstance(assertion, bytes):
        assertion = assertion.decode("utf-8")

    resp = requests.post(
        f"{SFDC_LOGIN_URL}/services/oauth2/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    if resp.status_code != 200:
        print(f"  ❌ SFDC 인증 실패: {resp.status_code} {resp.text}")
        sys.exit(1)
    data = resp.json()
    print(f"  ✅ SFDC 인증 성공 — instance={data['instance_url']}")
    return data["access_token"], data["instance_url"]


def sfdc_query(token, instance, soql_str):
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(
        f"{instance}/services/data/{SFDC_API_VERSION}/query/",
        headers=headers,
        params={"q": soql_str},
        timeout=20,
    )
    if r.status_code != 200:
        print(f"  [SOQL ERROR] {r.status_code} {r.text[:200]}")
        return None
    return r.json()


def get_bc2_opportunities(token, instance):
    """VIP Tech, Standard Tech Account 의 모든 Opportunity 조회 (+OLI sub-query).

    OLI sub-query 가 핵심: BC3 부터 SO line 은 단일 'Module X' fallback 이 아니라
    Opp 의 OpportunityLineItem 을 그대로 따라간다 (Product_Guide 패키지 매핑).
    """
    soql = (
        "SELECT Id, Name, AccountId, Account.Id, Account.Name, Account.CustomerPriority__c, "
        "StageName, Amount, RecordType.DeveloperName, CloseDate, "
        "(SELECT Id, Product2.Name, Product2.Family, Product2.ProductCode, "
        " Quantity, UnitPrice, TotalPrice FROM OpportunityLineItems) "
        "FROM Opportunity "
        "WHERE Account.Name IN ('VIP Tech', 'Standard Tech') "
        "ORDER BY CreatedDate DESC"
    )
    result = sfdc_query(token, instance, soql)
    return result.get("records", []) if result else []


# ============================================================
# Main
# ============================================================
def main():
    print("=" * 75)
    print("BC2 → Odoo 핸드오프 검증 (v2 — odoo_service 사용)")
    print("=" * 75)
    print()

    # 1. SFDC 에서 BC2 Opp 조회
    sfdc_token, sfdc_instance = sfdc_authenticate()
    opps = get_bc2_opportunities(sfdc_token, sfdc_instance)
    print(f"\n[SFDC] BC2 Opportunity 조회 완료 — {len(opps)}건")
    for opp in opps:
        print(f"  · {opp['Name']}")
        print(f"     Account: {opp['Account']['Name']} ({opp['Account']['CustomerPriority__c']})")
        print(f"     Stage: {opp['StageName']}, Amount: ${opp['Amount']:,.0f}")

    if not opps:
        print("\n❌ SFDC 에 'VIP Tech' 또는 'Standard Tech' Account 의 Opportunity 가 없습니다.")
        sys.exit(1)

    # 2. Odoo 인증 (services 사용)
    print("\n[Odoo] services.odoo_service 로 인증 시도 중...")
    if not odoo_service.authenticate_odoo():
        status = odoo_service.get_service_status()
        print(f"  ❌ Odoo 인증 실패: {status.get('reason', 'unknown')}")
        print("     ODOO_URL / ODOO_DB / ODOO_USERNAME / ODOO_API_KEY 환경변수를 확인하세요.")
        sys.exit(1)
    status = odoo_service.get_service_status()
    print(f"  ✅ Odoo 인증 성공 — uid={status['uid']}")

    # 3. 기본 제품 fallback (OLI=0 인 BC1 잔재 Opp 용)
    print("\n[Odoo] 기본 fallback 제품 (Module X) 준비")
    fallback_product_id = odoo_service.find_or_create_product(
        name="Module X", default_price=500, product_type="service"
    )
    print(f"  fallback Product Id: {fallback_product_id}")

    # 4. 각 Opp 을 Odoo Sales Order 로 push
    print("\n" + "=" * 75)
    print("[핸드오프] SFDC Opp → Odoo Sales Order 변환")
    print("=" * 75)

    created_orders = []
    for opp in opps:
        print(f"\n▶ {opp['Name']}")

        # 4-0. BC2 룰: Closed Lost 는 ERP push 안 함 (opp_lost → analytics only)
        if (opp.get("StageName") or "").lower() == "closed lost":
            print(f"    · ⏭️  Closed Lost — ERP push skip (BC2 opp_lost rule)")
            continue

        # 4-1. 멱등성 체크
        existing = odoo_service.find_existing_sales_order(opp["Name"])
        if existing:
            print(f"    · ⏭️  이미 존재 — {existing['name']} ({existing['state']}) — skip")
            continue

        # 4-2. Partner 매핑
        account = opp["Account"]
        tier = account.get("CustomerPriority__c") or "Standard"
        partner_id = odoo_service.find_or_create_partner(
            account["Name"], tier,
            comment_extra=f"SFDC Account Id: {account['Id']}",
        )
        print(f"    · Partner Id: {partner_id}")

        # 4-3. OLI → Odoo order_lines 동적 매핑 (BC3 본격)
        oli_records = (opp.get("OpportunityLineItems") or {}).get("records") or []
        order_lines = []
        if oli_records:
            for li in oli_records:
                p2 = li.get("Product2") or {}
                name = p2.get("Name") or "Unknown SKU"
                family = (p2.get("Family") or "Service").lower()
                product_type = "storable" if family == "hardware" else "service"
                unit_price = float(li.get("UnitPrice") or 0)
                qty = float(li.get("Quantity") or 1)
                pid = odoo_service.find_or_create_product(
                    name=name, default_price=unit_price, product_type=product_type,
                )
                order_lines.append((0, 0, {
                    "product_id": pid,
                    "product_uom_qty": qty,
                    "price_unit": unit_price,
                    "name": name,
                }))
                print(f"    · OLI→ {name:<28} qty={qty:>5g} × ${unit_price:>10,.2f} ({product_type})")
        else:
            # BC1 잔재: OLI 없으면 Amount 통째를 'Module X' 단일 라인으로
            order_lines.append((0, 0, {
                "product_id": fallback_product_id,
                "product_uom_qty": 1,
                "price_unit": opp["Amount"] or 0,
                "name": opp["Name"],
            }))
            print(f"    · OLI=0 → fallback Module X (단일 라인)")
        currency_id = odoo_service.find_currency_id("USD")

        so_result = odoo_service.create_sales_order(
            partner_id=partner_id,
            order_lines=order_lines,
            client_order_ref=opp["Name"],
            note=f"BC2 자동 생성 (SFDC tier: {tier})",
            currency_id=currency_id,
            confirm=True,
        )

        print(f"    · SO 생성 — {so_result['order']['name']} (Id={so_result['order_id']})")
        print(f"    · State: {so_result['order']['state']}, "
              f"Total: ${so_result['order']['amount_total']:,.0f}")
        print(f"    · 🔗 {so_result['url']}")
        created_orders.append({
            "sfdc_opp": opp["Name"],
            "odoo_so": so_result["order"]["name"],
            "amount": so_result["order"]["amount_total"],
            "url": so_result["url"],
        })

    # 5. 요약
    print("\n" + "=" * 75)
    print("결과 요약")
    print("=" * 75)
    for c in created_orders:
        print(f"  ✅ SFDC '{c['sfdc_opp']}'")
        print(f"     → Odoo {c['odoo_so']}  ${c['amount']:,.0f}")
        print(f"     {c['url']}")
    print()


if __name__ == "__main__":
    main()
