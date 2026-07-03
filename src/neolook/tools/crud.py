"""
Tier 1 - CRUD tools: the baseline operations every Shopify MCP offers
(search/update products, get/list orders, create a discount, adjust
inventory). These are thin wrappers around ShopifyClient - the value-add
tiers are analytics.py and agentic.py.
"""

import uuid
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from neolook.shopify_client import ShopifyClient, ShopifyAPIError


class PriceUpdate(BaseModel):
    variant_id: str = Field(description="The variant's GID, e.g. gid://shopify/ProductVariant/123")
    price: str = Field(description="New price as a decimal string, e.g. '19.99'")


def _money(money_set: dict | None) -> dict | None:
    if not money_set:
        return None
    shop_money = money_set.get("shopMoney", {})
    return {"amount": shop_money.get("amount"), "currency": shop_money.get("currencyCode")}


async def _resolve_order_id(client: ShopifyClient, order_id_or_name: str) -> str | None:
    # Only a full GID is treated as a direct ID. A bare number (e.g. "1009") is
    # the order's display name/number, NOT the opaque internal GID suffix, so
    # it must be resolved via a name search like everything else.
    if order_id_or_name.startswith("gid://shopify/Order/"):
        return order_id_or_name

    name = order_id_or_name if order_id_or_name.startswith("#") else f"#{order_id_or_name}"
    body = await client.query(
        "query FindOrderByName($query: String!) { orders(first: 1, query: $query) { edges { node { id } } } }",
        {"query": f"name:{name}"},
        namespace="orders",
    )
    edges = body.get("data", {}).get("orders", {}).get("edges", [])
    return edges[0]["node"]["id"] if edges else None


def register(mcp: FastMCP, client: ShopifyClient) -> None:
    @mcp.tool(
        description=(
            "Search the store's products using Shopify's search syntax (e.g. 'status:active', "
            "'title:*shirt*', 'tag:sale'). Use this to find products by name, type, vendor, tag, "
            "or status before reading or updating them. Returns a list of matching products with "
            "id, title, status, price range, and up to 5 variants each."
        )
    )
    async def search_products(query: str = "", first: int = 10) -> dict[str, Any]:
        try:
            body = await client.query(
                """
                query SearchProducts($query: String!, $first: Int!) {
                  products(first: $first, query: $query) {
                    edges { node {
                      id title handle status productType vendor tags totalInventory
                      priceRangeV2 { minVariantPrice { amount currencyCode } maxVariantPrice { amount currencyCode } }
                      variants(first: 5) { edges { node { id title price sku inventoryQuantity } } }
                    } }
                    pageInfo { hasNextPage endCursor }
                  }
                }
                """,
                {"query": query, "first": first},
                namespace="products",
            )
            edges = body.get("data", {}).get("products", {}).get("edges", [])
            return {"products": [e["node"] for e in edges], "count": len(edges)}
        except ShopifyAPIError as e:
            return {"error": str(e)}

    @mcp.tool(
        description=(
            "Update a product's title, status (ACTIVE/ARCHIVED/DRAFT), and/or variant prices. "
            "Pass product_id as a GID (gid://shopify/Product/...). Only the fields you provide are "
            "changed. To change prices, pass price_updates as a list of {variant_id, price}. "
            "Returns the updated product, or an error message on failure."
        )
    )
    async def update_product(
        product_id: str,
        title: str | None = None,
        status: str | None = None,
        price_updates: list[PriceUpdate] | None = None,
    ) -> dict[str, Any]:
        try:
            result: dict[str, Any] = {}

            if title is not None or status is not None:
                product_input: dict[str, Any] = {"id": product_id}
                if title is not None:
                    product_input["title"] = title
                if status is not None:
                    product_input["status"] = status.upper()

                body = await client.mutate(
                    """
                    mutation UpdateProduct($product: ProductUpdateInput!) {
                      productUpdate(product: $product) {
                        product { id title status }
                        userErrors { field message }
                      }
                    }
                    """,
                    {"product": product_input},
                    invalidate_namespaces=["products"],
                )
                payload = body["data"]["productUpdate"]
                if payload["userErrors"]:
                    return {"error": payload["userErrors"]}
                result["product"] = payload["product"]

            if price_updates:
                variants = [{"id": p.variant_id, "price": p.price} for p in price_updates]
                body = await client.mutate(
                    """
                    mutation UpdateVariantPrices($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
                      productVariantsBulkUpdate(productId: $productId, variants: $variants) {
                        productVariants { id price }
                        userErrors { field message }
                      }
                    }
                    """,
                    {"productId": product_id, "variants": variants},
                    invalidate_namespaces=["products"],
                )
                payload = body["data"]["productVariantsBulkUpdate"]
                if payload["userErrors"]:
                    return {"error": payload["userErrors"]}
                result["updated_variants"] = payload["productVariants"]

            if not result:
                return {"error": "Nothing to update - provide title, status, and/or price_updates."}
            return result
        except ShopifyAPIError as e:
            return {"error": str(e)}

    @mcp.tool(
        description=(
            "Get full details for one order by its numeric name (e.g. '1001' or '#1001') or GID. "
            "Returns financial/fulfillment status, total price, customer, line items, discount codes, "
            "and tags. Note: Shopify only exposes orders from the last 60 days by default. Returns "
            "an error if the order isn't found."
        )
    )
    async def get_order(order_id_or_name: str) -> dict[str, Any]:
        try:
            order_id = await _resolve_order_id(client, order_id_or_name)
            if not order_id:
                return {"error": f"No order found matching '{order_id_or_name}'"}

            body = await client.query(
                """
                query GetOrder($id: ID!) {
                  order(id: $id) {
                    id name createdAt displayFinancialStatus displayFulfillmentStatus
                    totalPriceSet { shopMoney { amount currencyCode } }
                    customer { id displayName defaultEmailAddress { emailAddress } }
                    lineItems(first: 20) {
                      edges { node { title quantity originalUnitPriceSet { shopMoney { amount currencyCode } } } }
                    }
                    discountCodes tags
                  }
                }
                """,
                {"id": order_id},
                namespace="orders",
            )
            order = body.get("data", {}).get("order")
            if not order:
                return {"error": f"No order found matching '{order_id_or_name}'"}
            order["totalPriceSet"] = _money(order["totalPriceSet"])
            order["lineItems"] = [
                {**e["node"], "originalUnitPriceSet": _money(e["node"]["originalUnitPriceSet"])}
                for e in order["lineItems"]["edges"]
            ]
            return order
        except ShopifyAPIError as e:
            return {"error": str(e)}

    @mcp.tool(
        description=(
            "List recent orders, optionally filtered with Shopify search syntax (e.g. "
            "'financial_status:paid', 'created_at:>2026-01-01'). Returns up to `first` orders "
            "(default 20) with id, name, status, total, and customer. Note: Shopify only exposes "
            "orders from the last 60 days by default."
        )
    )
    async def list_orders(first: int = 20, query: str | None = None) -> dict[str, Any]:
        try:
            body = await client.query(
                """
                query ListOrders($first: Int!, $query: String) {
                  orders(first: $first, query: $query) {
                    edges { node {
                      id name createdAt displayFinancialStatus displayFulfillmentStatus
                      totalPriceSet { shopMoney { amount currencyCode } }
                      customer { displayName defaultEmailAddress { emailAddress } }
                    } }
                    pageInfo { hasNextPage endCursor }
                  }
                }
                """,
                {"first": first, "query": query},
                namespace="orders",
            )
            edges = body.get("data", {}).get("orders", {}).get("edges", [])
            orders = []
            for e in edges:
                node = e["node"]
                node["totalPriceSet"] = _money(node["totalPriceSet"])
                orders.append(node)
            return {"orders": orders, "count": len(orders)}
        except ShopifyAPIError as e:
            return {"error": str(e)}

    @mcp.tool(
        description=(
            "Create a basic percentage-off discount code (e.g. '15% off SUMMER15'). starts_at and "
            "ends_at are ISO-8601 datetimes (ends_at optional = no expiry). min_purchase_amount "
            "optionally requires a minimum cart subtotal. collection_id optionally scopes the "
            "discount to one collection's products only (otherwise applies store-wide). Returns the "
            "created discount's id and code, or an error if the code already exists or inputs are invalid."
        )
    )
    async def create_discount(
        code: str,
        percentage: float,
        starts_at: str,
        ends_at: str | None = None,
        min_purchase_amount: float | None = None,
        collection_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            items: dict[str, Any] = {"all": True}
            if collection_id:
                items = {"collections": {"add": [collection_id]}}

            discount_input: dict[str, Any] = {
                "title": code,
                "code": code,
                "startsAt": starts_at,
                "context": {"all": "ALL"},
                "customerGets": {
                    "value": {"percentage": percentage / 100},
                    "items": items,
                },
            }
            if ends_at:
                discount_input["endsAt"] = ends_at
            if min_purchase_amount is not None:
                discount_input["minimumRequirement"] = {
                    "subtotal": {"greaterThanOrEqualToSubtotal": min_purchase_amount}
                }

            body = await client.mutate(
                """
                mutation CreateDiscount($basicCodeDiscount: DiscountCodeBasicInput!) {
                  discountCodeBasicCreate(basicCodeDiscount: $basicCodeDiscount) {
                    codeDiscountNode {
                      id
                      codeDiscount { ... on DiscountCodeBasic { title codes(first: 1) { edges { node { code } } } } }
                    }
                    userErrors { field message code }
                  }
                }
                """,
                {"basicCodeDiscount": discount_input},
                invalidate_namespaces=["discounts"],
            )
            payload = body["data"]["discountCodeBasicCreate"]
            if payload["userErrors"]:
                return {"error": payload["userErrors"]}
            node = payload["codeDiscountNode"]
            return {
                "id": node["id"],
                "code": node["codeDiscount"]["codes"]["edges"][0]["node"]["code"],
            }
        except ShopifyAPIError as e:
            return {"error": str(e)}

    @mcp.tool(
        description=(
            "Adjust (increment or decrement) the available inventory of a product variant by a "
            "delta amount (e.g. delta=-3 to reduce stock by 3, delta=10 to add 10). Pass variant_id "
            "as a GID (gid://shopify/ProductVariant/...) - the same id search_products returns. "
            "location_id is optional and only needed if the variant stocks inventory at more than "
            "one location; if omitted and there's exactly one stocking location, it's used "
            "automatically. Returns the applied change, or an error message if the adjustment failed."
        )
    )
    async def adjust_inventory(variant_id: str, delta: int, location_id: str | None = None) -> dict[str, Any]:
        try:
            variant_body = await client.query(
                """
                query VariantInventory($id: ID!) {
                  productVariant(id: $id) {
                    inventoryItem {
                      id
                      inventoryLevels(first: 10) { edges { node { location { id } } } }
                    }
                  }
                }
                """,
                {"id": variant_id},
                namespace="inventory",
            )
            variant = variant_body.get("data", {}).get("productVariant")
            if not variant:
                return {"error": f"No product variant found for id {variant_id}"}
            inventory_item_id = variant["inventoryItem"]["id"]
            locations = [e["node"]["location"] for e in variant["inventoryItem"]["inventoryLevels"]["edges"]]

            if location_id is None:
                if len(locations) == 1:
                    location_id = locations[0]["id"]
                elif len(locations) == 0:
                    return {"error": f"Variant {variant_id} isn't stocked at any location."}
                else:
                    return {
                        "error": "Variant is stocked at multiple locations - pass location_id explicitly.",
                        "locations": locations,
                    }

            body = await client.mutate(
                """
                mutation AdjustInventory($input: InventoryAdjustQuantitiesInput!, $idempotencyKey: String!) {
                  inventoryAdjustQuantities(input: $input) @idempotent(key: $idempotencyKey) {
                    userErrors { field message }
                    inventoryAdjustmentGroup { createdAt reason changes { name delta } }
                  }
                }
                """,
                {
                    "input": {
                        "reason": "correction",
                        "name": "available",
                        "changes": [
                            {
                                "delta": delta,
                                "inventoryItemId": inventory_item_id,
                                "locationId": location_id,
                                "changeFromQuantity": None,
                            }
                        ],
                    },
                    "idempotencyKey": str(uuid.uuid4()),
                },
                invalidate_namespaces=["inventory", "products"],
            )
            payload = body["data"]["inventoryAdjustQuantities"]
            if payload["userErrors"]:
                return {"error": payload["userErrors"]}
            return payload["inventoryAdjustmentGroup"]
        except ShopifyAPIError as e:
            return {"error": str(e)}
