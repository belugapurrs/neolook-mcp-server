"""
Quick sanity check: confirms the Shopify Client ID / Client Secret in .env work.

Run this after you've created your Dev Dashboard app and pasted your Client ID
and Client Secret into .env. It does two things:
  1. Exchanges the Client ID + Secret for a short-lived access token (this is
     the "client credentials grant" - see https://shopify.dev/docs/apps/build/dev-dashboard/get-api-access-tokens).
  2. Uses that access token to run the smallest possible GraphQL query
     ({ shop { name } }) and prints the store name if it succeeds.
"""

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

DOMAIN = os.environ.get("SHOPIFY_STORE_DOMAIN")
CLIENT_ID = os.environ.get("SHOPIFY_CLIENT_ID")
CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET")
API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2026-04")

QUERY = "{ shop { name primaryDomain { url } } }"


def get_access_token() -> str:
    """Exchange Client ID + Client Secret for a short-lived access token."""
    url = f"https://{DOMAIN}/admin/oauth/access_token"
    data = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    response = httpx.post(url, data=data, timeout=30)

    if response.status_code != 200:
        print(f"ERROR exchanging credentials for a token: HTTP {response.status_code}")
        print(response.text)
        sys.exit(1)

    token_data = response.json()
    print(f"Got an access token (expires in {token_data['expires_in']} seconds).")
    return token_data["access_token"]


def main() -> None:
    if not DOMAIN or not CLIENT_ID or not CLIENT_SECRET or "your_client" in (CLIENT_ID or ""):
        print("ERROR: SHOPIFY_STORE_DOMAIN, SHOPIFY_CLIENT_ID and/or SHOPIFY_CLIENT_SECRET are missing from .env.")
        print("Copy .env.example to .env and fill in your real values first.")
        sys.exit(1)

    access_token = get_access_token()

    url = f"https://{DOMAIN}/admin/api/{API_VERSION}/graphql.json"
    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json",
    }

    response = httpx.post(url, headers=headers, json={"query": QUERY}, timeout=30)

    if response.status_code != 200:
        print(f"ERROR: HTTP {response.status_code}")
        print(response.text)
        sys.exit(1)

    data = response.json()

    if "errors" in data:
        print("ERROR: Shopify returned errors:")
        print(data["errors"])
        sys.exit(1)

    shop = data["data"]["shop"]
    print("SUCCESS! Connected to Shopify store:")
    print(f"  Name: {shop['name']}")
    print(f"  URL:  {shop['primaryDomain']['url']}")


if __name__ == "__main__":
    main()
