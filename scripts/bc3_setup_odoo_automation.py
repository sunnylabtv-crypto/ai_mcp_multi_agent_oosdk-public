# scripts/bc3_setup_odoo_automation.py
"""
Odoo Automated Action 을 XML-RPC 로 1-shot 등록 (UI 클릭 우회).

배포 흐름
─────────
이 스크립트는 your-tenant.odoo.com 의 base.automation 모델에 record 를 create/update 한다.
실행 직후부터 Odoo 가 stock.picking 의 state='done' + picking_type='incoming' 전이 시
우리 VM 의 /webhook/stock_received 로 자동 POST → ontology engine 발화 → VIP 우선 reserve.

idempotent — 같은 name 의 action 이 이미 있으면 code/active 만 update.

실행:
    python scripts/bc3_setup_odoo_automation.py

요구 권한:
    .env 의 ODOO_USERNAME 이 base.group_erp_manager / base.group_system 권한 보유 필요
    (보통 admin 또는 그에 준하는 user — 본인 계정이면 OK).
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from mcp_server.services import odoo_service  # noqa: E402


# ─── 설정 ──────────────────────────────────────────────────────────────────

WEBHOOK_URL = "http://REDACTED_VM_IP:9101/webhook/stock_received"
WEBHOOK_TOKEN = ""
ACTION_NAME = "BC3: stock receipt -> MCP webhook"

# Odoo Automated Action 의 Python Code body.
# 컨텍스트 변수:
#   · records  : 트리거된 stock.picking recordset
#   · env, model, log 등 표준 Odoo server action env
PYTHON_CODE = f'''# BC3 — stock.picking 의 입고 검증 완료 (state=done, incoming)
# → 우리 GCP VM 의 webhook 호출 → ontology engine 자동 발화
import requests

WEBHOOK_URL = "{WEBHOOK_URL}"
WEBHOOK_TOKEN = "{WEBHOOK_TOKEN}"

for picking in records:
    for move in picking.move_ids:
        if move.product_id and move.quantity > 0:
            try:
                requests.post(
                    WEBHOOK_URL,
                    json={{
                        "picking_id": picking.id,
                        "product_id": move.product_id.id,
                        "qty": move.quantity,
                        "source_note": picking.name or "",
                    }},
                    headers={{"X-Webhook-Token": WEBHOOK_TOKEN}},
                    timeout=5,
                )
            except Exception:
                # webhook 실패는 picking validate 자체를 막지 않음 — 안전 default.
                # BC4 의 cron 이 drop 된 picking 을 sweep 으로 reconcile 예정.
                pass
'''


def banner(s):
    print("\n" + "=" * 78 + "\n " + s + "\n" + "=" * 78)


def main():
    if not odoo_service.is_available():
        ok = odoo_service.authenticate_odoo()
        if not ok:
            print("ERROR: Odoo 인증 실패")
            return 1

    # 1. stock.picking 모델 id 조회
    banner("1. stock.picking model_id 조회")
    model_ids = odoo_service.call("ir.model", "search", [("model", "=", "stock.picking")])
    if not model_ids:
        print("ERROR: ir.model 에서 stock.picking 못 찾음")
        return 1
    model_id = model_ids[0]
    print(f"  model_id = {model_id}")

    # 2. state field_id 조회 (trigger_field_ids 에 사용)
    banner("2. stock.picking.state field_id 조회")
    field_ids = odoo_service.call(
        "ir.model.fields", "search",
        [("model_id", "=", model_id), ("name", "=", "state")],
    )
    if not field_ids:
        print("ERROR: ir.model.fields 에서 state 못 찾음")
        return 1
    field_id = field_ids[0]
    print(f"  field_id = {field_id}")

    # 3. 동일 name 의 automation 이미 있는지 확인 (idempotent)
    banner(f"3. 기존 automation 확인 (name={ACTION_NAME!r})")
    existing = odoo_service.call(
        "base.automation", "search", [("name", "=", ACTION_NAME)],
    )
    if existing:
        print(f"  기존 발견 — id={existing[0]} → code/active 만 update")
        odoo_service.call(
            "base.automation", "write",
            [existing[0], {"code": PYTHON_CODE, "active": True}],
        )
        action_id = existing[0]
        action_op = "updated"
    else:
        print("  기존 없음 — 새로 create")
        vals = {
            "name": ACTION_NAME,
            "model_id": model_id,
            # Odoo 17+ trigger 옵션:
            #   on_create / on_write / on_create_or_write / on_unlink /
            #   on_change / on_time / on_time_created / on_time_updated
            # state field 변경 시 발화하려면 on_create_or_write + trigger_field_ids 조합.
            "trigger": "on_create_or_write",
            "trigger_field_ids": [(6, 0, [field_id])],  # M2M replace
            "filter_domain": "[('state','=','done'),('picking_type_id.code','=','incoming')]",
            # Odoo server action 의 'state' = action type. 'code' = Python 코드 실행.
            "state": "code",
            "code": PYTHON_CODE,
            "active": True,
        }
        action_id = odoo_service.call("base.automation", "create", [vals])
        action_op = "created"
    print(f"  action_id = {action_id} ({action_op})")

    # 4. 등록 확인 — read 로 다시 가져오기
    banner("4. 등록 검증 — base.automation.read")
    rec = odoo_service.call(
        "base.automation", "read", [action_id],
        fields=["id", "name", "model_id", "trigger", "filter_domain", "state", "active"],
    )
    if rec:
        for k, v in rec[0].items():
            print(f"  {k}: {v!r}")

    banner("✅ 끝")
    print(f"""
이제 Odoo 에서 incoming stock.picking 의 state 가 'done' 으로 전이될 때마다
자동으로 우리 VM 의 webhook 으로 POST 가 갑니다.

테스트:
  · Odoo UI 에서 WH/IN/00010 (SecureGate) 또는 WH/IN/00011 (SmartBox) 검증 클릭
  · 우리 VM 의 /webhook/stock_received 로그 확인 (1초 이내 도착해야 함)
  · Dashboard 의 SO 재고 탭 → S00009 조회 → Assigned (이 SO) 컬럼 채워졌는지

비활성화:
  · 같은 스크립트 다시 실행 후 base.automation write {{active: False}} 또는
  · Odoo UI 의 Automated Actions 에서 active 토글 OFF
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
