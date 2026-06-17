# scripts/bc3_diag_odoo_password.py
"""
Odoo auth diagnosis - try password instead of API key to isolate username/db.

NOTE: ASCII only output. Windows cp949 console safe.
"""
import os
import sys
import xmlrpc.client
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

URL = (os.getenv("ODOO_URL", "") or "").strip()
DB = (os.getenv("ODOO_DB", "") or "").strip()
USER = (os.getenv("ODOO_USERNAME", "") or "").strip()
API_KEY = (os.getenv("ODOO_API_KEY", "") or "").strip()
PASSWORD = (os.getenv("ODOO_PASSWORD", "") or "").strip()


def try_auth(label: str, secret: str) -> None:
    print(f"\n-- {label} auth attempt --")
    if not secret:
        print(f"  [SKIP] secret is empty")
        return
    if not (URL and DB and USER):
        print(f"  [FAIL] URL/DB/USER missing: URL={bool(URL)} DB={bool(DB)} USER={bool(USER)}")
        return
    try:
        common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common", allow_none=True)
        uid = common.authenticate(DB, USER, secret, {})
        if uid:
            print(f"  [OK] auth success: uid={uid}")
            models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object", allow_none=True)
            try:
                count = models.execute_kw(
                    DB, uid, secret,
                    "sale.order", "search_count", [[]],
                )
                print(f"  [OK] sale.order access: count={count}")
            except xmlrpc.client.Fault as e:
                print(f"  [WARN] sale.order access denied: {e.faultString[:200]}")
        else:
            print(f"  [FAIL] uid=False (wrong username/db/secret)")
    except xmlrpc.client.Fault as e:
        print(f"  [FAIL] XML-RPC Fault: {e.faultString[:200]}")
    except Exception as e:
        print(f"  [FAIL] {type(e).__name__}: {str(e)[:200]}")


print(f"URL  = {URL!r}")
print(f"DB   = {DB!r}")
print(f"USER = {USER!r}")
print(f"API_KEY len  = {len(API_KEY)}  prefix = {API_KEY[:8]!r}")
print(f"PASSWORD len = {len(PASSWORD)} ({'set' if PASSWORD else 'NOT SET - ODOO_PASSWORD env var empty'})")

try_auth("API key", API_KEY)
try_auth("password", PASSWORD)

print(
    "\n-- decision matrix --\n"
    "  API key FAIL + password OK   -> rotate API key (and update .env + GH Actions secret)\n"
    "  API key FAIL + password FAIL -> USERNAME (exact email) or DB is wrong\n"
    "  API key OK                   -> should have worked; check elsewhere"
)
