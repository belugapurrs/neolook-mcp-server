"""
Unit tests for the pandas analytics engine, using a ShopifyClient whose HTTP
layer is mocked. No network calls, no real Shopify store involved.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from neolook.engines import analytics_engine as engine
from neolook.shopify_client import ShopifyClient


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


def order_node(order_id: str, customer_id: str, days_ago: float, price: str, product_title: str, quantity: int, discount_codes=None, discount_amount="0.00"):
    return {
        "id": order_id,
        "name": f"#{order_id[-4:]}",
        "createdAt": iso_days_ago(days_ago),
        "displayFinancialStatus": "PAID",
        "totalPriceSet": {"shopMoney": {"amount": price, "currencyCode": "USD"}},
        "totalDiscountsSet": {"shopMoney": {"amount": discount_amount, "currencyCode": "USD"}},
        "discountCodes": discount_codes or [],
        "customer": {"id": customer_id, "displayName": "Test Customer"},
        "lineItems": {
            "edges": [
                {
                    "node": {
                        "title": product_title,
                        "quantity": quantity,
                        "originalUnitPriceSet": {"shopMoney": {"amount": price, "currencyCode": "USD"}},
                        "variant": {"id": "gid://shopify/ProductVariant/1", "product": {"id": f"gid://shopify/Product/{product_title}", "title": product_title, "totalInventory": 50}},
                    }
                }
            ]
        },
    }


@pytest.fixture
def client():
    return ShopifyClient(store_domain="test-shop.myshopify.com", client_id="id", client_secret="secret", cache_enabled=False)


async def test_fetch_order_data_filters_by_window(client, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "MANIFEST_PATH", tmp_path / "no_manifest.json")

    orders = {
        "orders": {
            "edges": [
                {"node": order_node("gid://shopify/Order/1", "gid://shopify/Customer/1", 5, "100.00", "Mug", 2)},
                {"node": order_node("gid://shopify/Order/2", "gid://shopify/Customer/2", 200, "50.00", "Mug", 1)},
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    }
    client._http.post = AsyncMock(side_effect=[TOKEN_RESPONSE, gql(orders)])

    orders_df, items_df = await engine.fetch_order_data(client, days=30)

    # Order 2 (200 days old) is outside a 30-day window and must be excluded
    # even though the mocked server "returned" it (real Shopify would filter
    # server-side via the query string; this proves our client-side guard works too).
    assert len(orders_df) == 1
    assert orders_df.iloc[0]["order_id"] == "gid://shopify/Order/1"
    assert len(items_df) == 1


async def test_revenue_summary_and_top_products(client, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "MANIFEST_PATH", tmp_path / "no_manifest.json")

    orders = {
        "orders": {
            "edges": [
                {"node": order_node("gid://shopify/Order/1", "gid://shopify/Customer/1", 1, "100.00", "Mug", 2)},
                {"node": order_node("gid://shopify/Order/2", "gid://shopify/Customer/2", 2, "50.00", "Candle", 1)},
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    }
    client._http.post = AsyncMock(side_effect=[TOKEN_RESPONSE, gql(orders)])

    orders_df, items_df = await engine.fetch_order_data(client, days=30)
    summary = engine.revenue_summary(orders_df, items_df, group_by="none")
    assert summary["total_revenue"] == 150.0
    assert summary["order_count"] == 2

    top = engine.top_products(items_df, limit=10, by="revenue")
    assert top[0]["product_title"] == "Mug"
    assert top[0]["revenue"] == 200.0  # 2 units * $100


async def test_discount_roi_groups_by_code(client, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "MANIFEST_PATH", tmp_path / "no_manifest.json")

    orders = {
        "orders": {
            "edges": [
                {"node": order_node("gid://shopify/Order/1", "gid://shopify/Customer/1", 1, "100.00", "Mug", 1, discount_codes=["SAVE10"], discount_amount="10.00")},
                {"node": order_node("gid://shopify/Order/2", "gid://shopify/Customer/2", 1, "80.00", "Mug", 1, discount_codes=["SAVE10"], discount_amount="8.00")},
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    }
    client._http.post = AsyncMock(side_effect=[TOKEN_RESPONSE, gql(orders)])

    orders_df, _ = await engine.fetch_order_data(client, days=30)
    roi = engine.discount_roi(orders_df)
    assert len(roi) == 1
    assert roi[0]["code"] == "SAVE10"
    assert roi[0]["orders"] == 2
    assert roi[0]["revenue"] == 180.0
    assert roi[0]["discount_given"] == 18.0


async def test_customer_repeat_rate(client, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "MANIFEST_PATH", tmp_path / "no_manifest.json")

    orders = {
        "orders": {
            "edges": [
                {"node": order_node("gid://shopify/Order/1", "gid://shopify/Customer/1", 1, "100.00", "Mug", 1)},
                {"node": order_node("gid://shopify/Order/2", "gid://shopify/Customer/1", 2, "50.00", "Mug", 1)},
                {"node": order_node("gid://shopify/Order/3", "gid://shopify/Customer/2", 1, "30.00", "Mug", 1)},
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    }
    client._http.post = AsyncMock(side_effect=[TOKEN_RESPONSE, gql(orders)])

    orders_df, _ = await engine.fetch_order_data(client, days=90)
    result = engine.customer_repeat_rate(orders_df)
    assert result["one_time_customers"] == 1
    assert result["repeat_customers"] == 1
    assert result["total_revenue"] == 180.0


async def test_stale_inventory_report(client, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "MANIFEST_PATH", tmp_path / "no_manifest.json")

    products = {
        "products": {
            "edges": [
                {"node": {"id": "gid://shopify/Product/Mug", "title": "Mug", "totalInventory": 100, "createdAt": iso_days_ago(10)}},
                {"node": {"id": "gid://shopify/Product/Candle", "title": "Candle", "totalInventory": 5, "createdAt": iso_days_ago(10)}},
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    }
    orders = {
        "orders": {
            "edges": [
                {"node": order_node("gid://shopify/Order/1", "gid://shopify/Customer/1", 1, "100.00", "Candle", 5)},
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    }
    client._http.post = AsyncMock(side_effect=[TOKEN_RESPONSE, gql(products), gql(orders)])

    products_df = await engine.fetch_all_active_products(client)
    _, items_df = await engine.fetch_order_data(client, days=60)
    stale = engine.stale_inventory_report(products_df, items_df, max_units_sold=2)

    # Mug: 100 in stock, 0 sold in window -> stale. Candle: 5 sold >= max_units_sold -> not stale.
    assert len(stale) == 1
    assert stale[0]["product_title"] == "Mug"
