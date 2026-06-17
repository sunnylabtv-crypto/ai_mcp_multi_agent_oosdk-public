"""
BC3 — 기존 BC2 demo Opp 3건에 OpportunityLineItem 부여 (Product_Guide 패키지 매핑).

매핑:
  VIP Tech (Closed Won)         → Enterprise package (5 lines, ~$737K)
  Standard Tech (Closed Won)    → Module X ×5 + USB ×5            ($1,550)
  Standard Tech (Closed Lost)   → Module X ×10 + USB ×10           ($3,100)

처리 흐름 (각 Opp 별):
  1. Pricebook2Id 가 비어있으면 Standard Pricebook (IsStandard=true) 으로 PATCH
  2. 기존 OLI count 가 0 이면 매핑 라인 일괄 추가
  3. OLI 가 1건 이상이면 skip (idempotent)

⚠️ SFDC 가 자동으로 Opp.Amount = sum(OLI.TotalPrice) 로 갱신함. 의도된 동작.
   Closed Opp 에 OLI 추가는 표준 SFDC 허용; validation rule 이 막는 경우 외엔 OK.
"""
import os
import sys
import time
import jwt
import requests
from pathlib import Path
from typing import Dict, Any, List, Optional

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


# (ProductCode, qty, unit_price) — UnitPrice 는 PBE 와 일치시킴.
PACKAGE_MAP: Dict[str, List[Dict[str, Any]]] = {
    # VIP Tech Closed Won → Enterprise
    "VIP Tech - Request for Quotation (RFQ): Module X Implementation": [
        {"code": "MOD-X",        "qty": 1200, "unit": 250.00},
        {"code": "HW-USBKEY",    "qty": 1200, "unit":  60.00},
        {"code": "HW-SGAPPL-G2", "qty":    2, "unit": 115000.00},
        {"code": "HW-EDGESVR",   "qty":    1, "unit":  60000.00},
        {"code": "OB-5W",        "qty":    1, "unit":  75000.00},
    ],
    # Standard Tech Closed Won → Small
    "Standard Tech - Request for Quotation": [
        {"code": "MOD-X",        "qty":    5, "unit": 250.00},
        {"code": "HW-USBKEY",    "qty":    5, "unit":  60.00},
    ],
    # Standard Tech Closed Lost (10 seats) → Mid-Small
    "Standard Tech - Request for Quotation (10 seats)": [
        {"code": "MOD-X",        "qty":   10, "unit": 250.00},
        {"code": "HW-USBKEY",    "qty":   10, "unit":  60.00},
    ],
}


def sfdc_auth():
    with open(SFDC_KEY_PATH, "r", encoding="utf-8") as f:
        private_key = f.read().strip()
    now = int(time.time())
    payload = {
        "iss": SFDC_CONSUMER_KEY, "sub": SFDC_USERNAME,
        "aud": SFDC_LOGIN_URL, "iat": now, "exp": now + 180,
    }
    a = jwt.encode(payload, private_key, algorithm="RS256")
    if isinstance(a, bytes):
        a = a.decode("utf-8")
    r = requests.post(
        f"{SFDC_LOGIN_URL}/services/oauth2/token",
        data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
              "assertion": a}, timeout=20,
    )
    if r.status_code != 200:
        print(f"FAIL: SFDC auth {r.status_code}: {r.text[:200]}")
        sys.exit(1)
    d = r.json()
    return d["access_token"], d["instance_url"]


def soql(token, instance, q) -> Optional[Dict[str, Any]]:
    r = requests.get(
        f"{instance}/services/data/{API_VER}/query/",
        headers={"Authorization": f"Bearer {token}"},
        params={"q": q}, timeout=20,
    )
    if r.status_code != 200:
        print(f"  SOQL FAIL {r.status_code}: {r.text[:300]}")
        return None
    return r.json()


def sf_create(token, instance, sobject: str, body: Dict[str, Any]) -> Dict[str, Any]:
    r = requests.post(
        f"{instance}/services/data/{API_VER}/sobjects/{sobject}/",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        json=body, timeout=20,
    )
    if r.status_code not in (200, 201):
        return {"error": f"{r.status_code}: {r.text[:300]}"}
    return r.json()


def sf_patch(token, instance, sobject: str, sid: str, body: Dict[str, Any]) -> bool:
    r = requests.patch(
        f"{instance}/services/data/{API_VER}/sobjects/{sobject}/{sid}",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        json=body, timeout=20,
    )
    if r.status_code not in (200, 204):
        print(f"  PATCH FAIL {sobject}/{sid} {r.status_code}: {r.text[:300]}")
        return False
    return True


def get_pbe_map(token, instance, std_pb_id: str) -> Dict[str, str]:
    """ProductCode → PricebookEntry.Id (Standard Pricebook 의 활성 항목만)."""
    codes = sorted({li["code"] for lines in PACKAGE_MAP.values() for li in lines})
    code_list = "', '".join(codes)
    r = soql(token, instance,
        f"SELECT Id, Product2.ProductCode FROM PricebookEntry "
        f"WHERE Pricebook2Id='{std_pb_id}' AND IsActive=true "
        f"AND Product2.ProductCode IN ('{code_list}')",
    )
    out: Dict[str, str] = {}
    for rec in (r["records"] if r else []):
        code = (rec.get("Product2") or {}).get("ProductCode")
        if code:
            out[code] = rec["Id"]
    return out


def main() -> None:
    print("=" * 72)
    print("BC3 — Attach OLI to existing BC2 demo Opps")
    print("=" * 72)
    token, instance = sfdc_auth()

    # 1. Standard Pricebook + PBE map
    r = soql(token, instance,
        "SELECT Id FROM Pricebook2 WHERE IsStandard=true LIMIT 1")
    if not (r and r["records"]):
        print("FAIL: Standard Pricebook 없음")
        sys.exit(1)
    std_pb_id = r["records"][0]["Id"]
    print(f"OK: Standard Pricebook = {std_pb_id}")

    pbe_map = get_pbe_map(token, instance, std_pb_id)
    print(f"OK: PBE map = {len(pbe_map)} entries")
    missing = [c for lines in PACKAGE_MAP.values() for li in lines
               if (c := li["code"]) not in pbe_map]
    if missing:
        print(f"FAIL: 다음 ProductCode 의 PBE 없음: {set(missing)}")
        sys.exit(1)

    # 2. 각 매핑 Opp 처리
    name_list = "', '".join(PACKAGE_MAP.keys())
    r = soql(token, instance,
        f"SELECT Id, Name, Pricebook2Id, HasOpportunityLineItem, "
        f"(SELECT Id FROM OpportunityLineItems) "
        f"FROM Opportunity WHERE Name IN ('{name_list}')",
    )
    opps = r["records"] if r else []
    print(f"OK: target opps = {len(opps)} / {len(PACKAGE_MAP)}")

    for opp in opps:
        name = opp["Name"]
        print(f"\n▶ {name}")
        existing_oli = (opp.get("OpportunityLineItems") or {}).get("totalSize") or 0
        print(f"  · existing OLI = {existing_oli}")

        if existing_oli > 0:
            print(f"  · SKIP (이미 OLI 있음)")
            continue

        # 2a. Pricebook2Id PATCH (비어있을 때만)
        if not opp.get("Pricebook2Id"):
            ok = sf_patch(token, instance, "Opportunity", opp["Id"],
                          {"Pricebook2Id": std_pb_id})
            if not ok:
                print(f"  · FAIL: Pricebook2Id 설정 실패")
                continue
            print(f"  · Pricebook2Id ← {std_pb_id}")

        # 2b. OLI 일괄 추가
        plan = PACKAGE_MAP[name]
        added = 0
        for li in plan:
            body = {
                "OpportunityId": opp["Id"],
                "PricebookEntryId": pbe_map[li["code"]],
                "Quantity": li["qty"],
                "UnitPrice": li["unit"],
            }
            res = sf_create(token, instance, "OpportunityLineItem", body)
            if "error" in res:
                print(f"  · FAIL OLI[{li['code']}]: {res['error']}")
            else:
                added += 1
                print(f"  · OLI+ {li['code']:<14} qty={li['qty']:>4} × ${li['unit']:>10,.2f} = ${li['qty']*li['unit']:,.2f}")
        print(f"  · added {added}/{len(plan)} lines")

    # 3. 최종 verify
    print("\n-- Final state --")
    r = soql(token, instance,
        f"SELECT Id, Name, Amount, Pricebook2Id, "
        f"(SELECT Id, Product2.Name, Quantity, UnitPrice, TotalPrice "
        f" FROM OpportunityLineItems) "
        f"FROM Opportunity WHERE Name IN ('{name_list}') "
        f"ORDER BY Amount DESC")
    for opp in (r["records"] if r else []):
        n_oli = (opp.get("OpportunityLineItems") or {}).get("totalSize") or 0
        print(f"  {opp['Name']:<60}  Amount=${(opp.get('Amount') or 0):,.2f}  OLI={n_oli}  pb2={opp.get('Pricebook2Id')}")


if __name__ == "__main__":
    main()
