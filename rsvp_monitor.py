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

import json
import os
import re
import smtplib
from datetime import datetime
from decimal import Decimal
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Dict

import requests  # HTTP client :contentReference[oaicite:2]{index=2}
from bs4 import BeautifulSoup  # HTML parser :contentReference[oaicite:3]{index=3}

# ───────────────────────────────  CONFIG  ────────────────────────────── #
BASE_URL = "https://rsvpcigars.com"
SEED_PAGES = [f"{BASE_URL}/en/cubans/", f"{BASE_URL}/en/non-cubans/"]

SMTP_HOST = os.getenv("SMTP_HOST")      # e.g. smtp.gmail.com
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER")      # full e‑mail address or username
SMTP_PASS = os.getenv("SMTP_PASS")      # app‑specific or SMTP password
ALERT_TO  = os.getenv("ALERT_TO")       # destination inbox


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


# ───────────────────────────────  ALERTING  ──────────────────────────── #
def compose_email(ch: Dict[str, List]) -> str:
    html = ["<h2>RSVP Cigars – site update</h2>"]

    if ch["new"]:
        html.append("<h3>New products</h3><ul>")
        for p in ch["new"]:
            html.append(f'<li><a href="{p["url"]}">{p["title"]}</a> – {p["price"]}</li>')
        html.append("</ul>")

    if ch["price"]:
        html.append("<h3>Price changes</h3><ul>")
        for p in ch["price"]:
            html.append(
                f'<li><a href="{p["url"]}">{p["title"]}</a>: {p["old"]} → <strong>{p["new"]}</strong></li>'
            )
        html.append("</ul>")

    return "\n".join(html)


def send_email(subject: str, html_body: str) -> None:
    # guard against unset SMTP settings
    missing = [x for x in (SMTP_HOST, SMTP_USER, SMTP_PASS, ALERT_TO) if not x]
    if missing:
        print("⚠️  E‑mail disabled – set SMTP_* env vars to enable alerts.")
        print(html_body)
        return
    msg = MIMEMultipart()
    msg["From"] = SMTP_USER
    msg["To"] = ALERT_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(msg)


# ────────────────────────────────  MAIN  ─────────────────────────────── #
def main() -> None:
    current = fetch_all_products()
    old_snapshot = load_previous()
    old_products = old_snapshot["products"] if old_snapshot else []

    changes = compare(old_products, current)

    if changes["new"] or changes["price"]:
        body = compose_email(changes)
        send_email("RSVP Cigars update", body)

    save_snapshot(current)
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} – scan complete.")


if __name__ == "__main__":
    main()
