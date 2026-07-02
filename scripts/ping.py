"""
Quick sanity check: confirms the Shopify Admin API token in .env works.

Run this after you've created your dev store and pasted your token into .env.
It sends the smallest possible GraphQL query ({ shop { name } }) and prints
the store name if authentication succeeds.
"""

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

DOMAIN = os.environ.get("SHOPIFY_STORE_DOMAIN")
TOKEN = os.environ.get("SHOPIFY_ADMIN_TOKEN")
API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2026-04")

QUERY = "{ shop { name primaryDomain { url } } }"


def main() -> None:
    if not DOMAIN or not TOKEN or "xxx" in TOKEN:
        print("ERROR: SHOPIFY_STORE_DOMAIN and/or SHOPIFY_ADMIN_TOKEN are missing from .env.")
        print("Copy .env.example to .env and fill in your real values first.")
        sys.exit(1)

    url = f"https://{DOMAIN}/admin/api/{API_VERSION}/graphql.json"
    headers = {
        "X-Shopify-Access-Token": TOKEN,
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
