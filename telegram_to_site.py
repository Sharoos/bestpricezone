#!/usr/bin/env python3
"""
bestpricezone_fetch_latest.py

Telegram-only product card generator that ensures messages newer than the last run
are always processed (no gaps). Single-file; configure via environment variables.

Required env:
  TG_API_ID, TG_API_HASH, TG_STRING_SESSION, CHANNEL_USERNAME

Optional env:
  TARGET_CARDS (default 40)
  MAX_SCAN_MESSAGES (default 1000)
  CLEAN_IMAGES_ON_RUN (default 1)   -> remove previous assets/images at start
  CLEAN_DB_ON_RUN (default 0)       -> clear processed DB at start
  SHOW_RELATIVE (default 1)         -> show "2h ago"-style timestamps
  OUT_FILE (default index.html)
  ASSETS_DIR (default assets)
  MIN_IMG_BYTES (default 800)
  HERO_BANNER (path to banner image in repo root, default 'banner.jpg')
  HERO_HEIGHT (CSS height value, default '160px')

Added env:
  MAX_KEEP (default 500) -> keep exactly this many cards/images after each run
"""
import os
import re
import time
import json
import sqlite3
import hashlib
import logging
import pathlib
import random
import shutil
from urllib.parse import urlparse
from datetime import datetime, timezone
from typing import Optional, Set

import requests
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# ---------- Config (env) ----------
# allow a simple .env file in repo root (optional)
DOTENV_PATH = os.path.join(os.getcwd(), ".env")
if os.path.exists(DOTENV_PATH):
    try:
        with open(DOTENV_PATH, "r", encoding="utf8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                if "=" not in ln:
                    continue
                k, v = ln.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass

TG_API_ID = int(os.environ.get("TG_API_ID", "0"))
TG_API_HASH = os.environ.get("TG_API_HASH")
TG_STRING_SESSION = os.environ.get("TG_STRING_SESSION")
CHANNEL = os.environ.get("CHANNEL_USERNAME")

TARGET_CARDS = int(os.environ.get("TARGET_CARDS", "100"))
MAX_SCAN_MESSAGES = int(os.environ.get("MAX_SCAN_MESSAGES", "3000"))

# NEW: how many cards to keep on every run (default 200)
MAX_KEEP = int(os.environ.get("MAX_KEEP", "500"))

CLEAN_IMAGES_ON_RUN = os.environ.get("CLEAN_IMAGES_ON_RUN", "1") == "1"
CLEAN_DB_ON_RUN = os.environ.get("CLEAN_DB_ON_RUN", "0") == "1"
SHOW_RELATIVE = os.environ.get("SHOW_RELATIVE", "1") == "1"

OUT_FILE = os.environ.get("OUT_FILE", "index.html")
ASSETS_DIR = os.environ.get("ASSETS_DIR", "assets")
IMGS_DIR = os.path.join(ASSETS_DIR, "images")
DB_PATH = os.path.join(ASSETS_DIR, "state.db")
CARDS_JSON_PATH = os.path.join(ASSETS_DIR, "cards.json")

USER_AGENT = os.environ.get("USER_AGENT", "Mozilla/5.0 (compatible; BestPriceZoneBot/1.0)")
MIN_IMG_BYTES = int(os.environ.get("MIN_IMG_BYTES", "800"))

# Banner settings
HERO_BANNER = os.environ.get("HERO_BANNER", "banner.jpg")  # path in repo root by default
HERO_HEIGHT = os.environ.get("HERO_HEIGHT", "300px")       # CSS value, e.g. "160px" or "12rem"

os.makedirs(IMGS_DIR, exist_ok=True)
os.makedirs(ASSETS_DIR, exist_ok=True)

# if banner exists in repo root, copy it into assets as banner.<ext>
BANNER_TARGET_REL = None
if os.path.exists(HERO_BANNER):
    try:
        ext = os.path.splitext(HERO_BANNER)[1] or ".jpg"
        target_name = "banner" + ext
        target_path = os.path.join(ASSETS_DIR, target_name)
        shutil.copy2(HERO_BANNER, target_path)
        # relative path for HTML (OUT_FILE is created in repo root by default)
        BANNER_TARGET_REL = os.path.join(os.path.basename(ASSETS_DIR), target_name).replace("\\", "/")
        logging.info("Copied banner %s -> %s", HERO_BANNER, target_path)
    except Exception as e:
        logging.debug("Failed to copy banner: %s", e)
        BANNER_TARGET_REL = None
else:
    # if HERO_BANNER set but missing file, log
    if HERO_BANNER and HERO_BANNER != "banner.jpg":
        logging.warning("HERO_BANNER set but file not found: %s", HERO_BANNER)

# ---------- Regex / helpers ----------
URL_RE = re.compile(r'https?://[^\s\)\]]+', flags=re.I)
DOMAIN_ONLY_RE = re.compile(r'\b(?:[a-z0-9-]+\.)+(?:com|in|net|org|co|io|me|store|shop|online|cc)\b', flags=re.I)
NOISE_WORDS_RE = re.compile(r'\b(link|links|buy now|buy|see more offers|view source|visit|apply coupon|coupon|note)\b', flags=re.I)
CURRENCY_ONLY_RE = re.compile(r'^[\s\-\.,₹Rs]*$')
TRAILING_JUST_RUPEE_RE = re.compile(r'(\bjust\b|\bat\b)?\s*[₹Rs\.,\s\-]+$', flags=re.I)

MERCHANT_MAP = {
    "fkrt": "Flipkart", "fktr": "Flipkart", "flipkart": "Flipkart",
    "myntr": "Myntra", "myntra": "Myntra", "ajio": "Ajio",
    "amazon": "Amazon", "snapdeal": "Snapdeal",
}

def normalize_merchant(text: Optional[str], url: Optional[str]) -> Optional[str]:
    if text:
        t = text.lower()
        for k, v in MERCHANT_MAP.items():
            if k in t:
                return v
    if url:
        try:
            host = urlparse(url).netloc.lower()
            if host.startswith("www."): host = host[4:]
            for k, v in MERCHANT_MAP.items():
                if k in host: return v
            if host: return host.split(".")[0].capitalize()
        except:
            pass
    return None

def clean_message_text(text: str, shortlink: Optional[str] = None) -> str:
    """Return a compact, cleaned title suitable for product card (affiliate URLs removed)."""
    if not text:
        return ""
    raw = text.strip().replace("•", "\n").replace("·", "\n")
    parts = []
    for chunk in re.split(r'[\n\r]+', raw):
        for part in re.split(r'[👉\-\u25B6\*]+', chunk):
            p = part.strip()
            if not p: continue
            if URL_RE.fullmatch(p): continue
            parts.append(p)
    def score_line(l: str) -> int:
        s = 0
        if re.search(r'\b(upto|up to|off on|off|discount|% off|%|loot|sale|deal|clearance|save|extra)\b', l, flags=re.I):
            s += 200
        if re.search(r'\b\d{1,3}\s*%|\d{2,5}\s*\/-|\d{3,6}\b', l):
            s += 120
        titlecase_words = re.findall(r'\b[A-Z][a-z0-9]{2,}\b', l)
        s += len(titlecase_words)*8
        words = re.findall(r'[A-Za-z0-9]{2,}', l)
        s += len(words)*4
        if re.match(r'^[\s\-\.,₹Rs0-9]+$', l): s -= 150
        if re.search(r'\b(ajio|flipkart|myntra|amazon|snapdeal|loot)\b', l, flags=re.I): s += 80
        return s
    scored = []
    for p in parts:
        p2 = URL_RE.sub('', p)
        p2 = DOMAIN_ONLY_RE.sub('', p2)
        p2 = NOISE_WORDS_RE.sub('', p2)
        p2 = TRAILING_JUST_RUPEE_RE.sub('', p2)
        p2 = p2.strip(" -:•·—–,.")
        if not p2: continue
        if CURRENCY_ONLY_RE.match(p2): continue
        scored.append((score_line(p2), p2))
    if scored:
        scored.sort(reverse=True, key=lambda x: x[0])
        chosen = scored[0][1]
    else:
        chosen = " ".join(parts[:2]) if parts else raw
    chosen = re.sub(r'\b(Link|Link:|Link :|Buy now|Buy Now)\b', '', chosen, flags=re.I)
    chosen = URL_RE.sub('', chosen)
    chosen = DOMAIN_ONLY_RE.sub('', chosen)
    chosen = re.sub(r'\s+', ' ', chosen).strip(" -:,.")
    chosen = re.sub(r'\bUpto\b', 'Up to', chosen, flags=re.I)
    merchant = normalize_merchant(chosen, shortlink)
    if merchant:
        # remove merchant name if it appears in the line
        chosen = re.sub(r'(?i)\b' + re.escape(merchant) + r'\b[:\s\-|]*', '', chosen).strip()
    # no merchant prefix anymore
    final = chosen
    # remove junk phrases
    final = re.sub(r'\b(grab here|grab now|grab|link|loot:?|buy now|click here)\b','', final, flags=re.I)
    # cleanup stray leading/trailing separators like "|" or ":" or "-"
    final = re.sub(r'^[\|\-:]+', '', final).strip()
    final = re.sub(r'[\|\-:]+$', '', final).strip()
    final = re.sub(r'\s*[\|\-:]+\s*', ' ', final)  # turn " | " or " - " into single space
    final = re.sub(r'\s+', ' ', final).strip()
    # collapse spaces
    final = re.sub(r'\s+', ' ', final).strip()
    if len(final) > 160:
        final = final[:157].rsplit(' ', 1)[0] + '…'

    return final


def safe_filename(base: str) -> str:
    name = os.path.basename(base) or "img"
    name = re.sub(r'[^a-zA-Z0-9._-]', '_', name)[:60]
    if "." not in name: name += ".jpg"
    h = hashlib.md5(base.encode('utf8')).hexdigest()[:10]
    return f"{name}_{h}"

REQ = requests.Session()
REQ.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-IN,en;q=0.9"})
REQ.max_redirects = 8

def extract_url_labels(raw_text: str, urls: list) -> list:
    """
    Return list of {"url":..., "label":...} in reading order.
    This version:
      - merges duplicate URLs (keep first occurrence, prefer first non-empty label)
      - removes duplicate header-only labels (keeps first)
      - normalizes / strips trivial 'buy now' noise from labels
      - ensures every original url appears at least once
    """
    out = []
    lines = [ln for ln in re.split(r'[\r\n]+', raw_text)]

    def only_urls_and_arrows(s: str) -> bool:
        if not s: return False
        found = URL_RE.findall(s)
        if not found: return False
        without_urls = s
        for u in found:
            without_urls = without_urls.replace(u, '')
        without_urls = re.sub(r'^[\s\-\u25B6👉\*•…\:\|]+|[\s\-\u25B6👉\*•…\:\|]+$', '', without_urls).strip()
        return len(without_urls) < 4

    def is_header_line(s: str) -> bool:
        if not s: return False
        s2 = s.strip()
        if URL_RE.search(s2): return False
        if NOISE_WORDS_RE.search(s2): return False
        if re.search(r'[,&]| and | & ', s2, flags=re.I) and re.search(r'[A-Za-z]{2,}', s2):
            return True
        words = re.findall(r"[A-Za-z0-9&'-]{2,}", s2)
        if len(words) >= 2:
            upper_words = sum(1 for w in words if w and w[0].isupper())
            if upper_words >= 1:
                return True
        return False

    def clean_label_candidate(s: str) -> str:
        if not s: return ""
        s = s.strip()
        s = re.sub(r'^[\s\-\u25B6👉\*•…\:\|]+', '', s)
        s = re.sub(r'[\|\-\:\s]+$', '', s)
        s = NOISE_WORDS_RE.sub('', s)
        s = re.sub(r'\s+', ' ', s).strip()
        s = s.rstrip('.').strip()
        if not s or CURRENCY_ONLY_RE.match(s) or len(re.findall(r'[A-Za-z0-9]{2,}', s)) < 1:
            return ""
        s = re.sub(r'^[A-Za-z0-9&\s]{2,40}\s*[:\-\|]+\s*', '', s).strip()
        return s

    discount_re = re.compile(r'\b(upto|up to|off|discount|% off|%|sale|save|flat)\b', re.I)

    current_header = ""
    i = 0
    while i < len(lines):
        ln = lines[i]
        if not ln or not ln.strip():
            i += 1
            continue
        stripped = ln.strip()

        # header lookahead and merge-with-next-url behaviour
        if is_header_line(stripped):
            candidate = clean_label_candidate(stripped)
            j = i + 1
            next_line = ""
            while j < len(lines):
                if lines[j] and lines[j].strip():
                    next_line = lines[j].strip()
                    break
                j += 1

            if candidate and discount_re.search(candidate) and next_line and only_urls_and_arrows(next_line):
                found_urls = URL_RE.findall(next_line)
                if found_urls:
                    for fu in found_urls:
                        matched = None
                        for u in urls:
                            if fu.strip() == u.strip() or fu.strip() in u or u in fu.strip():
                                matched = u
                                break
                        if not matched:
                            matched = fu
                        out.append({"url": matched, "label": candidate})
                    i = j + 1
                    continue
                else:
                    out.append({"url": "", "label": candidate})
                    current_header = candidate
                    i += 1
                    continue
            else:
                if candidate:
                    out.append({"url": "", "label": candidate})
                    current_header = candidate
                i += 1
                continue

        # normal url-bearing line
        found_urls = URL_RE.findall(stripped)
        if found_urls:
            for fu in found_urls:
                matched = None
                for u in urls:
                    if fu.strip() == u.strip() or fu.strip() in u or u in fu.strip():
                        matched = u
                        break
                if not matched:
                    matched = fu

                left = stripped.split(fu, 1)[0].strip()
                left_clean = clean_label_candidate(left)
                label = ""
                if left_clean:
                    label = left_clean
                elif current_header:
                    label = current_header
                else:
                    whole_no_url = URL_RE.sub('', stripped).strip()
                    whole_clean = clean_label_candidate(whole_no_url)
                    if whole_clean:
                        label = whole_clean

                out.append({"url": matched, "label": label})
            i += 1
            continue

        i += 1

    # ensure each original url appears at least once
    for u in urls:
        if not any(entry.get("url") == u for entry in out):
            out.append({"url": u, "label": ""})

    # --- Post-process and normalize labels ---
    # remove trivial label words and normalize whitespace
    def normalize_label(lbl: str) -> str:
        if not lbl:
            return ""
        s = lbl.strip()
        # remove very common noise short phrases that serve only as "buy" markers
        s = re.sub(r'\b(Buy now|Buy|buy now|Click here|Shop full collection here)\b', '', s, flags=re.I).strip()
        s = re.sub(r'\s+', ' ', s).strip()
        return s

    # merge duplicate urls (prefer first occ; if later label is non-empty and first was empty, fill it)
    merged = []
    seen_url_to_index = {}
    seen_headers = set()
    for entry in out:
        u = (entry.get("url") or "").strip()
        lbl = normalize_label(entry.get("label") or "")
        if not u:
            # header-only
            key = (lbl or "").lower()
            if not lbl:
                continue
            if key in seen_headers:
                # skip duplicates of header labels anywhere (keep first)
                continue
            # if a later url entry has the same label as its label, prefer the url-lined row and skip header
            # (we check original out list for presence)
            label_used_by_url = any((e.get("url") and (e.get("label") or "").strip().lower() == key) for e in out)
            if label_used_by_url:
                # skip header-only if it's just repeating a labeled url (avoid header then same label repeated)
                seen_headers.add(key)
                continue
            seen_headers.add(key)
            merged.append({"url": "", "label": lbl})
            continue

        # url present
        if u in seen_url_to_index:
            idx = seen_url_to_index[u]
            existing = merged[idx]
            existing_lbl = (existing.get("label") or "").strip()
            if not existing_lbl and lbl:
                existing["label"] = lbl
            # otherwise keep first
            continue
        # new url
        merged.append({"url": u, "label": lbl})
        seen_url_to_index[u] = len(merged) - 1

    return merged



# ---------- Telegram media downloader ----------
import asyncio as _asyncio
def download_telegram_media_if_present(client, msg):
    try:
        if not getattr(msg, 'media', None):
            return None
        fname = f"tgmsg_{getattr(msg,'id','')}.jpg"
        outpath = os.path.join(IMGS_DIR, fname)
        if os.path.exists(outpath) and os.path.getsize(outpath) > MIN_IMG_BYTES:
            return outpath.replace("\\", "/")
        maybe = None
        try:
            maybe = client.download_media(msg.media, file=outpath)
        except TypeError:
            try:
                client.download_media(msg.media, file=outpath)
            except Exception as e:
                logging.debug("download_media direct call failed (TypeError path): %s", e)
        except Exception as e:
            logging.debug("download_media immediate call failed: %s", e)
        try:
            if maybe and getattr(maybe, "__await__", None):
                loop = _asyncio.get_event_loop()
                if loop.is_running():
                    _asyncio.run(maybe)
                else:
                    loop.run_until_complete(maybe)
        except Exception as e:
            logging.debug("Running download_media coroutine failed: %s", e)
        if not (os.path.exists(outpath) and os.path.getsize(outpath) > MIN_IMG_BYTES):
            try:
                client.download_media(msg.media, file=outpath)
            except Exception:
                pass
        if os.path.exists(outpath) and os.path.getsize(outpath) > MIN_IMG_BYTES:
            logging.info("Saved Telegram media for msg %s -> %s", getattr(msg, 'id', '?'), outpath)
            return outpath.replace("\\", "/")
        # bytes fallback
        try:
            maybe_bytes = client.download_media(msg.media)
            if isinstance(maybe_bytes, (bytes, bytearray)) and len(maybe_bytes) > MIN_IMG_BYTES:
                path = os.path.join(IMGS_DIR, safe_filename(str(getattr(msg, 'id', 'tg')) + ".jpg"))
                with open(path, "wb") as fh:
                    fh.write(maybe_bytes)
                if os.path.exists(path) and os.path.getsize(path) > MIN_IMG_BYTES:
                    logging.info("Wrote bytes fallback for msg %s -> %s", getattr(msg, 'id', '?'), path)
                    return path.replace("\\", "/")
        except Exception:
            pass
    except Exception as e:
        logging.debug("Failed to download telegram media for msg %s : %s", getattr(msg, 'id', '?'), e)
    return None

# ---------- DB persistence ----------
def init_db():
    pathlib.Path(ASSETS_DIR).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed (
                msg_id TEXT PRIMARY KEY,
                shortlink TEXT,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    conn.close()

def clear_db():
    conn = sqlite3.connect(DB_PATH)
    with conn:
        conn.execute("DELETE FROM processed")
    conn.close()

def was_processed(msg_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM processed WHERE msg_id = ?", (msg_id,))
    row = cur.fetchone()
    conn.close()
    return bool(row)

def mark_processed(msg_id: str, shortlink: str = ""):
    conn = sqlite3.connect(DB_PATH)
    with conn:
        conn.execute("INSERT OR IGNORE INTO processed (msg_id, shortlink) VALUES (?,?)", (msg_id, shortlink))
    conn.close()

def get_last_run_time() -> Optional[datetime]:
    """Return the latest 'first_seen' timestamp from processed table (UTC) or None."""
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    try:
        cur.execute("SELECT max(first_seen) FROM processed")
        r = cur.fetchone()
        conn.close()
        if r and r[0]:
            # SQLite timestamp stored as text -> parse
            try:
                # try iso format
                return datetime.fromisoformat(r[0]).astimezone(timezone.utc)
            except Exception:
                try:
                    # fallback epoch parse
                    return datetime.utcfromtimestamp(float(r[0])).replace(tzinfo=timezone.utc)
                except Exception:
                    return None
    except Exception:
        try:
            conn.close()
        except:
            pass
    return None

# ---------- HTML builder ----------
def time_ago_str(dt: Optional[datetime], relative=True) -> str:
    if not dt: return ""
    if relative:
        now = datetime.now(timezone.utc)
        diff = now - dt
        secs = int(diff.total_seconds())
        if secs < 60: return f"{secs}s ago"
        if secs < 3600: return f"{secs//60}m ago"
        if secs < 86400: return f"{secs//3600}h ago"
        return f"{secs//86400}d ago"
    else:
        try:
            return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        except:
            return dt.strftime("%Y-%m-%d %H:%M:%S")

def build_index(cards, show_relative=True, banner_rel: Optional[str] = None, hero_height: str = "160px"):
    """
    Interactive shopping-style renderer. Expects each card dict to include:
      - title, description, merchant_label, local_image, date_obj (datetime), buy_link, shortlink
    Produces a paginated, store-like HTML (client-side rendering).
    """
    # Prepare serializable cards: convert date_obj to ISO strings and coerce other non-serializable values
    serializable = []
    for c in cards:
        cc = {}
        for k, v in c.items():
            if isinstance(v, datetime):
                cc[k] = v.astimezone(timezone.utc).isoformat()
            else:
                # for safety: convert bytes/other odd types to str
                try:
                    json.dumps(v)
                    cc[k] = v
                except Exception:
                    try:
                        cc[k] = str(v)
                    except Exception:
                        cc[k] = ""
        # also ensure local_image slashes
        if cc.get("local_image"):
            cc["local_image"] = cc["local_image"].replace("\\", "/")
        # keep a date_iso field for backwards compatibility
        if "date_obj" in cc and isinstance(cc["date_obj"], str):
            cc["date_iso"] = cc["date_obj"]
        else:
            cc.setdefault("date_iso", cc.get("date_obj", ""))
        serializable.append(cc)

    # Save cards JSON snapshot to assets for reuse next run
    try:
        with open(CARDS_JSON_PATH, "w", encoding="utf8") as fh:
            json.dump(serializable, fh, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        logging.debug("Failed to write cards snapshot: %s", e)

    # JSON for embedding — default safe conversion ensured above
    cards_json = json.dumps(serializable, ensure_ascii=False)

    # protect against accidentally closing the <script> tag when we embed JSON
    cards_json_safe = cards_json.replace("</", "<\\/")

    # banner HTML fragment if we have a banner
    banner_html = ""
    if banner_rel:
        banner_html = f'''
  <div class="banner-wrap">
    <a href="https://t.me/bestpricezone" target="_blank" rel="noopener">
      <img src="{banner_rel}" alt="BestPriceZone Banner" />
    </a>
  </div>
'''

    # generation timestamp for footer
    gen_ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

    # Build HTML (JS inside the string only)
    # NOTE: Small pager CSS added to make nice buttons
    html_template = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<meta name="google-site-verification" content="NItxRY7xmwWh_Uh0Jxx_6JQ5AqE7g9FTsYG85PLU-Cw" />
<title>BestPriceZone | Today's Best Online Shopping Deals & Discounts in India</title>
<link rel="icon" href="favicon.ico" type="image/x-icon">

<link rel="icon" type="image/png" href="assets/favicon.png">

<link rel="icon" type="image/png" sizes="48x48" href="assets/favicon-48x48.png">
<link rel="icon" type="image/png" sizes="96x96" href="assets/favicon-96x96.png">
<link rel="icon" type="image/png" sizes="144x144" href="assets/favicon-144x144.png">

<link rel="apple-touch-icon" sizes="180x180" href="assets/apple-touch-icon.png">

<meta name="title" content="BestPriceZone — Best Online Deals & Discounts" />
<meta name="description" content="BestPriceZone brings you the latest discounts from Amazon, Flipkart, Ajio, Myntra & more. Save money with daily updated deals, offers, and coupons.">

<!-- Open Graph / Facebook -->
<meta property="og:type" content="website" />
<meta property="og:title" content="BestPriceZone — Curated Online Shopping Deals" />
<meta property="og:description" content="Find the best discounts and offers from Flipkart, Amazon, Myntra, Ajio and more. Updated frequently." />
<meta property="og:image" content="__BANNER_IMAGE__" />
<meta property="og:url" content="https://bestpricezone.in/" />

<!-- Twitter -->
<meta name="twitter:card" content="summary_large_image" />
<meta name="twitter:title" content="BestPriceZone — Curated Online Shopping Deals" />
<meta name="twitter:description" content="Find the best discounts and offers from Flipkart, Amazon, Myntra, Ajio and more. Updated frequently." />
<meta name="twitter:image" content="__BANNER_IMAGE__" />
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700;800&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#f5f7fb;
  --card:#ffffff;
  --muted:#667085;
  --accent:#111827;
  --primary:#0f62fe;
  --pill:#eef2ff;
  --hero-height:__HERO_HEIGHT__; /* preserved for optional max-height */
  --max-width:1200px;
  --gutter:12px;
  --card-radius:12px;
  --shadow:0 8px 20px rgba(16,24,40,0.06);
}
*{box-sizing:border-box}
html,body{height:100%}
body{
  font-family:Inter, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial;
  margin:0; background:var(--bg); color:var(--accent); -webkit-font-smoothing:antialiased;
  -moz-osx-font-smoothing:grayscale;
}

/* ---------- Header (kept compact & stable) ---------- */
.header{
  background:linear-gradient(90deg,#fff 0%, #f9fbff 100%);
  padding:14px var(--gutter);
  border-bottom:1px solid rgba(0,0,0,0.04);
  position:sticky;
  top:0;
  z-index:80;
  backdrop-filter:saturate(120%) blur(4px);
}

/* use space-between so left brand and right controls stay aligned */
.header .wrap{
  max-width:var(--max-width);
  margin:0 auto;
  display:flex;
  gap:12px;
  align-items:center;
  justify-content:space-between;
  padding-top:6px;
  padding-bottom:6px;
}

/* brand area (left) */
.brand-wrap{
  display:flex;
  align-items:center;
  min-width:0;
  flex:1 1 auto;
}

/* title + tagline stack but don't overflow */
.site-header {
  display:flex;
  flex-direction:column;
  align-items:flex-start;
  gap:2px;
  min-width:0;
  margin:0;
  padding:0;
}

/* constrain title so search/control won't wrap underneath */
.site-title {
  font-weight:800;
  font-size:32.5px;
  line-height:1.2;
  color:var(--accent);
  margin:0;
  white-space:nowrap;
  overflow:hidden;
  text-overflow:ellipsis;
  max-width: calc(100vw - 340px); /* leave room for controls (tweak if needed) */
}

/* tagline */
.site-tagline {
  font-size:14px;
  font-weight:500;
  color:var(--muted);
  margin:0;
  line-height:1.3;
  white-space:normal;
}

/* controls area (right) stays fixed size and does not grow */
.controls{
  flex:0 0 auto;
  display:flex;
  gap:10px;
  align-items:center;
}

/* search box: keep compact and never force header wrap */
.search{
  display:flex;
  align-items:center;
  background:#fff;
  padding:8px;
  border-radius:12px;
  box-shadow:0 6px 18px rgba(16,24,40,0.04);
  min-width:0;
}
.search input{
  border:0;
  outline:0;
  font-size:14px;
  padding:6px 8px;
  width:260px;
  max-width: calc(100vw - 360px);
  min-width:0;
  background:transparent;
}

/* ---------- Banner (responsive, no crop, centered) ---------- */
/* banner container keeps full-width centering */
.banner-wrap{
  width:100%;
  display:flex;
  justify-content:center;
  padding:0 8px;
  margin:12px 0;
  box-sizing:border-box;
}

/* image: show whole image, keep natural height, but cap max-height on very large screens */
.banner-wrap img{
  display:block;
  width:100%;
  max-width:var(--max-width);
  height:auto;               /* natural height; no forced cropping */
  object-fit:contain;        /* show whole image without crop */
  object-position:center;
  border-radius:var(--card-radius);
  border-bottom:6px solid rgba(255,255,255,0.04);
  margin:0 auto;
  /* optional visual cap so banner doesn't grow excessively tall on very wide screens */
  max-height: calc(var(--hero-height, 300px) * 1.15);
}

/* ---------- Hero & filters: hug the banner, reduce big gaps ---------- */
.hero{
  max-width:var(--max-width);
  margin:8px auto 12px;   /* tighter spacing so hero hugs banner */
  padding:14px 16px;
  border-radius:var(--card-radius);
  background:linear-gradient(90deg,#ffffff,#f7fbff);
  display:flex;
  gap:12px;
  align-items:center;
  flex-wrap:wrap;
}
.hero .h{ font-size:18px; font-weight:800; margin:0; }

/* filter bar layout */
.filter-bar{
  max-width:var(--max-width);
  margin:12px auto;
  display:flex;
  gap:12px;
  align-items:center;
  padding:0 var(--gutter);
  box-sizing:border-box;
  flex-wrap:wrap;
}

/* keep the left label + chips in a single flow block that can shrink */
.filter-bar > div:first-child{
  display:flex;
  gap:12px;
  align-items:center;
  flex:1 1 auto;
  min-width:0;
}

/* right-side controls (sort / perpage) anchored */
.filter-bar > div:last-child{
  flex:0 0 auto;
  display:flex;
  align-items:center;
  gap:8px;
}

/* merchant chips wrap and behave predictably */
.filter-bar .chips {
  display:flex;
  gap:8px;
  flex-wrap:wrap;
  align-items:center;
}
.filter-bar .chips button {
  appearance:none;
  border:0;
  outline:0;
  cursor:pointer;
  font-size:13px;
  font-weight:600;
  padding:8px 14px;
  border-radius:999px;
  background:#f9fafb;
  color:#111827;
  transition:all .22s;
  box-shadow:0 1px 3px rgba(0,0,0,0.06);
}
.filter-bar .chips button:hover{ transform:translateY(-1px); background:#eef2ff; color:#1e3a8a; }
.filter-bar .chips button.active{ background:linear-gradient(90deg,#2563eb,#0f62fe); color:#fff; box-shadow:0 6px 18px rgba(37,99,235,0.12); }

/* select / controls small tweaks */
.filter-bar select{ padding:8px; border-radius:10px; border:1px solid rgba(16,24,40,0.06); background:#fff; }

/* ---------- Responsive tweaks ---------- */
@media (max-width: 980px) {
  .site-title { font-size: 26px; max-width: calc(100vw - 240px); }
  .search input { width: 180px; max-width: calc(100vw - 260px); }

  /* keep banner under control on medium screens */
  .banner-wrap img { max-height: calc(var(--hero-height, 260px) * 1.0); }

  .hero { padding: 14px; margin: 10px 12px; gap: 10px; }
  .filter-bar { padding-left: 10px; padding-right: 10px; gap: 10px; }
  .grid { gap: 14px; }
}

@media (max-width: 720px) {
  .site-title { font-size: 20px; max-width: calc(100vw - 140px); white-space: normal; }
  .search input { width: 140px; max-width: calc(100vw - 160px); }

  .banner-wrap { padding-left: 10px; padding-right: 10px; margin: 10px 0; }
  /* keep whole banner visible but cap height on phones */
  .banner-wrap img { border-radius: 10px; max-height: calc(var(--hero-height, 220px) * 0.95); }

  .hero { padding: 12px; margin: 6px 8px 10px; gap: 8px; }
  .filter-bar { padding-left: 10px; padding-right: 10px; gap: 8px; }

  /* force the right controls (sort/perpage) to the next line and align right */
  .filter-bar > div:last-child { width: 100%; justify-content: flex-end; margin-top: 6px; display: flex; }
  .filter-bar > div:first-child { min-width: 0; flex: 1 1 auto; }
  .grid { grid-template-columns: repeat(auto-fill, minmax(clamp(140px, 42%, 260px), 1fr)); gap: 12px; }
  #modal-image { height: 320px; }

  /* Slightly taller thumb on medium-narrow screens to reduce blank padding */
  .thumb {
    position: relative;
    aspect-ratio: 4 / 3;
    border-radius: 10px;
    overflow: hidden;
    background: #fff;
  }
}

@media (max-width: 520px) {
  .share-popup { min-width: 92vw; max-width: 92vw; left: 4vw !important; right: 4vw !important; }
  .share-preview .img { width: 56px; height: 56px; }
}

@media (max-width: 420px) {
  .site-title { font-size: 18px; }
  .search input { width: 92px; }

  /* very small phones: make the banner compact but still whole */
  .banner-wrap img { border-radius: 8px; max-height: 140px; height: auto; object-fit: contain; }

  .hero { padding: 10px; margin: 6px 8px 10px; gap: 8px; }
  .filter-bar { gap: 6px; padding-left: 8px; padding-right: 8px; }
  .filter-bar .chips button { padding: 7px 12px; font-size: 13px; }

  .grid { grid-template-columns: repeat(auto-fill, minmax(clamp(140px, 45%, 220px), 1fr)); gap: 12px; }

  /* make thumbs less tall on smallest screens but keep aspect-ratio to reduce blank space */
  .thumb {
    position: relative;
    aspect-ratio: 4 / 3;
    border-radius: 10px;
    overflow: hidden;
    background: #fff;
  }
}

/* Small utility: ensure header/hero/filter layering doesn't obscure interactive elements */
.header, .banner-wrap, .hero, .filter-bar { position: relative; z-index: 10; }


/* ---------- grid and cards (cleaned) ---------- */
.container {
  max-width: var(--max-width);
  margin: 0 auto;
  padding: 8px var(--gutter) 100px;
  box-sizing: border-box;
}

.grid {
  display: grid;
  gap: 16px;
  grid-template-columns: repeat(auto-fill, minmax(clamp(160px, 42%, 320px), 1fr));
}

/* card */
.card {
  background: var(--card);
  border-radius: 12px;
  padding: 12px;
  box-shadow: var(--shadow);
  display: flex;
  flex-direction: column;
  transition: transform .18s, box-shadow .18s;
  min-height: unset;
}
.card:hover { transform: translateY(-4px); box-shadow: 0 14px 28px rgba(16,24,40,0.08); }

/* thumbnail container: prefer aspect-ratio (modern) fallback to padding-top if needed */
.thumb {
  position: relative;
  aspect-ratio: 4 / 3;
  border-radius: 10px;
  overflow: hidden;
  background: #fff;
}

/* thumbnail image: centered and fully visible (no cropping) */
.thumb img {
  width: 100%;
  height: 100%;
  object-fit: contain;    /* show whole image without crop */
  object-position: center;
  display: block;
  /* absolute centering removed — simpler and more predictable */
}

/* meta / text */
.meta { display: flex; align-items: center; justify-content: space-between; margin-top: 10px; }
.title { font-weight: 700; font-size: 14px; margin: 8px 0; line-height: 1.18; min-height: 42px; overflow: hidden; }
.badge { display: inline-block; background: var(--pill); color: var(--primary); padding: 6px 10px; border-radius: 999px; font-weight: 700; font-size: 12px; }


/* ---------- Card actions: ensure View + Share always inline; Buy can drop ---------- */
/* Desktop: single-row layout */
.card .actions {
  margin-top: auto;
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: nowrap; /* desktop: do not wrap */
  box-sizing: border-box;
}

/* View button: now wide/flexible like Buy used to be */
.card .actions .view-btn {
  flex: 1 1 auto;          /* grow & shrink */
  min-width: 0;            /* allow shrinking */
  max-width: 100%;         /* don’t artificially cap */
  padding: 10px 12px;
  border-radius: 8px;
  text-align: center;
  text-decoration: none;
  color: #fff;
  background: #2563eb;     /* blue, primary */
  border: 1px solid rgba(16,24,40,0.06);
  font-weight: 700;
  box-sizing: border-box;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Share: unchanged */
.card .actions .share-btn {
  flex: 0 0 44px;
  width: 44px;
  height: 44px;
  padding: 8px;
  border-radius: 10px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: #fff;
  border: 1px solid rgba(16,24,40,0.06);
  box-sizing: border-box;
  flex-shrink: 0;
}

/* Buy: now compact/fixed like old View */
.card .actions .buy {
  flex: 0 0 auto;          /* no growth */
  min-width: 96px;         /* fixed base width */
  padding: 10px 14px;
  border-radius: 8px;
  text-decoration: none;
  font-weight: 600;
  background: #2563eb;
  color: #fff;
  box-shadow: 0 2px 6px rgba(37,99,235,0.25);
  box-sizing: border-box;
  white-space: nowrap;
  text-align: center;     /* centers the text */
}

/* ---------- Small screens: force View+Share inline, Buy full width below ---------- */
@media (max-width: 720px) {
  .card .actions {
    /* allow wrapping so Buy can go to its own row when needed */
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }

  /* Force VIEW first and let it shrink to fit next to SHARE.
     Keep it single-line (no wrapping) — truncate if too long. */
  .card .actions .view-btn {
    order: 1;
    flex: 1 1 calc(100% - 56px); /* leave room for share (44) + small gap */
    min-width: 0;
    max-width: 100%;
    white-space: nowrap;         /* CRITICAL: prevent wrapping */
    overflow: hidden;
    text-overflow: ellipsis;
    padding: 8px 10px;
    font-size: 14px;
    background: #2563eb;
    color: #fff;
  }

  /* SHARE to the right of VIEW, fixed size */
  .card .actions .share-btn {
    order: 2;
    flex: 0 0 44px;
    width: 44px;
    height: 44px;
    margin-left: 8px;
  }

  /* BUY on its own full-width row below */
  .card .actions .buy {
    order: 3;
    flex: 1 1 100%;
    width: 100%;
    margin-top: 8px;
    text-align: center;
    font-size: 14px;
    padding: 8px 10px;
    text-align: center;     /* centers the text */
  }
}

/* Extra-tight phones: tiny tweaks so View stays single-line and Share keeps size */
@media (max-width: 420px) {
  .card .actions .view-btn {
    flex: 1 1 calc(100% - 52px);
    padding: 7px 10px;
    font-size: 13px;
  }
  .card .actions .share-btn {
    flex: 0 0 40px;
    width: 40px;
    height: 40px;
    margin-left: 6px;
  }
  .card .actions .buy {
    padding: 8px 10px;
    font-size: 13px;
  }
}

/* force a two-column first row: left area for View, right for Share.
   Buy will wrap below when needed. This avoids calc() problems. */
@media (max-width: 720px) {
  .card .actions {
    display: grid;
    grid-template-columns: 1fr 44px;
    grid-auto-rows: auto;
    gap: 8px;
    align-items: center;
  }

  /* place the Buy button on the next row and make it full width */
  .card .actions .buy {
    grid-column: 1 / -1;
    width: 100%;
    order: unset; /* ensure CSS Grid handles placement */
    justify-self: stretch;
    text-align: center;     /* centers the text */
  }

  /* View spans the left cell */
  .card .actions .view-btn {
    grid-column: 1 / 2;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* Share goes to the right column */
  .card .actions .share-btn {
    grid-column: 2 / 3;
    justify-self: end;
  }
}

/* ---------- FORCE SWAP: on desktop make BUY expand, VIEW fixed ---------- */
@media (min-width: 721px) {
  /* Make Buy take remaining space (wide) */
  .card .actions .buy {
    flex: 1 1 auto !important;
    min-width: 0 !important;
    width: auto !important;
    max-width: 100% !important;
    padding: 10px 12px !important;
    box-sizing: border-box !important;
    text-align: center;     /* centers the text */
  }

  /* Make View a smaller fixed button (narrow) */
  .card .actions .view-btn {
    flex: 0 0 auto !important;
    min-width: 96px !important;
    width: 96px !important;
    white-space: nowrap !important;
    overflow: hidden !important;
    text-overflow: ellipsis !important;
    padding: 10px 12px !important;
    box-sizing: border-box !important;
    text-align: center;     /* centers the text */
    justify-content: center; /* if flex is applied somewhere */
    display: flex;          /* make it flex so justify works */
    align-items: center;    /* vertically centers text */
  }

  /* Keep share fixed */
  .card .actions .share-btn {
    flex: 0 0 44px !important;
    width: 44px !important;
    height: 44px !important;
  }

  /* Ensure layout spacing is sane */
  .card .actions { gap: 12px !important; align-items: center !important; }
}


/* ---------- Share popup, modal, footer, pagination unchanged (kept for completeness) ---------- */
.share-popup {
  position: absolute;
  z-index: 10010;
  background: #fff;
  border-radius: 12px;
  box-shadow: 0 20px 40px rgba(2,6,23,0.18);
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 320px;
  max-width: 420px;
  font-family: Inter, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial;
}
.share-preview { display: flex; gap: 10px; align-items: center; }
.share-preview .img { width: 64px; height: 64px; border-radius: 8px; overflow: hidden; flex: 0 0 64px; background: #f6f7fb; display:flex; align-items:center; justify-content:center; }
.share-preview .img img { width: 100%; height: 100%; object-fit: cover; display: block; }
.share-preview .meta { flex: 1; min-width: 0; }
.share-preview .meta .title { font-weight: 700; font-size: 14px; line-height: 1.2; color: #0f1724; max-height: 3.6em; overflow: hidden; }

.share-row { display: flex; gap: 8px; align-items: center; justify-content: space-between; flex-wrap: wrap; }
.share-row .url { font-size: 13px; color: #475569; word-break: break-all; flex: 1; min-width: 0; }
.actions-inline { display: flex; gap: 8px; align-items: center; flex: 0 0 auto; }
.share-btn-inline { background: #0f62fe; color: #fff; border: 0; padding: 8px 10px; border-radius: 8px; font-weight: 700; font-size: 13px; cursor: pointer; box-shadow: 0 6px 18px rgba(15,98,254,0.12); }
.copy-btn { background: transparent; border: 1px solid rgba(15,23,42,0.06); padding: 8px 10px; border-radius: 8px; cursor: pointer; font-weight: 600; }
.social-links { display: flex; gap: 6px; align-items: center; }
.social-links a { padding: 8px 10px; border-radius: 8px; text-decoration: none; font-weight: 700; font-size: 13px; border: 1px solid rgba(0,0,0,0.06); color: #0f1724; }

.share-toast { position: fixed; bottom: 22px; left: 50%; transform: translateX(-50%); background: #0f1724; color: white; padding: 8px 12px; border-radius: 999px; font-weight: 700; z-index: 12000; opacity: 0; transition: opacity .18s; }
.share-toast.show { opacity: 1; }

.modal-backdrop { position: fixed; inset: 0; background: rgba(2,6,23,0.6); display: none; align-items: center; justify-content: center; z-index: 140; }
.modal { background: #fff; border-radius: 12px; max-width: 920px; width: calc(100% - 32px); max-height: 92vh; overflow: auto; padding: 18px; display: flex; gap: 18px; flex-wrap: wrap; }
.modal .left { flex: 1; min-width: 220px; }
.modal .right { width: 360px; max-width: 100%; display: flex; flex-direction: column; }
.close-btn { background: transparent; border: 0; font-size: 18px; cursor: pointer; color: #666; padding: 6px 8px; border-radius: 8px; }
#modal-image { width: 100%; height: 420px; object-fit: contain; background: #fff; }

.modal .modal-link-row { width: 100%; box-sizing: border-box; display: flex; align-items: center; gap: 12px; margin-top: 8px; flex-wrap: wrap; }
.modal .modal-link-row > div { flex: 1 1 auto; word-break: break-word; white-space: normal; max-width: calc(100% - 120px); }
.modal .modal-link-row .buy { flex: 0 0 auto; min-width: 96px; margin-left: auto; flex-shrink: 0; }
#modal-links-wrap { width: 100%; display: flex; flex-direction: column; gap: 8px; }

.footer { background: #fff; border-top: 1px solid rgba(0,0,0,0.04); padding: 28px 12px; color: var(--muted); margin-top: 30px; }
.footer .wrap { max-width: var(--max-width); margin: 0 auto; display: flex; gap: 16px; align-items: flex-start; flex-wrap: wrap; }
.footer .col { flex: 1; min-width: 160px; }
.footer h4 { margin: 0 0 8px 0; font-size: 14px; }
.footer p, .footer a { color: var(--muted); text-decoration: none; font-size: 13px; }
.footer .bottom { max-width: var(--max-width); margin: 18px auto 0; text-align: center; font-size: 13px; color: var(--muted); }

/* Footer: make layout stack on small screens and add share button styles */
.footer .wrap {
  display: flex;
  gap: 16px;
  align-items: flex-start;
  flex-wrap: wrap; /* allow wrapping on small screens */
}

/* Footer share button (matches site button language, smaller visual weight) */
.footer .share-page {
  appearance: none;
  border: 0;
  outline: 0;
  cursor: pointer;
  font-weight: 700;
  padding: 10px 14px;
  border-radius: 10px;
  background: #fff;
  color: #000;
  box-shadow: 0 6px 18px rgba(37,99,235,0.12);
  font-size: 14px;
}

/* Place share control in footer bottom area for compact screens */
@media (max-width: 720px) {
  .footer .wrap {
    flex-direction: column;
    align-items: center;
    text-align: center;
    gap: 12px;
    padding: 12px;
  }
  .footer .col { width: 100%; }
  .footer .share-row { display:flex; gap:8px; justify-content:center; margin-top:6px; }
}

/* very small phones: slightly smaller share button */
@media (max-width: 420px) {
  .footer .share-page { padding: 8px 12px; font-size: 13px; border-radius: 9px; }
}

.pagination { display: flex; gap: 8px; justify-content: center; margin: 18px 0; }
.pagination button { background: #fff; border: 1px solid rgba(16,24,40,0.08); padding: 8px 12px; border-radius: 10px; cursor: pointer; font-weight: 600; box-shadow: 0 6px 12px rgba(16,24,40,0.03); }
.pagination button:hover { transform: translateY(-2px); }
.pagination button.active { background: var(--primary); color: #fff; border-color: transparent; box-shadow: 0 10px 20px rgba(15,98,254,0.14); }

/* ---------- Modal (restored, tightened, and consistent with site buttons) ---------- */
.modal-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(2,6,23,0.6);
  display: none;               /* toggled via JS */
  align-items: center;
  justify-content: center;
  z-index: 140;
}

/* modal shell */
.modal {
  background: #fff;
  border-radius: 12px;
  max-width: 920px;
  width: calc(100% - 32px);
  max-height: 92vh;
  overflow: auto;
  padding: 18px;
  display: flex;
  gap: 18px;
  flex-wrap: wrap;
  box-sizing: border-box;
  align-items: flex-start;
}

/* left (image) / right (meta) columns */
.modal .left {
  flex: 1 1 0;
  min-width: 220px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.modal .right {
  flex: 0 0 360px;
  max-width: 100%;
  display: flex;
  flex-direction: column;
  gap: 12px;
  box-sizing: border-box;
}

/* close button */
.close-btn {
  background: transparent;
  border: 0;
  font-size: 18px;
  cursor: pointer;
  color: #666;
  padding: 6px 8px;
  border-radius: 8px;
}
.close-btn:hover { background: rgba(0,0,0,0.03); }

/* Modal image: preserve aspect, avoid stretching */
#modal-image {
  width: 100%;
  height: 420px;
  object-fit: contain;
  background: #fff;
  border-radius: 8px;
  display: block;
}

/* header row inside modal right column: title + actions (close/share) */
.modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  width: 100%;
  box-sizing: border-box;
}
.modal-header .modal-title,
.modal-title {
  margin: 0;
  font-weight: 800;
  font-size: 18px;
  line-height: 1.2;
  flex: 1 1 auto;
  white-space: normal;
  word-break: break-word;
}

/* Ensure the header buttons stay aligned right and do not wrap */
.modal-header > * { flex-shrink: 0; }

/* modal link rows (for multiple buy links): label left, buy button right */
.modal .modal-link-row {
  width: 100%;
  box-sizing: border-box;
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 8px;
  flex-wrap: wrap;
}
.modal .modal-link-row > div {
  flex: 1 1 auto;
  word-break: break-word;
  white-space: normal;
  max-width: calc(100% - 120px);
}

/* container for multiple link rows */
#modal-links-wrap {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 8px;
  box-sizing: border-box;
}

/* ---------- Buttons inside modal (match site buttons) ---------- */

/* Shared buy button style (applies to rows and standalone buy buttons) */
.modal .modal-link-row .buy,
.modal .right .buy,
.modal .buy {
  background: #2563eb;
  color: #fff;
  padding: 10px 14px;
  border-radius: 8px;
  text-decoration: none;
  font-weight: 600;
  font-size: 14px;
  display: inline-block;
  text-align: center;
  transition: background 0.2s ease, transform 0.15s ease;
  box-shadow: 0 2px 6px rgba(37, 99, 235, 0.25);

  flex: 0 0 auto;
  min-width: 96px;
  margin-left: auto;
  flex-shrink: 0;
}
.modal .modal-link-row .buy:hover,
.modal .right .buy:hover,
.modal .buy:hover {
  background: #1d4ed8;
  transform: translateY(-2px);
  box-shadow: 0 4px 12px rgba(37, 99, 235, 0.3);
}

/* Share button inside modal link rows (matches .share-btn look from main site) */
.modal .modal-link-row .share-btn,
.modal .modal-header .share-btn,
#modal-share {
  background: #ffffff;
  color: #0f1724;
  padding: 10px 12px;
  border-radius: 8px;
  border: 1px solid rgba(16,24,40,0.06);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  box-shadow: 0 2px 6px rgba(2,6,23,0.06);
  transition: background 0.18s ease, transform 0.12s ease, color 0.18s ease;
  font-weight: 600;
  min-width: 44px;
  height: 44px;
}
.modal .modal-link-row .share-btn:hover,
.modal .modal-header .share-btn:hover,
#modal-share:hover {
  background: #1d4ed8;
  color: #fff;
  transform: translateY(-2px);
  box-shadow: 0 6px 16px rgba(15,98,254,0.12);
}

/* If you want icon-only variant in header to be a bit smaller */
.modal .modal-header .share-btn.icon,
#modal-share.icon {
  padding: 8px;
  width: 40px;
  height: 40px;
  border-radius: 10px;
}

/* ---------- Small-screen adjustments: modal stacks ---------- */
@media (max-width: 720px) {
  .modal {
    padding: 14px;
    gap: 12px;
  }
  .modal .left { min-width: 0; }
  .modal .right { flex: 1 1 100%; width: 100%; }
  #modal-image { height: 320px; }

  /* ensure modal header actions remain in one row */
  .modal-header { gap: 8px; }

  /* keep buy/share rows tidy */
  .modal .modal-link-row { align-items: center; }
  .modal .modal-link-row .buy { margin-top: 0; }
}

/* very small phones */
@media (max-width: 420px) {
  .modal { padding: 12px; gap: 10px; }
  #modal-image { height: 260px; }
  .modal .modal-link-row > div { max-width: calc(100% - 96px); }
  .modal .modal-link-row .share-btn { min-width: 40px; height: 40px; }
}

/* Footer share button styling */
.footer .share-page {
  appearance: none;
  border: 0;
  outline: 0;
  cursor: pointer;
  font-weight: 700;
  padding: 10px 14px;
  border-radius: 10px;
  background: #fff;
  color: #000;
  box-shadow: 0 6px 18px rgba(37,99,235,0.12);
  font-size: 14px;
  transition: background 0.2s ease, transform 0.15s ease;
}
.footer .share-page:hover {
  background: #1d4ed8;
  transform: translateY(-2px);
  box-shadow: 0 10px 20px rgba(37,99,235,0.25);
}

/* Mobile-friendly footer */
@media (max-width: 720px) {
  .footer .wrap {
    flex-direction: column;
    align-items: center;
    text-align: center;
    gap: 14px;
    padding: 12px;
  }
  .footer .col { width: 100%; }
  .footer .share-page { width: auto; }
}

/* Very small screens: shrink buttons a bit */
@media (max-width: 420px) {
  .footer .share-page { padding: 8px 12px; font-size: 13px; border-radius: 9px; }
}


</style>

</head>
<body>
  <header class="header">
  <div class="wrap">
    <div class="brand-wrap">
      <div class="site-header" role="banner" aria-label="Site title">
        <h1 class="site-title">Best Price Zone</h1>
        <div class="site-tagline">Best Online Deals &amp; Discounts in India</div>
      </div>
    </div>

    <div class="controls">
      <div class="search" role="search">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" style="opacity:.6;margin-left:4px">
          <path d="M21 21l-4.35-4.35" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
          <circle cx="11" cy="11" r="6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
        <input id="q" placeholder="Search products, brands, categories..." />
      </div>
    </div>
  </div>
</header>
__BANNER_HTML__
  <div class="hero" role="region" aria-label="Latest deals">
    <div style="flex:1">
      <div class="h">Curated online shopping deals in India</div>
      <div style="color:var(--muted);margin-top:6px;font-size:13px">Find the best prices from Amazon, Flipkart, Myntra, Ajio & more. Filter offers, sort by lowest price or latest arrivals, and click to shop securely.</div>
    </div>
    <div style="min-width:200px;text-align:right">
      <div style="font-size:13px;color:var(--muted)">Items per page</div>
      <select id="perpage" style="margin-top:6px;padding:6px;border-radius:8px">
        <option value="25">25</option>
        <option value="50">50</option>
        <option value="100">100</option>
      </select>
    </div>
  </div>

  <div class="filter-bar" role="region" aria-label="Filters">
    <div style="display:flex;gap:12px;align-items:center">
      <div style="font-weight:600;color:var(--muted)">Merchants</div>
      <div class="chips" id="merchant-chips"></div>
    </div>
    <div style="margin-left:auto;display:flex;gap:8px;align-items:center">
      <div class="small">Sort</div>
      <select id="sort">
        <option value="new">Newest</option>
        <option value="old">Oldest</option>
        <option value="price-asc">Price: Low → High</option>
        <option value="price-desc">Price: High → Low</option>
        <option value="title">Title (A→Z)</option>
      </select>
    </div>
  </div>

  <main class="container" role="main">
    <div id="grid" class="grid" aria-live="polite"></div>
    <div id="empty" class="empty" style="display:none">No deals found.</div>
    <div id="pager" class="pagination"></div>
  </main>

  <footer class="footer" role="contentinfo">
  <div class="wrap">
    <div class="col">
      <h4>About</h4>
      <p>BestPriceZone brings you the latest curated shopping deals, updated in real-time.</p>
    </div>
    <div class="col">
      <h4>Explore</h4>
      <p><a href="#">Back to Top</a></p>
    </div>
    <div class="col">
      <h4>Don’t miss a deal 👇</h4>
      <p><a href="https://t.me/bestpricezone" target="_blank">Follow BestPriceZone on Telegram</a></p>
    </div>
    <div class="col">
    <h4>Share this page</h4>
      <button id="share-page" class="share-page" aria-label="Share this page">
        Share
      </button>
    </div>

  </div>
  </div>
  <div class="bottom">Deals are collected from other stores — not sold here. Please shop carefully. © 2025 BestPriceZone. All rights reserved.</div>
</footer>

  <!-- modal -->
  <div id="modal-back" class="modal-backdrop" aria-hidden="true">
    <div class="modal" role="dialog" aria-modal="true">
      <div class="left">
        <div style="border-radius:10px;overflow:hidden;margin-top:6px" id="modal-image-wrap">
          <img id="modal-image" src="" style="width:100%;height:420px;object-fit:contain;background:#fff" />
        </div>
      </div>
      <div class="right">
        <div style="display:flex;flex-direction:column;gap:10px">
          <!-- title row with close button -->
          <div class="modal-header">
            <h2 id="modal-title" class="modal-title"></h2>
            <div style="display:flex;gap:8px;align-items:center">
              <button class="share-btn icon" id="modal-share" aria-label="Share this deal (modal)"></button>
              <button class="close-btn" id="modal-close" aria-label="Close">✕</button>
            </div>
          </div>

          <div style="display:flex;align-items:center;gap:10px">
            <div id="modal-merchant" style="font-weight:700"></div>
            <div id="modal-time" style="margin-left:auto" class="small"></div>
          </div>

          <div style="display:flex;gap:10px;align-items:center;margin-top:12px">
            <a id="modal-buy" class="buy" target="_blank" rel="noopener">Buy now</a>
            <!-- affiliate link text removed to avoid showing shortlink -->
          </div>
        </div>
      </div>
    </div>
  </div>


<script>
  // embed cards snapshot
  window.CARDS = __CARDS_JSON__;
  // tell client if we want relative timestamps
  const SHOW_RELATIVE = __SHOW_RELATIVE__;

  function parsePrice(text) {
    if(!text) return null;
    // rupee style amounts first
    let rupee = text.match(/₹\s*[\d,]+(?:\.\d+)?/);
    if(!rupee) rupee = text.match(/Rs\.?\s*[\d,]+(?:\.\d+)?/i);
    if(!rupee) rupee = text.match(/[\d,]+(?:\.\d+)?\s*(?:\/-|INR)\b/i);
    if(rupee) return rupee[0].replace(/\s+/g,'');

    let percent = text.match(/\b(\d{1,3})\s*%/);
    if(percent) return percent[0];

    const ctx = text.toLowerCase();
    const discountWords = /\b(upto|up to|off|discount|% off|sale|loot|save|clearance|extra|flat)\b/;
    if(discountWords.test(ctx)) {
      let possible = ctx.match(/\b(\d{1,3})\b/);
      if(possible && parseInt(possible[1],10) <= 100) {
        return possible[1] + '%';
      }
    }
    return null;
  }

  function timeAgo(iso) {
    if(!iso) return '';
    const d = new Date(iso);
    const s = Math.floor((Date.now() - d.getTime())/1000);
    if(s < 60) return s + 's ago';
    if(s < 3600) return Math.floor(s/60) + 'm ago';
    if(s < 86400) return Math.floor(s/3600) + 'h ago';
    return Math.floor(s/86400) + 'd ago';
  }

// (duplicate old shareCard removed — using improved shareCardById / openShareMenu below)

  let cards = (window.CARDS || []).map(c => {
    return {
      id: c.id || (c.shortlink||'') + Math.random().toString(36).slice(2,8),
      title: c.title || '',
      description: c.description || '',
      raw_text: c.raw_text || c.description || c.title || '',   // <-- added
      merchant_label: c.merchant_label || (c.source_host || ''),
      local_image: c.local_image || '',
      date_iso: c.date_iso || c.date_obj || '',
      buy_link: c.buy_link || c.shortlink || '',
      urls: Array.isArray(c.urls) ? c.urls : (c.urls ? [c.urls] : []),
      price_raw: parsePrice((c.title || '') + ' ' + (c.description || '')) || ''
    };
  });

  const merchants = Array.from(new Set(cards.map(c => (c.merchant_label || '').split('.')[0]).filter(Boolean)));
  const merchantChips = document.getElementById('merchant-chips');
  merchants.slice(0,12).forEach(m=>{
    const btn = document.createElement('button');
    btn.textContent = m;
    btn.dataset.m = m;
    btn.addEventListener('click', ()=>{ btn.classList.toggle('active'); render(); });
    merchantChips.appendChild(btn);
  });

  const qInput = document.getElementById('q');
  const grid = document.getElementById('grid');
  const empty = document.getElementById('empty');
  const sortSelect = document.getElementById('sort');
  const perpageSel = document.getElementById('perpage');
  const pager = document.getElementById('pager');

  qInput.addEventListener('input', ()=>render());
  sortSelect.addEventListener('change', ()=>render());
  perpageSel.addEventListener('change', ()=>render());

  function getActiveMerchants() {
    const act = Array.from(merchantChips.querySelectorAll('button.active')).map(b=>b.dataset.m);
    return new Set(act);
  }

  let currentPage = 1;

  function render() {
    const q = (qInput.value||'').toLowerCase();
    const active = getActiveMerchants();
    let out = cards.filter(c=>{
      if(active.size && !active.has((c.merchant_label||'').split('.')[0])) return false;
      if(q === '') return true;
      return (c.title||'').toLowerCase().includes(q) || (c.description||'').toLowerCase().includes(q) || (c.merchant_label||'').toLowerCase().includes(q);
    } );

    const s = sortSelect.value;
    out.sort((a,b)=>{
      if(s === 'new') return (new Date(b.date_iso || 0)) - (new Date(a.date_iso || 0));
      if(s === 'old') return (new Date(a.date_iso || 0)) - (new Date(b.date_iso || 0));
      if(s === 'title') return (a.title||'').localeCompare(b.title||'');
      if(s === 'price-asc') {
        const pa = parseInt((a.price_raw||'').replace(/[^0-9]/g,''))||0;
        const pb = parseInt((b.price_raw||'').replace(/[^0-9]/g,''))||0;
        return pa - pb;
      }
      if(s === 'price-desc') {
        const pa = parseInt((a.price_raw||'').replace(/[^0-9]/g,''))||0;
        const pb = parseInt((b.price_raw||'').replace(/[^0-9]/g,''))||0;
        return pb - pa;
      }
      return 0;
    });

    const perPage = parseInt(perpageSel.value || "12", 10);
    const totalPages = Math.max(1, Math.ceil(out.length / perPage));
    if(currentPage > totalPages) currentPage = totalPages;

    const start = (currentPage - 1) * perPage;
    const pageItems = out.slice(start, start + perPage);

    grid.innerHTML = '';
    if(pageItems.length === 0) {
      empty.style.display = 'block';
    } else {
      empty.style.display = 'none';
    }

    pageItems.forEach(c => {
      const el = document.createElement('div');
      el.className = 'card';

      // compute relative time or full time depending on SHOW_RELATIVE
      const timeStr = (typeof SHOW_RELATIVE !== 'undefined' && SHOW_RELATIVE) ? timeAgo(c.date_iso) : (c.date_iso ? new Date(c.date_iso).toLocaleString() : '');

el.innerHTML = `
  <div class="thumb">
    <img loading="lazy" src="${c.local_image}" alt="${escapeHtml(c.title)}" />
  </div>

  <div class="meta" style="display:flex;justify-content:space-between;align-items:center;margin-top:10px">
    <div class="badge">${escapeHtml(c.merchant_label||'')}</div>
    <div class="small" style="color:var(--muted)">${escapeHtml(timeStr)}</div>
  </div>

  <div class="title">${escapeHtml(c.title)}</div>

  <div class="actions">
    <!-- Buy (only .buy has blue/full-width behavior on small screens) -->
    <a class="buy" href="${c.buy_link}" target="_blank" rel="noopener noreferrer">Buy Now</a>
    <!-- View (only view-btn class) -->
    <a class="view-btn" href="javascript:void(0)" data-id="${c.id}" aria-label="View details">View</a>

    <!-- Share placed immediately after View so they remain on the same row -->
    <button class="share-btn" onclick="openShareMenu(this, '${encodeURIComponent(c.id)}')" aria-label="Share this deal">
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
        <path d="M4 12v7a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-7"></path>
        <polyline points="16 6 12 2 8 6"></polyline>
        <line x1="12" y1="2" x2="12" y2="15"></line>
      </svg>
    </button>
  </div>
`;


      const viewBtn = el.querySelector('.view-btn');
      if (viewBtn) viewBtn.addEventListener('click', ()=>openModal(c));
      grid.appendChild(el);
    } );

        // render pager (with auto-scroll back to top of main results)
    pager.innerHTML = '';
    const scrollTarget = () => {
      // prefer .hero top, fallback to container main top, fallback to page top
      const hero = document.querySelector('.hero');
      const main = document.querySelector('main');
      const y = (hero && hero.getBoundingClientRect) ? (window.scrollY + hero.getBoundingClientRect().top - 10)
                : (main ? (window.scrollY + main.getBoundingClientRect().top - 10) : 0);
      return Math.max(0, Math.floor(y));
    };

    for (let p = 1; p <= totalPages; p++) {
      const b = document.createElement('button');
      b.className = 'page-btn' + (p === currentPage ? ' active' : '');
      b.textContent = p;
      b.addEventListener('click', () => {
        if (p === currentPage) return;
        currentPage = p;
        render();
        // scroll after render so the new page view is visible
        try {
          window.scrollTo({ top: scrollTarget(), behavior: 'smooth' });
        } catch (e) {
          window.scrollTo(0, scrollTarget());
        }
      });
      pager.appendChild(b);
    }

  }

  const modalBack = document.getElementById('modal-back');
  const modalImage = document.getElementById('modal-image');
  const modalTitle = document.getElementById('modal-title');
  const modalDesc = document.getElementById('modal-desc');
  const modalMerchant = document.getElementById('modal-merchant');
  const modalTime = document.getElementById('modal-time');
  const modalBuy = document.getElementById('modal-buy');

  document.getElementById('modal-close').addEventListener('click', closeModal);
  modalBack.addEventListener('click', (e)=>{ if(e.target === modalBack) closeModal(); });

function openModal(card) {
  // set main modal image
  modalImage.src = card.local_image || "";

  // Clean title so merchant prefixes like "Myntra | ..." never appear
  let cleanTitle = card.title || "";
  if (card.merchant_label) {
    // strip merchant name at start like "Myntra | " or "Myntra - " (case-insensitive)
    const re = new RegExp(
      "^\\s*" +
        card.merchant_label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") +
        "\\s*[:\\-|\\|]+\\s*",
      "i"
    );
    cleanTitle = cleanTitle.replace(re, "").trim();
  }
  // strip stray leading separators
  cleanTitle = cleanTitle.replace(/^[\|\-:]+/, "").trim();
  // remove noisy phrases
  cleanTitle = cleanTitle
    .replace(/\b(grab here|grab now|grab|link|loot:?|buy now|click here)\b/gi, "")
    .trim();
  // final cleanup of duplicate separators/spaces
  cleanTitle = cleanTitle.replace(/\s*[\|\-:]+\s*/g, " ").replace(/\s+/g, " ").trim();

  modalTitle.textContent = cleanTitle || card.merchant_label || "";

  modalMerchant.textContent = card.merchant_label || "";
  modalTime.textContent =
    typeof SHOW_RELATIVE !== "undefined" && SHOW_RELATIVE
      ? timeAgo(card.date_iso)
      : card.date_iso
      ? new Date(card.date_iso).toLocaleString()
      : "";

  // remove previous multi-link container
  let linksWrap = document.getElementById("modal-links-wrap");
  if (linksWrap) {
    linksWrap.remove();
    linksWrap = null;
  }

  // helper to clean labels
  function cleanLabelText(txt) {
    if (!txt) return "";
    return txt
      .replace(/\b(grab here|grab now|grab|link|loot:?|buy now|click here)\b/gi, "")
      .replace(/[:\-–—]+$/g, "")
      .trim();
  }

  function guessLabelFromUrl(url, cardObj) {
    const raw = String(cardObj.raw_text || cardObj.description || cardObj.title || "");
    const lines = raw.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);

    // helper: determine if a line is likely a merchant/header or otherwise noisy
    function isNoiseLine(s) {
      if (!s) return true;
      // strip emojis and arrows for the check
      const stripped = s.replace(/^[\p{Emoji}_\s👉\->•\*…—–:]+|[\p{Emoji}_\s👉\->•\*…—–:]+$/gu, "").trim();
      if (!stripped) return true;
      // common single-word merchants / headings to ignore
      if (/^(myntra|amazon|flipkart|ajio|snapdeal|flipkart\W*)$/i.test(stripped)) return true;
      // if the line is very short and contains no letters (or just a single word), treat as noise
      if (stripped.length < 3) return true;
      // if line is mostly punctuation / currency / link text, treat as noise
      if (/^[\W\d_]+$/.test(stripped)) return true;
      return false;
    }

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      if (!line) continue;
      if (line.indexOf(url) !== -1) {
        const idx = line.indexOf(url);
        const left = line
          .slice(0, idx)
          .replace(/^[👉\-\:\s]+/, "")
          .replace(/[:\-\u2014\u2013\.\s]+$/, "")
          .trim();

        if (left && !left.match(/^https?:\/\//i)) {
          const leftClean = cleanLabelText(left);
          // if leftClean is not just noise, use it
          if (leftClean && !isNoiseLine(leftClean)) return leftClean;
        }

        // look at previous non-empty line but skip it when it's likely a merchant/header/noise
        if (i > 0) {
          // scan backwards to find the nearest non-empty previous line
          for (let j = i - 1; j >= 0; j--) {
            const prev = lines[j].trim();
            if (!prev) continue;
            if (prev.match(/^https?:\/\//i)) break; // if previous is a link, don't use it
            const prevClean = cleanLabelText(prev);
            if (prevClean && !isNoiseLine(prevClean)) {
              return prevClean;
            } else {
              // if prev line is noisy, don't keep falling back to earlier lines blindly;
              // allow one more earlier line as a last attempt (helps when list has a header + empty line + product)
              if (j - 1 >= 0) {
                const prev2 = cleanLabelText(lines[j - 1] || "");
                if (prev2 && !isNoiseLine(prev2)) return prev2;
              }
            }
            break;
          }
        }

        const withoutUrl = line
          .replace(url, "")
          .replace(/^[👉\-\:\s]+/, "")
          .replace(/[:\-\u2014\u2013]+$/, "")
          .trim();
        if (withoutUrl) {
          const wClean = cleanLabelText(withoutUrl);
          if (wClean && !isNoiseLine(wClean)) return wClean;
        }

        try {
          const u = new URL(url);
          return u.hostname.replace("www.", "");
        } catch (e) {
          return url;
        }
      }
    }

    try {
      const u = new URL(url);
      return u.hostname.replace("www.", "");
    } catch (e) {
      return url;
    }
  }


  // if card.urls exists and has items, render multiple buy rows
    // if card.urls exists and has items, render multiple buy rows
  if (Array.isArray(card.urls) && card.urls.length > 0) {
    if (modalBuy) modalBuy.style.display = "none";

    // create linksWrap container
    linksWrap = document.createElement("div");
    linksWrap.id = "modal-links-wrap";
    linksWrap.style.display = "flex";
    linksWrap.style.flexDirection = "column";
    linksWrap.style.alignItems = "stretch";
    linksWrap.style.marginTop = "12px";
    const parentForBuy =
      modalBuy && modalBuy.parentNode
        ? modalBuy.parentNode
        : document.querySelector(".modal .right") || document.body;
    parentForBuy.appendChild(linksWrap);

    // ---------- NORMALIZE card.urls into {url,label} objects ----------
    const rawUrls = Array.isArray(card.urls) ? card.urls.slice() : [];
    const unified = rawUrls.map(it => {
      if (!it) return { url: "", label: "" };
      if (typeof it === "string") return { url: it.trim(), label: "" };
      // object expected: {url:..., label:...} or custom shape
      return { url: (it.url || "").toString().trim(), label: (it.label || "").toString().trim() };
    });

    // dedupe exact pairs (preserve first occurrence)
    const seenPairs = new Set();
    const deduped = [];
    for (const obj of unified) {
      const key = (obj.url || "") + "||" + (obj.label || "");
      if (seenPairs.has(key)) continue;
      seenPairs.add(key);
      deduped.push(obj);
    }

    // If a header-only row (no url) is immediately followed by a buy row with the same label,
    // clear the buy-row label to avoid "header then same label repeated".
    for (let i = 0; i < deduped.length - 1; i++) {
      const cur = deduped[i];
      const nxt = deduped[i + 1];
      if ((!cur.url || cur.url.trim() === "") && nxt && nxt.url) {
        const h = (cur.label || "").trim().toLowerCase();
        const l = (nxt.label || "").trim().toLowerCase();
        if (h && l && h === l) {
          nxt.label = "";
        }
      }
    }

    // Render rows from deduped list. Use a for-loop so we can continue cleanly.
    for (const itemObj of deduped) {
      if (!itemObj) continue;
      const url = (itemObj.url || "").trim();
      let labelText = (itemObj.label || "").trim();

      // If no label from Python, try to guess from message text
      if (!labelText && url) {
        try {
          labelText = guessLabelFromUrl(url, card) || "";
        } catch (e) {
          labelText = "";
        }
      }

      // If header-only row (no URL) -> render as label-only row
      if (!url) {
        // avoid adding an identical consecutive header-only row
        const last = linksWrap.lastElementChild;
        const lastText = last ? (last.textContent || "").trim().toLowerCase() : "";
        const curText = (labelText || "").trim().toLowerCase();
        if (curText && curText === lastText) {
          continue;
        }

        const row = document.createElement("div");
        row.className = "modal-link-row";
        row.style.display = "flex";
        row.style.alignItems = "center";
        row.style.gap = "12px";
        row.style.marginTop = "8px";
        row.style.width = "100%";
        row.style.flexWrap = "wrap";

        const labelOnly = document.createElement("div");
        labelOnly.style.flex = "1 1 auto";
        labelOnly.style.fontSize = "14px";
        labelOnly.style.fontWeight = "700";
        labelOnly.style.wordBreak = "break-word";
        labelOnly.style.whiteSpace = "normal";
        labelOnly.style.maxWidth = "100%";
        labelOnly.textContent = labelText || "";
        row.appendChild(labelOnly);

        linksWrap.appendChild(row);
        continue;
      }

      // Defensive label cleanups: drop domain-like labels, strip merchant prefix, sanitize text
      const domainLike = /^[a-z0-9-]+(\.[a-z0-9-]+)+$/i;
      if (labelText && domainLike.test(labelText)) labelText = "";

      if (labelText) {
        if (card.merchant_label) {
          const merchantEsc = card.merchant_label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
          const re = new RegExp("^\\s*" + merchantEsc + "\\s*[:\\-|\\|]+\\s*", "i");
          labelText = labelText.replace(re, "").trim();
        }
        labelText = labelText.replace(/^[A-Za-z0-9&\s]{2,40}\s*[:\-\|]+\s*/i, "").trim();
        labelText = labelText
          .replace(/\b(grab here|grab now|grab|link|loot:?|buy now|click here)\b/gi, "")
          .replace(/^[\|\-:]+/, "")
          .replace(/[:\-–—]+$/g, "")
          .replace(/\s*[\|\-:]+\s*/g, " ")
          .replace(/\s+/g, " ")
          .trim();
        labelText = labelText.replace(/\b(MasterLink|Master)\b/gi, "Visit merchant site");
      }

      // Create UI row: label + buy button
      const row = document.createElement("div");
      row.className = "modal-link-row";
      row.style.display = "flex";
      row.style.alignItems = "center";
      row.style.gap = "12px";
      row.style.marginTop = "8px";
      row.style.width = "100%";
      row.style.flexWrap = "wrap";

      const label = document.createElement("div");
      label.style.flex = "1 1 auto";
      label.style.fontSize = "14px";
      label.style.fontWeight = "600";
      label.style.wordBreak = "break-word";
      label.style.whiteSpace = "normal";
      label.style.maxWidth = "calc(100% - 120px)";
      label.textContent = labelText || "";
      row.appendChild(label);

      const a = document.createElement("a");
      a.className = "buy";
      a.href = url;
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = "Buy now";
      a.style.flex = "0 0 auto";
      a.style.marginLeft = "auto";
      a.style.padding = "8px 12px";
      a.style.fontSize = "13px";
      a.style.minWidth = "96px";
      row.appendChild(a);

      linksWrap.appendChild(row);
    }

  } else {
    if (modalBuy) {
      modalBuy.style.display = "";
      modalBuy.href = card.buy_link || card.shortlink || "#";
    }
  }


  // open the modal
  modalBack.style.display = "flex";
  modalBack.setAttribute("aria-hidden", "false");

  // === setup modal share button & currentModalCard ===
  currentModalCard = card;
  const modalShareBtn = document.getElementById("modal-share");
  if (modalShareBtn) {
    modalShareBtn.innerHTML = `
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"
           width="16" height="16" fill="none" stroke="currentColor"
           stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
        <path d="M4 12v7a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-7"></path>
        <polyline points="16 6 12 2 8 6"></polyline>
        <line x1="12" y1="2" x2="12" y2="15"></line>
      </svg>`;
    modalShareBtn.title = "Share this deal";
    modalShareBtn.style.display = ""; // ensure visible
  }
}

function closeModal() {
  modalBack.style.display = "none";
  modalBack.setAttribute("aria-hidden", "true");
  modalImage.src = "";
}

function escapeHtml(s) {
  if (!s) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// ----------------- Unified improved share helpers -----------------
/* ---------- Improved share helpers: share product image for cards/modal,
   and banner image for footer share (never expose affiliate links) ---------- */

function getCardUrl(card) {
  // Always share the site permalink that references the card id.
  // This avoids exposing affiliate/buy links.
  try {
    const base = window.location.origin + window.location.pathname;
    return base + (base.indexOf('?') === -1 ? '?' : '&') + 'id=' + encodeURIComponent(card.id || '');
  } catch (e) {
    return window.location.href;
  }
}

// Show small toast (reuse if already defined)
function showShareToastSafe(msg) {
  if (typeof showShareToast === "function") {
    showShareToast(msg);
    return;
  }
  const t = document.createElement("div");
  t.className = "share-toast show";
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.classList.remove("show"), 1800);
  setTimeout(() => t.remove(), 2400);
}

// Try to fetch an image URL and convert to a File or Blob usable by navigator.share
// Improved fetchImageAsFile: 1) fetch+blob 2) image->canvas fallback 3) final fetch fallback
async function fetchImageAsFile(imgUrl, filenameHint = "image") {
  try {
    if (!imgUrl) return null;
    // resolve relative URLs
    try { imgUrl = (new URL(imgUrl, window.location.href)).toString(); } catch(e){}

    console.debug("fetchImageAsFile: fetching", imgUrl);
    showShareToastSafe("Preparing image...");

    // 1) Try fetch with CORS mode
    try {
      const res = await fetch(imgUrl, { mode: 'cors' });
      if (res.ok) {
        const blob = await res.blob();
        if (blob && blob.type && blob.type.startsWith("image/")) {
          const ext = (blob.type.split('/')[1] || "jpg").split('+')[0];
          const fname = (filenameHint || "img") + "." + ext;
          try { return new File([blob], fname, { type: blob.type }); }
          catch (e) { blob.name = fname; return blob; }
        } else {
          console.debug("fetchImageAsFile: fetch returned non-image blob/type:", blob && blob.type);
        }
      } else {
        console.debug("fetchImageAsFile: fetch returned not ok:", res.status);
      }
    } catch (errFetch) {
      console.debug("fetchImageAsFile: fetch(err) (CORS or network):", errFetch);
    }

    // 2) Try image -> canvas route (may be tainted by CORS)
    try {
      const img = document.createElement('img');
      img.crossOrigin = "anonymous";
      const p = new Promise((resolve, reject) => {
        img.onload = () => resolve(true);
        img.onerror = (e) => reject(e);
      });
      img.src = imgUrl;
      await p;
      // draw into canvas
      const canvas = document.createElement('canvas');
      canvas.width = img.naturalWidth || img.width || 800;
      canvas.height = img.naturalHeight || img.height || 600;
      const ctx = canvas.getContext('2d');
      ctx.drawImage(img, 0, 0);
      const blob = await new Promise(resolve => {
        try {
          canvas.toBlob(b => resolve(b), 'image/jpeg', 0.92);
        } catch (e) {
          resolve(null);
        }
      });
      if (blob) {
        const fname = (filenameHint || "img") + ".jpg";
        try { return new File([blob], fname, { type: blob.type || 'image/jpeg' }); }
        catch (e) { blob.name = fname; return blob; }
      } else {
        console.debug("fetchImageAsFile: canvas.toBlob returned null (tainted?)");
      }
    } catch (errCanvas) {
      console.debug("fetchImageAsFile: image->canvas failed (likely CORS):", errCanvas);
    }

    // 3) Last-chance fetch (no CORS mode) — may be blocked
    try {
      const res2 = await fetch(imgUrl);
      if (res2.ok) {
        const blob2 = await res2.blob();
        if (blob2 && blob2.type && blob2.type.startsWith("image/")) {
          const ext = (blob2.type.split('/')[1] || "jpg").split('+')[0];
          const fname = (filenameHint || "img") + "." + ext;
          try { return new File([blob2], fname, { type: blob2.type }); }
          catch (e) { blob2.name = fname; return blob2; }
        }
      }
    } catch (errFallback) {
      console.debug("fetchImageAsFile: final fetch fallback failed:", errFallback);
    }

  } catch (err) {
    console.debug("fetchImageAsFile: unexpected error:", err);

  }
  // give user a short toast so they know image attach failed and link-only share will be used
  showShareToastSafe("Image unavailable — sharing link only");
  return null;
}

// Main share entry for cards/modal: attempts image+text share, falls back to text share or desktop popup
// ---------- NO-CLIPBOARD share handlers (paste to replace existing) ----------

const SHARE_PREFIX = "I just found this deal on BestPriceZone.in";

/* Helper: robust card lookup by encoded id, raw id, shortlink, buy_link, or substring */
function findCardById(encodedOrRaw) {
  try {
    const key = (encodedOrRaw || "").toString();
    let decoded = key;
    try { decoded = decodeURIComponent(key || ""); } catch (e) { /* ignore */ }

    // exact id match
    let c = cards.find(x => (x.id || "") === decoded || (x.id || "") === key);
    if (c) return c;

    // match against shortlink/buy_link
    c = cards.find(x => (x.shortlink || "").toString() === decoded || (x.buy_link || "").toString() === decoded || (x.shortlink || "").toString() === key || (x.buy_link || "").toString() === key);
    if (c) return c;

    // fallback: substring match in title or merchant
    const lower = decoded.toLowerCase();
    return cards.find(x => (((x.title||"") + " " + (x.merchant_label||"")).toLowerCase().includes(lower))) || null;
  } catch (e) {
    console.debug("findCardById error:", e);
    return null;
  }
}

async function tryOpenSocialIntent(permalink, title, imgSrc) {
  // Preferred order: WhatsApp -> Telegram -> Twitter -> Facebook
  // Text includes product image URL as well (so apps can preview it if they fetch it)
  const text = `${SHARE_PREFIX}\n${title}\n${permalink}\n${imgSrc || ''}`.trim();
  // WhatsApp mobile intent generally opens app on mobile
  const wa = `https://wa.me/?text=${encodeURIComponent(text)}`;
  const tg = `https://t.me/share/url?url=${encodeURIComponent(permalink)}&text=${encodeURIComponent(SHARE_PREFIX + "\n" + title)}`;
  const tw = `https://twitter.com/intent/tweet?url=${encodeURIComponent(permalink)}&text=${encodeURIComponent(SHARE_PREFIX + " " + title)}`;
  const fb = `https://www.facebook.com/sharer/sharer.php?u=${encodeURIComponent(permalink)}`;



  try {


    const isMobile = /Mobi|Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
    if (isMobile) {
      // open WhatsApp first (best mobile reach)
      window.open(wa, '_blank');
      return true;
    } else {
      // desktop: don't auto-open external intents; let popup handle it
      return false;
    }
  } catch (e) {
    return false;
  }
}

/* Unified share flow for card/modal that prioritizes product image + title.
   This preserves the footer/page share behavior while ensuring modal & card
   share attach the product image (when supported). */
async function shareCardById(encodedCardId, anchorEl) {
  try {
    const card = findCardById(encodedCardId);
    if (!card) {
      // no card found — fallback to showing page share popup
      openDesktopSharePopup({ id: '', title: document.title || '', merchant_label: '', local_image: (document.querySelector('.banner-wrap img')||{}).src || '' }, anchorEl || document.body, true);
      return;
    }

    const permalink = getCardUrl(card) || window.location.href;
    const title = (card.title || "Check this deal").trim();
    const text = `${SHARE_PREFIX}\n${title}\n${permalink}`;
    let imgSrc = card.local_image || '';

    // If no product image on card, try to fall back to banner (like page share)
    if (!imgSrc) {
      const bannerImg = document.querySelector('.banner-wrap img');
      if (bannerImg && bannerImg.src) imgSrc = bannerImg.src;
    }

    // 1) Try Web Share Level 2 (files) with product image attached
    try {
      if (navigator && navigator.share && navigator.canShare) {
        if (imgSrc) {
          const file = await fetchImageAsFile(imgSrc, "product");
          if (file && navigator.canShare({ files: [file] })) {
            await navigator.share({ title, text, files: [file], url: permalink });
            return; // DONE: native sheet with image
          }
        }
      }
    } catch (err) {
      console.warn("Web Share with files failed:", err);
      // fallthrough to next step
    }





    // 2) Try native share with text+url (no image attach)
    try {
      if (navigator && navigator.share) {
        await navigator.share({ title, text, url: permalink });
        return; // DONE: native sheet (may be without image)
      }
    } catch (err) {
      console.warn("Native text share failed/cancelled:", err);
      // fallthrough to social intents / popup (no clipboard)
    }

    // 3) If on mobile, directly open social intent (WhatsApp/Telegram) — avoids clipboard entirely.
    const openedIntent = await tryOpenSocialIntent(permalink, title, imgSrc);
    if (openedIntent) return;

    // 4) Desktop fallback: open desktop share popup (no clipboard). The popup has social links
    //    and also shows the product image/title/permalink so the user can click the social links.
    openDesktopSharePopup(card, anchorEl, /* preferImage */ true);

  } catch (err) {
    console.warn("shareCardById unexpected error:", err);
    // Final fallback: show page share popup with banner image
    try {
      const bannerSrc = (document.querySelector('.banner-wrap img')||{}).src || "";
      openDesktopSharePopup({ id: "", title: document.title || "BestPriceZone", merchant_label: "", local_image: bannerSrc }, anchorEl || document.body, true);
    } catch (e) {
      console.warn("Final share fallback failed:", e);
    }
  }








}

// openShareMenu wrapper used by card buttons
function openShareMenu(el, cardId) {
  shareCardById(cardId, el).catch((err) => {
    console.warn("shareCardById failed:", err);
    // As a final fallback we still show the desktop popup UI (explicit) so user can share manually.
    try {
      const card = findCardById(cardId);
      if (card) {
        openDesktopSharePopup(card, el, true);
      } else {
        // if findCardById fails (rare), open page share popup with banner
        openDesktopSharePopup({ id: '', title: document.title || '', merchant_label: '', local_image: (document.querySelector('.banner-wrap img')||{}).src || '' }, el, true);
      }
    } catch (e) {
      console.warn("Final fallback popup failed:", e);
    }
  });
}

// Modify openDesktopSharePopup slightly so social links include the full multiline text
// and openDesktopSharePopup can be used as the desktop/manual UI (no clipboard).
function openDesktopSharePopup(card, anchorEl, preferImage = true) {
  // remove existing first
  const existing = document.getElementById("share-popup");
  if (existing) existing.remove();

  const rect = (anchorEl && anchorEl.getBoundingClientRect) ? anchorEl.getBoundingClientRect() : { left: 20, bottom: 80 };
  let imgSrc = "";
  if (preferImage && card && card.local_image) imgSrc = card.local_image;
  if (!imgSrc) {
    const bannerImg = document.querySelector('.banner-wrap img');
    if (bannerImg && bannerImg.src) imgSrc = bannerImg.src;
  }

  const permalink = getCardUrl(card);
  const titleText = card.title || card.merchant_label || "BestPriceZone Deal";
  const fullText = `${SHARE_PREFIX}\n${titleText}\n${permalink}\n${imgSrc || ''}`.trim();

  const popup = document.createElement("div");
  popup.className = "share-popup";
  popup.id = "share-popup";
  popup.style.minWidth = "320px";
  popup.style.maxWidth = "420px";
  popup.style.top = Math.max(8, window.scrollY + (rect.bottom || 80) + 8) + "px";
  popup.style.left = Math.min(Math.max(8, rect.left || 8), Math.max(8, window.innerWidth - 440)) + "px";
  popup.style.zIndex = 12001;

  popup.innerHTML = `
    <div style="display:flex;flex-direction:column;gap:10px;font-family:Inter,system-ui,Arial;">
      <div style="display:flex;gap:12px;align-items:center">
        <div style="flex:0 0 72px; width:72px; height:72px; border-radius:8px; overflow:hidden; background:#f6f7fb; display:flex;align-items:center;justify-content:center;">
          ${imgSrc ? `<img src="${imgSrc}" alt="${escapeHtml(titleText)}" style="width:100%;height:100%;object-fit:cover;display:block" />` : `<div style="width:100%;height:100%;display:flex;align-items:center;justify-content:center;color:#94a3b8">No image</div>`}
        </div>
        <div style="flex:1;min-width:0;">
          <div style="font-weight:700;font-size:15px;line-height:1.2;color:#0f1724;word-break:break-word;">${escapeHtml(titleText)}</div>
          <div style="font-size:13px;color:#64748b;margin-top:6px;">${escapeHtml(card.merchant_label || "")}</div>
        </div>
      </div>

      <div style="display:flex;flex-direction:column;gap:8px">
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <div class="url" style="flex:1;min-width:0;font-size:13px;color:#475569;word-break:break-all;">${escapeHtml(permalink)}</div>
          <button class="share-btn-inline" id="share-copy-btn" style="flex:0 0 auto;padding:8px 12px;border-radius:8px;border:0;background:#0f62fe;color:#fff;font-weight:700;cursor:pointer">Open share options</button>
        </div>

        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <a class="social-links" id="popup-wa" href="https://wa.me/?text=${encodeURIComponent(fullText)}" target="_blank" rel="noopener">WhatsApp</a>
          <a class="social-links" id="popup-tg" href="https://t.me/share/url?url=${encodeURIComponent(permalink)}&text=${encodeURIComponent(SHARE_PREFIX + "\\n" + titleText)}" target="_blank" rel="noopener">Telegram</a>
          <a class="social-links" id="popup-fb" href="https://www.facebook.com/sharer/sharer.php?u=${encodeURIComponent(permalink)}" target="_blank" rel="noopener">Facebook</a>
          <a class="social-links" id="popup-tw" href="https://twitter.com/intent/tweet?url=${encodeURIComponent(permalink)}&text=${encodeURIComponent(SHARE_PREFIX + " " + titleText)}" target="_blank" rel="noopener">Twitter</a>
        </div>
      </div>
    </div>
  `;

  document.body.appendChild(popup);

  // When user clicks "Open share options" we try to open WhatsApp/Telegram directly (mobile-first)
  const copyBtn = popup.querySelector("#share-copy-btn");
  if (copyBtn) {
    copyBtn.addEventListener("click", async () => {
      // Try to open mobile intent first
      const isMobile = /Mobi|Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
      if (isMobile) {
        // open whatsapp intent
        window.open(`https://wa.me/?text=${encodeURIComponent(fullText)}`, '_blank');
      } else {
        // on desktop, just visually show the social links (user clicks)
        // optionally focus first link
        const waLink = popup.querySelector("#popup-wa");
        if (waLink) waLink.focus();
      }
    });
  }

  // close popup when clicking outside / on scroll / resize
  const closeFn = () => {
    const el = document.getElementById("share-popup");
    if (el) el.remove();
    window.removeEventListener("scroll", closeFn, true);
    window.removeEventListener("resize", closeFn);
  };
  setTimeout(() => {
    window.addEventListener("scroll", closeFn, true);
    window.addEventListener("resize", closeFn);
    document.addEventListener("click", closeFn, { once: true, capture: true });
  }, 10);

  popup.addEventListener("click", (ev) => ev.stopPropagation());
}



// Wire modal share button
(function wireModalShare() {
  const modalShareBtn = document.getElementById("modal-share");
  if (modalShareBtn) {
    modalShareBtn.addEventListener("click", () => {
      if (typeof currentModalCard !== "undefined" && currentModalCard) {
        const encoded = encodeURIComponent(currentModalCard.id || currentModalCard.shortlink || "");
        shareCardById(encoded, modalShareBtn);
      }
    });
  }
})();

// Footer "Share page" button - NO CLIPBOARD fallback
(function wireFooterShare() {
  const shareBtn = document.getElementById('share-page');
  if (!shareBtn) return;

  shareBtn.addEventListener('click', async function() {
    const url = window.location.href;
    const title = document.title || 'BestPriceZone — check this out';
    const SHARE_PREFIX = "I just found this deal on BestPriceZone.in";
    const text = `${SHARE_PREFIX}\n${title}\n${url}`;

    const bannerImgEl = document.querySelector('.banner-wrap img');
    let bannerSrc = bannerImgEl ? (bannerImgEl.src || '') : '';

    // Try Web Share Level 2 with banner image (mobile Android)
    if (navigator && navigator.share && navigator.canShare && bannerSrc) {
      const file = await fetchImageAsFile(bannerSrc, "banner");
      try {
        if (file && navigator.canShare({ files: [file] })) {
          await navigator.share({ title, text, files: [file], url });
          return;
        }
      } catch (e) {
        console.warn("Footer share with files failed:", e);
      }
    }

    // Try native share with text+url (no image)
    if (navigator && navigator.share) {
      try {
        await navigator.share({ title, text, url });
        return;
      } catch (e) {
        console.warn("Footer native share failed:", e);
      }
    }

    // Desktop: show the desktop share popup (social links + preview) — NO clipboard
    const dummyCard = { id: "", title, merchant_label: "", local_image: bannerSrc };
    openDesktopSharePopup(dummyCard, shareBtn, true);
  });
})();


// Keep the global openShareMenu
window.openShareMenu = function(el, cardId) {
  try {
    openShareMenu(el, cardId);
  } catch (e) {
    try {
      const permalink = getCardUrl({ id: decodeURIComponent(cardId) });
      navigator.clipboard && navigator.clipboard.writeText(permalink);
      showShareToastSafe("Link copied!");
    } catch (x) {
      showShareToastSafe("Link copied!");
    }
  }
};

window.shareCardById = shareCardById;


  // initial render
  render();

  // insert footer source link content  
</script>
</body>
</html>
"""

    # replace placeholders and return
    html = html_template
    html = html.replace("__CARDS_JSON__", cards_json_safe)
    html = html.replace("__BANNER_HTML__", banner_html)
    html = html.replace("__HERO_HEIGHT__", hero_height)
    html = html.replace("__GEN_TS__", gen_ts)

    # set OG/Twitter image to banner if present, else empty to avoid leaking placeholder
    og_img = banner_rel or ""
    html = html.replace("__BANNER_IMAGE__", og_img)

    # inject SHOW_RELATIVE boolean literal into the HTML for client-side JS
    html = html.replace("__SHOW_RELATIVE__", "true" if show_relative else "false")
    return html

# ---------- images helper ----------
def remove_images_folder():
    for fn in os.listdir(IMGS_DIR):
        fp = os.path.join(IMGS_DIR, fn)
        try:
            if os.path.isfile(fp):
                os.remove(fp)
        except:
            pass

def remove_image_file(path: str):
    """Remove a single image file if it's inside IMGS_DIR (safety check)."""
    try:
        if not path:
            return False
        # normalize path
        abs_path = os.path.abspath(path)
        abs_imgs = os.path.abspath(IMGS_DIR)
        # only delete files inside IMGS_DIR
        if not abs_path.startswith(abs_imgs):
            return False
        if os.path.exists(abs_path) and os.path.isfile(abs_path):
            os.remove(abs_path)
            logging.info("Removed image file: %s", abs_path)
            return True
    except Exception as e:
        logging.debug("Failed to remove image %s : %s", path, e)
    return False

# ---------- main ----------
def main():
    if not (TG_API_ID and TG_API_HASH and TG_STRING_SESSION and CHANNEL):
        logging.error("Set TG_API_ID, TG_API_HASH, TG_STRING_SESSION, CHANNEL_USERNAME in env")
        return

    if CLEAN_IMAGES_ON_RUN:
        logging.info("CLEAN_IMAGES_ON_RUN=1 — removing existing images in %s", IMGS_DIR)
        remove_images_folder()

    init_db()
    if CLEAN_DB_ON_RUN:
        logging.info("CLEAN_DB_ON_RUN=1 — clearing processed DB")
        clear_db()

    last_run_time = get_last_run_time()
    logging.info("Last run time (from DB) = %s", last_run_time.isoformat() if last_run_time else "never")

    # Load existing cards snapshot (so we don't rebuild everything every run)
    existing_cards = []
    try:
        if os.path.exists(CARDS_JSON_PATH):
            with open(CARDS_JSON_PATH, "r", encoding="utf8") as fh:
                existing_cards = json.load(fh)
            logging.info("Loaded %d existing cards from %s", len(existing_cards), CARDS_JSON_PATH)
    except Exception as e:
        logging.debug("Failed loading existing cards snapshot: %s", e)
        existing_cards = []

    # populate seen_shortlinks and seen_titles from existing snapshot
    cards = list(existing_cards)  # will append new items at front
    seen_shortlinks: Set[str] = set()
    seen_titles: Set[str] = set()
    for ec in existing_cards:
        sl = ec.get("shortlink") or ec.get("buy_link") or ""
        if sl:
            seen_shortlinks.add(sl)
        t = (ec.get("title") or "")
        t_norm = re.sub(r'\W+', ' ', t).strip().lower()
        if t_norm:
            seen_titles.add(t_norm)

    client = TelegramClient(StringSession(TG_STRING_SESSION), TG_API_ID, TG_API_HASH)
    client.start()
    logging.info("Connected to Telegram. Fetching up to %d messages (cap %d) from %s", TARGET_CARDS, MAX_SCAN_MESSAGES, CHANNEL)

    try:
        try:
            entity = client.get_entity(CHANNEL)
        except Exception:
            entity = CHANNEL

        # Collect a batch of messages, then sort them newest -> older explicitly
        msgs = list(client.iter_messages(entity, limit=MAX_SCAN_MESSAGES))
        msgs = [m for m in msgs if m is not None]
        msgs.sort(key=lambda m: getattr(m, "date", datetime.min.replace(tzinfo=timezone.utc)), reverse=True)

        scanned = 0
        new_added = 0

        for msg in msgs:
            scanned += 1
            # stop if we've reached target of NEW cards (note: we keep existing cards)
            if new_added >= TARGET_CARDS:
                break
            try:
                msg_id = getattr(msg, 'id', None)
                msg_id_str = str(msg_id or scanned)
                raw = (msg.message or "").strip()
                if not raw:
                    continue

                msg_date = getattr(msg, "date", None)
                if msg_date and msg_date.tzinfo is None:
                    msg_date = msg_date.replace(tzinfo=timezone.utc)

                urls = URL_RE.findall(raw)
                if not urls:
                    continue
                shortlink = urls[0].strip()
                if shortlink in seen_shortlinks:
                    # if we already have this shortlink from snapshot, ensure it's marked processed (best-effort)
                    mark_processed(msg_id_str, shortlink)
                    logging.info("Reached already processed deals. Stopping scan.")
                    break

                treat_as_new = False
                if last_run_time and msg_date:
                    treat_as_new = (msg_date > last_run_time)
                elif not last_run_time:
                    treat_as_new = True

                if not treat_as_new:
                    if was_processed(msg_id_str):
                        continue

                local_img = download_telegram_media_if_present(client, msg)
                if not local_img:
                    mark_processed(msg_id_str, shortlink)
                    continue

                title = clean_message_text(raw, shortlink)[:140]
                merchant_label = normalize_merchant(title, shortlink) or ""
                #if merchant_label and not title.lower().startswith(merchant_label.lower()):
                #    title = f"{merchant_label} | {title}"

                title_norm = re.sub(r'\W+', ' ', title).strip().lower()
                if title_norm in seen_titles and not treat_as_new:
                    mark_processed(msg_id_str, shortlink)
                    continue

                # Build description from raw message but strip URLs and collapse repeated lines
                description = raw
                # remove inline URLs (we keep labeled urls separately)
                description = URL_RE.sub('', description).strip()
                # collapse multiple whitespace to single spaces
                description = re.sub(r'\s+', ' ', description).strip()
                # remove the chosen title from the description (case-insensitive)
                try:
                    description = re.sub(re.escape(title), '', description, flags=re.I).strip()
                except Exception:
                    pass

                # collapse repeated identical lines from the raw message (helps with duplicate lines)
                lines = [ln.strip() for ln in re.split(r'[\r\n]+', raw) if ln and ln.strip()]
                seen_lines = set()
                dedup_lines = []
                for ln in lines:
                    key = ln.strip().lower()
                    if key in seen_lines:
                        continue
                    seen_lines.add(key)
                    dedup_lines.append(ln.strip())
                if dedup_lines:
                    description_from_lines = " ".join(dedup_lines)
                    # prefer the shorter/cleaner of the two or use deduped if original was small
                    if len(description_from_lines) < len(description) or len(description) < 80:
                        description = description_from_lines

                # final trim to sensible length
                if len(description) > 220:
                    description = description[:220].rsplit(' ',1)[0] + '…'


                final = shortlink
                try:
                    net = urlparse(shortlink).netloc.lower()
                    if any(x in net for x in ("earnkaro","ek.link","fktr","fkrt","fktr.in")):
                        try:
                            r = REQ.head(shortlink, allow_redirects=True, timeout=6)
                            if r.ok and r.url:
                                final = r.url
                        except Exception:
                            pass
                except Exception:
                    pass

                buy_link = shortlink

                # raw extraction (preserve reading order & heuristics)
                _raw_urls = extract_url_labels(raw, urls)

                # Final pass: clean trivial labels and dedupe predictable duplicates.
                # - remove pure "Buy now" / "Shop full collection here" noise
                # - collapse duplicate header-only rows
                # - collapse duplicate URLs (prefer first occurrence, fill empty labels)
                cleaned_urls = []
                seen_url = set()
                seen_header = set()

                for it in _raw_urls:
                    u = (it.get("url") or "").strip()
                    lbl = (it.get("label") or "").strip()

                    # Normalize trivial labels (remove pure "Buy now" etc.)
                    lbl = re.sub(r'\b(Buy now|Buy|buy now|Click here|Shop full collection here)\b', '', lbl, flags=re.I).strip()
                    lbl = re.sub(r'\s+', ' ', lbl).strip()

                    if not u:
                        # header-only row
                        key = lbl.lower()
                        if not lbl:
                            continue
                        if key in seen_header:
                            continue
                        # if some url row already used this label, skip header-only repeat
                        if any((x.get("url") and (x.get("label") or "").strip().lower() == key) for x in _raw_urls):
                            seen_header.add(key)
                            continue
                        seen_header.add(key)
                        cleaned_urls.append({"url": "", "label": lbl})
                        continue

                    # url row: collapse duplicates by URL, prefer first non-empty label
                    if u in seen_url:
                        for ex in cleaned_urls:
                            if ex.get("url") == u:
                                if not ex.get("label") and lbl:
                                    ex["label"] = lbl
                                break
                        continue

                    seen_url.add(u)
                    cleaned_urls.append({"url": u, "label": lbl})

                    # ---------- Improved: preserve possessive labels (Men's) and ignore coupon lines ----------
                    COUPON_HDR_RE = re.compile(r'\b(apply\s*code|code|use\s*code|coupon|apply coupon|offer code|use coupon)\b', flags=re.I)

                    def _normalize_label_token(tok: str) -> str:
                        """Normalize a candidate token: preserve ASCII and curly apostrophes, ampersand;
                          strip surrounding junk, return normalized string or ''."""
                        if not tok:
                            return ""
                        tok = tok.strip()
                        tok = tok.replace("’", "'")   # normalize curly apostrophe to ASCII
                        # strip only surrounding characters that are not letters, digits, apostrophe or ampersand
                        tok = re.sub(r'^[^A-Za-z0-9\'&]+|[^A-Za-z0-9\'&]+$', '', tok).strip()
                        return tok

                    def infer_left_short_label_v2(url, raw_text):
                        """Return a short label (like "Men's", "Women", "Kids & Co") found immediately left of URL.
                          Avoids coupon/header lines and rejects very-short garbage tokens.
                        """
                        try:
                            lines = [ln for ln in re.split(r'[\r\n]+', raw_text)]
                            # same-line search first
                            for ln in lines:
                                if url in ln:
                                    # try "Label : url" or "Label - url"
                                    m = re.search(r'(.{1,60}?)\s*[:\-\|]\s*' + re.escape(url), ln, flags=re.I)
                                    if m:
                                        cand = m.group(1).strip()
                                        # take last token-ish candidate (but allow apostrophes & ampersand)
                                        tok = cand.split()[-1] if cand.split() else cand
                                        tok = _normalize_label_token(tok)

                                        # reject coupon-like tokens or pure codes
                                        if not tok or COUPON_HDR_RE.search(tok):
                                            pass
                                        else:
                                            # require length >= 2 and at least one letter (so "s" or pure codes are rejected)
                                            if len(tok) >= 2 and re.search(r'[A-Za-z\u00C0-\u024F]', tok):
                                                return tok

                                    # fallback: token immediately left of url on same line
                                    left = ln.split(url, 1)[0].strip()
                                    left = re.sub(r'^[👉\-\:\s]+', '', left)
                                    left = re.sub(r'[:\-\u2014\u2013\.\s]+$', '', left).strip()
                                    if left:
                                        tok = left.split()[-1]
                                        tok = _normalize_label_token(tok)
                                        if tok and not COUPON_HDR_RE.search(tok) and len(tok) >= 2 and re.search(r'[A-Za-z\u00C0-\u024F]', tok):
                                            return tok

                            # not on same line: look 1-2 lines above
                            for i, ln in enumerate(lines):
                                if url in ln:
                                    for j in (i-1, i-2):
                                        if j >= 0:
                                            candidate = lines[j].strip()
                                            candidate = re.sub(r'^[👉\-\:\s]+', '', candidate)
                                            candidate = re.sub(r'[:\-\u2014\u2013\.\s]+$', '', candidate).strip()
                                            if candidate and len(candidate) <= 40 and not COUPON_HDR_RE.search(candidate):
                                                tokens = candidate.split()
                                                if tokens and len(tokens) <= 4:
                                                    tok = tokens[-1]
                                                    tok = _normalize_label_token(tok)
                                                    if tok and len(tok) >= 2 and re.search(r'[A-Za-z\u00C0-\u024F]', tok):
                                                        return tok
                        except Exception:
                            pass
                        return ""


                card = {
                  "id": f"msg{msg_id_str}",
                  "title": title,
                  "description": description,
                  "shortlink": shortlink,
                  "urls": cleaned_urls,
                  "raw_text": raw,        # <-- keep raw for client-side heuristics
                  "final_url": final,
                  "local_image": local_img.replace("\\", "/"),
                  "source_host": urlparse(final).netloc if final else urlparse(shortlink).netloc,
                  "merchant_label": merchant_label,
                  "date_obj": msg_date,
                  "buy_link": buy_link
                }



                # insert at start (newest first)
                cards.insert(0, card)
                seen_shortlinks.add(shortlink)
                seen_titles.add(title_norm)

                mark_processed(msg_id_str, shortlink)
                new_added += 1
                logging.info("Collected new %d: %s", new_added, title)
                time.sleep(0.08 + random.random()*0.18)

            except Exception as e:
                logging.debug("Error processing message: %s", e)
                continue

        # --- ADD THIS LINE HERE TO FIX THE ORDERING ---
        cards.sort(key=lambda x: int(x.get('id', 0)) if str(x.get('id', '')).isdigit() else 0, reverse=True)

        # Trim cards to MAX_KEEP if needed and remove corresponding images
        try:
            if len(cards) > MAX_KEEP:
                # After sorting, the ones AFTER MAX_KEEP are guaranteed to be the oldest
                to_remove = cards[MAX_KEEP:]
                logging.info("Trimming %d old cards to respect MAX_KEEP=%d", len(to_remove), MAX_KEEP)
                for rem in to_remove:
                    # try to remove associated image
                    img = rem.get("local_image") or ""
                    if img:
                        # local_image may be relative (assets/...), convert to absolute path
                        # ensure we only delete files inside IMGS_DIR
                        candidate = os.path.abspath(img)
                        # if image path seems relative (no drive), join with cwd
                        if not os.path.isabs(img):
                            candidate = os.path.abspath(os.path.join(os.getcwd(), img))
                        # safety: ensure candidate inside IMGS_DIR
                        abs_imgs_dir = os.path.abspath(IMGS_DIR)
                        if candidate.startswith(abs_imgs_dir):
                            try:
                                if os.path.exists(candidate) and os.path.isfile(candidate):
                                    os.remove(candidate)
                                    logging.info("Removed trimmed card image: %s", candidate)
                            except Exception as e:
                                logging.debug("Failed to remove trimmed image %s : %s", candidate, e)
                # now trim cards list
                cards = cards[:MAX_KEEP]
        except Exception as e:
            logging.debug("Error trimming cards/images: %s", e)

        # --- NEW SORTING AND TRIMMING LOGIC ---
        if not cards:
            logging.info("No cards built.")
        else:
            # 1. Sort the cards by Telegram ID (highest/newest first)
            # This ensures the most recent deals appear at the top of your site
            cards.sort(key=lambda x: int(x.get('id', 0)) if str(x.get('id', '')).isdigit() else 0, reverse=True)

            # 2. Trim the list to your MAX_KEEP limit (default is 500 in your script)
            cards = cards[:MAX_KEEP]

            # 3. Build the index with the correctly sorted list
            html = build_index(cards, show_relative=SHOW_RELATIVE, banner_rel=BANNER_TARGET_REL, hero_height=HERO_HEIGHT)
            
            with open(OUT_FILE, "w", encoding="utf8") as f:
                f.write(html)
            
            logging.info("Wrote %s with %d cards (Sorted Newest-First). Scanned %d messages. New added: %d", 
                         OUT_FILE, len(cards), scanned, new_added)

    finally:
        try:
            client.disconnect()
        except:
            pass

if __name__ == "__main__":
    main()









