"""
Unit tests for ShopifyClient + the caching layer, using mocked HTTP
responses. No network calls, no real Shopify store involved.
"""

from unittest.mock import AsyncMock

import pytest

from neolook.shopify_client import ShopifyClient


class FakeResponse:
    def __init__(self, status_code: int, json_data: dict, text: str = ""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text or str(json_data)

    def json(self) -> dict:
        return self._json_data


TOKEN_RESPONSE = FakeResponse(200, {"access_token": "fake-token", "scope": "*", "expires_in": 86399})


def shop_query_response(name: str = "test-shop") -> FakeResponse:
    return FakeResponse(
        200,
        {
            "data": {"shop": {"name": name}},
            "extensions": {
                "cost": {
                    "requestedQueryCost": 1,
                    "actualQueryCost": 1,
                    "throttleStatus": {
                        "maximumAvailable": 1000,
                        "currentlyAvailable": 999,
                        "restoreRate": 50,
                    },
                }
            },
        },
    )


@pytest.fixture
def client():
    c = ShopifyClient(
        store_domain="test-shop.myshopify.com",
        client_id="id",
        client_secret="secret",
        api_version="2026-04",
        cache_ttl_seconds=300,
        cache_enabled=True,
    )
    yield c


async def test_query_fetches_token_once_and_caches_reads(client):
    client._http.post = AsyncMock(side_effect=[TOKEN_RESPONSE, shop_query_response()])

    result1 = await client.query("{ shop { name } }", namespace="products")
    assert result1["data"]["shop"]["name"] == "test-shop"

    # Second identical call should be served from cache -> no new HTTP call needed.
    result2 = await client.query("{ shop { name } }", namespace="products")
    assert result2 == result1

    # Only 2 HTTP calls total: 1 token exchange + 1 actual query.
    assert client._http.post.call_count == 2
    metrics = client.get_metrics()
    assert metrics["requests_attempted"] == 2
    assert metrics["requests_sent_to_shopify"] == 1
    assert metrics["cache_hits"] == 1


async def test_mutation_invalidates_namespace(client):
    client._http.post = AsyncMock(
        side_effect=[TOKEN_RESPONSE, shop_query_response(), shop_query_response("renamed-shop")]
    )

    await client.query("{ shop { name } }", namespace="products")

    await client.mutate(
        "mutation { productUpdate { product { id } } }",
        variables={"id": "gid://shopify/Product/1"},
        invalidate_namespaces=["products"],
    )

    # After invalidation, an identical read must hit the network again, not the cache.
    client._http.post.side_effect = [shop_query_response("after-invalidation")]
    result = await client.query("{ shop { name } }", namespace="products")
    assert result["data"]["shop"]["name"] == "after-invalidation"


async def test_throttled_response_retries_then_succeeds(client):
    throttled = FakeResponse(
        200,
        {
            "errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
            "extensions": {
                "cost": {
                    "requestedQueryCost": 50,
                    "throttleStatus": {"maximumAvailable": 1000, "currentlyAvailable": 10, "restoreRate": 50},
                }
            },
        },
    )
    client._http.post = AsyncMock(side_effect=[TOKEN_RESPONSE, throttled, shop_query_response()])

    result = await client.query("{ shop { name } }", namespace="products")
    assert result["data"]["shop"]["name"] == "test-shop"
    assert client.throttle_events >= 1


async def test_token_reused_across_multiple_calls(client):
    client._http.post = AsyncMock(
        side_effect=[TOKEN_RESPONSE, shop_query_response("a"), shop_query_response("b")]
    )

    await client.query("{ shop { name } }", variables={"x": 1}, namespace="products")
    await client.query("{ shop { name } }", variables={"x": 2}, namespace="products")

    # 3 calls total: 1 token exchange (not repeated) + 2 distinct queries.
    assert client._http.post.call_count == 3
