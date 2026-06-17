# mcp_server/tools/erp_tools.py
"""
ERP (Odoo) MCP 도구들
─────────────────────────────────────────────
Claude Desktop 이 직접 호출할 수 있는 Odoo 조회/검증 도구.

설계 의도:
  · tools/ = MCP 표면에 노출되는 얇은 wrapper.
  · 비즈니스 액션 (create_sales_order 등) 은 erp_agent.py 의 정책 action 으로 남기고,
    여기는 read-only 또는 멱등 검증 도구만 둔다.
  · 모든 호출은 services.odoo_service 를 거친다 (직접 XML-RPC 호출 금지).
"""
import logging
from typing import Dict, Any, Optional

from ..services import odoo_service

logger = logging.getLogger(__name__)


def register_erp_tools(mcp):
    """Odoo ERP 도구들을 MCP 서버에 등록"""

    @mcp.tool()
    def get_odoo_status() -> Dict[str, Any]:
        """
        Odoo ERP 연결 상태를 확인합니다 (인증 시도 포함).

        Returns:
            {"connected": bool, "uid"?: int, "url"?: str, "db"?: str, "reason"?: str}

        Example:
            get_odoo_status()
            # {"connected": true, "uid": 7, "url": "https://your-tenant.odoo.com", "db": "your_db_name"}
        """
        logger.info("🔌 Odoo 상태 확인 요청")
        # is_available 은 필요 시 인증 시도까지 함
        odoo_service.is_available()
        return odoo_service.get_service_status()

    @mcp.tool()
    def find_existing_sales_order(opp_name: str) -> Dict[str, Any]:
        """
        SFDC Opportunity 이름 (client_order_ref) 으로 이미 push 된 Odoo SO 조회.
        멱등성 검증 / 중복 방지에 사용.

        Args:
            opp_name: SFDC Opportunity Name (예: "VIP Tech - Module X 도입 검토")

        Returns:
            {"found": bool, "order"?: {name, state, amount_total, ...}, "reason"?: str}

        Example:
            find_existing_sales_order(opp_name="VIP Tech - Module X 도입 검토")
        """
        logger.info(f"🔍 기존 SO 조회: {opp_name}")
        try:
            order = odoo_service.find_existing_sales_order(opp_name)
            if order:
                return {"found": True, "order": order}
            return {"found": False, "reason": "no matching client_order_ref"}
        except RuntimeError as e:
            return {"found": False, "reason": str(e)}

    # ─────────────────────────────────────────────────────────
    # BC3 inventory inspection tools (read-only — Claude 가 직접 조회용)
    # ─────────────────────────────────────────────────────────
    @mcp.tool()
    def list_order_deliveries(sale_order_id: int) -> Dict[str, Any]:
        """
        주어진 Sales Order ID 의 stock.picking (Delivery Order) 목록 반환.
        BC3 inventory 분기에서 SO 가 자동 생성한 DO 들의 상태 확인용.

        Args:
            sale_order_id: Odoo sale.order ID

        Returns:
            {"pickings": [{name, state, scheduled_date, ...}], "count": int}
            state 값: draft / waiting / confirmed / assigned / done / cancel

        Example:
            list_order_deliveries(sale_order_id=42)
        """
        logger.info(f"📦 SO #{sale_order_id} 의 Delivery Order 조회")
        try:
            pickings = odoo_service.list_pickings_for_order(sale_order_id)
            return {"pickings": pickings, "count": len(pickings)}
        except RuntimeError as e:
            return {"pickings": [], "count": 0, "reason": str(e)}

    @mcp.tool()
    def get_product_stock(product_id: int) -> Dict[str, Any]:
        """
        제품의 현재 재고 분포 (모든 location 의 stock.quant).
        BC3 의 가용재고 / 할당가능 재고 시각화 진입점.

        Args:
            product_id: Odoo product.product ID

        Returns:
            {
              "product_id": int,
              "quants": [{location_id, quantity, reserved_quantity, available_quantity}],
              "totals": {on_hand: float, reserved: float, available: float}
            }

        Example:
            get_product_stock(product_id=15)
        """
        logger.info(f"📊 제품 #{product_id} 재고 분포 조회")
        try:
            quants = odoo_service.get_product_quants(product_id)
            on_hand = sum(q.get("quantity", 0) for q in quants)
            reserved = sum(q.get("reserved_quantity", 0) for q in quants)
            available = on_hand - reserved
            return {
                "product_id": product_id,
                "quants": quants,
                "totals": {
                    "on_hand": on_hand,
                    "reserved": reserved,
                    "available": available,
                },
            }
        except RuntimeError as e:
            return {"product_id": product_id, "quants": [], "totals": {}, "reason": str(e)}

    logger.info("✅ ERP (Odoo) 도구 등록 완료 — 4개")
