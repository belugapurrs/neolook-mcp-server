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
