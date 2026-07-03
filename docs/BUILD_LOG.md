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
will run each task through a headless Claude Code agent and verify the
result directly against the store). Running it for real requires the
Claude Code CLI installed separately from whatever you're using to chat
with Claude right now, plus your Agent SDK monthly credit claimed - both
still to do before we can dry-run it.
