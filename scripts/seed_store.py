"""
Seeds the dev store with realistic demo data so the analytics tools have
something meaningful to analyze: collections, products (some fast-selling,
some stale), customers (a few VIPs who buy often), discount codes, and
orders.

Honesty note on dates: Shopify's API always stamps a new order's createdAt
as "now" - there is no way to backdate an order through the API. So this
script does the simplest honest thing instead of faking it: it assigns each
order an *intended* historical date (spread across the last N days) and
records that in seed_manifest.json, alongside the real Shopify order ID.
NeoLook's analytics tools can then optionally read this manifest to
simulate a realistic 120-day order timeline for demo purposes - clearly a
simulation, not a claim that Shopify itself backdated anything. See
docs/BUILD_LOG.md Phase 5 for the full explanation.

Usage:
    python scripts/seed_store.py                     # full seed (60/150/400)
    python scripts/seed_store.py --products 5 --customers 5 --orders 10   # small test run
    python scripts/seed_store.py --wipe               # delete everything this script created
"""

import argparse
import asyncio
import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.progress import Progress

from neolook.shopify_client import ShopifyClient, ShopifyAPIError

load_dotenv()
console = Console()

SEED_TAG = "neolook-seed"
RNG_SEED = 42
MANIFEST_PATH = Path(__file__).parent.parent / "seed_manifest.json"
HISTORY_DAYS = 120
VIP_CUSTOMER_COUNT = 10
DISCOUNT_CODES = ["NEOLOOK-WELCOME10", "NEOLOOK-SALE15", "NEOLOOK-VIP20"]

# 10 categories x 6 products = 60 products. Within each category: 1 "stale"
# (rarely/never ordered), 2 "fast" (ordered often), 3 "normal".
CATEGORIES = [
    ("Apparel", ["T-Shirt", "Hoodie", "Jacket", "Cap", "Socks", "Scarf"], (15, 80)),
    ("Home Goods", ["Candle", "Throw Blanket", "Wall Clock", "Vase", "Picture Frame", "Cushion"], (12, 60)),
    ("Electronics Accessories", ["Phone Case", "USB Cable", "Wireless Charger", "Laptop Sleeve", "Earbuds Case", "Screen Protector"], (8, 45)),
    ("Outdoors", ["Water Bottle", "Camping Mug", "Hiking Backpack", "Trail Snack Bar", "Rain Poncho", "Compass"], (10, 120)),
    ("Kitchen", ["Cutting Board", "Ceramic Mug", "Apron", "Spice Rack", "Tea Towel", "Trivet"], (10, 55)),
    ("Office", ["Notebook", "Desk Organizer", "Pen Set", "Mouse Pad", "Sticky Notes", "Planner"], (6, 35)),
    ("Fitness", ["Yoga Mat", "Resistance Bands", "Water Bottle Pro", "Gym Towel", "Foam Roller", "Jump Rope"], (10, 70)),
    ("Beauty", ["Lip Balm", "Face Serum", "Bath Bomb", "Hand Cream", "Makeup Bag", "Hair Brush"], (8, 40)),
    ("Kids", ["Plush Toy", "Puzzle Set", "Coloring Book", "Building Blocks", "Kids Backpack", "Story Book"], (9, 45)),
    ("Pet Supplies", ["Dog Leash", "Cat Toy", "Pet Bed", "Food Bowl", "Chew Toy", "Pet Brush"], (8, 50)),
]

FIRST_NAMES = [
    "Ava", "Liam", "Maya", "Noah", "Zara", "Ethan", "Priya", "Lucas", "Nora", "Kai",
    "Isla", "Omar", "Elena", "Leo", "Aisha", "Mateo", "Ruby", "Ivan", "Sofia", "Theo",
]
LAST_NAMES = [
    "Bennett", "Cole", "Reyes", "Patel", "Nguyen", "Fischer", "Morales", "Hughes",
    "Kramer", "Okafor", "Silva", "Torres", "Whitman", "Yamamoto", "Zimmer",
]


def gid(resource: str, numeric_id: str) -> str:
    return f"gid://shopify/{resource}/{numeric_id}"


class Seeder:
    def __init__(self, client: ShopifyClient, n_products: int, n_customers: int, n_orders: int):
        self.client = client
        self.n_products = n_products
        self.n_customers = n_customers
        self.n_orders = n_orders
        self.location_id: str | None = None
        self.collections: list[dict] = []
        self.products: list[dict] = []  # {id, title, velocity, variant_id, price}
        self.customers: list[dict] = []  # {id, email, is_vip}
        self.discount_codes: list[str] = []
        self.manifest: list[dict] = []

    async def get_location(self) -> str:
        body = await self.client.query(
            "query Locations($first: Int!) { locations(first: $first) { edges { node { id } } } }",
            {"first": 5},
            namespace="inventory",
        )
        edges = body["data"]["locations"]["edges"]
        if not edges:
            raise RuntimeError("Store has no locations - can't seed inventory.")
        return edges[0]["node"]["id"]

    async def create_collections(self) -> None:
        with Progress(console=console) as progress:
            task = progress.add_task("Creating collections...", total=len(CATEGORIES))
            for name, _, _ in CATEGORIES:
                try:
                    body = await self.client.mutate(
                        """
                        mutation CreateCollection($input: CollectionInput!) {
                          collectionCreate(input: $input) { collection { id title } userErrors { field message } }
                        }
                        """,
                        {"input": {"title": name, "descriptionHtml": f"<p>{name} - seeded by NeoLook demo data.</p>"}},
                        invalidate_namespaces=["collections"],
                    )
                    payload = body["data"]["collectionCreate"]
                    if payload["userErrors"]:
                        console.print(f"[yellow]Skipping collection {name}: {payload['userErrors']}[/yellow]")
                    else:
                        self.collections.append(payload["collection"])
                except ShopifyAPIError as e:
                    console.print(f"[red]Failed to create collection {name}: {e}[/red]")
                progress.advance(task)

    async def create_products(self) -> None:
        plan = []
        for cat_index, (category, items, (lo, hi)) in enumerate(CATEGORIES):
            collection_id = self.collections[cat_index]["id"] if cat_index < len(self.collections) else None
            for item_index, item_name in enumerate(items):
                velocity = "stale" if item_index == 0 else ("fast" if item_index in (1, 2) else "normal")
                plan.append((category, item_name, collection_id, velocity, lo, hi))
        plan = plan[: self.n_products]

        with Progress(console=console) as progress:
            task = progress.add_task("Creating products...", total=len(plan))
            for category, item_name, collection_id, velocity, lo, hi in plan:
                title = f"{item_name} - {category}"
                price = round(random.uniform(lo, hi), 2)
                inventory = 5 if velocity == "stale" else random.randint(20, 100)
                variants_input = {
                    "price": f"{price:.2f}",
                    "optionValues": [{"optionName": "Title", "name": "Default Title"}],
                    "inventoryQuantities": [{"locationId": self.location_id, "name": "available", "quantity": inventory}],
                }
                product_input: dict = {
                    "title": title,
                    "productType": category,
                    "status": "ACTIVE",
                    "tags": [SEED_TAG, f"velocity:{velocity}"],
                    "productOptions": [{"name": "Title", "values": [{"name": "Default Title"}]}],
                    "variants": [variants_input],
                }
                if collection_id:
                    product_input["collections"] = [collection_id]

                try:
                    body = await self.client.mutate(
                        """
                        mutation SetProduct($input: ProductSetInput!, $synchronous: Boolean!) {
                          productSet(input: $input, synchronous: $synchronous) {
                            product { id title variants(first: 5) { edges { node { id price } } } }
                            userErrors { field message }
                          }
                        }
                        """,
                        {"input": product_input, "synchronous": True},
                        invalidate_namespaces=["products"],
                    )
                    payload = body["data"]["productSet"]
                    if payload["userErrors"]:
                        console.print(f"[yellow]Skipping product {title}: {payload['userErrors']}[/yellow]")
                    else:
                        product = payload["product"]
                        variant = product["variants"]["edges"][0]["node"]
                        self.products.append(
                            {
                                "id": product["id"],
                                "title": product["title"],
                                "velocity": velocity,
                                "variant_id": variant["id"],
                                "price": float(variant["price"]),
                            }
                        )
                except ShopifyAPIError as e:
                    console.print(f"[red]Failed to create product {title}: {e} (skipping, degrading gracefully)[/red]")
                progress.advance(task)

    async def create_customers(self) -> None:
        with Progress(console=console) as progress:
            task = progress.add_task("Creating customers...", total=self.n_customers)
            for i in range(self.n_customers):
                first = random.choice(FIRST_NAMES)
                last = random.choice(LAST_NAMES)
                email = f"neolook.seed.{i}.{first.lower()}.{last.lower()}@example.com"
                is_vip = i < VIP_CUSTOMER_COUNT
                tags = [SEED_TAG] + (["vip"] if is_vip else [])
                try:
                    body = await self.client.mutate(
                        """
                        mutation CreateCustomer($input: CustomerInput!) {
                          customerCreate(input: $input) { customer { id } userErrors { field message } }
                        }
                        """,
                        {"input": {"email": email, "firstName": first, "lastName": last, "tags": tags}},
                        invalidate_namespaces=["customers"],
                    )
                    payload = body["data"]["customerCreate"]
                    if payload["userErrors"]:
                        console.print(f"[yellow]Skipping customer {email}: {payload['userErrors']}[/yellow]")
                    else:
                        self.customers.append({"id": payload["customer"]["id"], "email": email, "is_vip": is_vip})
                except ShopifyAPIError as e:
                    console.print(f"[red]Failed to create customer {email}: {e}[/red]")
                progress.advance(task)

    async def create_discount_codes(self) -> None:
        with Progress(console=console) as progress:
            task = progress.add_task("Creating discount codes...", total=len(DISCOUNT_CODES))
            starts_at = (datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)).isoformat()
            for code in DISCOUNT_CODES:
                percentage = random.choice([10, 15, 20])
                try:
                    body = await self.client.mutate(
                        """
                        mutation CreateDiscount($basicCodeDiscount: DiscountCodeBasicInput!) {
                          discountCodeBasicCreate(basicCodeDiscount: $basicCodeDiscount) {
                            codeDiscountNode { id }
                            userErrors { field message code }
                          }
                        }
                        """,
                        {
                            "basicCodeDiscount": {
                                "title": code,
                                "code": code,
                                "startsAt": starts_at,
                                "context": {"all": "ALL"},
                                "customerGets": {"value": {"percentage": percentage / 100}, "items": {"all": True}},
                            }
                        },
                        invalidate_namespaces=["discounts"],
                    )
                    payload = body["data"]["discountCodeBasicCreate"]
                    if payload["userErrors"]:
                        console.print(f"[yellow]Skipping discount {code}: {payload['userErrors']}[/yellow]")
                    else:
                        self.discount_codes.append(code)
                except ShopifyAPIError as e:
                    console.print(f"[red]Failed to create discount {code}: {e}[/red]")
                progress.advance(task)

    def _pick_customer(self) -> dict:
        # VIPs are picked ~55% of the time despite being a small slice of the
        # customer list, so they accumulate many orders (for RFM "Champions").
        vips = [c for c in self.customers if c["is_vip"]]
        non_vips = [c for c in self.customers if not c["is_vip"]]
        if vips and random.random() < 0.55:
            return random.choice(vips)
        return random.choice(non_vips or vips)

    def _pick_line_items(self) -> list[dict]:
        fast = [p for p in self.products if p["velocity"] == "fast"]
        normal = [p for p in self.products if p["velocity"] == "normal"]
        stale = [p for p in self.products if p["velocity"] == "stale"]
        pool = fast * 6 + normal * 2 + stale  # weighted sampling via repetition
        n_items = random.choice([1, 1, 2, 2, 3])
        chosen = random.sample(pool, k=min(n_items, len(pool)))
        # dedupe while preserving order
        seen_ids = set()
        items = []
        for product in chosen:
            if product["id"] in seen_ids:
                continue
            seen_ids.add(product["id"])
            items.append({"product": product, "quantity": random.randint(1, 3)})
        return items or [{"product": random.choice(self.products), "quantity": 1}]

    async def create_orders(self) -> None:
        with Progress(console=console) as progress:
            task = progress.add_task("Creating orders (draft + complete)...", total=self.n_orders)
            for i in range(self.n_orders):
                customer = self._pick_customer()
                line_items = self._pick_line_items()
                use_discount = random.random() < 0.18 and self.discount_codes
                discount_code = random.choice(self.discount_codes) if use_discount else None

                days_ago = random.randint(0, HISTORY_DAYS - 1)
                intended_date = datetime.now(timezone.utc) - timedelta(
                    days=days_ago, hours=random.randint(0, 23), minutes=random.randint(0, 59)
                )

                draft_input: dict = {
                    "purchasingEntity": {"customerId": customer["id"]},
                    "lineItems": [
                        {"variantId": li["product"]["variant_id"], "quantity": li["quantity"]} for li in line_items
                    ],
                    "tags": [SEED_TAG],
                }
                if discount_code:
                    draft_input["discountCodes"] = [discount_code]

                try:
                    body = await self.client.mutate(
                        """
                        mutation CreateDraftOrder($input: DraftOrderInput!) {
                          draftOrderCreate(input: $input) { draftOrder { id } userErrors { field message } }
                        }
                        """,
                        {"input": draft_input},
                        invalidate_namespaces=["orders"],
                    )
                    payload = body["data"]["draftOrderCreate"]
                    if payload["userErrors"]:
                        console.print(f"[yellow]Skipping order {i}: {payload['userErrors']}[/yellow]")
                        progress.advance(task)
                        continue
                    draft_id = payload["draftOrder"]["id"]

                    complete_body = await self.client.mutate(
                        """
                        mutation CompleteDraftOrder($id: ID!) {
                          draftOrderComplete(id: $id) {
                            draftOrder { id order { id name createdAt } }
                            userErrors { field message }
                          }
                        }
                        """,
                        {"id": draft_id},
                        invalidate_namespaces=["orders"],
                    )
                    complete_payload = complete_body["data"]["draftOrderComplete"]
                    if complete_payload["userErrors"]:
                        console.print(f"[yellow]Order {i} completed with errors: {complete_payload['userErrors']}[/yellow]")
                        progress.advance(task)
                        continue

                    order = complete_payload["draftOrder"]["order"]
                    self.manifest.append(
                        {
                            "order_id": order["id"],
                            "order_name": order["name"],
                            "real_created_at": order["createdAt"],
                            "intended_created_at": intended_date.isoformat(),
                            "customer_id": customer["id"],
                            "customer_is_vip": customer["is_vip"],
                            "line_items": [
                                {
                                    "product_id": li["product"]["id"],
                                    "product_title": li["product"]["title"],
                                    "quantity": li["quantity"],
                                    "price": li["product"]["price"],
                                }
                                for li in line_items
                            ],
                            "discount_code": discount_code,
                        }
                    )
                except ShopifyAPIError as e:
                    console.print(f"[red]Failed to create order {i}: {e}[/red]")
                progress.advance(task)

                if (i + 1) % 25 == 0:
                    self.write_manifest()

    def write_manifest(self) -> None:
        MANIFEST_PATH.write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "orders": self.manifest}, indent=2))
        console.print(f"[green]Wrote {len(self.manifest)} order records to {MANIFEST_PATH}[/green]")

    async def run(self) -> None:
        console.print("[bold]Fetching store location...[/bold]")
        self.location_id = await self.get_location()

        await self.create_collections()
        await self.create_products()
        await self.create_customers()
        await self.create_discount_codes()

        if not self.products:
            console.print("[red]No products were created - aborting order creation.[/red]")
            return
        if not self.customers:
            console.print("[red]No customers were created - aborting order creation.[/red]")
            return

        try:
            await self.create_orders()
        finally:
            self.write_manifest()

        console.print("\n[bold green]Seeding complete.[/bold green]")
        console.print(f"  Collections: {len(self.collections)}")
        console.print(f"  Products:    {len(self.products)}")
        console.print(f"  Customers:   {len(self.customers)}")
        console.print(f"  Discounts:   {len(self.discount_codes)}")
        console.print(f"  Orders:      {len(self.manifest)}")
        console.print(f"\n  Client metrics: {self.client.get_metrics()}")


async def wipe(client: ShopifyClient) -> None:
    console.print("[bold red]Wiping all neolook-seed data...[/bold red]")

    async def delete_all(query_field: str, gql_type: str, delete_mutation: str, build_variables) -> int:
        count = 0
        while True:
            body = await client.query(
                f'query Find($first: Int!) {{ {query_field}(first: $first, query: "tag:{SEED_TAG}") {{ edges {{ node {{ id }} }} }} }}',
                {"first": 50},
                namespace=gql_type,
            )
            edges = body["data"][query_field]["edges"]
            if not edges:
                break
            for edge in edges:
                node_id = edge["node"]["id"]
                await client.mutate(delete_mutation, build_variables(node_id), invalidate_namespaces=[gql_type])
                count += 1
        return count

    n_orders = await delete_all(
        "orders", "orders",
        "mutation($orderId: ID!) { orderDelete(orderId: $orderId) { deletedId userErrors { field message } } }",
        lambda node_id: {"orderId": node_id},
    )
    console.print(f"Deleted {n_orders} orders")

    n_products = await delete_all(
        "products", "products",
        'mutation($input: ProductDeleteInput!) { productDelete(input: $input) { deletedProductId userErrors { field message } } }',
        lambda node_id: {"input": {"id": node_id}},
    )
    console.print(f"Deleted {n_products} products")

    n_collections = await delete_all(
        "collections", "collections",
        'mutation($input: CollectionDeleteInput!) { collectionDelete(input: $input) { deletedCollectionId userErrors { field message } } }',
        lambda node_id: {"input": {"id": node_id}},
    )
    console.print(f"Deleted {n_collections} collections")

    n_customers = await delete_all(
        "customers", "customers",
        'mutation($input: CustomerDeleteInput!) { customerDelete(input: $input) { deletedCustomerId userErrors { field message } } }',
        lambda node_id: {"input": {"id": node_id}},
    )
    console.print(f"Deleted {n_customers} customers")

    n_discounts = 0
    for code in DISCOUNT_CODES:
        body = await client.query(
            "query FindDiscount($code: String!) { codeDiscountNodeByCode(code: $code) { id } }",
            {"code": code},
            namespace="discounts",
        )
        node = body["data"].get("codeDiscountNodeByCode")
        if node:
            await client.mutate(
                "mutation($id: ID!) { discountCodeDelete(id: $id) { deletedCodeDiscountId userErrors { field message } } }",
                {"id": node["id"]},
                invalidate_namespaces=["discounts"],
            )
            n_discounts += 1
    console.print(f"Deleted {n_discounts} discount codes")

    if MANIFEST_PATH.exists():
        MANIFEST_PATH.unlink()
        console.print(f"Removed {MANIFEST_PATH}")

    console.print("[bold green]Wipe complete.[/bold green]")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--products", type=int, default=60)
    parser.add_argument("--customers", type=int, default=150)
    parser.add_argument("--orders", type=int, default=400)
    parser.add_argument("--wipe", action="store_true")
    args = parser.parse_args()

    random.seed(RNG_SEED)
    client = ShopifyClient()
    try:
        if args.wipe:
            await wipe(client)
        else:
            seeder = Seeder(client, args.products, args.customers, args.orders)
            await seeder.run()
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
