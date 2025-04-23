#!/usr/bin/env python3
"""
RSVP Cigars product/price monitor
--------------------------------
 • Crawls the two master catalogue pages (/en/cubans/ and /en/non‑cubans/) :contentReference[oaicite:0]{index=0}
 • Follows every link that contains "-p<digits>/" – the stable product‑page pattern :contentReference[oaicite:1]{index=1}
 • Extracts title and the **current** price (skips strikethrough/old prices)
 • Compares with the last snapshot in `previous_products.json`
 • Sends an HTML e‑mail if new items or price changes are found
"""
import html            # <-- new import
import time
import requests, os, json, re
from datetime import datetime
from decimal import Decimal
from typing import List, Dict

import requests  # HTTP client :contentReference[oaicite:2]{index=2}
from bs4 import BeautifulSoup  # HTML parser :contentReference[oaicite:3]{index=3}

# ───────────────────────────────  CONFIG  ────────────────────────────── #
BASE_URL = "https://rsvpcigars.com"
SEED_PAGES = [f"{BASE_URL}/en/cubans/", f"{BASE_URL}/en/non-cubans/"]


DATA_FILE = "previous_products.json"
HEADERS   = {"User-Agent": "Mozilla/5.0 (RSVPMonitor/1.0)"}

PRODUCT_RE = re.compile(r"-p\d+/")     # e.g. “…-p1070/”

PRICE_RE   = re.compile(r"\$[\d,]+\.\d{2}")  # $1,234.56 :contentReference[oaicite:4]{index=4}


# ──────────────────────────────  SCRAPING  ───────────────────────────── #
def fetch_soup(url: str, session: requests.Session) -> BeautifulSoup:
    r = session.get(url, timeout=15)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def parse_price(soup: BeautifulSoup) -> str:
    """
    Return the current (non‑struck) USD price as a string like “$300.00”.
    Works for:
      • pages with a sale price (“<del>$4500</del> $3000”)
      • pages with a single regular price
    """
    # 1  Try common markup first
    tag = (
        soup.select_one("span.price")            # single price
        or soup.select_one("span.price-item--sale")  # Shopify sale class
        or soup.select_one("span.price-item")    # fallback
    )
    if tag and PRICE_RE.search(tag.get_text()):
        return PRICE_RE.search(tag.get_text()).group()

    # 2  Fallback: scan visible text and take the **last** price,
    #     which is the sale/current price when both shown.
    text_prices = PRICE_RE.findall(soup.get_text(" ", strip=True))
    if text_prices:
        return text_prices[-1]

    raise ValueError("Price not found")


def fetch_all_products() -> List[Dict[str, str]]:
    session = requests.Session()
    session.headers.update(HEADERS)

    # Discover all product URLs
    product_urls = set()
    for seed in SEED_PAGES:
        soup = fetch_soup(seed, session)
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if PRODUCT_RE.search(href):
                full = href if href.startswith("http") else BASE_URL + href
                product_urls.add(full)

    # Visit each product page once
    products = []
    for url in sorted(product_urls):
        s = fetch_soup(url, session)
        title = s.find("h1").get_text(strip=True)
        price = parse_price(s)
        products.append({"title": title, "price": price, "url": url})

    return products


# ─────────────────────────────  DIFF & STORE  ────────────────────────── #
def load_previous() -> List[Dict[str, str]]:
    if not os.path.exists(DATA_FILE):
        return []
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_snapshot(products: List[Dict[str, str]]) -> None:
    snapshot = {
        "scraped_at": datetime.utcnow().isoformat(timespec="seconds"),
        "products": products,
    }
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)


def compare(old: List[Dict[str, str]], new: List[Dict[str, str]]) -> Dict[str, List]:
    changes = {"new": [], "price": []}
    old_lookup = {p["title"]: p for p in old}

    for item in new:
        if item["title"] not in old_lookup:
            changes["new"].append(item)
        else:
            old_price = Decimal(old_lookup[item["title"]]["price"].replace("$", "").replace(",", ""))
            new_price = Decimal(item["price"].replace("$", "").replace(",", ""))
            if old_price != new_price:
                changes["price"].append(
                    {
                        "title": item["title"],
                        "old": f"${old_price:,}",
                        "new": f"${new_price:,}",
                        "url": item["url"],
                    }
                )
    return changes

# ──────────  DISCORD HELPER (with auto-split)  ────────── #
WEBHOOK = os.getenv("DISCORD_WEBHOOK")        # set as a GitHub secret
MAX_LEN = 2000                                # Discord hard cap :contentReference[oaicite:2]{index=2}
RATE_PAUSE = 0.3                              # 5 msgs/s safety pause :contentReference[oaicite:3]{index=3}

def html_to_discord(text: str) -> str:
    """Very light HTML→Discord markdown."""
    text = re.sub(r"</?h\d>", "**", text)      # headings -> bold
    text = text.replace("<br>", "\n")
    text = text.replace("</li>", "\n• ")
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)

def compose_discord(changes: Dict[str, List]) -> str:
    parts = []
    if changes["new"]:
        parts.append("**🆕 New products**")
        for p in changes["new"]:
            parts.append(f"• {p['title']} – {p['price']}  <{p['url']}>")
    if changes["price"]:
        parts.append("**💲 Price changes**")
        for p in changes["price"]:
            parts.append(f"• {p['title']}: {p['old']} → **{p['new']}**  <{p['url']}>")

    msg = "\n".join(parts).strip()
    return msg or "Nothing changed, but monitor ran."

def send_alert(message: str) -> None:
    """Split long content into 2 000-char chunks and POST sequentially."""
    if not WEBHOOK:
        print("⚠️  DISCORD_WEBHOOK not set; skipping alert.")
        return

    for start in range(0, len(message), MAX_LEN):
        chunk = message[start:start + MAX_LEN]
        r = requests.post(WEBHOOK, json={"content": chunk}, timeout=10)
        if r.status_code >= 400:
            print(f"Discord error {r.status_code}: {r.text}")
            r.raise_for_status()
        if len(message) > MAX_LEN:
            time.sleep(RATE_PAUSE)             # stay under 5 req/s


# ───────────────  MAIN  ─────────────── #
def main() -> None:
    current = fetch_all_products()
    old_snapshot = load_previous()
    old_products = old_snapshot["products"] if old_snapshot else []

    diff = compare(old_products, current)
    if diff["new"] or diff["price"]:
        send_alert(compose_discord(diff))  # 🔔 send to Discord
    save_snapshot(current)
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} – scan complete.")

if __name__ == "__main__":
    main()
