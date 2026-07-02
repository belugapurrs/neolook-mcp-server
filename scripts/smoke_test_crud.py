"""
Manual smoke test for the Tier 1 CRUD tools against the live dev store.
Not part of the automated test suite - run by hand during development:

    python scripts/smoke_test_crud.py
"""

import asyncio
import json

from neolook.server import mcp, _client


async def call(name: str, **kwargs) -> None:
    print(f"\n--- {name}({kwargs}) ---")
    result = await mcp.call_tool(name, kwargs)
    print(json.dumps(result, indent=2, default=str)[:2000])


async def main() -> None:
    await call("search_products", query="", first=5)
    await call("list_orders", first=5)

    result = await mcp.call_tool(
        "create_discount",
        {
            "code": "NEOLOOK-SMOKETEST",
            "percentage": 10,
            "starts_at": "2026-07-02T00:00:00Z",
            "min_purchase_amount": 50,
        },
    )
    print("create_discount raw result:", result)
    created = result[1] if isinstance(result, tuple) else result
    discount_id = created.get("id") if isinstance(created, dict) else None
    if discount_id:
        print(f"\n--- cleaning up test discount {discount_id} ---")
        body = await _client.mutate(
            "mutation DeleteDiscount($id: ID!) { discountCodeDelete(id: $id) { deletedCodeDiscountId userErrors { field message } } }",
            {"id": discount_id},
            invalidate_namespaces=["discounts"],
        )
        print(body["data"]["discountCodeDelete"])

    print("\n--- metrics ---")
    print(_client.get_metrics())
    await _client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
