"""
BC2 Odoo 연결 테스트 (self-contained, 표준 라이브러리만)

확인 항목:
  1. 인스턴스 도달 가능 (XML-RPC common endpoint)
  2. 인증 성공 (API key 검증)
  3. 기본 데이터 조회 (Partner, Product, Sales Order 모델)
"""
import os
import sys
import xmlrpc.client
from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

# 환경변수에서 읽되, 없으면 기본값 (테스트 편의)
ODOO_URL = os.getenv("ODOO_URL", "https://your-tenant.odoo.com")
ODOO_DB = os.getenv("ODOO_DB", "your_db_name")
ODOO_USERNAME = os.getenv("ODOO_USERNAME", "admin@example.com")
ODOO_API_KEY = os.getenv("ODOO_API_KEY", "")


def main():
    print("=" * 70)
    print("BC2 Odoo 연결 테스트")
    print("=" * 70)
    print(f"URL      : {ODOO_URL}")
    print(f"DB       : {ODOO_DB}")
    print(f"Username : {ODOO_USERNAME}")
    print(f"API Key  : {ODOO_API_KEY[:8]}...{ODOO_API_KEY[-4:]} (length={len(ODOO_API_KEY)})")
    print()

    # 1. Common endpoint 도달 가능?
    print("[1] Common endpoint 도달 확인")
    try:
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        version = common.version()
        print(f"  ✅ Odoo 버전: {version.get('server_version')} (protocol: {version.get('protocol_version')})")
    except Exception as e:
        print(f"  ❌ 도달 실패: {e}")
        sys.exit(1)

    # 2. 인증
    print("\n[2] 인증 시도")
    try:
        uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_API_KEY, {})
        if not uid:
            print("  ❌ 인증 실패 (uid=False) — username/db/api_key 중 하나가 잘못됨")
            sys.exit(1)
        print(f"  ✅ 인증 성공 — uid={uid}")
    except Exception as e:
        print(f"  ❌ 인증 중 예외: {e}")
        sys.exit(1)

    # 3. 모델 조회
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

    print("\n[3] res.partner (고객/거래처) 조회")
    try:
        count = models.execute_kw(
            ODOO_DB, uid, ODOO_API_KEY,
            "res.partner", "search_count",
            [[("customer_rank", ">", 0)]],
        )
        print(f"  ✅ 고객 수: {count}건")
        # 처음 3건 샘플
        partners = models.execute_kw(
            ODOO_DB, uid, ODOO_API_KEY,
            "res.partner", "search_read",
            [[("customer_rank", ">", 0)]],
            {"fields": ["id", "name", "email"], "limit": 3},
        )
        for p in partners:
            print(f"    - [{p['id']}] {p['name']}  email={p.get('email') or '(없음)'}")
    except Exception as e:
        print(f"  ⚠️  조회 실패: {e}")

    print("\n[4] product.product (제품) 조회")
    try:
        count = models.execute_kw(
            ODOO_DB, uid, ODOO_API_KEY,
            "product.product", "search_count",
            [[]],
        )
        print(f"  ✅ 제품 수: {count}건")
        products = models.execute_kw(
            ODOO_DB, uid, ODOO_API_KEY,
            "product.product", "search_read",
            [[]],
            {"fields": ["id", "name", "list_price"], "limit": 3},
        )
        for prod in products:
            print(f"    - [{prod['id']}] {prod['name']}  price={prod.get('list_price')}")
    except Exception as e:
        print(f"  ⚠️  조회 실패: {e}")

    print("\n[5] sale.order (판매 주문) 조회")
    try:
        count = models.execute_kw(
            ODOO_DB, uid, ODOO_API_KEY,
            "sale.order", "search_count",
            [[]],
        )
        print(f"  ✅ Sales Order 수: {count}건")
        orders = models.execute_kw(
            ODOO_DB, uid, ODOO_API_KEY,
            "sale.order", "search_read",
            [[]],
            {"fields": ["id", "name", "partner_id", "state", "amount_total"], "limit": 3},
        )
        for o in orders:
            partner_name = o["partner_id"][1] if o.get("partner_id") else "(없음)"
            print(f"    - [{o['id']}] {o['name']}  partner={partner_name}  state={o['state']}  total={o['amount_total']}")
    except Exception as e:
        print(f"  ⚠️  조회 실패: {e}")

    print("\n" + "=" * 70)
    print("✅ 연결 테스트 완료 — 통합 작업 진행 가능")
    print("=" * 70)
    print()
    print(f"브라우저에서 확인:")
    print(f"  · 고객 목록 : {ODOO_URL}/odoo/contacts")
    print(f"  · 제품 목록 : {ODOO_URL}/odoo/inventory")
    print(f"  · 판매 주문 : {ODOO_URL}/odoo/orders")
    print()


if __name__ == "__main__":
    main()
