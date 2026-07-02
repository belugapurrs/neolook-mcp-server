"""
Async Shopify Admin GraphQL client.

Handles:
  - Dev Dashboard auth (client credentials grant), with automatic token
    caching + refresh before the 24h expiry.
  - Cost-aware rate limiting using Shopify's extensions.cost.throttleStatus.
  - A namespaced TTL cache for read queries; mutations invalidate the
    relevant namespace(s) instead of being cached.
  - Request instrumentation (requests_attempted / requests_sent_to_shopify /
    cache_hits / throttle_events) used to measure the caching-driven traffic
    reduction claim.
"""

import asyncio
import os
import time
from typing import Any

import httpx

from neolook.cache import NamespacedTTLCache, make_cache_key

TOKEN_EXPIRY_BUFFER_SECONDS = 60
MAX_RETRIES = 3


class ShopifyAPIError(Exception):
    pass


class ShopifyClient:
    def __init__(
        self,
        store_domain: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        api_version: str | None = None,
        cache_ttl_seconds: int | None = None,
        cache_enabled: bool | None = None,
    ):
        self.store_domain = store_domain or os.environ["SHOPIFY_STORE_DOMAIN"]
        self.client_id = client_id or os.environ["SHOPIFY_CLIENT_ID"]
        self.client_secret = client_secret or os.environ["SHOPIFY_CLIENT_SECRET"]
        self.api_version = api_version or os.environ.get("SHOPIFY_API_VERSION", "2026-04")

        if cache_ttl_seconds is None:
            cache_ttl_seconds = int(os.environ.get("CACHE_TTL_SECONDS", "300"))
        if cache_enabled is None:
            cache_enabled = os.environ.get("CACHE_ENABLED", "true").lower() == "true"

        self.cache = NamespacedTTLCache(ttl_seconds=cache_ttl_seconds, enabled=cache_enabled)

        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

        self._http = httpx.AsyncClient(timeout=30.0)

        # Metrics
        self.requests_attempted = 0
        self.requests_sent_to_shopify = 0
        self.throttle_events = 0

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---------------------------------------------------------------- auth

    async def _get_access_token(self) -> str:
        """Return a valid access token, fetching/refreshing it if needed."""
        async with self._token_lock:
            now = time.monotonic()
            if self._access_token and now < self._token_expires_at - TOKEN_EXPIRY_BUFFER_SECONDS:
                return self._access_token

            url = f"https://{self.store_domain}/admin/oauth/access_token"
            data = {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
            response = await self._http.post(url, data=data)
            if response.status_code != 200:
                raise ShopifyAPIError(f"Token exchange failed: HTTP {response.status_code} {response.text}")

            payload = response.json()
            self._access_token = payload["access_token"]
            self._token_expires_at = time.monotonic() + payload["expires_in"]
            return self._access_token

    # ------------------------------------------------------------ requests

    async def _post_graphql(self, query: str, variables: dict[str, Any] | None) -> dict[str, Any]:
        """Send one GraphQL request, handling cost-based throttling and
        exponential backoff retries on 5xx errors."""
        url = f"https://{self.store_domain}/admin/api/{self.api_version}/graphql.json"

        backoff = 1.0
        for attempt in range(MAX_RETRIES + 1):
            token = await self._get_access_token()
            headers = {
                "X-Shopify-Access-Token": token,
                "Content-Type": "application/json",
            }
            response = await self._http.post(
                url, headers=headers, json={"query": query, "variables": variables or {}}
            )

            if response.status_code >= 500:
                if attempt == MAX_RETRIES:
                    raise ShopifyAPIError(f"Shopify server error: HTTP {response.status_code}")
                await asyncio.sleep(backoff)
                backoff *= 2
                continue

            if response.status_code != 200:
                raise ShopifyAPIError(f"HTTP {response.status_code}: {response.text}")

            body = response.json()

            errors = body.get("errors")
            if errors:
                error_codes = [
                    e.get("extensions", {}).get("code") for e in errors if isinstance(e, dict)
                ]
                if "THROTTLED" in error_codes:
                    self.throttle_events += 1
                    wait = self._throttle_wait_seconds(body)
                    if attempt == MAX_RETRIES:
                        raise ShopifyAPIError(f"Throttled after {MAX_RETRIES} retries: {errors}")
                    await asyncio.sleep(wait)
                    continue
                raise ShopifyAPIError(f"GraphQL errors: {errors}")

            # Proactively back off if this call ate most of our available cost,
            # so the *next* call doesn't get throttled.
            throttle_status = body.get("extensions", {}).get("cost", {}).get("throttleStatus")
            if throttle_status:
                requested = body["extensions"]["cost"].get("requestedQueryCost", 0)
                available = throttle_status.get("currentlyAvailable", requested)
                restore_rate = throttle_status.get("restoreRate", 50) or 50
                if available < requested:
                    self.throttle_events += 1
                    wait = max((requested - available) / restore_rate, 0)
                    await asyncio.sleep(wait)

            return body

        raise ShopifyAPIError("Exhausted retries without a response")

    @staticmethod
    def _throttle_wait_seconds(body: dict[str, Any]) -> float:
        cost = body.get("extensions", {}).get("cost", {})
        throttle_status = cost.get("throttleStatus", {})
        requested = cost.get("requestedQueryCost", 50)
        available = throttle_status.get("currentlyAvailable", 0)
        restore_rate = throttle_status.get("restoreRate", 50) or 50
        return max((requested - available) / restore_rate, 1.0)

    # ------------------------------------------------------------- public

    async def query(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        namespace: str = "products",
    ) -> dict[str, Any]:
        """Run a read-only GraphQL query, served from cache when possible."""
        self.requests_attempted += 1
        cache_key = make_cache_key(query, variables)

        cached = self.cache.get(namespace, cache_key)
        if cached is not None:
            return cached

        self.requests_sent_to_shopify += 1
        body = await self._post_graphql(query, variables)
        self.cache.set(namespace, cache_key, body)
        return body

    async def mutate(
        self,
        mutation: str,
        variables: dict[str, Any] | None = None,
        invalidate_namespaces: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run a GraphQL mutation. Never cached; invalidates the given
        namespaces afterward so subsequent reads see fresh data."""
        self.requests_attempted += 1
        self.requests_sent_to_shopify += 1
        body = await self._post_graphql(mutation, variables)
        for namespace in invalidate_namespaces or []:
            self.cache.clear_namespace(namespace)
        return body

    def get_metrics(self) -> dict[str, Any]:
        cache_hits = self.cache.hits
        total_reads = cache_hits + self.cache.misses
        hit_rate = cache_hits / total_reads if total_reads else 0.0
        return {
            "requests_attempted": self.requests_attempted,
            "requests_sent_to_shopify": self.requests_sent_to_shopify,
            "cache_hits": cache_hits,
            "cache_hit_rate": round(hit_rate, 4),
            "throttle_events": self.throttle_events,
        }
