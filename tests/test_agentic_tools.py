"""
Unit tests for the Tier 3 agentic tools, using mocked HTTP responses.
No network calls, no real Shopify store involved.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp import FastMCP

from neolook.shopify_client import ShopifyClient
from neolook.tools import agentic


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


def iso_days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def mcp_and_client(tmp_path, monkeypatch):
    from neolook.engines import analytics_engine as engine

    monkeypatch.setattr(engine, "MANIFEST_PATH", tmp_path / "no_manifest.json")
    client = ShopifyClient(store_domain="test-shop.myshopify.com", client_id="id", client_secret="secret", cache_enabled=False)
    mcp = FastMCP("test")
    agentic.register(mcp, client)
    return mcp, client


async def _call(mcp: FastMCP, name: str, **kwargs):
    _, structured = await mcp.call_tool(name, kwargs)
    return structured


async def test_recover_abandoned_carts_creates_discount_and_drafts_emails(mcp_and_client):
    mcp, client = mcp_and_client
    checkout_node = {
        "id": "gid://shopify/AbandonedCheckout/1",
        "createdAt": iso_days_ago(1),
        "totalPriceSet": {"shopMoney": {"amount": "150.00", "currencyCode": "USD"}},
        "customer": {"id": "gid://shopify/Customer/1", "displayName": "Ada Lovelace", "defaultEmailAddress": {"emailAddress": "ada@example.com"}},
        "abandonedCheckoutUrl": "https://example.com/checkout/1",
        "lineItems": {"edges": [{"node": {"title": "Mug", "quantity": 2}}]},
    }
    client._http.post = AsyncMock(
        side_effect=[
            TOKEN_RESPONSE,
            gql({"abandonedCheckouts": {"nodes": [checkout_node]}}),
            gql(
                {
                    "discountCodeBasicCreate": {
                        "codeDiscountNode": {"id": "gid://shopify/DiscountCodeNode/1", "codeDiscount": {"codes": {"edges": [{"node": {"code": "COMEBACK10-ABCDEF"}}]}}},
                        "userErrors": [],
                    }
                }
            ),
        ]
    )
    result = await _call(mcp, "recover_abandoned_carts", min_cart_value=100, discount_percentage=10, expires_in_days=7)
    assert result["discount_code"] is not None
    assert len(result["emails"]) == 1
    assert result["emails"][0]["customer_email"] == "ada@example.com"
    assert "COMEBACK" in result["emails"][0]["body"] or result["discount_code"] in result["emails"][0]["body"]


async def test_recover_abandoned_carts_no_candidates(mcp_and_client):
    mcp, client = mcp_and_client
    client._http.post = AsyncMock(
        side_effect=[
            TOKEN_RESPONSE,
            gql({"abandonedCheckouts": {"nodes": []}}),
            gql({"draftOrders": {"edges": []}}),
        ]
    )
    result = await _call(mcp, "recover_abandoned_carts", min_cart_value=100)
    assert result["discount_code"] is None
    assert result["emails"] == []


async def test_create_checkout_link_returns_invoice_url(mcp_and_client):
    mcp, client = mcp_and_client
    client._http.post = AsyncMock(
        side_effect=[
            TOKEN_RESPONSE,
            gql(
                {
                    "draftOrderCreate": {
                        "draftOrder": {
                            "id": "gid://shopify/DraftOrder/1",
                            "name": "#D1",
                            "invoiceUrl": "https://example.com/pay/1",
                            "totalPriceSet": {"shopMoney": {"amount": "20.00", "currencyCode": "USD"}},
                        },
                        "userErrors": [],
                    }
                }
            ),
        ]
    )
    result = await _call(
        mcp, "create_checkout_link", line_items=[{"variant_id": "gid://shopify/ProductVariant/1", "quantity": 2}]
    )
    assert result["checkout_url"] == "https://example.com/pay/1"


async def test_get_server_metrics_returns_client_metrics(mcp_and_client):
    mcp, client = mcp_and_client
    result = await _call(mcp, "get_server_metrics")
    assert "requests_attempted" in result
    assert "cache_hits" in result
