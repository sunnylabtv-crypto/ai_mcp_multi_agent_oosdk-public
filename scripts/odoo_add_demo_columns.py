# scripts/odoo_add_demo_columns.py
"""
판매(견적서/주문) list 에 **고객 Tier** + **USB 수량** 컬럼을 추가.

둘 다 sale.order 의 기본 필드가 아니므로 manual computed 필드를 만든다:
  · x_customer_tier (Char)  — 고객(res.partner)의 category 태그에서 VIP/Standard/Bronze 추출
  · x_usb_qty       (Integer) — 주문 라인 중 'USB SecureKey' 품목 수량 합
그리고 견적서 list(sale.view_quotation_tree=1126) + 주문 list(sale.view_order_tree=1123)
를 상속해 두 컬럼을 optional='show' 로 노출 (사용자가 컬럼 토글 가능).

Studio 불필요 (이 인스턴스는 web_studio 미설치). 되돌리기: --remove.

실행:
    python scripts/odoo_add_demo_columns.py            # 추가
    python scripts/odoo_add_demo_columns.py --remove   # 원복(필드/뷰 삭제)
"""
import argparse
import sys
from pathlib import Path

for _s in ("stdout", "stderr"):
    try:
        getattr(sys, _s).reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from mcp_server.services import odoo_service as o  # noqa: E402

MODEL = "sale.order"
QUOTE_VIEW = 1126   # sale.view_quotation_tree (견적서)
ORDER_VIEW = 1123   # sale.view_order_tree (주문)

TIER_COMPUTE = (
    "for record in self:\n"
    "    tier = 'Standard'\n"
    "    if record.partner_id:\n"
    "        names = record.partner_id.category_id.mapped('name')\n"
    "        if 'VIP' in names:\n"
    "            tier = 'VIP'\n"
    "        elif 'Bronze' in names:\n"
    "            tier = 'Bronze'\n"
    "    record['x_customer_tier'] = tier\n"
)
USB_COMPUTE = (
    "for record in self:\n"
    "    total = 0\n"
    "    for line in record.order_line:\n"
    "        if line.product_id and 'USB SecureKey' in (line.product_id.name or ''):\n"
    "            total += line.product_uom_qty\n"
    "    record['x_usb_qty'] = int(total)\n"
)

FIELDS = [
    {"name": "x_customer_tier", "field_description": "고객 Tier", "ttype": "char",
     "compute": TIER_COMPUTE, "depends": "partner_id,partner_id.category_id"},
    {"name": "x_usb_qty", "field_description": "USB Security Key수량", "ttype": "integer",
     "compute": USB_COMPUTE, "depends": "order_line.product_id,order_line.product_uom_qty"},
]

INHERIT_NAME_PREFIX = "demo.tier_usb"   # 우리가 만든 상속뷰 식별용


def _model_id():
    return o.call("ir.model", "search", [("model", "=", MODEL)])[0]


def add():
    mid = _model_id()
    # 1) 필드 생성 (멱등)
    for f in FIELDS:
        existing = o.call("ir.model.fields", "search",
                          [("model", "=", MODEL), ("name", "=", f["name"])])
        if existing:
            print(f"  · 필드 {f['name']} 이미 있음 (skip)")
            continue
        fid = o.call("ir.model.fields", "create", [{
            "name": f["name"],
            "field_description": f["field_description"],
            "model_id": mid,
            "model": MODEL,
            "ttype": f["ttype"],
            "state": "manual",
            "store": False,          # 비저장 계산 — 대량 recompute 없음, 표시용
            "compute": f["compute"],
            "depends": f["depends"],
            "readonly": True,
        }])
        print(f"  ✅ 필드 생성 {f['name']} (id={fid})")

    # 2) list 뷰 상속해 컬럼 추가 (멱등)
    targets = [
        (QUOTE_VIEW, "state", "견적서"),
        (ORDER_VIEW, "invoice_status", "주문"),
    ]
    for view_id, anchor, label in targets:
        vname = f"{INHERIT_NAME_PREFIX}.{view_id}"
        if o.call("ir.ui.view", "search", [("name", "=", vname)]):
            print(f"  · 상속뷰 {vname} 이미 있음 (skip)")
            continue
        # 고객 Tier 는 고객(partner_id) 바로 옆에, USB 수량은 anchor(상태 등) 앞에.
        arch = (
            f'<data>'
            f'<xpath expr="//field[@name=\'partner_id\']" position="after">'
            f'<field name="x_customer_tier" optional="show"/>'
            f'</xpath>'
            f'<xpath expr="//field[@name=\'{anchor}\']" position="before">'
            f'<field name="x_usb_qty" optional="show"/>'
            f'</xpath>'
            f'</data>'
        )
        try:
            vid = o.call("ir.ui.view", "create", [{
                "name": vname,
                "model": MODEL,
                "inherit_id": view_id,
                "arch_base": arch,
            }])
            print(f"  ✅ {label} list 컬럼 추가 (상속뷰 id={vid})")
        except Exception as e:
            print(f"  ⚠️ {label}(view {view_id}) 상속 실패: {str(e)[:160]}")
    print("\n완료. Odoo 브라우저 새로고침(F5) → 판매 list 에 '고객 Tier'/'USB 수량' 컬럼.")
    print("컬럼 안 보이면 list 우상단 컬럼옵션(슬라이더) 아이콘에서 토글.")


def remove():
    # 상속뷰 먼저 삭제
    vids = o.call("ir.ui.view", "search",
                  [("name", "like", INHERIT_NAME_PREFIX)])
    if vids:
        o.call("ir.ui.view", "unlink", vids)   # ids 직접 전달 (감싸면 nested list 오류)
        print(f"  ✅ 상속뷰 {len(vids)}건 삭제: {vids}")
    for f in FIELDS:
        fids = o.call("ir.model.fields", "search",
                      [("model", "=", MODEL), ("name", "=", f["name"])])
        if fids:
            o.call("ir.model.fields", "unlink", fids)
            print(f"  ✅ 필드 {f['name']} 삭제")
    print("\n원복 완료.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--remove", action="store_true", help="필드/뷰 삭제(원복)")
    args = ap.parse_args()
    if not o.is_available():
        o.authenticate_odoo()
    if not o.is_available():
        print("❌ Odoo 미연결")
        return 1
    if args.remove:
        remove()
    else:
        add()
    return 0


if __name__ == "__main__":
    sys.exit(main())
