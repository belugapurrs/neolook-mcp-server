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
