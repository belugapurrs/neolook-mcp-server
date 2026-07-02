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
