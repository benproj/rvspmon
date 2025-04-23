#!/usr/bin/env python3
"""
RSVP Cigars product/price monitor
---------------------------------
• Crawls both catalogue roots (/en/cubans/, /en/non-cubans/)
• Follows every link matching “-p<id>/”
• Extracts the *current* sale/regular price (meta tag > span)
• Diffs against previous snapshot and posts Discord alerts
"""

import html
import json
import os
import re
import time
from datetime import datetime
from decimal import Decimal
from typing import Dict, List

import requests
from bs4 import BeautifulSoup

# ───────────────────────────────  CONFIG  ──────────────────────────────
BASE_URL = "https://rsvpcigars.com"
SEED_PAGES = [f"{BASE_URL}/en/cubans/", f"{BASE_URL}/en/non-cubans/"]
HEADERS = {"User-Agent": "Mozilla/5.0 (RSVPMonitor/1.0)"}

DATA_FILE = "previous_products.json"
PRODUCT_RE = re.compile(r"-p\d+/")               # product URL pattern
PRICE_RE = re.compile(r"\$[\d,]+\.\d{2}")        # $1,234.56

WEBHOOK = os.getenv("DISCORD_WEBHOOK")           # GitHub secret
MAX_LEN = 2000                                   # Discord hard cap
RATE_PAUSE = 0.3                                 # 5 req/s safety

# ───────────────────────────────  SCRAPING  ────────────────────────────
def fetch_soup(url: str, session: requests.Session) -> BeautifulSoup:
    r = session.get(url, timeout=15)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def parse_price(soup: BeautifulSoup) -> str:
    """Return the live USD price as “$#,###.##”."""
    # 1️⃣  Most reliable: micro-data
    meta = soup.select_one('meta[itemprop="price"]')
    if meta and meta.get("content"):
        return f"${Decimal(meta['content']):,}"

    # 2️⃣  Visible span markup
    tag = (
        soup.select_one("span.price") or
        soup.select_one("span.price-item--sale") or
        soup.select_one("span.price-item")
    )
    if tag and PRICE_RE.search(tag.get_text()):
        return PRICE_RE.search(tag.get_text()).group()

    # 3️⃣  Last resort: scan all text
    prices = PRICE_RE.findall(soup.get_text(" ", strip=True))
    if prices:
        return prices[-1]

    raise ValueError("Price not found")


def fetch_all_products() -> List[Dict[str, str]]:
    session = requests.Session()
    session.headers.update(HEADERS)

    product_urls = set()
    # single pages today; loop allows pagination if Shopify ever splits
    for seed in SEED_PAGES:
        page = 1
        while True:
            soup = fetch_soup(f"{seed}?page={page}", session)
            new_links = {
                BASE_URL + a["href"] if not a["href"].startswith("http") else a["href"]
                for a in soup.find_all("a", href=True) if PRODUCT_RE.search(a["href"])
            }
            if not new_links or new_links.issubset(product_urls):
                break
            product_urls |= new_links
            page += 1

    products = []
    for url in sorted(product_urls):
        s = fetch_soup(url, session)
        title = s.find("h1").get_text(strip=True)
        price = parse_price(s)
        products.append({"title": title, "price": price, "url": url})
    return products

# ────────────────────────────  DIFF & STORE  ───────────────────────────
def load_previous() -> List[Dict[str, str]]:
    if not os.path.exists(DATA_FILE):
        return []
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_snapshot(products: List[Dict[str, str]]) -> None:
    snap = {"scraped_at": datetime.utcnow().isoformat(timespec="seconds"),
            "products": products}
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=2)


def compare(old: List[Dict[str, str]], new: List[Dict[str, str]]) -> Dict[str, List]:
    changes = {"new": [], "price": []}
    old_lookup = {p["title"]: p for p in old}

    for item in new:
        if item["title"] not in old_lookup:
            changes["new"].append(item)
        else:
            o = Decimal(old_lookup[item["title"]]["price"].strip("$").replace(",", ""))
            n = Decimal(item["price"].strip("$").replace(",", ""))
            if o != n:
                changes["price"].append({
                    "title": item["title"],
                    "old": f"${o:,}",
                    "new": f"${n:,}",
                    "url": item["url"]
                })
    return changes

# ────────────────────────  DISCORD HELPER  ────────────────────────────
def compose_discord(ch: Dict[str, List]) -> str:
    parts = []
    if ch["new"]:
        parts.append("**🆕 New products**")
        parts += [f"• {p['title']} – {p['price']}  <{p['url']}>" for p in ch["new"]]
    if ch["price"]:
        parts.append("**💲 Price changes**")
        parts += [f"• {p['title']}: {p['old']} → **{p['new']}**  <{p['url']}>"
                  for p in ch["price"]]
    msg = "\n".join(parts).strip()
    return msg or "Nothing changed, but monitor ran."


def send_alert(message: str) -> None:
    if not WEBHOOK:
        print("⚠️  DISCORD_WEBHOOK not set; skipping alert.")
        return

    for i in range(0, len(message), MAX_LEN):
        chunk = message[i:i + MAX_LEN]
        r = requests.post(WEBHOOK, json={"content": chunk}, timeout=10)
        if r.status_code >= 400:
            print(f"Discord error {r.status_code}: {r.text}")
            r.raise_for_status()
        if len(message) > MAX_LEN:
            time.sleep(RATE_PAUSE)        # keep under 5 req/s

# ────────────────────────────────  MAIN  ───────────────────────────────
def main() -> None:
    current = fetch_all_products()
    old_products = load_previous()
    diff = compare(old_products, current)

    if diff["new"] or diff["price"]:
        send_alert(compose_discord(diff))

    save_snapshot(current)
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} – scan complete.")

if __name__ == "__main__":
    main()
