"""
Tier 3 - Agentic commerce tools. Unlike Tiers 1-2 (single reads/writes and
read-only analysis), these each execute a small multi-step workflow -
finding candidates, taking an action, and reporting what happened - in one
tool call. This is what "beyond CRUD" means in practice: an agent can say
"run a flash sale on my slow movers" and get one tool call instead of
orchestrating five.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from neolook.engines import analytics_engine as engine
from neolook.shopify_client import ShopifyClient, ShopifyAPIError


class CheckoutLineItem(BaseModel):
    variant_id: str = Field(description="Product variant GID, e.g. gid://shopify/ProductVariant/123")
    quantity: int = Field(default=1, description="Number of units")


def register(mcp: FastMCP, client: ShopifyClient) -> None:
    @mcp.tool(
        description=(
            "Autonomous cart-recovery workflow: finds abandoned carts worth at least min_cart_value "
            "in the last 30 days, creates ONE scoped discount code (discount_percentage off, valid "
            "for expires_in_days), and drafts a personalized recovery email (subject + body text) "
            "for each affected customer referencing that code. Does NOT send any email - it only "
            "prepares drafts for a human (or another tool) to send. Returns a step-by-step report of "
            "what was found and created. Falls back to open draft orders when no real abandoned "
            "checkouts exist (typical on a dev store - see abandoned_checkout_report)."
        )
    )
    async def recover_abandoned_carts(
        min_cart_value: float = 100, discount_percentage: float = 10, expires_in_days: int = 7
    ) -> dict[str, Any]:
        try:
            steps: list[str] = []
            candidates = await engine.fetch_cart_recovery_candidates(client, min_cart_value, days=30)
            steps.append(
                f"Searched for carts worth >= ${min_cart_value:.2f} (source: {candidates['source']}) -> found {candidates['count']}."
            )

            if candidates["count"] == 0:
                steps.append("No qualifying carts found - nothing to recover.")
                return {"steps": steps, "discount_code": None, "emails": []}

            code = f"COMEBACK{discount_percentage:g}-{uuid.uuid4().hex[:6].upper()}"
            starts_at = datetime.now(timezone.utc).isoformat()
            ends_at = (datetime.now(timezone.utc) + timedelta(days=expires_in_days)).isoformat()

            body = await client.mutate(
                """
                mutation CreateDiscount($basicCodeDiscount: DiscountCodeBasicInput!) {
                  discountCodeBasicCreate(basicCodeDiscount: $basicCodeDiscount) {
                    codeDiscountNode { id codeDiscount { ... on DiscountCodeBasic { codes(first: 1) { edges { node { code } } } } } }
                    userErrors { field message }
                  }
                }
                """,
                {
                    "basicCodeDiscount": {
                        "title": code,
                        "code": code,
                        "startsAt": starts_at,
                        "endsAt": ends_at,
                        "context": {"all": "ALL"},
                        "customerGets": {"value": {"percentage": discount_percentage / 100}, "items": {"all": True}},
                    }
                },
                invalidate_namespaces=["discounts"],
            )
            payload = body["data"]["discountCodeBasicCreate"]
            if payload["userErrors"]:
                steps.append(f"Failed to create discount code: {payload['userErrors']}")
                return {"steps": steps, "discount_code": None, "emails": []}
            steps.append(f"Created discount code {code} ({discount_percentage:g}% off, expires {ends_at}).")

            emails = []
            for checkout in candidates["checkouts"]:
                name = checkout.get("customer_name") or "there"
                items_desc = ", ".join(f"{li['quantity']}x {li['title']}" for li in checkout["line_items"]) or "your cart"
                emails.append(
                    {
                        "customer_id": checkout.get("customer_id"),
                        "customer_email": checkout.get("customer_email"),
                        "subject": f"You left something behind, {name.split()[0] if name != 'there' else 'there'}!",
                        "body": (
                            f"Hi {name},\n\nWe noticed you left {items_desc} in your cart "
                            f"(${checkout['value']:.2f}). Use code {code} for {discount_percentage:g}% off "
                            f"if you complete your order in the next {expires_in_days} days.\n\n"
                            f"Finish checking out: {checkout['recovery_url']}\n\nThanks!"
                        ),
                    }
                )
            steps.append(f"Drafted {len(emails)} recovery email(s) (not sent).")

            return {"steps": steps, "discount_code": code, "emails": emails}
        except ShopifyAPIError as e:
            return {"error": str(e)}

    @mcp.tool(
        description=(
            "Autonomous flash-sale workflow: finds the product_count stalest-selling products (using "
            "the same logic as stale_inventory_report), creates a new collection containing them, and "
            "creates a discount code scoped to that collection for discount_percentage off, active for "
            "duration_hours. Returns the plan plus the created collection/discount IDs."
        )
    )
    async def create_flash_sale(product_count: int = 5, discount_percentage: float = 20, duration_hours: int = 48) -> dict[str, Any]:
        try:
            steps: list[str] = []
            products_df = await engine.fetch_all_active_products(client)
            _, items_df = await engine.fetch_order_data(client, days=60)
            stale = engine.stale_inventory_report(products_df, items_df, max_units_sold=10_000)  # rank all, we'll slice
            chosen = stale[:product_count]
            steps.append(f"Identified {len(chosen)} stalest-selling product(s) out of {len(products_df)} active products.")

            if not chosen:
                steps.append("No products available - nothing to put on sale.")
                return {"steps": steps, "collection_id": None, "discount_code": None}

            product_ids = [p["product_id"] for p in chosen]
            collection_title = f"Flash Sale {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
            body = await client.mutate(
                """
                mutation CreateCollection($input: CollectionInput!) {
                  collectionCreate(input: $input) { collection { id title } userErrors { field message } }
                }
                """,
                {"input": {"title": collection_title, "products": product_ids}},
                invalidate_namespaces=["collections"],
            )
            payload = body["data"]["collectionCreate"]
            if payload["userErrors"]:
                steps.append(f"Failed to create collection: {payload['userErrors']}")
                return {"steps": steps, "collection_id": None, "discount_code": None}
            collection_id = payload["collection"]["id"]
            steps.append(f"Created collection '{collection_title}' ({collection_id}) with {len(product_ids)} product(s).")

            code = f"FLASH{discount_percentage:g}-{uuid.uuid4().hex[:6].upper()}"
            starts_at = datetime.now(timezone.utc).isoformat()
            ends_at = (datetime.now(timezone.utc) + timedelta(hours=duration_hours)).isoformat()
            body = await client.mutate(
                """
                mutation CreateDiscount($basicCodeDiscount: DiscountCodeBasicInput!) {
                  discountCodeBasicCreate(basicCodeDiscount: $basicCodeDiscount) {
                    codeDiscountNode { id codeDiscount { ... on DiscountCodeBasic { codes(first: 1) { edges { node { code } } } } } }
                    userErrors { field message }
                  }
                }
                """,
                {
                    "basicCodeDiscount": {
                        "title": collection_title,
                        "code": code,
                        "startsAt": starts_at,
                        "endsAt": ends_at,
                        "context": {"all": "ALL"},
                        "customerGets": {
                            "value": {"percentage": discount_percentage / 100},
                            "items": {"collections": {"add": [collection_id]}},
                        },
                    }
                },
                invalidate_namespaces=["discounts"],
            )
            payload = body["data"]["discountCodeBasicCreate"]
            if payload["userErrors"]:
                steps.append(f"Failed to create discount code: {payload['userErrors']}")
                return {"steps": steps, "collection_id": collection_id, "discount_code": None}
            steps.append(f"Created discount code {code} ({discount_percentage:g}% off, {duration_hours}h, scoped to the new collection).")

            return {
                "steps": steps,
                "collection_id": collection_id,
                "collection_title": collection_title,
                "products": chosen,
                "discount_code": code,
                "expires_at": ends_at,
            }
        except ShopifyAPIError as e:
            return {"error": str(e)}

    @mcp.tool(
        description=(
            "Create a real, payable checkout link for a specific set of items - the 'autonomous "
            "checkout' capability. Builds a draft order from line_items (variant_id + quantity), "
            "optionally attaches customer_email and a discount_code, and returns the invoiceUrl a "
            "customer can open to pay immediately. Use this when an agent needs to hand someone a "
            "direct way to complete a purchase (e.g. after a support conversation)."
        )
    )
    async def create_checkout_link(
        line_items: list[CheckoutLineItem], customer_email: str | None = None, discount_code: str | None = None
    ) -> dict[str, Any]:
        try:
            draft_input: dict[str, Any] = {
                "lineItems": [{"variantId": li.variant_id, "quantity": li.quantity} for li in line_items]
            }
            if customer_email:
                draft_input["email"] = customer_email
            if discount_code:
                draft_input["discountCodes"] = [discount_code]

            body = await client.mutate(
                """
                mutation CreateDraftOrder($input: DraftOrderInput!) {
                  draftOrderCreate(input: $input) {
                    draftOrder { id name invoiceUrl totalPriceSet { shopMoney { amount currencyCode } } }
                    userErrors { field message }
                  }
                }
                """,
                {"input": draft_input},
                invalidate_namespaces=["orders"],
            )
            payload = body["data"]["draftOrderCreate"]
            if payload["userErrors"]:
                return {"error": payload["userErrors"]}
            draft = payload["draftOrder"]
            return {
                "draft_order_id": draft["id"],
                "name": draft["name"],
                "checkout_url": draft["invoiceUrl"],
                "total": {"amount": draft["totalPriceSet"]["shopMoney"]["amount"], "currency": draft["totalPriceSet"]["shopMoney"]["currencyCode"]},
            }
        except ShopifyAPIError as e:
            return {"error": str(e)}

    @mcp.tool(
        description=(
            "HEURISTIC pricing suggestions (not machine learning): flags fast-selling products with "
            "no current discount as raise-price candidates, and slow-selling overstocked products as "
            "discount candidates, each with the reasoning behind the flag. This is simple rule-based "
            "analysis over sales_velocity and stale_inventory_report data, not a trained pricing model."
        )
    )
    async def price_optimization_suggestions(days: int = 30) -> dict[str, Any]:
        try:
            orders_df, items_df = await engine.fetch_order_data(client, days)
            velocity = engine.sales_velocity(items_df, days)
            products_df = await engine.fetch_all_active_products(client)
            stale = engine.stale_inventory_report(products_df, items_df, max_units_sold=2)

            discounted_titles = set()
            for _, order in orders_df.iterrows():
                if order["discount_codes"]:
                    discounted_titles.update(items_df[items_df["order_id"] == order["order_id"]]["product_title"])

            raise_candidates = [
                {
                    "product_title": v["product_title"],
                    "units_per_day": v["units_per_day"],
                    "trend_vs_prior_period_pct": v["trend_vs_prior_period_pct"],
                    "reasoning": f"Selling {v['units_per_day']}/day with a {v['trend_vs_prior_period_pct']}% trend and no discount applied in this window - demand may support a higher price.",
                }
                for v in velocity
                if v["units_per_day"] > 0 and v["product_title"] not in discounted_titles
            ][:10]

            discount_candidates = [
                {
                    "product_title": s["product_title"],
                    "units_in_stock": s["units_in_stock"],
                    "units_sold_in_window": s["units_sold_in_window"],
                    "reasoning": f"{s['units_in_stock']} units in stock but only {s['units_sold_in_window']} sold in the last {days} days - a discount could clear inventory.",
                }
                for s in stale
            ][:10]

            return {"raise_candidates": raise_candidates, "discount_candidates": discount_candidates}
        except ShopifyAPIError as e:
            return {"error": str(e)}

    @mcp.tool(
        description=(
            "Return server-side request metrics: how many requests the tools wanted to make "
            "(requests_attempted), how many actually went to Shopify (requests_sent_to_shopify), how "
            "many were served from cache (cache_hits), and how many throttle events occurred. Use "
            "this to check caching effectiveness or diagnose rate-limit behavior."
        )
    )
    async def get_server_metrics() -> dict[str, Any]:
        return client.get_metrics()
