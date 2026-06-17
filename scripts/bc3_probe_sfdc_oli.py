"""
BC3 사전점검 — SFDC 의 BC2 demo Opp 들이 OpportunityLineItem(OLI) 를 가지고 있는지,
그리고 Product2 / Pricebook2 셋업이 어떤지 dump.

확인 사항:
  1. VIP Tech / Standard Tech Account 의 모든 Opp (OLI count, total Amount)
  2. 각 Opp 의 OLI 상세 (Product2.Name, Quantity, UnitPrice)
  3. 활성 Product2 카운트 + Pricebook2 (Standard) 와 PricebookEntry 카운트
  4. Product_Guide 의 7개 제품 이름이 SFDC 에 이미 있는지 매칭

근거 SOQL:
  · Pricebook2 IsStandard=true 가 SFDC 표준 가격표
  · OpportunityLineItem 의 OpportunityId/Product2Id/Quantity/UnitPrice/TotalPrice
"""
import os
import sys
import time
import jwt
import requests
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass


SFDC_CONSUMER_KEY = os.getenv("SF_CLIENT_ID")
SFDC_USERNAME = os.getenv("SF_USERNAME")
SFDC_LOGIN_URL = os.getenv("SF_LOGIN_URL", "https://login.salesforce.com")
SFDC_KEY_PATH = (
    PROJECT_ROOT / "credentials" / "sf_new.key"
    if (PROJECT_ROOT / "credentials" / "sf_new.key").exists()
    else os.getenv("SF_JWT_KEY")
)
API_VER = "v60.0"


PRODUCT_GUIDE_NAMES = [
    # service
    "Module X",
    "SmartBox Pro",
    "SmartBox Lite",
    "SecureGate Software",
    "Onboarding Consulting",
    # storable
    "USB SecureKey-100",
    "SecureGate Appliance G2",
    "SmartBox Edge Server",
]


def sfdc_auth():
    with open(SFDC_KEY_PATH, "r", encoding="utf-8") as f:
        private_key = f.read().strip()
    now = int(time.time())
    payload = {
        "iss": SFDC_CONSUMER_KEY,
        "sub": SFDC_USERNAME,
        "aud": SFDC_LOGIN_URL,
        "iat": now, "exp": now + 180,
    }
    assertion = jwt.encode(payload, private_key, algorithm="RS256")
    if isinstance(assertion, bytes):
        assertion = assertion.decode("utf-8")
    r = requests.post(
        f"{SFDC_LOGIN_URL}/services/oauth2/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        },
        timeout=20,
    )
    if r.status_code != 200:
        print(f"FAIL: SFDC auth {r.status_code}: {r.text[:200]}")
        sys.exit(1)
    d = r.json()
    print(f"OK: SFDC auth (instance={d['instance_url']})")
    return d["access_token"], d["instance_url"]


def soql(token, instance, q):
    r = requests.get(
        f"{instance}/services/data/{API_VER}/query/",
        headers={"Authorization": f"Bearer {token}"},
        params={"q": q}, timeout=20,
    )
    if r.status_code != 200:
        print(f"  SOQL FAIL {r.status_code}: {r.text[:300]}")
        return None
    return r.json()


def main():
    token, instance = sfdc_auth()

    print("\n-- BC2 demo Opp 들 (VIP Tech / Standard Tech) --")
    res = soql(token, instance,
        "SELECT Id, Name, Account.Name, Account.CustomerPriority__c, StageName, "
        "Amount, Pricebook2Id, "
        "(SELECT Id, Product2.Name, Product2.Family, Product2Id, Quantity, "
        "UnitPrice, TotalPrice FROM OpportunityLineItems) "
        "FROM Opportunity "
        "WHERE Account.Name IN ('VIP Tech','Standard Tech') "
        "ORDER BY CreatedDate DESC LIMIT 50",
    )
    if not res:
        return
    opps = res.get("records", [])
    print(f"  total: {len(opps)} opp")
    for opp in opps:
        oli_node = opp.get("OpportunityLineItems")
        oli_count = oli_node.get("totalSize") if oli_node else 0
        print(
            f"  · {opp['Name']:<50}  "
            f"acct={opp['Account']['Name']:<14}  "
            f"tier={opp['Account'].get('CustomerPriority__c') or '-':<8}  "
            f"stage={opp['StageName']:<14}  "
            f"amount={opp.get('Amount')}  pb2={opp.get('Pricebook2Id') or '-':<18}  "
            f"OLI={oli_count}"
        )
        if oli_node and oli_node.get("records"):
            for li in oli_node["records"]:
                p = li.get("Product2") or {}
                print(
                    f"      OLI: {p.get('Name','-'):<28} "
                    f"qty={li.get('Quantity')}  unit=${li.get('UnitPrice')}  "
                    f"total=${li.get('TotalPrice')}"
                )

    print("\n-- Pricebook2 (IsStandard=true) --")
    res = soql(token, instance,
        "SELECT Id, Name, IsActive, IsStandard FROM Pricebook2 WHERE IsActive=true",
    )
    if res:
        for pb in res["records"]:
            print(f"  {pb}")

    print("\n-- Product2 매칭 (Product_Guide 7개 이름) --")
    name_list = "', '".join(PRODUCT_GUIDE_NAMES)
    res = soql(token, instance,
        f"SELECT Id, Name, ProductCode, IsActive, Family "
        f"FROM Product2 WHERE Name IN ('{name_list}')",
    )
    found = res["records"] if res else []
    found_names = {p["Name"] for p in found}
    for name in PRODUCT_GUIDE_NAMES:
        match = next((p for p in found if p["Name"] == name), None)
        if match:
            print(f"  OK   {name:<28} id={match['Id']}  active={match['IsActive']}  family={match.get('Family')}")
        else:
            print(f"  MISS {name:<28} (Product2 에 없음)")

    print(f"\n  summary: {len(found_names)}/{len(PRODUCT_GUIDE_NAMES)} present in SFDC Product2")

    print("\n-- PricebookEntry (Standard Pricebook 의 활성 항목) --")
    std_pb_id = None
    res = soql(token, instance, "SELECT Id FROM Pricebook2 WHERE IsStandard=true LIMIT 1")
    if res and res["records"]:
        std_pb_id = res["records"][0]["Id"]
        print(f"  Standard Pricebook Id = {std_pb_id}")
        res = soql(token, instance,
            f"SELECT Id, Product2.Name, UnitPrice, IsActive "
            f"FROM PricebookEntry WHERE Pricebook2Id='{std_pb_id}' "
            f"AND Product2.Name IN ('{name_list}')",
        )
        for pbe in (res["records"] if res else []):
            print(f"  {pbe['Product2']['Name']:<28} ${pbe['UnitPrice']}  active={pbe['IsActive']}")
    else:
        print("  (Standard Pricebook 없음)")


if __name__ == "__main__":
    main()
