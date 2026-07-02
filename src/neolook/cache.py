"""
In-process TTL (time-to-live) cache for Shopify read queries.

Keys are namespaced by resource (e.g. "products", "orders") so that a
mutation on one resource can cheaply invalidate just that resource's cached
reads, without needing to know exactly which queries were cached.
"""

import hashlib
import json
from typing import Any

from cachetools import TTLCache

# Resources that reads/writes get grouped under. Namespaces are just labels -
# any string works, but we centralize the known ones here for consistency.
NAMESPACES = ("products", "orders", "customers", "discounts", "inventory", "collections")


def make_cache_key(query: str, variables: dict[str, Any] | None) -> str:
    """SHA256 of the query text plus its variables, so identical requests
    (same query + same args) reuse the same cache entry."""
    payload = json.dumps({"query": query, "variables": variables or {}}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class NamespacedTTLCache:
    """A separate TTLCache per resource namespace, so we can invalidate one
    resource (e.g. "products" after update_product) without wiping others."""

    def __init__(self, ttl_seconds: int, enabled: bool = True, maxsize: int = 512):
        self.enabled = enabled
        self._ttl_seconds = ttl_seconds
        self._maxsize = maxsize
        self._caches: dict[str, TTLCache] = {ns: TTLCache(maxsize=maxsize, ttl=ttl_seconds) for ns in NAMESPACES}
        self.hits = 0
        self.misses = 0

    def _cache_for(self, namespace: str) -> TTLCache:
        if namespace not in self._caches:
            self._caches[namespace] = TTLCache(maxsize=self._maxsize, ttl=self._ttl_seconds)
        return self._caches[namespace]

    def get(self, namespace: str, key: str) -> Any | None:
        if not self.enabled:
            return None
        cache = self._cache_for(namespace)
        if key in cache:
            self.hits += 1
            return cache[key]
        self.misses += 1
        return None

    def set(self, namespace: str, key: str, value: Any) -> None:
        if not self.enabled:
            return
        self._cache_for(namespace)[key] = value

    def clear_namespace(self, namespace: str) -> None:
        self._cache_for(namespace).clear()

    def clear_all(self) -> None:
        for cache in self._caches.values():
            cache.clear()
