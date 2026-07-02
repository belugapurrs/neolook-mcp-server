"""
Tier 2 - Analytics tools. Unlike Tier 1 CRUD, these don't just pass through
a Shopify query: they fetch raw order/product data and run pandas
computations server-side, producing insights (trends, ROI, segments) that
plain CRUD-only Shopify MCPs can't.
"""

from typing import Any

from mcp.server.fastmcp import FastMCP

from neolook.engines import analytics_engine as engine
from neolook.shopify_client import ShopifyClient, ShopifyAPIError


def register(mcp: FastMCP, client: ShopifyClient) -> None:
    @mcp.tool(
        description=(
            "Get total revenue and order count over a time window, optionally broken down by day "
            "or by product. Use this for questions like 'how much did we make this month' or "
            "'show me the daily revenue trend'. group_by: 'day' (revenue per day), 'product' "
            "(revenue per product), or 'none' (just the totals)."
        )
    )
    async def revenue_summary(days: int = 30, group_by: str = "none") -> dict[str, Any]:
        try:
            orders_df, items_df = await engine.fetch_order_data(client, days)
            return engine.revenue_summary(orders_df, items_df, group_by)
        except ShopifyAPIError as e:
            return {"error": str(e)}

    @mcp.tool(
        description=(
            "Find the best-selling products over a time window, ranked by revenue or units sold. "
            "Use this for questions like 'what are my top 10 products this month'. by: 'revenue' or "
            "'units'."
        )
    )
    async def top_products(days: int = 30, limit: int = 10, by: str = "revenue") -> dict[str, Any]:
        try:
            _, items_df = await engine.fetch_order_data(client, days)
            return {"products": engine.top_products(items_df, limit, by)}
        except ShopifyAPIError as e:
            return {"error": str(e)}

    @mcp.tool(
        description=(
            "Compute each product's selling pace (units per day) over a window, and how that pace "
            "changed vs. the prior period of the same length (trend_vs_prior_period_pct). Use this "
            "to spot products that are accelerating or slowing down, not just what's popular right now."
        )
    )
    async def sales_velocity(days: int = 30) -> dict[str, Any]:
        try:
            _, items_df = await engine.fetch_order_data(client, days)
            return {"products": engine.sales_velocity(items_df, days)}
        except ShopifyAPIError as e:
            return {"error": str(e)}

    @mcp.tool(
        description=(
            "Find products that are sitting in inventory with little or no recent sales - candidates "
            "for a flash sale or discontinuation. Flags active products with more than 0 units in "
            "stock that sold at most max_units_sold units in the last `days` days."
        )
    )
    async def stale_inventory_report(days: int = 60, max_units_sold: int = 2) -> dict[str, Any]:
        try:
            products_df = await engine.fetch_all_active_products(client)
            _, items_df = await engine.fetch_order_data(client, days)
            return {"stale_products": engine.stale_inventory_report(products_df, items_df, max_units_sold)}
        except ShopifyAPIError as e:
            return {"error": str(e)}

    @mcp.tool(
        description=(
            "Find carts customers started but didn't complete, above min_value, in the last `days` "
            "days - useful for cart-recovery campaigns. IMPORTANT: this uses Shopify's real "
            "abandoned-checkout data when available, but on a development store with no real "
            "shoppers, that list is always empty (abandoned checkouts can only be created by an "
            "actual customer leaving checkout - there's no API to fabricate one). When no real "
            "abandoned checkouts are found, this tool falls back to open (incomplete) draft orders "
            "as a stand-in signal, and clearly labels the result with which data source was used."
        )
    )
    async def abandoned_checkout_report(min_value: float = 0, days: int = 30) -> dict[str, Any]:
        try:
            return await engine.fetch_cart_recovery_candidates(client, min_value, days)
        except ShopifyAPIError as e:
            return {"error": str(e)}

    @mcp.tool(
        description=(
            "Measure the return on investment of each discount code used in the last `days` days: "
            "how many orders used it, the revenue those orders generated, the total discount amount "
            "given away, and the ratio of discount-given to revenue-generated. Use this to see which "
            "promo codes are actually paying for themselves."
        )
    )
    async def discount_roi(days: int = 90) -> dict[str, Any]:
        try:
            orders_df, _ = await engine.fetch_order_data(client, days)
            return {"discounts": engine.discount_roi(orders_df)}
        except ShopifyAPIError as e:
            return {"error": str(e)}

    @mcp.tool(
        description=(
            "Measure customer loyalty over the last `days` days: how many customers bought exactly "
            "once vs. more than once, and what share of total revenue came from repeat customers. "
            "Use this for questions like 'how much of our revenue comes from repeat buyers'."
        )
    )
    async def customer_repeat_rate(days: int = 90) -> dict[str, Any]:
        try:
            orders_df, _ = await engine.fetch_order_data(client, days)
            return engine.customer_repeat_rate(orders_df)
        except ShopifyAPIError as e:
            return {"error": str(e)}
