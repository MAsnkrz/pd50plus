"""
Qogita Full Catalog — Deep Price Drop Monitor
Monitors the ENTIRE Qogita catalogue (all brands, no filter) via the
official catalog download API:
  GET /variants/search/download/   (no brand_name filter = everything)

This is intentionally narrow in scope: it ONLY alerts on very steep
price drops — 50% to 100% off the previous price. No new-listing
alerts, no back-in-stock alerts, no small price-drop alerts. The idea
is to surface only the rare, extreme deals worth acting on across the
whole marketplace, not the noise of everyday small fluctuations.

Given the catalog can be very large, the snapshot stores ONLY the
minimal data needed for next time (qid -> price), not full product
metadata, to keep the snapshot file a manageable size. Full details
(title, image, stock, etc.) are read fresh from the catalog every run
and only attached to the Discord embed at the moment an alert fires.

Deps: pip install requests
"""

import csv
import io
import json
import os
import re
import time
import requests
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

API_BASE        = "https://api.qogita.com"
SNAPSHOT_FILE    = "snapshot_qogita_fullcatalog_prices.json"
BASELINE_FLAG    = "baseline_done_fullcatalog.txt"
RUN_ONCE         = os.getenv("RUN_ONCE", "false").lower() == "true"
CHECK_INTERVAL   = int(os.getenv("CHECK_INTERVAL", "1800"))  # 30 min

QOGITA_EMAIL    = os.getenv("QOGITA_EMAIL",    "")
QOGITA_PASSWORD = os.getenv("QOGITA_PASSWORD", "")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")

# Only alert on drops within this range (fractions: 0.50 = 50%, 1.00 = 100%)
MIN_DROP_PCT = 0.50
MAX_DROP_PCT = 1.00

# Minimum absolute price difference required, to avoid noise on
# extremely cheap items where a "50% drop" might just be a few pence
MIN_ABS_DROP = 0.05

COLOUR_DEEP_DROP = 0xFF0066  # hot pink/red — these are big, rare deals

# ---------------------------------------------------------------------------
# AUTH
# ---------------------------------------------------------------------------

_token_cache = {"token": None, "expires": 0}


def get_token():
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires"]:
        return _token_cache["token"]

    print("  Authenticating with Qogita API...")
    r = requests.post(
        f"{API_BASE}/auth/login/",
        json={"email": QOGITA_EMAIL, "password": QOGITA_PASSWORD},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    token = data.get("accessToken") or data.get("access")
    if not token:
        raise ValueError(f"No token in response: {data}")

    _token_cache["token"]   = token
    _token_cache["expires"] = now + 3300
    print("  Authenticated successfully")
    return token


def auth_headers():
    return {"Authorization": f"Bearer {get_token()}"}


# ---------------------------------------------------------------------------
# CATALOG DOWNLOAD — whole catalogue, no brand filter
# ---------------------------------------------------------------------------

def _safe_int(val):
    try:
        return int(float(str(val).replace(",", "")))
    except (TypeError, ValueError):
        return None


def fetch_full_catalog(retries=4):
    """
    Fetch the ENTIRE Qogita catalogue in one request (no brand_name
    filter applied). Returns a list of parsed product dicts, or None
    on failure/rate-limit exhaustion.
    """
    url = f"{API_BASE}/variants/search/download/"
    last_status = None

    for attempt in range(retries):
        print(f"  Requesting full catalog (attempt {attempt+1}/{retries})... this may take a while")
        r = requests.get(url, headers=auth_headers(), timeout=300)
        last_status = r.status_code

        if r.status_code == 401:
            _token_cache["token"] = None
            r = requests.get(url, headers=auth_headers(), timeout=300)
            last_status = r.status_code

        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 30 * (attempt + 1)))
            print(f"  [!] Rate limited (429) — waiting {wait}s")
            time.sleep(wait)
            continue

        if not r.ok:
            print(f"  [!] Catalog download failed: HTTP {r.status_code}")
            return None

        text = r.content.decode("utf-8", errors="replace")
        print(f"  Downloaded {len(r.content):,} bytes")
        break
    else:
        print(f"  [!] Still rate limited after {retries} attempts (last status {last_status}) — skipping this run")
        return None

    reader = csv.DictReader(io.StringIO(text))
    products = []
    for row in reader:
        product_url = row.get("Product URL", "") or ""
        qid_m = re.search(r"/products/([A-Za-z0-9]+)/", product_url)
        gtin  = (row.get("GTIN", "") or "").strip()
        qid   = qid_m.group(1) if qid_m else gtin
        if not qid:
            continue

        cheapest_stock = _safe_int(row.get("Lowest Priced Offer Inventory", ""))
        total_stock    = _safe_int(row.get("Total Inventory of All Offers", ""))
        num_offers     = _safe_int(row.get("Number of Offers", "")) or 0

        products.append({
            "qid":            qid,
            "title":          row.get("Name", "") or "",
            "brand":          row.get("Brand", "") or "",
            "category":       row.get("Category", "") or "",
            "url":            product_url or f"https://www.qogita.com/products/{qid}/",
            "image":          row.get("Image URL", "") or "",
            "barcode":        gtin,
            "price":          (row.get("£ Lowest Price inc. shipping", "") or "").strip(),
            "bundle_size":    (row.get("Unit", "") or "").strip(),
            "cheapest_stock": cheapest_stock,
            "stock":          total_stock,
            "all_offers":     num_offers,
        })

    print(f"  Parsed {len(products):,} products")
    return products


# ---------------------------------------------------------------------------
# PRICING HELPERS
# ---------------------------------------------------------------------------

def vat_price(price_str):
    try:
        return f"{float(price_str) * 1.2:.2f}"
    except (ValueError, TypeError):
        return price_str


def safe_float(val):
    try:
        return float(str(val).replace(",", ""))
    except (TypeError, ValueError):
        return None


def selleramp_url(barcode, cost_price_str):
    if not barcode:
        return None
    return (
        f"https://sas.selleramp.com/sas/lookup/"
        f"?search_term={barcode}&sas_cost_price={vat_price(cost_price_str)}"
    )


# ---------------------------------------------------------------------------
# DISCORD EMBED
# ---------------------------------------------------------------------------

def _send_embed(embed):
    payload = {"embeds": [embed]}
    try:
        r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        if r.status_code == 429:
            wait = float(r.json().get("retry_after", 5)) + 0.5
            time.sleep(wait)
            requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        else:
            r.raise_for_status()
    except Exception as e:
        print(f"  [!] Discord error: {e}")


def notify_deep_price_drop(product, old_price, new_price, pct_change):
    old_f = safe_float(old_price)
    new_f = safe_float(new_price)
    diff  = f"£{abs(new_f - old_f):.2f}" if old_f and new_f else "?"
    pct_display = f"{pct_change * 100:.1f}%"

    barcode = product.get("barcode", "")
    sas_url = selleramp_url(barcode, new_price)

    fields = [
        {"name": "💰 Old Price", "value": f"£{old_price}",     "inline": True},
        {"name": "💰 New Price", "value": f"**£{new_price}**", "inline": True},
        {"name": "📉 Drop",      "value": f"↓ {diff} (**{pct_display}**)", "inline": True},
        {"name": "💷 New Price (inc. VAT)", "value": f"£{vat_price(new_price)}" if new_price else "-", "inline": True},
        {"name": "🏷️ Brand",     "value": product.get("brand", "") or "-",    "inline": True},
        {"name": "📂 Category",  "value": product.get("category", "") or "-", "inline": True},
        {"name": "🔢 GTIN / EAN", "value": f"`{barcode}`" if barcode else "-", "inline": True},
        {"name": "📊 Total Stock", "value": f"{product.get('stock'):,} units" if product.get("stock") is not None else "-", "inline": True},
        {"name": "🏭 Sellers",    "value": f"{product.get('all_offers', 0)}", "inline": True},
    ]
    if sas_url:
        fields.append({"name": "🔍 SellerAmp SAS", "value": f"[Open in SellerAmp]({sas_url})", "inline": False})

    embed = {
        "title":     f"🚨  DEEP PRICE DROP -{pct_display} — {product.get('title', '')}",
        "url":       product.get("url", "https://www.qogita.com"),
        "color":     COLOUR_DEEP_DROP,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Qogita Full Catalog Monitor • qogita.com"},
    }
    image = product.get("image", "")
    if image:
        embed["thumbnail"] = {"url": image}

    _send_embed(embed)
    print(f"  Discord: DEEP DROP -{pct_display} — {product.get('title', '')[:50]}")


# ---------------------------------------------------------------------------
# SNAPSHOT — minimal (qid -> price only) to keep the file small at scale
# ---------------------------------------------------------------------------

def load_price_snapshot():
    if os.path.exists(SNAPSHOT_FILE):
        try:
            with open(SNAPSHOT_FILE) as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            print(f"  [!] Snapshot corrupted ({e}) — backing up and starting fresh")
            try:
                os.rename(SNAPSHOT_FILE, f"{SNAPSHOT_FILE}.corrupted.{int(time.time())}")
            except OSError:
                pass
            return {}
    return {}


def save_price_snapshot(data):
    tmp_file = f"{SNAPSHOT_FILE}.tmp"
    with open(tmp_file, "w") as f:
        json.dump(data, f)
    os.replace(tmp_file, SNAPSHOT_FILE)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def run_check():
    print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}] Checking full Qogita catalog for deep price drops...")

    price_snapshot = load_price_snapshot()   # {qid: price_str}
    baseline_done  = os.path.exists(BASELINE_FLAG)
    is_first_run   = not baseline_done

    products = fetch_full_catalog()
    if products is None:
        print("  [!] Could not fetch catalog this run (failure/rate limit) — will retry next scheduled run")
        return
    if not products:
        print("  [!] Catalog returned zero products — unexpected, skipping")
        return

    if is_first_run:
        print(f"  First run — recording baseline prices for {len(products)} products (no alerts)...")
        new_snapshot = {p["qid"]: p["price"] for p in products if p.get("qid")}
        save_price_snapshot(new_snapshot)
        with open(BASELINE_FLAG, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
        print(f"  Baseline complete — {len(new_snapshot)} prices recorded. No alerts sent.")
        return

    alerts_fired = 0
    new_snapshot = {}

    for product in products:
        qid = product.get("qid")
        if not qid:
            continue

        new_price = product.get("price", "")
        new_snapshot[qid] = new_price

        old_price = price_snapshot.get(qid)
        if old_price is None:
            continue  # genuinely new product — no alert, just record it

        old_f = safe_float(old_price)
        new_f = safe_float(new_price)
        if not old_f or not new_f or old_f <= 0:
            continue

        pct_change = (old_f - new_f) / old_f
        abs_change = old_f - new_f

        if MIN_DROP_PCT <= pct_change <= MAX_DROP_PCT and abs_change >= MIN_ABS_DROP:
            print(f"  -> DEEP DROP: {product['title'][:50]} £{old_price} -> £{new_price} (-{pct_change*100:.1f}%)")
            notify_deep_price_drop(product, old_price, new_price, pct_change)
            alerts_fired += 1
            time.sleep(1)

    save_price_snapshot(new_snapshot)
    print(f"  Done — {len(new_snapshot)} prices tracked, {alerts_fired} deep-drop alert(s) fired")


def main():
    print("=" * 55)
    print("  Qogita Full Catalog — Deep Price Drop Monitor")
    print(f"  Scope: entire catalogue, all brands")
    print(f"  Alerting only on drops of {MIN_DROP_PCT*100:.0f}%-{MAX_DROP_PCT*100:.0f}%")
    print("=" * 55)

    if not QOGITA_EMAIL or not QOGITA_PASSWORD:
        print("  [!] QOGITA_EMAIL and QOGITA_PASSWORD must be set")
        return
    if not DISCORD_WEBHOOK:
        print("  [!] DISCORD_WEBHOOK must be set")
        return

    if RUN_ONCE:
        run_check()
    else:
        while True:
            try:
                run_check()
            except Exception as e:
                print(f"  [!] Unexpected error: {e}")
            print(f"  Sleeping {CHECK_INTERVAL}s...")
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
