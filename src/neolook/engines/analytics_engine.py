"""
Pandas computations over Shopify order/product data. tools/analytics.py
calls these and formats results as MCP tool output; this module stays
Shopify-plumbing-free (it works on already-fetched DataFrames) except for
the fetch_* helpers, which own the GraphQL pagination.

Demo-data note: scripts/seed_store.py creates ~400 orders "live" - Shopify
always stamps a new order's createdAt as now, since there's no way to
backdate an order through the API. To make that seed data useful for
time-windowed analytics (revenue trends, sales velocity, etc.), fetch_orders
optionally overlays each order's *intended* historical date from
seed_manifest.json (written by the seed script) in place of the real
createdAt - clearly a demo simulation layered on top of real Shopify order
records, not a claim that Shopify itself backdated anything. When no
manifest file is present (e.g. a real store), this module uses Shopify's
real createdAt and pushes the date filter server-side for efficiency.
See docs/BUILD_LOG.md Phase 5/6 for the full explanation.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from neolook.shopify_client import ShopifyClient

MANIFEST_PATH = Path(__file__).resolve().parent.parent.parent.parent / "seed_manifest.json"

ORDERS_QUERY = """
query AnalyticsOrders($first: Int!, $after: String, $query: String) {
  orders(first: $first, after: $after, query: $query, sortKey: CREATED_AT, reverse: true) {
    edges { node {
      id name createdAt displayFinancialStatus
      totalPriceSet { shopMoney { amount currencyCode } }
      totalDiscountsSet { shopMoney { amount currencyCode } }
      discountCodes
      customer { id displayName }
      lineItems(first: 50) { edges { node {
        title quantity
        originalUnitPriceSet { shopMoney { amount currencyCode } }
        variant { id product { id title totalInventory } }
      } } }
    } }
    pageInfo { hasNextPage endCursor }
  }
}
"""

PRODUCTS_QUERY = """
query AnalyticsProducts($first: Int!, $after: String) {
  products(first: $first, after: $after, query: "status:active") {
    edges { node { id title totalInventory createdAt } }
    pageInfo { hasNextPage endCursor }
  }
}
"""


def _money(money_set: dict | None) -> float:
    if not money_set:
        return 0.0
    return float(money_set.get("shopMoney", {}).get("amount", 0) or 0)


def _load_manifest_dates() -> dict[str, str]:
    if not MANIFEST_PATH.exists():
        return {}
    data = json.loads(MANIFEST_PATH.read_text())
    return {o["order_id"]: o["intended_created_at"] for o in data.get("orders", [])}


def _parse_dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


async def fetch_order_data(client: ShopifyClient, days: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (orders_df, line_items_df) for orders whose *effective* date
    (manifest-simulated if available, else real) falls within the last
    `days` days. line_items_df has one row per line item, joined back to
    its order's date/customer for aggregation."""
    manifest = _load_manifest_dates()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Without a manifest, real createdAt is trustworthy, so push the date
    # filter server-side. With a manifest (demo mode), real createdAt is
    # all "now" for seeded orders, so we must fetch broadly and filter by
    # the simulated date client-side instead.
    query_filter = None
    if not manifest:
        query_filter = f"created_at:>='{cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')}'"

    order_rows: list[dict[str, Any]] = []
    item_rows: list[dict[str, Any]] = []
    after = None

    while True:
        body = await client.query(
            ORDERS_QUERY, {"first": 100, "after": after, "query": query_filter}, namespace="orders"
        )
        data = body["data"]["orders"]
        for edge in data["edges"]:
            node = edge["node"]
            effective_iso = manifest.get(node["id"]) or node["createdAt"]
            effective_dt = _parse_dt(effective_iso)
            if effective_dt < cutoff:
                continue

            total = _money(node["totalPriceSet"])
            discount_amount = _money(node["totalDiscountsSet"])
            customer = node.get("customer") or {}

            order_rows.append(
                {
                    "order_id": node["id"],
                    "order_name": node["name"],
                    "date": effective_dt,
                    "financial_status": node["displayFinancialStatus"],
                    "total_price": total,
                    "discount_amount": discount_amount,
                    "discount_codes": node["discountCodes"],
                    "customer_id": customer.get("id"),
                    "customer_name": customer.get("displayName"),
                }
            )

            for li_edge in node["lineItems"]["edges"]:
                li = li_edge["node"]
                variant = li.get("variant") or {}
                product = variant.get("product") or {}
                unit_price = _money(li["originalUnitPriceSet"])
                item_rows.append(
                    {
                        "order_id": node["id"],
                        "date": effective_dt,
                        "product_id": product.get("id"),
                        "product_title": product.get("title") or li["title"],
                        "quantity": li["quantity"],
                        "unit_price": unit_price,
                        "line_revenue": unit_price * li["quantity"],
                    }
                )

        if not data["pageInfo"]["hasNextPage"]:
            break
        after = data["pageInfo"]["endCursor"]

    orders_df = pd.DataFrame(order_rows)
    items_df = pd.DataFrame(item_rows)
    return orders_df, items_df


async def fetch_all_active_products(client: ShopifyClient) -> pd.DataFrame:
    rows = []
    after = None
    while True:
        body = await client.query(PRODUCTS_QUERY, {"first": 100, "after": after}, namespace="products")
        data = body["data"]["products"]
        for edge in data["edges"]:
            node = edge["node"]
            rows.append({"product_id": node["id"], "product_title": node["title"], "total_inventory": node["totalInventory"]})
        if not data["pageInfo"]["hasNextPage"]:
            break
        after = data["pageInfo"]["endCursor"]
    return pd.DataFrame(rows)


def revenue_summary(orders_df: pd.DataFrame, items_df: pd.DataFrame, group_by: str) -> dict[str, Any]:
    if orders_df.empty:
        return {"total_revenue": 0.0, "order_count": 0, "breakdown": []}

    total_revenue = round(float(orders_df["total_price"].sum()), 2)
    order_count = int(len(orders_df))

    if group_by == "day":
        grouped = orders_df.assign(day=orders_df["date"].dt.date).groupby("day")["total_price"].agg(["sum", "count"])
        breakdown = [
            {"day": str(day), "revenue": round(float(row["sum"]), 2), "order_count": int(row["count"])}
            for day, row in grouped.sort_index().iterrows()
        ]
    elif group_by == "product" and not items_df.empty:
        grouped = items_df.groupby("product_title")["line_revenue"].sum().sort_values(ascending=False)
        breakdown = [{"product_title": title, "revenue": round(float(revenue), 2)} for title, revenue in grouped.items()]
    else:
        breakdown = []

    return {"total_revenue": total_revenue, "order_count": order_count, "average_order_value": round(total_revenue / order_count, 2) if order_count else 0.0, "breakdown": breakdown}


def top_products(items_df: pd.DataFrame, limit: int, by: str) -> list[dict[str, Any]]:
    if items_df.empty:
        return []
    grouped = items_df.groupby("product_title").agg(units_sold=("quantity", "sum"), revenue=("line_revenue", "sum"))
    sort_col = "revenue" if by == "revenue" else "units_sold"
    grouped = grouped.sort_values(sort_col, ascending=False).head(limit)
    return [
        {"product_title": title, "units_sold": int(row["units_sold"]), "revenue": round(float(row["revenue"]), 2)}
        for title, row in grouped.iterrows()
    ]


def sales_velocity(items_df: pd.DataFrame, days: int) -> list[dict[str, Any]]:
    if items_df.empty:
        return []
    cutoff_mid = datetime.now(timezone.utc) - timedelta(days=days / 2)
    current = items_df[items_df["date"] >= cutoff_mid]
    prior = items_df[items_df["date"] < cutoff_mid]

    half_days = max(days / 2, 1)
    current_rate = current.groupby("product_title")["quantity"].sum() / half_days
    prior_rate = prior.groupby("product_title")["quantity"].sum() / half_days

    all_products = sorted(set(current_rate.index) | set(prior_rate.index))
    results = []
    for product in all_products:
        cur = float(current_rate.get(product, 0.0))
        prev = float(prior_rate.get(product, 0.0))
        trend_pct = round(((cur - prev) / prev) * 100, 1) if prev > 0 else (100.0 if cur > 0 else 0.0)
        results.append({"product_title": product, "units_per_day": round(cur, 2), "trend_vs_prior_period_pct": trend_pct})
    results.sort(key=lambda r: r["units_per_day"], reverse=True)
    return results


def stale_inventory_report(products_df: pd.DataFrame, items_df: pd.DataFrame, max_units_sold: int) -> list[dict[str, Any]]:
    if products_df.empty:
        return []
    sold = items_df.groupby("product_id")["quantity"].sum() if not items_df.empty else pd.Series(dtype=int)
    products_df = products_df.copy()
    products_df["units_sold"] = products_df["product_id"].map(sold).fillna(0).astype(int)
    stale = products_df[(products_df["units_sold"] <= max_units_sold) & (products_df["total_inventory"] > 0)]
    stale = stale.sort_values(["units_sold", "total_inventory"], ascending=[True, False])
    return [
        {
            "product_id": row["product_id"],
            "product_title": row["product_title"],
            "units_in_stock": int(row["total_inventory"]),
            "units_sold_in_window": int(row["units_sold"]),
        }
        for _, row in stale.iterrows()
    ]


def discount_roi(orders_df: pd.DataFrame) -> list[dict[str, Any]]:
    if orders_df.empty:
        return []
    exploded = orders_df.explode("discount_codes").dropna(subset=["discount_codes"])
    if exploded.empty:
        return []
    grouped = exploded.groupby("discount_codes").agg(
        orders=("order_id", "count"), revenue=("total_price", "sum"), discount_given=("discount_amount", "sum")
    )
    results = []
    for code, row in grouped.iterrows():
        revenue = float(row["revenue"])
        discount_given = float(row["discount_given"])
        results.append(
            {
                "code": code,
                "orders": int(row["orders"]),
                "revenue": round(revenue, 2),
                "discount_given": round(discount_given, 2),
                "discount_to_revenue_ratio": round(discount_given / revenue, 4) if revenue else 0.0,
            }
        )
    results.sort(key=lambda r: r["revenue"], reverse=True)
    return results


ABANDONED_CHECKOUTS_QUERY = """
query AbandonedCheckouts($first: Int!) {
  abandonedCheckouts(first: $first, sortKey: CREATED_AT, reverse: true) {
    nodes {
      id createdAt
      totalPriceSet { shopMoney { amount currencyCode } }
      customer { id displayName defaultEmailAddress { emailAddress } }
      abandonedCheckoutUrl
      lineItems(first: 10) { edges { node { title quantity } } }
    }
  }
}
"""

OPEN_DRAFT_ORDERS_QUERY = """
query OpenDraftOrders($first: Int!) {
  draftOrders(first: $first, query: "status:open", sortKey: UPDATED_AT, reverse: true) {
    edges { node {
      id name createdAt
      totalPriceSet { shopMoney { amount currencyCode } }
      customer { id displayName defaultEmailAddress { emailAddress } }
      invoiceUrl
      lineItems(first: 10) { edges { node { title quantity } } }
    } }
  }
}
"""


async def fetch_cart_recovery_candidates(client: ShopifyClient, min_value: float, days: int) -> dict[str, Any]:
    """Returns {source, checkouts, count}. Prefers real abandoned checkouts;
    falls back to open (incomplete) draft orders when none exist - which is
    the normal case on a dev store, since abandoned checkouts can only be
    created by an actual customer leaving checkout mid-flow, and there's no
    API to fabricate one. See module docstring / docs/BUILD_LOG.md."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    body = await client.query(ABANDONED_CHECKOUTS_QUERY, {"first": 50}, namespace="orders")
    checkouts = [
        {
            "id": n["id"],
            "created_at": n["createdAt"],
            "value": _money(n["totalPriceSet"]),
            "customer_id": (n.get("customer") or {}).get("id"),
            "customer_name": (n.get("customer") or {}).get("displayName"),
            "customer_email": ((n.get("customer") or {}).get("defaultEmailAddress") or {}).get("emailAddress"),
            "recovery_url": n["abandonedCheckoutUrl"],
            "line_items": [e["node"] for e in n["lineItems"]["edges"]],
        }
        for n in body["data"]["abandonedCheckouts"]["nodes"]
        if _money(n["totalPriceSet"]) >= min_value and _parse_dt(n["createdAt"]) >= cutoff
    ]
    if checkouts:
        return {"source": "abandoned_checkouts", "checkouts": checkouts, "count": len(checkouts)}

    body = await client.query(OPEN_DRAFT_ORDERS_QUERY, {"first": 50}, namespace="orders")
    drafts = [
        {
            "id": e["node"]["id"],
            "name": e["node"]["name"],
            "created_at": e["node"]["createdAt"],
            "value": _money(e["node"]["totalPriceSet"]),
            "customer_id": (e["node"].get("customer") or {}).get("id"),
            "customer_name": (e["node"].get("customer") or {}).get("displayName"),
            "customer_email": ((e["node"].get("customer") or {}).get("defaultEmailAddress") or {}).get("emailAddress"),
            "recovery_url": e["node"]["invoiceUrl"],
            "line_items": [x["node"] for x in e["node"]["lineItems"]["edges"]],
        }
        for e in body["data"]["draftOrders"]["edges"]
        if _money(e["node"]["totalPriceSet"]) >= min_value and _parse_dt(e["node"]["createdAt"]) >= cutoff
    ]
    return {
        "source": "open_draft_orders_fallback",
        "note": "No real abandoned checkouts found (expected on a dev store with no live shoppers). Showing open/incomplete draft orders as a stand-in signal instead.",
        "checkouts": drafts,
        "count": len(drafts),
    }


def customer_repeat_rate(orders_df: pd.DataFrame) -> dict[str, Any]:
    if orders_df.empty:
        return {"one_time_customers": 0, "repeat_customers": 0, "repeat_revenue_share": 0.0}
    per_customer = orders_df.dropna(subset=["customer_id"]).groupby("customer_id").agg(
        order_count=("order_id", "count"), revenue=("total_price", "sum")
    )
    one_time = per_customer[per_customer["order_count"] == 1]
    repeat = per_customer[per_customer["order_count"] > 1]
    total_revenue = float(per_customer["revenue"].sum())
    repeat_revenue = float(repeat["revenue"].sum())
    return {
        "one_time_customers": int(len(one_time)),
        "repeat_customers": int(len(repeat)),
        "repeat_revenue_share": round(repeat_revenue / total_revenue, 4) if total_revenue else 0.0,
        "total_revenue": round(total_revenue, 2),
    }
