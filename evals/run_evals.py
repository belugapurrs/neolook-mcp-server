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
  - Logged in with `claude` using your existing Pro/Max plan. This suite
    runs on your normal plan usage, NOT a paid API key - there is no
    separate "Agent SDK credit" to claim (an earlier assumption in this
    project's spec that turned out to be outdated - see docs/BUILD_LOG.md).
    We deliberately never set ANTHROPIC_API_KEY, which would switch billing
    to pay-per-token API usage instead.

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

# `--allowedTools` only pre-approves tools - it does NOT exclude everything
# else. Confirmed live: even with only our MCP tools + ToolSearch allowed,
# the agent still had full Bash/Read/Write access (they're built-in tools,
# not gated by the allowlist) and burned an entire task's turns exploring
# this repo's source instead of doing the task - it even Read our real
# .env file, printing the live Shopify client secret into its transcript.
# These must be explicitly blocked so the agent can only use our tools.
BUILTIN_TOOLS_TO_BLOCK = [
    "Task", "Artifact", "Bash", "CronCreate", "CronDelete", "CronList", "DesignSync",
    "Edit", "EnterWorktree", "ExitWorktree", "Glob", "Grep", "ListMcpResourcesTool", "Monitor",
    "NotebookEdit", "PushNotification", "Read", "ReadMcpResourceDirTool",
    "ReadMcpResourceTool", "RemoteTrigger", "ReportFindings", "ScheduleWakeup",
    "SendMessage", "Skill", "TaskOutput", "TaskStop", "TodoWrite", "WebFetch",
    "WebSearch", "Write",
]

MAX_TURNS = 15
SUBPROCESS_TIMEOUT_SECONDS = 180


def write_mcp_config(metrics_file: Path | None = None) -> None:
    """Writes .mcp.json pointing at our server. When metrics_file is set,
    it's passed through as an env var so the server's ShopifyClient
    accumulates request/cache counters across the many short-lived
    subprocesses the eval harness launches (one per task) instead of each
    one starting from zero - see shopify_client.py's metrics_file param."""
    python_bin = REPO_ROOT / ".venv" / "bin" / "python"
    env = {"NEOLOOK_METRICS_FILE": str(metrics_file)} if metrics_file else {}
    config = {
        "mcpServers": {
            "neolook": {
                "command": str(python_bin),
                "args": ["-m", "neolook.server"],
                "env": env,
            }
        }
    }
    MCP_CONFIG_PATH.write_text(json.dumps(config, indent=2))


async def run_agent_task(prompt: str) -> dict[str, Any]:
    """Runs one task through headless Claude Code, returns {tools_called, raw_result, error}.

    Notes from live debugging (see docs/BUILD_LOG.md):
    - `--bare` breaks authentication entirely ("Not logged in"), so it's
      deliberately NOT used here even though it would otherwise be the
      cleaner/more isolated choice for reproducible eval runs.
    - This environment defers MCP tool schemas until a `ToolSearch` call
      resolves them, so `ToolSearch` must be in the allowlist alongside our
      own tools, or the agent can never actually reach them.
    - We hand the agent today's date directly instead of letting it reach
      for `Bash` to compute relative dates ("in 7 days") - keeps the tool
      surface scoped to just our tools + ToolSearch, per the spec's
      "auto-approved for only our tools" intent.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dated_prompt = f"Today's date is {today} (UTC). {prompt}"
    cmd = [
        "claude", "-p", dated_prompt,
        "--mcp-config", str(MCP_CONFIG_PATH),
        "--strict-mcp-config",
        "--allowedTools", "ToolSearch", *MCP_TOOL_NAMES,
        "--disallowedTools", *BUILTIN_TOOLS_TO_BLOCK,
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

CHECK_RETRIES = 3
CHECK_RETRY_DELAY_SECONDS = 2.0


async def _with_retry(check_fn):
    """Shopify's discount/collection reads can lag a moment behind a write
    (confirmed live: a just-created discount code returned "not found" on
    the first check, then "already exists" on a manual re-check seconds
    later). Retry a few times before concluding a check genuinely failed."""
    for attempt in range(CHECK_RETRIES):
        passed, detail = await check_fn()
        if passed or attempt == CHECK_RETRIES - 1:
            return passed, detail
        await asyncio.sleep(CHECK_RETRY_DELAY_SECONDS)
    return passed, detail


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

async def snapshot_state(client: ShopifyClient) -> dict[str, list]:
    collections_body = await client.query(
        'query { collections(first: 50, sortKey: TITLE, query: "title:Flash Sale*") { edges { node { id } } } }',
        namespace="collections",
    )
    drafts_body = await client.query(
        "query { draftOrders(first: 50, sortKey: UPDATED_AT, reverse: true) { edges { node { id } } } }",
        namespace="orders",
    )
    discounts_body = await client.query(
        """
        query { discountNodes(first: 50, sortKey: CREATED_AT, reverse: true) {
          edges { node { id discount { ... on DiscountCodeBasic { codes(first: 1) { edges { node { code } } } } } } }
        } }
        """,
        namespace="discounts",
    )
    discount_codes = []
    for e in discounts_body["data"]["discountNodes"]["edges"]:
        code_edges = (e["node"]["discount"] or {}).get("codes", {}).get("edges", [])
        if code_edges:
            discount_codes.append(code_edges[0]["node"]["code"])
    return {
        "collection_ids": [e["node"]["id"] for e in collections_body["data"]["collections"]["edges"]],
        "draft_order_ids": [e["node"]["id"] for e in drafts_body["data"]["draftOrders"]["edges"]],
        "discount_codes": discount_codes,
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
            passed, detail = await _with_retry(lambda: check_discount_exists(client, check))
        elif check_type == "discount_exists_scoped_to_collection":
            passed, detail = await _with_retry(lambda: check_discount_exists_scoped_to_collection(client, check, new_discount_codes))
        elif check_type == "collection_exists_with_products":
            passed, detail = await _with_retry(lambda: check_collection_exists_with_products(client, check, new_collection_ids))
        elif check_type == "checkout_link_created":
            passed, detail = await _with_retry(lambda: check_checkout_link_created(client, check, new_draft_order_ids))
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
    new_discount_codes = [c for c in after["discount_codes"] if c not in before["discount_codes"]]
    # Also check codes named explicitly in this task's own checks, in case
    # the discount-listing snapshot lags behind the write (same eventual-
    # consistency lag _with_retry guards against elsewhere).
    for check in task["checks"]:
        if check["type"] == "discount_exists" and check["code"] not in new_discount_codes:
            new_discount_codes.append(check["code"])

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


def _read_metrics_file(path: Path) -> dict[str, Any]:
    """Raw counters only (requests_attempted/requests_sent_to_shopify/
    cache_hits/throttle_events) - no derived hit-rate here, since
    requests_sent_to_shopify mixes cache-missed reads with mutations (which
    are never cached), so a hits/(hits+sent) ratio would be misleading."""
    if not path.exists():
        return {"requests_attempted": 0, "requests_sent_to_shopify": 0, "cache_hits": 0, "throttle_events": 0}
    return json.loads(path.read_text())


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", type=int, default=None, help="Only run the first N tasks")
    parser.add_argument("--skip-cache-comparison", action="store_true", help="Run once with current CACHE_ENABLED instead of twice")
    args = parser.parse_args()

    tasks = yaml.safe_load(TASKS_PATH.read_text())
    if args.dry_run:
        tasks = tasks[: args.dry_run]

    RESULTS_DIR.mkdir(exist_ok=True)
    # Used only for our own verification queries (snapshot_state, checks) -
    # NOT the metric of interest. The agent's actual tool-call traffic is
    # tracked separately via the metrics_file mechanism below, because each
    # task launches a fresh MCP server subprocess whose in-memory client
    # would otherwise reset to zero every time (see docs/BUILD_LOG.md).
    # cache_enabled=False is required here: this one instance is reused
    # across all tasks in both passes, and the agent's mutations happen in a
    # separate subprocess that can never invalidate this client's cache. A
    # cached snapshot would silently go stale for up to CACHE_TTL_SECONDS,
    # making every diff-based check (new checkout links, new collections)
    # fail regardless of what the agent actually did.
    verification_client = ShopifyClient(cache_enabled=False)

    if args.skip_cache_comparison:
        metrics_file = RESULTS_DIR / ".metrics_single_pass.json"
        metrics_file.unlink(missing_ok=True)
        write_mcp_config(metrics_file)
        results = await run_suite(verification_client, tasks)
        metrics = {"agent_tool_traffic": _read_metrics_file(metrics_file)}
        traffic_reduction = None
    else:
        metrics_off_file = RESULTS_DIR / ".metrics_cache_off.json"
        metrics_on_file = RESULTS_DIR / ".metrics_cache_on.json"
        metrics_off_file.unlink(missing_ok=True)
        metrics_on_file.unlink(missing_ok=True)

        os.environ["CACHE_ENABLED"] = "false"
        write_mcp_config(metrics_off_file)
        console.print("\n[bold]--- Pass 1: cache disabled ---[/bold]")
        results = await run_suite(verification_client, tasks)
        metrics_off = _read_metrics_file(metrics_off_file)

        os.environ["CACHE_ENABLED"] = "true"
        write_mcp_config(metrics_on_file)
        console.print("\n[bold]--- Pass 2: cache enabled ---[/bold]")
        await run_suite(verification_client, tasks)
        metrics_on = _read_metrics_file(metrics_on_file)

        metrics = {"cache_disabled": metrics_off, "cache_enabled": metrics_on}
        if metrics_off["requests_sent_to_shopify"]:
            traffic_reduction = round(1 - (metrics_on["requests_sent_to_shopify"] / metrics_off["requests_sent_to_shopify"]), 4)
        else:
            traffic_reduction = None

    await verification_client.aclose()

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
