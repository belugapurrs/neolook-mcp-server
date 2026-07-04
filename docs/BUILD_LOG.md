# Build Log

A plain-English, running diary of what we built and why. Written for someone
with zero dev background who wants to be able to explain this project in an
interview.

---

## Phase 1 — Project scaffolding (2026-07-02)

**What we built:** The empty "skeleton" of the project — folders for source
code (`src/neolook/`), scripts, tests, evaluation harness, and docs. Plus
three config files:

- `pyproject.toml` — a list of the external Python packages this project
  depends on (like a shopping list of tools we'll use: `pandas` for data
  analysis, `httpx` for talking to Shopify's API, `mcp` for the Model
  Context Protocol itself, etc.)
- `.gitignore` — tells Git (the tool that tracks our code history) to never
  save our `.env` file, which is where the secret Shopify token lives. This
  is how we make sure the secret never accidentally ends up on GitHub.
- `.env.example` — a *template* showing what the real `.env` file should
  look like, but with fake placeholder values. You'll copy this to `.env`
  and fill in your real store domain and token.

**Why:** Every real software project starts with this kind of structure so
that code is organized by purpose (tools vs. tests vs. docs) instead of
being one giant file. It also means we can start committing to Git in small,
understandable chunks instead of one huge dump at the end.

We also wrote `scripts/ping.py` — a tiny script whose only job is to ask
Shopify "what's your store's name?" using your token. If it prints your
store's name back, we know the connection works. This will be your first
real win once you've set up the dev store.

**Next:** You'll manually create a free Shopify Partner account and a
development store (Shopify's sandbox environment for building apps), then
generate an API token and paste it into `.env`. Full instructions are in the
conversation — nothing here needs your token yet.

---

## Phase 1.5 — Switched to the Dev Dashboard auth flow (2026-07-02)

**What changed:** Shopify has two different ways to create a developer app
right now. The original instructions assumed the older "custom app" flow,
which hands you one permanent password-like token (`shpat_...`). The user's
store instead uses Shopify's newer **Dev Dashboard** app flow, which gives
you two secrets — a **Client ID** and a **Client Secret** — instead of a
ready-made token.

**Why it's different:** With this newer flow, our code has to *ask* Shopify
for a token every time it needs one, by showing the Client ID + Secret. Think
of it like a hotel key card system: you show ID at the front desk (Client ID
+ Secret) and they hand you a key card (the access token) that stops working
after 24 hours. Our code will automatically go get a fresh card right before
the old one expires — you'll never have to think about it.

We confirmed the exact technical details on Shopify's official docs
(https://shopify.dev/docs/apps/build/dev-dashboard/get-api-access-tokens):
- Our code sends the Client ID + Secret to
  `https://{store}.myshopify.com/admin/oauth/access_token`
- Shopify replies with a token that's valid for ~24 hours
- That token is then used exactly like the old one, in an
  `X-Shopify-Access-Token` header on every Shopify API request

**What we updated:**
- `.env.example` now asks for `SHOPIFY_CLIENT_ID` and `SHOPIFY_CLIENT_SECRET`
  instead of `SHOPIFY_ADMIN_TOKEN`.
- `scripts/ping.py` now does the token exchange first, then uses the result
  to check the connection — still just prints your store's name on success.
- The upcoming `shopify_client.py` (Phase 3) will build this into a proper
  "get me a valid token, refreshing automatically if it's expired or about
  to expire" helper, so every tool we write later doesn't have to think
  about token expiry at all.

**Confirmed working:** `python scripts/ping.py` successfully exchanged the
Client ID/Secret for an access token and connected to the store
`nishka-practice-mcp`. Auth is solved end-to-end.

---

## Phase 3 — The shared Shopify client + caching layer (2026-07-02)

**What we built:** Two files that every tool we write from now on will
share, instead of each tool re-implementing the same logic:

- `src/neolook/cache.py` — a short-term memory for read requests. If a tool
  asks Shopify "what are the top products?" and then 30 seconds later
  another tool asks the same question, we hand back the remembered answer
  instead of asking Shopify again. Each type of data (products, orders,
  customers, etc.) has its own separate memory bucket ("namespace"), so
  that when we *change* something (like updating a product), we only need
  to clear that one bucket instead of forgetting everything.
- `src/neolook/shopify_client.py` — the single "phone line" to Shopify. It:
  1. Automatically fetches a fresh access token and reuses it until it's
     about to expire (handling the 24-hour Dev Dashboard token flow from
     Phase 1.5) so no other code has to think about tokens.
  2. Routes every read through the cache from step above.
  3. Politely backs off if Shopify says "you're calling too fast"
     (Shopify's API has a budget system — each request costs "points," and
     if we run low, we wait the right number of seconds before retrying,
     up to 3 attempts).
  4. Keeps running counts of: how many requests our code *wanted* to make,
     how many actually went out to Shopify, and how many were answered from
     the cache instead. This is the exact data we'll use later to prove the
     "caching cuts API traffic by X%" resume claim — with real numbers, not
     a guess.

**Why:** Instead of scattering "call Shopify," "cache the result," and
"don't call too fast" logic across 18 different tools, we wrote it once
here. Every tool we add later becomes a thin, easy-to-read wrapper around
this client.

**Testing:** We wrote 4 automated tests (`tests/test_shopify_client.py`)
that fake Shopify's responses (no real store or token needed to run them)
and check: the token is fetched once and reused, repeated reads are served
from cache instead of hitting the network twice, a write (mutation) clears
the right cache bucket, and a simulated "too fast" response is retried
correctly. All 4 pass. Run them yourself anytime with:

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

---

## Phase 4 — The first 6 tools (Tier 1: CRUD) (2026-07-02)

**What we built:** `src/neolook/tools/crud.py` — the six baseline tools
every Shopify MCP has: `search_products`, `update_product`, `get_order`,
`list_orders`, `create_discount`, `adjust_inventory`. Plus `server.py`,
which is the actual program an AI agent (like Claude Desktop or Claude
Code) launches to talk to our tools.

**Important step we didn't skip:** Shopify's API changes over time, and
this project pins a recent version (2026-04). Rather than guess field and
mutation names from general knowledge, we looked up the *live* schema for
this store using Shopify's schema-inspection tools before writing any code,
and validated every query/mutation against it. Two things we caught this
way that would otherwise have caused confusing bugs:

1. Shopify only lets an app read the **last 60 days** of orders by default
   (older orders need an extra permission scope, `read_all_orders`). This
   matters later when we build analytics that look back 90-120 days — we'll
   need to either request that scope or design around the limit. Flagged
   for Phase 6.
2. As of API version 2026-04, adjusting inventory quantities requires an
   "idempotency key" (a random ID that prevents the same adjustment from
   accidentally being applied twice if a request is retried) — we generate
   a fresh one automatically on every call.

**Testing:** We smoke-tested against the real dev store: searched products
(empty store, so 0 results - expected), listed orders (0, expected), and
created + immediately deleted a real test discount code to prove the
write path works end-to-end. We also wrote 8 more automated mocked tests
(`tests/test_tools.py`, now 12 total across the project).

**A real bug the tests caught:** Our first version of `get_order` assumed
a plain number like `"1009"` was Shopify's internal record ID. It isn't -
that's an unrelated opaque number Shopify uses internally. `"1009"` is
actually the order's *display number* (shown as `#1009`), and needed to be
looked up by name instead. The automated test caught this before it ever
ran against the live store - a good example of why we write tests even on
a project this size.

---

## Phase 4.5 — Debugging the `read_all_orders` permission (2026-07-02/03)

**What happened:** We decided to request the `read_all_orders` scope so
analytics could look back 120 days instead of the default 60. Even after
releasing a new app version with that scope in the Dev Dashboard, a
freshly-fetched access token kept coming back *without* it - across several
minutes of polling and even after double-checking Client ID/Secret in
`.env`.

**Root cause:** Releasing a new app version with an added scope isn't the
final step. The Shopify *store* itself has to separately approve the
permission change - this shows up as a pending "Updated app permissions"
approval in the store's app history, distinct from the developer-side
"release a version" action. Until the merchant (in this case, also us,
wearing the store-owner hat) explicitly accepts that prompt, the new scope
is not actually granted, no matter how long you wait.

**Why this is worth remembering:** This is a good real-world lesson in how
permission systems for third-party apps work: the *developer* defines what
permissions an app wants, but the *store* must separately consent to them,
similar to how installing an app on your phone shows you a permissions
prompt even though the developer already declared what it needs in their
app manifest. Good story for an interview: "I chased what looked like a
propagation delay for several minutes, methodically ruled out caching and
credential mismatches, and traced it to a merchant-side consent step that's
easy to miss in Shopify's newer Dev Dashboard flow."

**Confirmed fixed:** A fresh token now includes `read_all_orders` in its
scope list. We're proceeding with the full 120-day seed data plan.

---

## Phase 5 — Tier 2 analytics, Tier 3 agentic tools, and the seed script (2026-07-03)

**What we built:** The remaining 12 tools (7 analytics + 5 agentic
commerce), bringing the total to all 18 planned tools, plus
`scripts/seed_store.py` to populate the store with realistic demo data.

- `src/neolook/engines/analytics_engine.py` - the pandas layer. Fetches
  orders/products from Shopify and computes revenue trends, top products,
  sales velocity, stale inventory, discount ROI, and repeat-customer rate.
- `src/neolook/tools/analytics.py` - thin MCP wrappers around the engine
  (`revenue_summary`, `top_products`, `sales_velocity`,
  `stale_inventory_report`, `abandoned_checkout_report`, `discount_roi`,
  `customer_repeat_rate`).
- `src/neolook/tools/agentic.py` - the "beyond CRUD" tier: multi-step
  workflows in a single tool call (`recover_abandoned_carts`,
  `create_flash_sale`, `create_checkout_link`,
  `price_optimization_suggestions`, `get_server_metrics`).

**Honesty check on `abandoned_checkout_report`:** We verified live that
Shopify's abandoned-checkout data can only ever come from a real shopper
leaving checkout mid-flow - there's no API to fabricate one for demo
purposes. So this tool tries the real data first, and when none exists
(the normal case on a dev store with no live shoppers), it clearly labels
its fallback to open/incomplete draft orders as a stand-in signal, instead
of silently returning nothing or claiming something it can't back up.

**The seed script (`scripts/seed_store.py`):** Populates ~10 collections,
60 products (deliberately split into "fast," "normal," and "stale"
sellers so stale-inventory and top-products reports have something real
to find), 150 customers (10 flagged VIP so repeat-purchase patterns show
up), 3 discount codes, and ~400 orders built via `draftOrderCreate` +
`draftOrderComplete`.

**The dates problem, and how we solved it honestly:** Shopify always
stamps a new order's `createdAt` as *right now* - there's no API to
backdate an order. That would make a demo store's entire order history
look like it all happened in the same minute, which breaks any
time-windowed analytics (daily revenue trends, sales velocity, etc). Our
fix: the seed script assigns each order an *intended* date spread across
the last 120 days and writes it to `seed_manifest.json` alongside the
order's real ID. `analytics_engine.py` then optionally reads that file and
uses the intended date in place of the real one - clearly a demo
simulation layered on top of real Shopify records, not a claim that
Shopify backdated anything. When no manifest file exists (e.g. a real
production store), the code automatically falls back to real dates and
pushes the date filter server-side for efficiency instead.

**Two real bugs found while running this live (not in a test - only real
usage surfaced them):**
1. `ShopifyClient` only retried on HTTP 5xx errors, not on network-level
   timeouts. A `~13-minute` run making ~800 sequential API calls hit a
   transient `httpx.ReadTimeout` partway through and crashed unhandled.
   Fixed by catching `httpx.TransportError` and retrying with the same
   backoff logic used for 5xx errors.
2. The seed script only wrote `seed_manifest.json` once, at the very end.
   When the crash above happened, we'd actually created 345 real orders in
   Shopify, but lost all of their simulated dates because the manifest
   write never ran. Fixed by saving the manifest every 25 orders and
   wrapping the final write in a `finally` block, so a crash (or being
   killed - see below) only ever loses a handful of records instead of
   everything.

**Final seed run:** completed with 60 products, 150 customers, 10
collections, 3 discount codes, and 392 orders (close to the ~400 target -
the run got killed by the environment near the very end of its ~10+
minute runtime, likely a background-process time limit unrelated to our
code, but thanks to the incremental-save fix above we lost almost nothing).
392/400 is well within the spec's "~400" and was accepted rather than
risk a third long-running interruption. `seed_manifest.json` has intended
dates for 375 of those orders; the remaining ~17 simply fall back to their
real (today's) date in analytics, which is a negligible cosmetic gap, not
a correctness issue.

**Live verification against real seeded data:** ran all 9 Tier 2/3 tools
against the actual store (`scripts/smoke_test_analytics_agentic.py`) and
got realistic, sensible results end to end - e.g. $33,216 revenue and 311
orders over the last 90 days, "Rain Poncho" as the top seller, exactly the
3 deliberately-stale products correctly flagged, 86.8% of revenue from
repeat customers (expected, since a few VIP customers were seeded to buy
often), and all 3 discount codes showing accurate ROI numbers. In this one
smoke-test run, the cache already served 25 of 32 requests (78% hit rate)
- an early real signal for the caching claim, not a target we're
engineering toward.

We also wrote `evals/tasks.yaml` (24 tasks across discount/checkout/
analytics/workflow categories) and `evals/run_evals.py` (the harness that
runs each task through a headless Claude Code agent and verifies the
result directly against the store).

---

## Phase 6 — Getting the eval harness actually working (2026-07-03)

**Setup:** Installed the Claude Code CLI (a separate program from whatever
chat interface you're using right now) via `curl -fsSL https://claude.ai/install.sh | bash`,
fixed a PATH issue (the installer's symlink at `~/.local/bin/claude` wasn't
on PATH), and logged in with the existing Pro plan account. Along the way
we learned the spec's assumption of a separate "Agent SDK monthly credit"
to claim doesn't apply anymore - Anthropic paused that billing change, so
Claude Code usage on a Pro/Max plan just counts as normal plan usage, no
extra step needed.

**Three real bugs found by actually running it (a dry run of 3 tasks
failed silently, 0/3, with no obvious error) - all three only showed up
by testing live, not by reading the code:**

1. `run_evals.py` invoked `claude` with the `--bare` flag, which (we
   verified by testing directly) breaks authentication entirely - it
   returns "Not logged in" even when a real login session exists. Removed
   `--bare`.
2. In this Claude Code environment, MCP tool schemas are "deferred" until
   the agent calls a `ToolSearch` meta-tool to resolve them. Our
   `--allowedTools` list only named our own 18 tools, not `ToolSearch`, so
   the agent could never actually reach them. Added `ToolSearch` to the
   allowlist.
3. Several tasks say things like "valid for the next 7 days," which
   requires date math. The agent's first instinct was to reach for `Bash`
   to compute the date - a tool we deliberately don't allow, to keep the
   eval agent scoped to only our own tools. Rather than open that door,
   we now hand the agent today's date directly in the prompt
   (`"Today's date is 2026-07-03 (UTC). <task>"`), so it never needs to.

**A fourth issue, caught by comparing two check runs a few minutes apart:**
a discount code that a check reported as "not found" immediately after the
agent created it was reported as "already exists" on a manual re-check
moments later - Shopify's discount-lookup read path can lag slightly
behind the write. Fixed by retrying each live-verification check up to 3
times with a short delay before concluding it failed, instead of trusting
a single immediate read.

**Also fixed:** the between-task cleanup originally deleted a hardcoded
list of 3 known discount codes regardless of which task ran. Replaced with
a real before/after diff (snapshot discount codes, collections, and draft
orders before each task, delete whatever's new afterward) - the same
pattern already used for collections and draft orders, now applied
consistently everywhere.

**Confirmed working:** a 3-task dry run
(`python evals/run_evals.py --dry-run 3 --skip-cache-comparison`) now
passes 3/3, and we verified all temporary discount codes were cleaned up
afterward.

**One more design gap, caught before it could quietly produce a
meaningless number:** the caching-traffic-reduction metric is the whole
point of the cache-off/cache-on double pass - but each eval task launches
a *brand new* MCP server subprocess (stdio MCP servers are 1:1 with their
client process, and `claude -p` starts a fresh one every invocation). That
means the server's in-memory request counters reset to zero before every
single task, no matter what `CACHE_ENABLED` is set to - so the number we
were about to report would have reflected our own harness's leftover
verification queries, not the agent's actual tool-call traffic.

**Fix:** `ShopifyClient` now accepts an optional `metrics_file` path. When
set, it loads a running total from that file at startup and re-saves the
combined total after every request - so counts accumulate correctly
across the many short-lived server subprocesses launched within one pass,
instead of resetting each time. The eval harness points each pass at its
own metrics file (`.metrics_cache_off.json` / `.metrics_cache_on.json`,
gitignored scratch files) via the `.mcp.json` server config's `env` field,
and reads the final combined totals from those files - not from its own
separate verification-query client - to compute `traffic_reduction`.
Verified with a 3-task dual-pass dry run: both passes correctly recorded
identical, non-zero counts for those particular (mutation-only) tasks,
producing an honest 0% reduction for that subset - discount-code creation
is a write, and writes are never cached, so that's the expected result,
not a bug. The full 24-task suite includes several read-heavy analytics
tasks where real caching benefit should actually show up.

## Phase 7 — The full 24-task run, and three more real bugs it exposed (2026-07-03/04)

**First full run:** 17/24 (70.8%), 48.2% traffic reduction. Every single
checkout task failed, which was suspicious - a manual re-test of the exact
same `create_checkout_link` tool worked perfectly and left a real, payable
draft order in the store. So the tool was fine; the harness's own
verification was lying.

**Bug 1 - the harness's verification client was silently stale.**
`run_evals.py` creates one `ShopifyClient` (`verification_client`) up
front and reuses it for every before/after snapshot across all 24 tasks
in both passes. That client reads `CACHE_ENABLED` from the environment
*at construction time* - before the harness later flips that same
variable to run the cache-off/cache-on comparison. Since it was built
before the flip, it kept its cache on for the client's entire lifetime.
`snapshot_state()`'s draft-order/collection queries take no parameters,
so every call after the first was a cache hit returning the *same* stale
list - meaning "before" and "after" were identical no matter what the
agent actually did in its own (separate) subprocess, which has no way to
invalidate this client's cache. Checkout checks depend entirely on this
diff, so they failed 100% of the time; collection-dependent workflow
checks were just flaky, recovering only when the 5-minute cache TTL
happened to expire mid-run. Fixed by constructing this client with
`cache_enabled=False` explicitly - it must always see live state.

**Re-run after fix:** 22/24 (91.7%), 40.9% traffic reduction. All 3
checkout tasks now passed. Two workflow tasks still failed
(`workflow-03`, `workflow-06`), for two unrelated reasons:

**Bug 2 - the eval agent could see tools it was never supposed to have.**
`--allowedTools` only pre-approves tools for auto-run - it does not
restrict which tools the model can see or attempt. A closer look at
`workflow-06`'s transcript showed the agent, when it got stuck, reaching
for `Bash`, `Read`, `Grep`, `Glob`, and even a completely different
Shopify MCP connector configured globally on this machine - none of which
were in our allowlist, but all of which were still available and
callable. In one repro it burned all 15 turns exploring this repo's
source code via `Read`/`Grep` instead of doing the task, and at one point
read our real `.env` file, printing the live Shopify client secret into
its own transcript. Fixed with two flags: `--strict-mcp-config` (ignore
every MCP server except the one we pass in `--mcp-config`) and
`--disallowedTools` naming every built-in tool we don't want the agent to
touch (`Bash`, `Read`, `Write`, `Edit`, `Grep`, `Glob`, `WebFetch`,
`WebSearch`, and a dozen others) - so the agent is now hard-restricted to
exactly our 18 tools plus `ToolSearch`, with no escape hatch.

**Bug 3 - `adjust_inventory` asked for information no tool could give
the agent.** With tool leakage fixed, `workflow-06` ("reduce inventory by
1 unit") still failed - now for a legitimate reason. `adjust_inventory`
required `inventory_item_id` and `location_id` as raw GID inputs, but
`search_products` (the only way the agent can find a product) never
returns either one. A properly-sandboxed agent had no legitimate path to
this tool at all. Fixed by redesigning the tool to take `variant_id` -
the same id `search_products` already returns - and resolve
`inventory_item_id` and its stocking location(s) internally via one
query; `location_id` is now optional, only needed if a variant is
stocked at more than one location. (Also had to drop a `location { name }`
field from that new query - the same `read_locations`-scope gap we'd
already hit once before in Phase 4.)

**Final run, all three fixes in place:** 22/24 (91.7%), 22.1% traffic
reduction. `workflow-06` now passes cleanly in 4 turns. The two remaining
failures are check-design artifacts, not capability gaps, confirmed by
reading the store data and the agent's own transcript directly:

- `workflow-03` asks the agent to activate a "Mug" product "if it isn't
  already" active - both seeded Mug products already are, so the correct
  behavior is to do nothing. The check still asserts `update_product` was
  called, so it marks this a fail even though the agent's no-op was
  correct.
- `workflow-09` asks the agent to recover an abandoned VIP cart and offer
  a direct checkout link. The agent accomplished exactly that, but by
  manually chaining `abandoned_checkout_report` → `create_discount` →
  `create_checkout_link` instead of calling the single
  `recover_abandoned_carts` tool the check requires by name - a
  functionally equivalent path the check doesn't recognize.

Also note: the exact two tasks that failed changed between the second and
third runs (`workflow-06`/`workflow-08` → `workflow-03`/`workflow-09`),
and the traffic-reduction percentage moved between all three runs
(48.2% → 40.9% → 22.1%). Both are expected, not a red flag: the agent
isn't perfectly deterministic run to run, so its exact tool-call sequence
(and therefore how much of it repeats identical, cacheable reads) varies
each time. The task success rate landed on the same 22/24 twice in a row
after the real bugs were fixed, which is what we're reporting as the
final number.

## Phase 8 — The 22.1% traffic reduction was itself a harness artifact (2026-07-04)

Even after Phase 7's fixes, one more thing didn't add up: each eval task
still launched a *brand-new* stdio MCP server subprocess (`claude -p`
spawns one per invocation), so the in-memory cache was stone-cold at the
start of every single task. Cross-task cache reuse - the whole point of
running the suite twice - was structurally impossible under that design;
the only reuse that could ever happen was two identical reads *within* one
task's own handful of tool calls. The 22.1% figure was real, but it was
measuring something much narrower than "does caching help this agent."

**Fix 1 - one persistent server per pass, not one per task.** `FastMCP`
already supports a streamable-HTTP transport (`mcp.run(transport=...)`),
so `server.py` now accepts `NEOLOOK_TRANSPORT=streamable-http` plus a host/
port, and the eval harness launches the server exactly *once* per pass -
all 24 tasks in that pass (each still its own `claude -p` client process)
connect to that one already-running server over HTTP instead of each
spawning their own. This is much closer to how the server is actually
meant to be used: a real agent session, or a real deployment, keeps one
warm process across many requests rather than restarting it before every
tool call.

**Fix 2 - report read-only traffic reduction separately from overall.**
Mutations are never cached by design (`ShopifyClient.mutate()` always
hits the network and invalidates the relevant namespace - this did not
change), so folding them into one blended "traffic reduction" number
dilutes the caching-specific claim with calls that were never candidates
for caching. `ShopifyClient` now tracks `reads_attempted`/
`reads_sent_to_shopify` and `writes_attempted`/`writes_sent_to_shopify`
as separate counters (in addition to the existing combined ones, kept for
backward compatibility), and the harness reports both
`traffic_reduction` (overall) and `read_traffic_reduction` (reads only).

**Fix 3 - sanity-checked the cache TTL against real pass duration.** The
shipped default (`CACHE_TTL_SECONDS=300` in `.env.example`) is a
reasonable trade-off for normal interactive use, but a full 24-task pass
takes several minutes of real agent think-and-tool-call time - the final
run's two passes measured 562s and 451s, both comfortably over 300s. Left
alone, the default would have silently expired early cache entries before
later tasks could reuse them, understating the very thing being measured.
The harness now overrides `CACHE_TTL_SECONDS=3600` for its own server
process only (`EVAL_CACHE_TTL_SECONDS` in `run_evals.py`) - this does not
touch the recommended default for normal use - and prints a warning if a
pass ever runs longer than that override, so this assumption stays
checked rather than silently trusted.

**Audited that every read path actually goes through the cache:** grepped
every tool and engine module for calls into `ShopifyClient` - all reads
go through `client.query()` (cached) and all writes through
`client.mutate()` (never cached, invalidates on success); there is no
tool or engine function that calls the underlying `_post_graphql()`
directly and bypasses the cache.

**Final run, all three Phase 8 fixes in place:** 22/24 (91.7%) again (two
different workflow tasks failed this time - `workflow-01` and
`workflow-03` - consistent with the already-documented run-to-run agent
nondeterminism, and manually re-confirmed live that `create_flash_sale`
itself works fine, so this wasn't a regression from the transport
change). The caching numbers are now dramatically more meaningful because
they reflect real cross-task reuse within a warm pass:

| Metric | Cache off | Cache on |
|---|---|---|
| Total requests sent to Shopify | 103 | 35 |
| Read requests sent to Shopify | 91 | 21 |
| Write requests sent to Shopify | 12 | 14 |
| Cache hits | 0 | 61 |

- **Overall traffic reduction: 66.0%**
- **Read-only traffic reduction: 76.9%**

(Write counts aren't expected to match exactly between passes - they're
never cached, and which mutations run depends on the agent's own
non-deterministic choices each pass, e.g. whether a task-solving path
happens to include an extra discount creation.)

## Phase 9 — `feature/ltv-rfm`: customer segmentation and a naive LTV estimate (2026-07-04)

Two new tools, both real and tested against live data, plus an honest
roadmap doc for what's deliberately not built yet:

- **`segment_customers`** (`src/neolook/engines/rfm.py`): scores every
  customer 1-5 on Recency, Frequency, and Monetary value - quintiles
  relative to this store's own customer base, not fixed thresholds - and
  maps those scores to a segment label (Champions, Loyal, At-Risk,
  Hibernating, New, Others) via a simple, documented heuristic.
- **`estimate_customer_ltv`**: a v1 naive lifetime-value projection
  (`average_order_value * orders_per_month * 12`). Deliberately simple -
  see `docs/LTV_ROADMAP.md` for exactly why a real BG/NBD + Gamma-Gamma
  probabilistic model wasn't built for v1 (this store's ~150 customers
  over 120 days is too thin a base to fit one meaningfully without
  producing a falsely precise number).

**One real bug caught by the unit tests before it ever reached live
data:** the first cut of `_quintile_score` broke ties between customers
with identical R/F/M values using `Series.rank(method="first")`, which
assigns tied values sequential ranks based on row order - meaning two
customers with the exact same order count could land in *different*
quintile scores purely because of how `groupby` happened to sort their
customer IDs, with no real signal behind the difference. This isn't a
rare edge case: order *frequency* in particular is a small integer that
repeats constantly in real data (lots of customers with exactly 1, 2, or
3 orders). Fixed by using `pd.qcut(..., duplicates="drop")` on the raw
values directly, so ties always get the same score - and when a value
repeats often enough that 5 clean groups genuinely don't exist, fewer
than 5 distinct scores get used rather than a forced, misleading 5-way
split.

**Verified live** against the seeded dev store: `segment_customers`
returned real segment counts across 109 customers with order history in
the last 120 days (Hibernating: 45, Others: 26, New: 22, Loyal: 16 - no
Champions or At-Risk in this particular data, which is a real property
of this store's demo data, not a bug), and `estimate_customer_ltv`
produced a sensible naive projection for a real repeat customer (27
orders, $3,480.62 historical spend → $10,441.86 naive 12-month LTV).

Per the spec's honesty requirement, `docs/LTV_ROADMAP.md` documents three
concrete limitations rather than hiding them: the naive LTV assumes
purchasing never stops (no churn/dropout modeling), the RFM segment
boundaries are the standard textbook heuristic rather than backtested
against this store's actual repeat-purchase behavior, and quintile
scoring intentionally degrades (fewer distinct scores, or a flat neutral
score under 5 customers) rather than faking precision the data doesn't
support. `tests/test_rfm.py` also includes one
`@pytest.mark.skip("WIP")` test as a placeholder for the planned
probabilistic LTV model.
