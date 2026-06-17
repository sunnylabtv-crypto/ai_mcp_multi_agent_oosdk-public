"""
BC3 fix — VIP Tech / Standard Tech partner 에 res.partner.category (Customer Tag) 부여.

문제:
  · odoo_service.find_or_create_partner 는 tier 를 partner.comment 에 텍스트로만 저장.
  · get_sale_order_tier_map 는 partner.category_id 의 이름을 본다 → 매칭 실패 → default 'Standard'.
  · 결과: BC3 batched allocation / preempt 가 모두 Standard 로 인식되어 tier 우선순위 무시.

이 스크립트는:
  1. res.partner.category 에 'VIP', 'Standard', 'Bronze' tag 가 없으면 create
  2. 'VIP Tech' partner.category_id 에 'VIP' 추가 (없을 때만)
  3. 'Standard Tech' partner.category_id 에 'Standard' 추가 (없을 때만)

Idempotent — 재실행 안전.
"""
import sys
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from mcp_server.services import odoo_service  # noqa: E402


TIER_TAGS = ["VIP", "Standard", "Bronze"]
PARTNER_TIER_MAP = {
    "VIP Tech": "VIP",
    "Standard Tech": "Standard",
}


def ensure_tags() -> Dict[str, int]:
    """tag 이름 → id. 없으면 create."""
    result: Dict[str, int] = {}
    for name in TIER_TAGS:
        ids = odoo_service.call("res.partner.category", "search", [("name", "=", name)])
        if ids:
            result[name] = ids[0]
            print(f"  tag '{name:<8}' exists  id={ids[0]}")
        else:
            new_id = odoo_service.call("res.partner.category", "create", {"name": name})
            result[name] = new_id
            print(f"  tag '{name:<8}' CREATED id={new_id}")
    return result


def attach_tag(partner_name: str, tag_id: int, tag_name: str) -> str:
    pids = odoo_service.call("res.partner", "search", [("name", "=", partner_name)])
    if not pids:
        return f"partner '{partner_name}' 없음 — skip"
    pid = pids[0]
    rec = odoo_service.call(
        "res.partner", "read", [pid], fields=["category_id", "name"],
    )[0]
    current = rec.get("category_id") or []
    if tag_id in current:
        return f"partner id={pid} '{partner_name}' 이미 '{tag_name}' 보유 — skip"
    # many2many write 명령: (4, id) = add link
    odoo_service.call(
        "res.partner", "write", [pid], {"category_id": [(4, tag_id)]},
    )
    return f"partner id={pid} '{partner_name}' ← '{tag_name}' attached"


def main() -> None:
    if not odoo_service.authenticate_odoo():
        print(f"FAIL: {odoo_service.get_service_status()}")
        sys.exit(1)
    print(f"OK: Odoo uid={odoo_service.get_service_status()['uid']}")

    print("\n-- 1. res.partner.category (tags) --")
    tag_ids = ensure_tags()

    print("\n-- 2. partner 에 tag 부여 --")
    for pname, tname in PARTNER_TIER_MAP.items():
        msg = attach_tag(pname, tag_ids[tname], tname)
        print(f"  {msg}")

    print("\n-- 3. verify (get_sale_order_tier_map) --")
    # 우리가 만든 SO 들 (S00008/9 = id 8/9)
    tier_map = odoo_service.get_sale_order_tier_map([8, 9])
    for so_id in (8, 9):
        print(f"  SO {so_id} → tier = {tier_map.get(so_id)!r}")


if __name__ == "__main__":
    main()
