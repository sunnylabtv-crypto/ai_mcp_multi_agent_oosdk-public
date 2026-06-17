# scripts/bc3_diag_odoo_env.py
"""
Odoo 인증 실패 진단 — 값 노출 없이 길이/prefix 만 출력.

확인 항목:
  · .env 로딩이 됐는지
  · 4개 키가 다 truthy 인지
  · API key 의 길이 + 앞 4글자 (확실하게 갱신됐는지 사용자가 GitHub Actions secret 과 비교 가능)
  · 인증 시도 결과 + odoo_service 의 last_auth_error
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    print(f"[diag] .env 로딩됨: {PROJECT_ROOT / '.env'} (존재={Path(PROJECT_ROOT/'.env').exists()})")
except ImportError:
    print("[diag] dotenv 미설치 — 시스템 env var 만 사용")


def show(name: str, *, mask_keep_prefix: int = 0) -> None:
    v = os.getenv(name, "")
    length = len(v)
    has_lead_ws = v != v.lstrip() if v else False
    has_trail_ws = v != v.rstrip() if v else False
    repr_show = f"len={length}"
    if mask_keep_prefix and v:
        prefix = v[:mask_keep_prefix]
        repr_show += f" prefix={prefix!r}"
    flags = []
    if has_lead_ws: flags.append("LEADING_WHITESPACE")
    if has_trail_ws: flags.append("TRAILING_WHITESPACE")
    if "\r" in v: flags.append("CR_INSIDE")
    if "\n" in v: flags.append("LF_INSIDE")
    if not v: flags.append("EMPTY")
    print(f"  {name:<18} {repr_show:<32} {' '.join(flags) if flags else '(clean)'}")


print("\n── 환경변수 상태 (값 마스킹) ──")
show("ODOO_URL")        # URL 은 도메인까지 노출돼도 무방하지만 일단 통일
show("ODOO_DB")
show("ODOO_USERNAME")
show("ODOO_API_KEY", mask_keep_prefix=4)


print("\n── 실제 인증 시도 ──")
from mcp_server.services import odoo_service  # noqa: E402

ok = odoo_service.authenticate_odoo()
status = odoo_service.get_service_status()
print(f"  authenticate_odoo() → {ok}")
print(f"  service_status      → {status}")

print(
    "\n비교 방법:\n"
    "  1) GitHub repo → Settings → Secrets and variables → Actions\n"
    "  2) ODOO_API_KEY 의 첫 4글자 prefix 가 위 출력과 동일한지 확인\n"
    "  3) 다르면 로컬 .env 의 ODOO_API_KEY 가 stale — 갱신된 값으로 교체"
)
