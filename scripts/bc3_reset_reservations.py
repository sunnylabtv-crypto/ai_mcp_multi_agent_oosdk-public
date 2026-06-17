# scripts/bc3_reset_reservations.py
"""
시뮬레이션 reset — 모든 outgoing picking 의 reservation 만 해제 (on_hand 그대로 유지).

상태 전이:
    stock.move.state:
       assigned, partially_available  →  confirmed
    stock.quant.reserved_quantity:
       N  →  0  (해당 move 분만큼 풀림)
    stock.quant.quantity (= on_hand):
       변화 없음 ★

이후 effects:
    · 모든 가용 stock 다시 free 됨 (available = on_hand)
    · 모든 SO picking 다시 state='confirmed' (waiting)
    · 시뮬레이션 재실행 가능 — 새 trigger 호출 시 VIP-first 부터 다시 분배
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


def banner(s):
    print("\n" + "=" * 78 + "\n " + s + "\n" + "=" * 78)


def main():
    if not odoo_service.is_available():
        odoo_service.authenticate_odoo()

    # 1. reservation 풀어야 할 outgoing picking 찾기
    banner("1. 현재 reserve 된 outgoing pickings 조회")
    pids = odoo_service.call(
        "stock.picking", "search",
        [
            ("picking_type_id.code", "=", "outgoing"),
            ("state", "in", ["assigned", "partially_available"]),
        ],
    )
    if not pids:
        print("  (reserve 된 outgoing picking 없음 — 이미 깨끗한 상태)")
        return 0

    picks = odoo_service.call(
        "stock.picking", "read", pids,
        fields=["name", "state", "origin"],
    )
    print(f"  {len(picks)} 건:")
    for p in picks:
        print(f"    · {p['name']:15s}  state={p['state']!r:25s}  origin={p['origin']!r}")

    # 2. 각 picking 에 do_unreserve 호출
    banner("2. do_unreserve 일괄 호출")
    success = 0
    fail = 0
    for p in picks:
        try:
            odoo_service.call("stock.picking", "do_unreserve", [p["id"]])
            success += 1
            print(f"  ✅ {p['name']}")
        except Exception as e:
            fail += 1
            print(f"  ❌ {p['name']}: {e}")

    # 3. 결과 검증
    banner("3. 결과 — 모든 outgoing picking state 확인")
    after = odoo_service.call(
        "stock.picking", "read", pids,
        fields=["name", "state"],
    )
    for p in after:
        mark = "✅" if p["state"] == "confirmed" else "⚠️"
        print(f"  {mark} {p['name']:15s}  state={p['state']!r}")

    # 4. inventory 상태
    banner("4. USB SecureKey inventory")
    inv = odoo_service.get_inventory_state(2)
    print(f"  on_hand   = {inv['on_hand']:.0f}")
    print(f"  reserved  = {inv['reserved']:.0f}  ← 0 으로 떨어졌어야 함")
    print(f"  available = {inv['available']:.0f}  ← on_hand 와 같아야 함 (전부 free)")

    print(f"\n  처리: 성공 {success} / 실패 {fail}")
    print("""
이제 시뮬레이션 재실행 가능:
   · "USB N PO+trigger" — 새 입고 + VIP-first 분배
   · 또는 그냥 trigger 만 — 현재 가용 (on_hand 만큼) VIP first 분배
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
