# scripts/bc3_delete_test_sos.py
"""
S00012 ~ S00023 (총 12건) SO 일괄 삭제.

confirmed (state='sale') SO 는 unlink 직접 불가:
   1. action_cancel  → state='cancel' (picking 도 함께 cancel)
   2. unlink         → DB 에서 제거

None 반환 marshaller 이슈 (Odoo SaaS trial):
   wrap-and-verify — 호출 후 state 다시 read 해서 검증.

옵션: --keep-cancelled 플래그 추가 가능 (= cancel 만 하고 unlink 안 함).
     기본은 cancel + unlink.
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


TARGETS = [f"S{i:05d}" for i in range(12, 24)]  # S00012..S00023


def banner(s):
    print("\n" + "=" * 78 + "\n " + s + "\n" + "=" * 78)


def safe_call(model, method, *args, **kwargs):
    """None 반환 marshaller 이슈를 try/except 로 흡수 — side effect 는 일어남."""
    try:
        return odoo_service.call(model, method, *args, **kwargs)
    except Exception as e:
        msg = str(e)
        if "cannot marshal None" in msg:
            return None  # 정상 — None 반환 함수의 marshaller 이슈
        raise


def main():
    if not odoo_service.is_available():
        odoo_service.authenticate_odoo()

    banner(f"1. 대상 SO 조회 ({len(TARGETS)} 건: {TARGETS[0]}~{TARGETS[-1]})")
    so_ids = odoo_service.call(
        "sale.order", "search", [("name", "in", TARGETS)],
    )
    if not so_ids:
        print("  (대상 SO 없음 — 이미 삭제됐거나 이름 mismatch)")
        return 0

    sos = odoo_service.call(
        "sale.order", "read", so_ids,
        fields=["id", "name", "state", "picking_ids"],
    )
    print(f"  발견: {len(sos)} 건")
    for s in sorted(sos, key=lambda x: x["name"]):
        pcount = len(s.get("picking_ids") or [])
        print(f"    · {s['name']:8s}  id={s['id']:3d}  state={s['state']!r:10s}  pickings={pcount}")

    # 2. cancel — sale.order.action_cancel 가 picking 도 함께 cancel
    banner("2. SO + picking 일괄 cancel")
    cancel_results = []
    for s in sorted(sos, key=lambda x: x["name"]):
        safe_call("sale.order", "action_cancel", [s["id"]])
        # 검증 — re-read
        after = odoo_service.call(
            "sale.order", "read", [s["id"]], fields=["state"],
        )[0]
        cancelled = after.get("state") == "cancel"
        cancel_results.append((s, cancelled, after.get("state")))
        mark = "✅" if cancelled else "⚠️"
        print(f"  {mark} {s['name']:8s}  state → {after['state']!r}")

    # 3. unlink (DB 제거) — cancelled 만 시도
    banner("3. unlink (DB 제거)")
    to_unlink = [s["id"] for s, ok, _ in cancel_results if ok]
    skip_unlink = [s["name"] for s, ok, _ in cancel_results if not ok]

    if skip_unlink:
        print(f"  cancel 안 된 것 unlink skip: {skip_unlink}")

    unlinked = 0
    for so_id in to_unlink:
        try:
            safe_call("sale.order", "unlink", [so_id])
        except Exception as e:
            print(f"  ❌ id={so_id} unlink 실패: {e}")
            continue
        # 검증 — search 해서 사라졌는지
        still = odoo_service.call("sale.order", "search", [("id", "=", so_id)])
        if not still:
            unlinked += 1
        else:
            print(f"  ⚠️ id={so_id} unlink call 끝났지만 record 존재 — constraint?")

    # 4. 결과 요약
    banner("4. 결과")
    print(f"  처리 대상: {len(sos)}")
    print(f"  cancelled: {sum(1 for _,ok,_ in cancel_results if ok)}")
    print(f"  unlinked  : {unlinked}")

    # 5. 남은 SO 확인
    banner("5. 남은 전체 SO (state='sale')")
    remaining = odoo_service.call(
        "sale.order", "search_read",
        [("state", "=", "sale")],
        fields=["id", "name", "partner_id"],
        order="id",
    )
    print(f"  {len(remaining)} 건:")
    for s in remaining:
        partner = s["partner_id"][1] if isinstance(s["partner_id"], list) else "?"
        print(f"    · {s['name']:8s}  id={s['id']:3d}  partner={partner!r}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
