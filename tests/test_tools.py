"""
Unit tests for the MCP tools, using a ShopifyClient whose HTTP layer is
mocked. No network calls, no real Shopify store involved.
"""

from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp import FastMCP

from neolook.shopify_client import ShopifyClient
from neolook.tools import crud


class FakeResponse:
    def __init__(self, status_code: int, json_data: dict):
        self.status_code = status_code
        self._json_data = json_data
        self.text = str(json_data)

    def json(self) -> dict:
        return self._json_data


TOKEN_RESPONSE = FakeResponse(200, {"access_token": "fake-token", "scope": "*", "expires_in": 86399})


def gql(data: dict) -> FakeResponse:
    return FakeResponse(200, {"data": data, "extensions": {"cost": {"requestedQueryCost": 1}}})


@pytest.fixture
def mcp_and_client():
    client = ShopifyClient(
        store_domain="test-shop.myshopify.com",
        client_id="id",
        client_secret="secret",
        cache_enabled=False,
    )
    mcp = FastMCP("test")
    crud.register(mcp, client)
    return mcp, client


async def _call(mcp: FastMCP, name: str, **kwargs):
    _, structured = await mcp.call_tool(name, kwargs)
    return structured


async def test_search_products_returns_matches(mcp_and_client):
    mcp, client = mcp_and_client
    client._http.post = AsyncMock(
        side_effect=[
            TOKEN_RESPONSE,
            gql({"products": {"edges": [{"node": {"id": "gid://shopify/Product/1", "title": "Mug"}}]}}),
        ]
    )
    result = await _call(mcp, "search_products", query="mug", first=5)
    assert result["count"] == 1
    assert result["products"][0]["title"] == "Mug"


async def test_update_product_title_and_status(mcp_and_client):
    mcp, client = mcp_and_client
    client._http.post = AsyncMock(
        side_effect=[
            TOKEN_RESPONSE,
            gql({"productUpdate": {"product": {"id": "gid://shopify/Product/1", "title": "New", "status": "ACTIVE"}, "userErrors": []}}),
        ]
    )
    result = await _call(mcp, "update_product", product_id="gid://shopify/Product/1", title="New", status="active")
    assert result["product"]["title"] == "New"


async def test_update_product_reports_user_errors(mcp_and_client):
    mcp, client = mcp_and_client
    client._http.post = AsyncMock(
        side_effect=[
            TOKEN_RESPONSE,
            gql({"productUpdate": {"product": None, "userErrors": [{"field": ["title"], "message": "too long"}]}}),
        ]
    )
    result = await _call(mcp, "update_product", product_id="gid://shopify/Product/1", title="x" * 500)
    assert "error" in result


async def test_update_product_with_no_fields_errors(mcp_and_client):
    mcp, client = mcp_and_client
    result = await _call(mcp, "update_product", product_id="gid://shopify/Product/1")
    assert "error" in result


async def test_get_order_by_name_resolves_then_fetches(mcp_and_client):
    mcp, client = mcp_and_client
    client._http.post = AsyncMock(
        side_effect=[
            TOKEN_RESPONSE,
            gql({"orders": {"edges": [{"node": {"id": "gid://shopify/Order/9"}}]}}),
            gql(
                {
                    "order": {
                        "id": "gid://shopify/Order/9",
                        "name": "#1009",
                        "createdAt": "2026-01-01T00:00:00Z",
                        "displayFinancialStatus": "PAID",
                        "displayFulfillmentStatus": "UNFULFILLED",
                        "totalPriceSet": {"shopMoney": {"amount": "42.00", "currencyCode": "USD"}},
                        "customer": {"id": "gid://shopify/Customer/1", "displayName": "Ada", "defaultEmailAddress": None},
                        "lineItems": {"edges": []},
                        "discountCodes": [],
                        "tags": [],
                    }
                }
            ),
        ]
    )
    result = await _call(mcp, "get_order", order_id_or_name="1009")
    assert result["name"] == "#1009"
    assert result["totalPriceSet"] == {"amount": "42.00", "currency": "USD"}


async def test_get_order_not_found(mcp_and_client):
    mcp, client = mcp_and_client
    client._http.post = AsyncMock(side_effect=[TOKEN_RESPONSE, gql({"orders": {"edges": []}})])
    result = await _call(mcp, "get_order", order_id_or_name="9999")
    assert "error" in result


async def test_create_discount_success(mcp_and_client):
    mcp, client = mcp_and_client
    client._http.post = AsyncMock(
        side_effect=[
            TOKEN_RESPONSE,
            gql(
                {
                    "discountCodeBasicCreate": {
                        "codeDiscountNode": {
                            "id": "gid://shopify/DiscountCodeNode/1",
                            "codeDiscount": {"title": "SUMMER15", "codes": {"edges": [{"node": {"code": "SUMMER15"}}]}},
                        },
                        "userErrors": [],
                    }
                }
            ),
        ]
    )
    result = await _call(
        mcp, "create_discount", code="SUMMER15", percentage=15, starts_at="2026-01-01T00:00:00Z", min_purchase_amount=100
    )
    assert result["code"] == "SUMMER15"


async def test_adjust_inventory_success(mcp_and_client):
    mcp, client = mcp_and_client
    client._http.post = AsyncMock(
        side_effect=[
            TOKEN_RESPONSE,
            gql(
                {
                    "productVariant": {
                        "inventoryItem": {
                            "id": "gid://shopify/InventoryItem/1",
                            "inventoryLevels": {"edges": [{"node": {"location": {"id": "gid://shopify/Location/1"}}}]},
                        }
                    }
                }
            ),
            gql(
                {
                    "inventoryAdjustQuantities": {
                        "userErrors": [],
                        "inventoryAdjustmentGroup": {"createdAt": "2026-01-01T00:00:00Z", "reason": "correction", "changes": [{"name": "available", "delta": 5}]},
                    }
                }
            ),
        ]
    )
    result = await _call(
        mcp,
        "adjust_inventory",
        variant_id="gid://shopify/ProductVariant/1",
        delta=5,
    )
    assert result["changes"][0]["delta"] == 5
