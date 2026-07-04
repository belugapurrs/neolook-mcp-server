"""
Unit tests for the analytics MCP tool wrappers (registration + JSON
serialization through the MCP layer), using mocked HTTP responses.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp import FastMCP

from neolook.shopify_client import ShopifyClient
from neolook.tools import analytics


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
def mcp_and_client(tmp_path, monkeypatch):
    from neolook.engines import analytics_engine as engine

    monkeypatch.setattr(engine, "MANIFEST_PATH", tmp_path / "no_manifest.json")
    client = ShopifyClient(store_domain="test-shop.myshopify.com", client_id="id", client_secret="secret", cache_enabled=False)
    mcp = FastMCP("test")
    analytics.register(mcp, client)
    return mcp, client


async def _call(mcp: FastMCP, name: str, **kwargs):
    _, structured = await mcp.call_tool(name, kwargs)
    return structured


async def test_revenue_summary_tool_returns_json_safe_output(mcp_and_client):
    mcp, client = mcp_and_client
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    orders = {
        "orders": {
            "edges": [
                {
                    "node": {
                        "id": "gid://shopify/Order/1",
                        "name": "#1001",
                        "createdAt": now,
                        "displayFinancialStatus": "PAID",
                        "totalPriceSet": {"shopMoney": {"amount": "42.50", "currencyCode": "USD"}},
                        "totalDiscountsSet": {"shopMoney": {"amount": "0.00", "currencyCode": "USD"}},
                        "discountCodes": [],
                        "customer": {"id": "gid://shopify/Customer/1", "displayName": "Ada"},
                        "lineItems": {"edges": []},
                    }
                }
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    }
    client._http.post = AsyncMock(side_effect=[TOKEN_RESPONSE, gql(orders)])

    result = await _call(mcp, "revenue_summary", days=30, group_by="none")
    assert result["total_revenue"] == 42.5
    assert result["order_count"] == 1


async def test_abandoned_checkout_report_falls_back_to_drafts(mcp_and_client):
    mcp, client = mcp_and_client
    client._http.post = AsyncMock(
        side_effect=[
            TOKEN_RESPONSE,
            gql({"abandonedCheckouts": {"nodes": []}}),
            gql({"draftOrders": {"edges": []}}),
        ]
    )
    result = await _call(mcp, "abandoned_checkout_report", min_value=0, days=30)
    assert result["source"] == "open_draft_orders_fallback"
    assert result["count"] == 0


def _order_node(order_id: str, customer_id: str, customer_name: str, price: str) -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "id": order_id,
        "name": f"#{order_id[-4:]}",
        "createdAt": now,
        "displayFinancialStatus": "PAID",
        "totalPriceSet": {"shopMoney": {"amount": price, "currencyCode": "USD"}},
        "totalDiscountsSet": {"shopMoney": {"amount": "0.00", "currencyCode": "USD"}},
        "discountCodes": [],
        "customer": {"id": customer_id, "displayName": customer_name},
        "lineItems": {"edges": []},
    }


async def test_segment_customers_tool_returns_segments(mcp_and_client):
    mcp, client = mcp_and_client
    orders = {
        "orders": {
            "edges": [
                {"node": _order_node("gid://shopify/Order/1", "gid://shopify/Customer/1", "Ada", "100.00")},
                {"node": _order_node("gid://shopify/Order/2", "gid://shopify/Customer/2", "Bea", "50.00")},
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    }
    client._http.post = AsyncMock(side_effect=[TOKEN_RESPONSE, gql(orders)])

    result = await _call(mcp, "segment_customers", days=120)
    assert result["total_customers_analyzed"] == 2
    assert len(result["customers"]) == 2
    assert sum(result["segment_counts"].values()) == 2


async def test_estimate_customer_ltv_tool(mcp_and_client):
    mcp, client = mcp_and_client
    orders = {
        "orders": {
            "edges": [
                {"node": _order_node("gid://shopify/Order/1", "gid://shopify/Customer/1", "Ada", "100.00")},
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    }
    client._http.post = AsyncMock(side_effect=[TOKEN_RESPONSE, gql(orders)])

    result = await _call(mcp, "estimate_customer_ltv", customer_id="gid://shopify/Customer/1", days=120)
    assert result["customer_id"] == "gid://shopify/Customer/1"
    assert result["orders_in_window"] == 1
    assert result["methodology"] == "naive_v1_linear_projection"


async def test_estimate_customer_ltv_tool_no_orders_returns_error(mcp_and_client):
    mcp, client = mcp_and_client
    orders = {"orders": {"edges": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}
    client._http.post = AsyncMock(side_effect=[TOKEN_RESPONSE, gql(orders)])

    result = await _call(mcp, "estimate_customer_ltv", customer_id="gid://shopify/Customer/999", days=120)
    assert "error" in result
