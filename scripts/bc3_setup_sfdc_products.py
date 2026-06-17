"""
BC3 — SFDC 에 Product_Guide 의 12개 SKU 등록 + Standard Pricebook 의 PricebookEntry.

근거: D:\\Dev\\projects\\archive\\ai_test_file\\Product_guide.txt
  (SecureGate Software 는 "quote on request" — PricebookEntry 잡기 애매해 제외)

특성:
  · Idempotent — Name 또는 ProductCode 로 search 후 없으면 create.
    PricebookEntry 는 (Product2Id, Pricebook2Id) tuple 로 중복 검사.
  · Standard Pricebook (IsStandard=true) 사용. probe 결과 id=01sgL000003po5uQAA.
  · Family 를 "Service" / "Hardware" 로 채워 BC3 ontology engine 분기 보조.

실행:
    python scripts/bc3_setup_sfdc_products.py
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


PRODUCTS: List[Dict[str, Any]] = [
    # ── Service (Module X license per seat) ──
    {"name": "Module X",                       "code": "MOD-X",        "family": "Service",  "price":    250.00, "desc": "Enterprise SaaS Integration Module (per-seat/yr)"},
    # ── Service (SmartBox cloud storage subscriptions) ──
    {"name": "SmartBox Pro 1TB",               "code": "SBP-1TB",      "family": "Service",  "price":      9.99, "desc": "Cloud storage Pro plan 1TB / month"},
    {"name": "SmartBox Pro 5TB",               "code": "SBP-5TB",      "family": "Service",  "price":     29.99, "desc": "Cloud storage Pro plan 5TB / month"},
    {"name": "SmartBox Pro Unlimited",         "code": "SBP-UNL",      "family": "Service",  "price":     99.00, "desc": "Cloud storage Pro plan unlimited / month"},
    {"name": "SmartBox Lite 100GB",            "code": "SBL-100",      "family": "Service",  "price":      2.99, "desc": "Cloud storage Lite plan 100GB / month"},
    {"name": "SmartBox Lite 500GB",            "code": "SBL-500",      "family": "Service",  "price":      5.99, "desc": "Cloud storage Lite plan 500GB / month"},
    # ── Service (Onboarding consulting packages) ──
    {"name": "Onboarding Consulting 2w",       "code": "OB-2W",        "family": "Service",  "price":  25000.00, "desc": "Onboarding consulting — 2 weeks"},
    {"name": "Onboarding Consulting 5w",       "code": "OB-5W",        "family": "Service",  "price":  75000.00, "desc": "Onboarding consulting — 5 weeks"},
    {"name": "Onboarding Consulting 12w",      "code": "OB-12W",       "family": "Service",  "price": 180000.00, "desc": "Onboarding consulting — 12 weeks"},
    # ── Hardware (storable) ──
    {"name": "USB SecureKey-100",              "code": "HW-USBKEY",    "family": "Hardware", "price":     60.00, "desc": "FIDO2/WebAuthn 2FA token (2w lead)"},
    {"name": "SecureGate Appliance G2",        "code": "HW-SGAPPL-G2", "family": "Hardware", "price": 115000.00, "desc": "1U rackmount firewall (6w lead) — most risk SKU"},
    {"name": "SmartBox Edge Server",           "code": "HW-EDGESVR",   "family": "Hardware", "price":  60000.00, "desc": "4U 24-bay on-prem storage (4w lead)"},
]


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


def sf_create(token, instance, sobject: str, body: Dict[str, Any]) -> Optional[str]:
    r = requests.post(
        f"{instance}/services/data/{API_VER}/sobjects/{sobject}/",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        json=body, timeout=20,
    )
    if r.status_code not in (200, 201):
        print(f"  CREATE FAIL {r.status_code}: {r.text[:300]}")
        return None
    return r.json().get("id")


def get_standard_pricebook_id(token, instance) -> Optional[str]:
    r = soql(token, instance,
        "SELECT Id, Name FROM Pricebook2 WHERE IsStandard=true LIMIT 1")
    if r and r["records"]:
        return r["records"][0]["Id"]
    return None


def find_product2(token, instance, code: str) -> Optional[str]:
    r = soql(token, instance,
        f"SELECT Id, Name FROM Product2 WHERE ProductCode='{code}' LIMIT 1")
    if r and r["records"]:
        return r["records"][0]["Id"]
    return None


def find_pbe(token, instance, product_id: str, pricebook_id: str) -> Optional[str]:
    r = soql(token, instance,
        f"SELECT Id FROM PricebookEntry "
        f"WHERE Product2Id='{product_id}' AND Pricebook2Id='{pricebook_id}' LIMIT 1")
    if r and r["records"]:
        return r["records"][0]["Id"]
    return None


def upsert_one(token, instance, std_pb_id: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    name = spec["name"]
    code = spec["code"]

    # 1) Product2 — by ProductCode
    pid = find_product2(token, instance, code)
    created_p = False
    if not pid:
        pid = sf_create(token, instance, "Product2", {
            "Name": name, "ProductCode": code, "Family": spec["family"],
            "Description": spec["desc"], "IsActive": True,
        })
        created_p = True
        if not pid:
            return {"name": name, "error": "Product2 create 실패"}

    # 2) PricebookEntry — by (Product2Id, Pricebook2Id) tuple
    pbe_id = find_pbe(token, instance, pid, std_pb_id)
    created_pbe = False
    if not pbe_id:
        pbe_id = sf_create(token, instance, "PricebookEntry", {
            "Product2Id": pid, "Pricebook2Id": std_pb_id,
            "UnitPrice": spec["price"], "IsActive": True,
            "UseStandardPrice": False,
        })
        created_pbe = True

    return {
        "name": name, "product_id": pid, "pbe_id": pbe_id,
        "p": "created" if created_p else "exists",
        "pbe": "created" if created_pbe else "exists",
    }


def main() -> None:
    print("=" * 72)
    print("BC3 — SFDC Product2 + Standard Pricebook entries")
    print("=" * 72)
    token, instance = sfdc_auth()
    std_pb_id = get_standard_pricebook_id(token, instance)
    if not std_pb_id:
        print("FAIL: Standard Pricebook (IsStandard=true) 없음")
        sys.exit(1)
    print(f"OK: Standard Pricebook = {std_pb_id}")
    print(f"OK: target SKUs        = {len(PRODUCTS)}")

    results = []
    for spec in PRODUCTS:
        try:
            r = upsert_one(token, instance, std_pb_id, spec)
        except Exception as e:
            r = {"name": spec["name"], "error": str(e)[:200]}
        results.append(r)

    print("\n-- Results --")
    for r in results:
        if "error" in r:
            print(f"  FAIL: {r['name']:<32} {r['error']}")
        else:
            print(
                f"  OK:   {r['name']:<32} "
                f"Product2[{r['p']:<7}] {r['product_id']}  "
                f"PBE[{r['pbe']:<7}] {r['pbe_id']}"
            )

    created_p = sum(1 for r in results if r.get("p") == "created")
    created_pbe = sum(1 for r in results if r.get("pbe") == "created")
    print(f"\n  summary: Product2 created={created_p}, "
          f"PBE created={created_pbe}, total SKUs={len(PRODUCTS)}")


if __name__ == "__main__":
    main()
