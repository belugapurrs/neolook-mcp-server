"""
Manual smoke test for Tier 2 analytics + Tier 3 agentic tools against the
live dev store (now seeded with real data). Not part of the automated test
suite - run by hand:

    python scripts/smoke_test_analytics_agentic.py
"""

import asyncio
import json

from neolook.server import mcp, _client


async def call(name: str, **kwargs) -> None:
    print(f"\n--- {name}({kwargs}) ---")
    result = await mcp.call_tool(name, kwargs)
    structured = result[1] if isinstance(result, tuple) else result
    print(json.dumps(structured, indent=2, default=str)[:1500])


async def main() -> None:
    await call("revenue_summary", days=90, group_by="day")
    await call("top_products", days=90, limit=5, by="revenue")
    await call("sales_velocity", days=60)
    await call("stale_inventory_report", days=60, max_units_sold=2)
    await call("abandoned_checkout_report", min_value=0, days=30)
    await call("discount_roi", days=120)
    await call("customer_repeat_rate", days=120)
    await call("price_optimization_suggestions", days=60)
    await call("get_server_metrics")

    print("\n--- final metrics ---")
    print(_client.get_metrics())
    await _client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
