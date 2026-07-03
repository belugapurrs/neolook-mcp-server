"""
Eval harness: runs each task in tasks.yaml through a headless Claude Code
agent with the NeoLook MCP server registered, then verifies the resulting
store state directly via GraphQL (never by trusting the agent's own report
of what it did). Produces the project's two headline metrics from real
logs - task success rate and caching-driven API traffic reduction - never
hardcoded.

Requirements to run this for real:
  - The Claude Code CLI (`claude`) installed and on PATH - this is a
    separate install from whatever client you're using to chat with
    Claude right now (e.g. the VS Code extension). See
    https://code.claude.com/docs/en/headless for install instructions.
  - Your Agent SDK monthly credit claimed in your Claude account settings
    (Settings -> Usage -> Claude Code). This suite runs on that free
    monthly credit, NOT a paid API key - see docs/BUILD_LOG.md for why we
    deliberately never set ANTHROPIC_API_KEY.

Usage:
    python evals/run_evals.py                  # full 24-task suite, twice (cache off/on)
    python evals/run_evals.py --dry-run 3       # first 3 tasks only, cache on, for a quick check
    python evals/run_evals.py --skip-cache-comparison   # run once with current CACHE_ENABLED
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from neolook.shopify_client import ShopifyClient  # noqa: E402

load_dotenv()
console = Console()

REPO_ROOT = Path(__file__).parent.parent
TASKS_PATH = Path(__file__).parent / "tasks.yaml"
RESULTS_DIR = Path(__file__).parent / "results"
MCP_CONFIG_PATH = REPO_ROOT / ".mcp.json"

ALL_TOOL_NAMES = [
    "search_products", "update_product", "get_order", "list_orders", "create_discount", "adjust_inventory",
    "revenue_summary", "top_products", "sales_velocity", "stale_inventory_report", "abandoned_checkout_report",
    "discount_roi", "customer_repeat_rate",
    "recover_abandoned_carts", "create_flash_sale", "create_checkout_link", "price_optimization_suggestions",
    "get_server_metrics",
]
MCP_TOOL_NAMES = [f"mcp__neolook__{name}" for name in ALL_TOOL_NAMES]

MAX_TURNS = 15
SUBPROCESS_TIMEOUT_SECONDS = 180


def write_mcp_config() -> None:
    python_bin = REPO_ROOT / ".venv" / "bin" / "python"
    config = {
        "mcpServers": {
            "neolook": {
                "command": str(python_bin),
                "args": ["-m", "neolook.server"],
            }
        }
    }
    MCP_CONFIG_PATH.write_text(json.dumps(config, indent=2))


async def run_agent_task(prompt: str) -> dict[str, Any]:
    """Runs one task through headless Claude Code, returns {tools_called, raw_result, error}."""
    cmd = [
        "claude", "--bare", "-p", prompt,
        "--mcp-config", str(MCP_CONFIG_PATH),
        "--allowedTools", *MCP_TOOL_NAMES,
        "--max-turns", str(MAX_TURNS),
        "--output-format", "stream-json",
        "--verbose",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=SUBPROCESS_TIMEOUT_SECONDS)
    except FileNotFoundError:
        return {"tools_called": set(), "error": "claude CLI not found on PATH - install it first (see module docstring)."}
    except asyncio.TimeoutError:
        return {"tools_called": set(), "error": f"Timed out after {SUBPROCESS_TIMEOUT_SECONDS}s"}

    tools_called: set[str] = set()
    final_result = None
    for line in stdout.decode(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Streamed assistant turns contain tool_use content blocks.
        content = event.get("message", {}).get("content", []) if isinstance(event.get("message"), dict) else []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tools_called.add(block.get("name", ""))
        if event.get("type") == "result":
            final_result = event

    if final_result is None and proc.returncode != 0:
        return {"tools_called": tools_called, "error": stderr.decode(errors="replace")[:500] or f"exit code {proc.returncode}"}

    return {"tools_called": tools_called, "raw_result": final_result, "error": None}


# --------------------------------------------------------------- checks

async def _discount_by_code(client: ShopifyClient, code: str) -> dict | None:
    body = await client.query(
        """
        query FindDiscount($code: String!) {
          codeDiscountNodeByCode(code: $code) {
            id
            codeDiscount {
              ... on DiscountCodeBasic {
                codes(first: 1) { edges { node { code } } }
                customerGets { value { ... on DiscountPercentage { percentage } } items { ... on DiscountCollections { collections(first: 5) { edges { node { id } } } } } }
                minimumRequirement { ... on DiscountMinimumSubtotal { greaterThanOrEqualToSubtotal { amount } } }
              }
            }
          }
        }
        """,
        {"code": code},
        namespace="discounts",
    )
    return body["data"].get("codeDiscountNodeByCode")


async def check_discount_exists(client: ShopifyClient, check: dict) -> tuple[bool, str]:
    node = await _discount_by_code(client, check["code"])
    if not node:
        return False, f"No discount found with code {check['code']}"
    discount = node["codeDiscount"]
    pct = discount["customerGets"]["value"].get("percentage")
    if pct is not None and round(pct * 100, 2) != check.get("percentage"):
        return False, f"Expected {check.get('percentage')}%, got {round((pct or 0) * 100, 2)}%"
    if "min_purchase" in check:
        min_req = (discount.get("minimumRequirement") or {}).get("greaterThanOrEqualToSubtotal")
        if not min_req or float(min_req["amount"]) != check["min_purchase"]:
            return False, f"Expected min purchase {check['min_purchase']}, got {min_req}"
    return True, "OK"


async def check_discount_exists_scoped_to_collection(client: ShopifyClient, check: dict, new_discount_codes: list[str]) -> tuple[bool, str]:
    for code in new_discount_codes:
        node = await _discount_by_code(client, code)
        if not node:
            continue
        discount = node["codeDiscount"]
        pct = discount["customerGets"]["value"].get("percentage")
        collections = discount["customerGets"]["items"].get("collections", {}).get("edges", [])
        if collections and pct is not None and round(pct * 100, 2) == check.get("percentage"):
            return True, "OK"
    return False, f"No new collection-scoped discount at {check.get('percentage')}% found among {new_discount_codes}"


async def check_collection_exists_with_products(client: ShopifyClient, check: dict, new_collection_ids: list[str]) -> tuple[bool, str]:
    for collection_id in new_collection_ids:
        body = await client.query(
            "query($id: ID!) { collection(id: $id) { productsCount { count } } }", {"id": collection_id}, namespace="collections"
        )
        count = (body["data"].get("collection") or {}).get("productsCount", {}).get("count", 0)
        if count >= check["min_products"]:
            return True, "OK"
    return False, f"No new collection with >= {check['min_products']} products found among {new_collection_ids}"


async def check_checkout_link_created(client: ShopifyClient, check: dict, new_draft_order_ids: list[str]) -> tuple[bool, str]:
    if not new_draft_order_ids:
        return False, "No new draft order (checkout link) was created"
    if "min_line_items" not in check and "customer_email" not in check:
        return True, "OK"
    for draft_id in new_draft_order_ids:
        body = await client.query(
            "query($id: ID!) { draftOrder(id: $id) { email lineItems(first: 10) { edges { node { id } } } } }",
            {"id": draft_id},
            namespace="orders",
        )
        draft = body["data"].get("draftOrder")
        if not draft:
            continue
        if "customer_email" in check and draft.get("email") != check["customer_email"]:
            continue
        if "min_line_items" in check and len(draft["lineItems"]["edges"]) < check["min_line_items"]:
            continue
        return True, "OK"
    return False, f"No new draft order matched {check}"


def check_tool_called(tools_called: set[str], check: dict) -> tuple[bool, str]:
    expected = f"mcp__neolook__{check['tool']}"
    if expected in tools_called:
        return True, "OK"
    return False, f"Tool {expected} was not called (called: {sorted(tools_called)})"


# ----------------------------------------------------------- state diff

async def snapshot_state(client: ShopifyClient) -> dict[str, list[str]]:
    collections_body = await client.query(
        'query { collections(first: 50, sortKey: TITLE, query: "title:Flash Sale*") { edges { node { id } } } }',
        namespace="collections",
    )
    drafts_body = await client.query(
        "query { draftOrders(first: 50, sortKey: UPDATED_AT, reverse: true) { edges { node { id } } } }",
        namespace="orders",
    )
    return {
        "collection_ids": [e["node"]["id"] for e in collections_body["data"]["collections"]["edges"]],
        "draft_order_ids": [e["node"]["id"] for e in drafts_body["data"]["draftOrders"]["edges"]],
    }


async def cleanup_new_resources(client: ShopifyClient, new_collection_ids: list[str], new_draft_order_ids: list[str], new_discount_codes: list[str]) -> None:
    for collection_id in new_collection_ids:
        try:
            await client.mutate(
                "mutation($input: CollectionDeleteInput!) { collectionDelete(input: $input) { deletedCollectionId userErrors { field message } } }",
                {"input": {"id": collection_id}},
                invalidate_namespaces=["collections"],
            )
        except Exception:
            pass
    for draft_id in new_draft_order_ids:
        try:
            await client.mutate(
                "mutation($id: ID!) { draftOrderDelete(input: { id: $id }) { deletedId userErrors { field message } } }",
                {"id": draft_id},
                invalidate_namespaces=["orders"],
            )
        except Exception:
            pass
    for code in new_discount_codes:
        node = await _discount_by_code(client, code)
        if node:
            try:
                await client.mutate(
                    "mutation($id: ID!) { discountCodeDelete(id: $id) { deletedCodeDiscountId userErrors { field message } } }",
                    {"id": node["id"]},
                    invalidate_namespaces=["discounts"],
                )
            except Exception:
                pass


# ------------------------------------------------------------- scoring

async def score_task(client: ShopifyClient, task: dict, tools_called: set[str], new_collection_ids: list[str], new_draft_order_ids: list[str], new_discount_codes: list[str]) -> dict[str, Any]:
    check_results = []
    for check in task["checks"]:
        check_type = check["type"]
        if check_type == "discount_exists":
            passed, detail = await check_discount_exists(client, check)
        elif check_type == "discount_exists_scoped_to_collection":
            passed, detail = await check_discount_exists_scoped_to_collection(client, check, new_discount_codes)
        elif check_type == "collection_exists_with_products":
            passed, detail = await check_collection_exists_with_products(client, check, new_collection_ids)
        elif check_type == "checkout_link_created":
            passed, detail = await check_checkout_link_created(client, check, new_draft_order_ids)
        elif check_type == "tool_called":
            passed, detail = check_tool_called(tools_called, check)
        else:
            passed, detail = False, f"Unknown check type: {check_type}"
        check_results.append({"check": check, "passed": passed, "detail": detail})

    task_passed = all(c["passed"] for c in check_results)
    return {"id": task["id"], "category": task["category"], "passed": task_passed, "checks": check_results}


async def run_task(client: ShopifyClient, task: dict) -> dict[str, Any]:
    before = await snapshot_state(client)
    agent_result = await run_agent_task(task["prompt"])

    if agent_result["error"]:
        return {"id": task["id"], "category": task["category"], "passed": False, "error": agent_result["error"], "checks": []}

    after = await snapshot_state(client)
    new_collection_ids = [c for c in after["collection_ids"] if c not in before["collection_ids"]]
    new_draft_order_ids = [d for d in after["draft_order_ids"] if d not in before["draft_order_ids"]]

    new_discount_codes = []
    for candidate in ["EVALSUMMER15", "EVALFLASH20", "EVALWELCOME10"]:
        new_discount_codes.append(candidate)  # checked for existence, not assumed created

    result = await score_task(client, task, agent_result["tools_called"], new_collection_ids, new_draft_order_ids, new_discount_codes)
    await cleanup_new_resources(client, new_collection_ids, new_draft_order_ids, new_discount_codes)
    return result


# -------------------------------------------------------------- report

def summarize(results: list[dict]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    by_category: dict[str, dict[str, int]] = {}
    for r in results:
        cat = by_category.setdefault(r["category"], {"total": 0, "passed": 0})
        cat["total"] += 1
        cat["passed"] += 1 if r["passed"] else 0
    return {
        "total": total,
        "passed": passed,
        "success_rate": round(passed / total, 4) if total else 0.0,
        "by_category": by_category,
    }


async def run_suite(client: ShopifyClient, tasks: list[dict]) -> list[dict]:
    results = []
    for task in tasks:
        console.print(f"Running [bold]{task['id']}[/bold] ({task['category']})...")
        result = await run_task(client, task)
        status = "[green]PASS[/green]" if result["passed"] else "[red]FAIL[/red]"
        console.print(f"  {status}")
        results.append(result)
    return results


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", type=int, default=None, help="Only run the first N tasks")
    parser.add_argument("--skip-cache-comparison", action="store_true", help="Run once with current CACHE_ENABLED instead of twice")
    args = parser.parse_args()

    tasks = yaml.safe_load(TASKS_PATH.read_text())
    if args.dry_run:
        tasks = tasks[: args.dry_run]

    write_mcp_config()
    RESULTS_DIR.mkdir(exist_ok=True)

    if args.skip_cache_comparison:
        client = ShopifyClient()
        results = await run_suite(client, tasks)
        metrics = client.get_metrics()
        await client.aclose()
        traffic_reduction = None
    else:
        os.environ["CACHE_ENABLED"] = "false"
        client_off = ShopifyClient(cache_enabled=False)
        console.print("\n[bold]--- Pass 1: cache disabled ---[/bold]")
        results = await run_suite(client_off, tasks)
        metrics_off = client_off.get_metrics()
        await client_off.aclose()

        client_on = ShopifyClient(cache_enabled=True)
        console.print("\n[bold]--- Pass 2: cache enabled ---[/bold]")
        await run_suite(client_on, tasks)
        metrics_on = client_on.get_metrics()
        await client_on.aclose()

        metrics = {"cache_disabled": metrics_off, "cache_enabled": metrics_on}
        if metrics_off["requests_sent_to_shopify"]:
            traffic_reduction = round(1 - (metrics_on["requests_sent_to_shopify"] / metrics_off["requests_sent_to_shopify"]), 4)
        else:
            traffic_reduction = None

    summary = summarize(results)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = {
        "timestamp": timestamp,
        "summary": summary,
        "results": results,
        "metrics": metrics,
        "traffic_reduction": traffic_reduction,
    }
    (RESULTS_DIR / f"eval_{timestamp}.json").write_text(json.dumps(output, indent=2, default=str))

    table = Table(title="Per-category results")
    table.add_column("Category")
    table.add_column("Passed")
    table.add_column("Total")
    for cat, stats in summary["by_category"].items():
        table.add_row(cat, str(stats["passed"]), str(stats["total"]))
    console.print(table)

    reduction_str = f"{traffic_reduction * 100:.1f}%" if traffic_reduction is not None else "N/A"
    console.print(
        f"\n[bold]Task success: {summary['passed']}/{summary['total']} "
        f"({summary['success_rate'] * 100:.1f}%) · API traffic reduction: {reduction_str}[/bold]"
    )


if __name__ == "__main__":
    asyncio.run(main())
