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
  MAX_KEEP (default 200) -> keep exactly this many cards/images after each run
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
MAX_SCAN_MESSAGES = int(os.environ.get("MAX_SCAN_MESSAGES", "1000"))

# NEW: how many cards to keep on every run (default 200)
MAX_KEEP = int(os.environ.get("MAX_KEEP", "200"))

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
        chosen = re.sub(r'(?i)\b' + re.escape(merchant) + r'\b[:\s\-]*', '', chosen).strip()
        final = f"{merchant} | {chosen}".strip()
    else:
        final = chosen
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
    html_template = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>BestPriceZone — Best Online Deals & Discounts</title>
<meta name="title" content="BestPriceZone — Best Online Deals & Discounts" />
<meta name="description" content="BestPriceZone curates trending shopping offers and discounts from Flipkart, Amazon, Myntra, Ajio and more. Updated frequently." />

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
  --hero-height:__HERO_HEIGHT__;
}
*{box-sizing:border-box}
body{font-family:Inter, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial; margin:0; background:var(--bg); color:var(--accent)}
.header{background:linear-gradient(90deg,#fff 0%, #f9fbff 100%);padding:18px 24px;border-bottom:1px solid rgba(0,0,0,0.04);position:sticky;top:0;z-index:40}
.header .wrap{max-width:1200px;margin:0 auto;display:flex;gap:16px;align-items:center}
.brand{font-weight:800;font-size:20px;letter-spacing:-0.4px}
.controls{margin-left:auto;display:flex;gap:10px;align-items:center}
.search{display:flex;align-items:center;background:#fff;padding:8px;border-radius:10px;box-shadow:0 6px 18px rgba(16,24,40,0.06)}
.search input{border:0;outline:0;font-size:14px;padding:6px 8px;width:260px}

/* banner (optional) */
/* keep banner centered and constrained to the same max-width and side padding
   as the main container / filter bar. This prevents the banner from spanning
   full browser width while preserving object-fit behavior. */
.banner-wrap{width:100%;display:flex;justify-content:center;padding:0 12px;margin:12px 0}
.banner-wrap img{max-width:1200px;width:100%;height:var(--hero-height);object-fit:cover;display:block;border-radius:12px;border-bottom:6px solid rgba(255,255,255,0.04)}

/* reduce banner height on small screens */
@media (max-width:900px){
  :root{--hero-height: calc(var(--hero-height) * 0.7);}
}
@media (max-width:480px){
  :root{--hero-height: calc(var(--hero-height) * 0.55);}
}

.hero{max-width:1200px;margin:18px auto 4px;padding:18px;border-radius:12px;background:linear-gradient(90deg,#ffffff,#f7fbff);display:flex;align-items:center;gap:16px}
.hero .h{font-size:20px;font-weight:800}
.filter-bar{max-width:1200px;margin:12px auto;display:flex;gap:12px;align-items:center;padding:0 12px}
.filter-bar select, .filter-bar .chips{padding:8px;border-radius:10px;border:1px solid rgba(16,24,40,0.06);background:#fff}
.chips{display:flex;gap:8px;flex-wrap:wrap;padding:8px}
.chips button{background:transparent;border:1px solid transparent;padding:6px 10px;border-radius:999px;cursor:pointer}
.chips button.active{background:var(--pill);color:var(--primary);border-color:rgba(15,98,254,0.08)}
.container{max-width:1200px;margin:0 auto;padding:8px 12px 80px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:18px}
.card{background:var(--card);border-radius:12px;padding:12px;box-shadow:0 8px 20px rgba(16,24,40,0.06);display:flex;flex-direction:column;transition:transform .18s, box-shadow .18s;min-height:320px}
.card:hover{transform:translateY(-6px);box-shadow:0 18px 30px rgba(16,24,40,0.08)}
.thumb{height:180px;border-radius:10px;overflow:hidden;background:linear-gradient(180deg,#fafafa,#fff);display:flex;align-items:center;justify-content:center}
.thumb img{max-width:100%;max-height:100%;object-fit:contain;display:block}
.meta{display:flex;align-items:center;justify-content:space-between;margin-top:10px}
.title{font-weight:700;font-size:14px;margin:8px 0;min-height:40px;line-height:1.15}
.desc{font-size:13px;color:var(--muted);min-height:40px;margin-bottom:8px}
.badge{display:inline-block;background:var(--pill);color:var(--primary);padding:6px 10px;border-radius:999px;font-weight:700;font-size:12px}
.price{background:linear-gradient(90deg,#fff,#fef6e7);padding:8px 10px;border-radius:10px;font-weight:800}
.actions{margin-top:auto;display:flex;gap:8px;align-items:center}
.buy{background:linear-gradient(90deg,#111827,#0b6bff);color:#fff;padding:10px 12px;border-radius:10px;text-decoration:none;font-weight:700}
.small{font-size:12px;color:var(--muted)}
.modal-backdrop{position:fixed;inset:0;background:rgba(2,6,23,0.6);display:none;align-items:center;justify-content:center;z-index:80}
.modal{background:#fff;border-radius:12px;max-width:920px;width:calc(100% - 48px);max-height:86vh;overflow:auto;padding:18px;display:flex;gap:18px}
.modal .left{flex:1}
.modal .right{width:360px;display:flex;flex-direction:column}
.close-btn{background:transparent;border:0;font-size:18px;cursor:pointer;color:#666;float:right}
.empty{text-align:center;color:var(--muted);padding:48px}
.footer{background:#fff;border-top:1px solid rgba(0,0,0,0.04);padding:36px 12px;color:var(--muted);margin-top:40px}
.footer .wrap{max-width:1200px;margin:0 auto;display:flex;gap:24px;align-items:flex-start;flex-wrap:wrap}
.footer .col{flex:1;min-width:180px}
.footer h4{margin:0 0 8px 0;font-size:14px}
.footer p, .footer a{color:var(--muted);text-decoration:none;font-size:13px}
.footer .bottom{max-width:1200px;margin:24px auto 0;text-align:center;font-size:13px;color:var(--muted)}

/* pager styling (improved buttons) */
.pagination{display:flex;gap:8px;justify-content:center;margin:22px 0}
.pagination button{background:#fff;border:1px solid rgba(16,24,40,0.08);padding:8px 12px;border-radius:10px;cursor:pointer;font-weight:600;box-shadow:0 6px 12px rgba(16,24,40,0.03)}
.pagination button:hover{transform:translateY(-2px)}
.pagination button.active{background:var(--primary);color:#fff;border-color:transparent;box-shadow:0 10px 20px rgba(15,98,254,0.14)}

/* smaller screen tweaks */
@media (max-width:720px){ .search input{width:140px} .modal{flex-direction:column} .modal .right{width:100%} .thumb{height:160px} .grid{grid-template-columns:repeat(auto-fill,minmax(160px,1fr));} }
</style>
</head>
<body>
  <header class="header">
    <div class="wrap">
      <div class="brand">BestPriceZone <span style="font-weight:400;color:var(--muted);font-size:13px">Best online deals & discounts</span></div>
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
      <div class="h">Latest curated deals — handpicked for shoppers</div>
      <div style="color:var(--muted);margin-top:6px;font-size:13px">Filter by merchant, sort by newest or price, click a product to view details and buy.</div>
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
  </div>
  <div class="bottom">© 2025 BestPriceZone. All rights reserved.</div>
</footer>

  <!-- modal -->
  <div id="modal-back" class="modal-backdrop" aria-hidden="true">
    <div class="modal" role="dialog" aria-modal="true">
      <div class="left">
        <button class="close-btn" id="modal-close" aria-label="Close">✕</button>
        <div style="border-radius:10px;overflow:hidden;margin-top:6px" id="modal-image-wrap">
          <img id="modal-image" src="" style="width:100%;height:420px;object-fit:contain;background:#fff" />
        </div>
      </div>
      <div class="right">
        <div style="display:flex;flex-direction:column;gap:10px">
          <div style="font-weight:800;font-size:18px" id="modal-title"></div>
          <div style="display:flex;align-items:center;gap:10px">
            <div id="modal-merchant" style="font-weight:700"></div>
            <div id="modal-time" style="margin-left:auto" class="small"></div>
          </div>
          <div style="font-size:14px;color:var(--muted)" id="modal-desc"></div>
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

  let cards = (window.CARDS || []).map(c => {
    return {
      id: c.id || (c.shortlink||'') + Math.random().toString(36).slice(2,8),
      title: c.title || '',
      description: c.description || '',
      merchant_label: c.merchant_label || (c.source_host || ''),
      local_image: c.local_image || '',
      date_iso: c.date_iso || c.date_obj || '',
      buy_link: c.buy_link || c.shortlink || '',
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
        <div class="thumb"><img loading="lazy" src="${c.local_image}" alt="${escapeHtml(c.title)}" /></div>
        <div class="meta">
          <div>
            <div class="badge">${escapeHtml(c.merchant_label||'')}</div>
            <div class="small" style="margin-top:6px">${escapeHtml(timeStr)}</div>
          </div>
          <div class="price">${c.price_raw ? escapeHtml(c.price_raw) : ''}</div>
        </div>
        <div class="title">${escapeHtml(c.title)}</div>
        <div class="desc">${escapeHtml(c.description)}</div>
        <div class="actions">
          <a class="buy view-btn" href="javascript:void(0)" data-id="${c.id}">View</a>
          <a class="buy" href="${c.buy_link}" target="_blank" rel="noopener">Buy Now</a>
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
    modalImage.src = card.local_image;
    modalTitle.textContent = card.title;
    modalDesc.textContent = card.description;
    modalMerchant.textContent = card.merchant_label;
    modalTime.textContent = timeAgo(card.date_iso);
    modalBuy.href = card.buy_link || '#';
    modalBack.style.display = 'flex';
    modalBack.setAttribute('aria-hidden','false');
  }
  function closeModal() {
    modalBack.style.display = 'none';
    modalBack.setAttribute('aria-hidden','true');
    modalImage.src = '';
  }

  function escapeHtml(s) {
    if(!s) return '';
    return String(s)
      .replace(/&/g,'&amp;')
      .replace(/</g,'&lt;')
      .replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;')
      .replace(/'/g,'&#39;');
  }

  // initial render
  render();

  // insert footer source link content  
</script>
</body>
</html>
"""

    # replace placeholders and return
    html = html_template.replace("__CARDS_JSON__", cards_json)
    html = html.replace("__BANNER_HTML__", banner_html)
    html = html.replace("__HERO_HEIGHT__", hero_height)
    html = html.replace("__GEN_TS__", gen_ts)
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
                    continue

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
                if merchant_label and not title.lower().startswith(merchant_label.lower()):
                    title = f"{merchant_label} | {title}"

                title_norm = re.sub(r'\W+', ' ', title).strip().lower()
                if title_norm in seen_titles and not treat_as_new:
                    mark_processed(msg_id_str, shortlink)
                    continue

                description = re.sub(r'\s+', ' ', raw).strip()
                description = URL_RE.sub('', description).strip()
                description = re.sub(re.escape(title), '', description, flags=re.I).strip()
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

                card = {
                    "id": f"msg{msg_id_str}",
                    "title": title,
                    "description": description,
                    "shortlink": shortlink,
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

        # Trim cards to MAX_KEEP if needed and remove corresponding images
        try:
            if len(cards) > MAX_KEEP:
                # Items to remove are the oldest ones: after position MAX_KEEP-1
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

        if not cards:
            logging.info("No cards built.")
        else:
            html = build_index(cards, show_relative=SHOW_RELATIVE, banner_rel=BANNER_TARGET_REL, hero_height=HERO_HEIGHT)
            with open(OUT_FILE, "w", encoding="utf8") as f:
                f.write(html)
            # also ensure the cards.json written by build_index contains only the trimmed cards
            logging.info("Wrote %s with %d cards (images in %s). Scanned %d messages. New added: %d", OUT_FILE, len(cards), IMGS_DIR, scanned, new_added)

    finally:
        try:
            client.disconnect()
        except:
            pass

if __name__ == "__main__":
    main()
