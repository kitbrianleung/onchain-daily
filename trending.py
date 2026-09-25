#!/usr/bin/env python3
"""
Trending x Smart Money — daily Instagram story + post.

Pipeline:
  Dexscreener latest boosted tokens (top 100)
    -> top 10 by 24h volume, MCAP < $2M
    -> GMGN smart-money count per token (Playwright headless browser)
    -> Pillow-rendered table (1080x1920 story + 1080x1350 post)
    -> pushed to repo, published to Instagram

Cron-friendly two-step CLI:
  python trending.py generate   # fetch -> filter -> render -> push images + state
  python trending.py publish    # publish story + post to Instagram
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent
STATE_FILE = REPO_ROOT / "state" / "trending.json"
IMAGES_DIR = REPO_ROOT / "images" / "trending"

GRAPH = "https://graph.facebook.com/v24.0"
DEXSCREENER_BOOSTS = "https://api.dexscreener.com/token-boosts/latest/v1"
DEXSCREENER_PAIRS = "https://api.dexscreener.com/latest/dex/tokens/{address}"
GMGN_SMART = ("https://gmgn.ai/api/v1/smart_money/{chain}/token/{address}/now"
              "?app_lang=en&device_id=1f1fd0c4-9f77-45ee-88b0-4b9d2454cf0e"
              "&client_id=gmgn_web_20260222-5641-b9d9f88&from_app=gmgn&app_ver=20260222-5641-b9d9f88"
              "&tz_name=America%2FWinnipeg&tz_offset=-18000&current_tz_offset=21600"
              "&fpid=93eb713fb01d3af9dd1608470b6aa9d5&os=web&sec-ch-ua-platform=Windows"
              "&sec-ch-ua-mobile=?0&sec-ch-ua=%22Not:A-Brand%22%3Bv%22%24%22%2C%20%22Chromium%22%3Bv%22147%22"
              "&sec-ch-ua-full-version-list=Not:A-Brand%3Bv24%2C%20Chromium%3Bv147")
GMGN_CHAIN = {"solana": "sol", "ethereum": "eth", "base": "base", "bsc": "bsc"}

MAX_MCAP = 2_000_000
TOP_N = 10
GMGN_DELAY = 2.0          # seconds between GMGN calls

NAVY_TOP = (16, 20, 52)
NAVY_BOT = (8, 10, 32)
GOLD = (255, 214, 92)
GREEN = (72, 219, 120)
RED = (255, 104, 116)
TEXT = (235, 238, 255)

IG_ACCESS_TOKEN = os.environ["IG_ACCESS_TOKEN"]
IG_USER_ID = os.environ["IG_USER_ID"]
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN", "")
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]

REPO_NAME = os.environ.get("GITHUB_REPOSITORY", "kitbrianleung/onchain-daily")
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")
RAW = f"https://raw.githubusercontent.com/{REPO_NAME}/{BRANCH}/images/trending"

NOW = datetime.now(timezone.utc)
DAY = NOW.strftime("%Y-%m-%d")


def log(msg):
    print(f"[trending {datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def notify(msg):
    log("discord: " + msg.splitlines()[0])
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": msg[:1900]}, timeout=15)
    except Exception as e:
        log(f"discord notify failed: {e}")


def git(*args):
    env = os.environ.copy()
    if GITHUB_TOKEN:
        env["GIT_ASKPASS"] = "echo"
        env["GIT_USERNAME"] = "x-access-token"
        env["GIT_PASSWORD"] = GITHUB_TOKEN
        url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{REPO_NAME}.git"
    else:
        url = f"git@github.com:{REPO_NAME}.git"
    r = subprocess.run(["git", *args], cwd=REPO_ROOT, env=env,
                       capture_output=True, text=True)
    if r.returncode != 0:
        log(f"git {args[0]} stderr: {r.stderr.strip()[:200]}")
    return r.returncode == 0


def configure_git_remote():
    if GITHUB_TOKEN:
        url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{REPO_NAME}.git"
        subprocess.run(["git", "remote", "set-url", "origin", url],
                       cwd=REPO_ROOT, capture_output=True)


def push_state_and_images():
    configure_git_remote()
    git("add", str(IMAGES_DIR.relative_to(REPO_ROOT)), str(STATE_FILE.relative_to(REPO_ROOT)))
    if git("diff", "--cached", "--quiet"):
        log("nothing new to commit")
        return
    git("-c", "user.name=onchain-daily-bot", "-c", "user.email=bot@users.noreply.github.com",
        "commit", "-m", f"trending table {DAY} [skip ci]")
    git("push", "origin", f"HEAD:{BRANCH}")


def wait_for_raw(path, retries=20, delay=3):
    """Poll raw.githubusercontent until the pushed file is served."""
    url = f"{RAW}/{path}"
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(delay)
    raise RuntimeError(f"raw URL never became available: {url}")


# ---------------- 1. Fetch trending + filter ----------------

def fetch_trending():
    r = requests.get(DEXSCREENER_BOOSTS, timeout=30,
                     headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    r.raise_for_status()
    boosts = r.json()[:100]
    log(f"dexscreener boosts: {len(boosts)} tokens")

    addrs_by_chain = {}
    for b in boosts:
        chain_id = b["chainId"].lower()
        if chain_id in GMGN_CHAIN:
            addrs_by_chain.setdefault(chain_id, []).append(b["tokenAddress"])

    pools = []
    for chain_id, addrs in addrs_by_chain.items():
        for i in range(0, len(addrs), 30):
            batch = addrs[i:i + 30]
            try:
                r = requests.get(DEXSCREENER_PAIRS.format(address=",".join(batch)),
                                 timeout=30, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    pools.extend(r.json().get("pairs") or [])
            except Exception as e:
                log(f"dexscreener pairs fetch failed: {e}")
            time.sleep(0.3)
    log(f"total pools: {len(pools)}")

    # keep the highest-liquidity pool per token address
    best = {}
    for p in pools:
        bt = p.get("baseToken", {})
        key = bt.get("address", "")
        if not key:
            continue
        liq = (p.get("liquidity") or {}).get("usd") or 0
        if key not in best or liq > (best[key].get("liquidity") or {}).get("usd", 0):
            best[key] = p
    pools = list(best.values())

    cands = []
    for p in pools:
        mc = p.get("marketCap") or p.get("fdv") or 0
        vol24 = (p.get("volume") or {}).get("h24") or 0
        if mc and mc < MAX_MCAP and vol24 > 0:
            cands.append(p)
    cands.sort(key=lambda p: (p.get("volume") or {}).get("h24") or 0, reverse=True)
    log(f"candidates under ${MAX_MCAP:,} mcap: {len(cands)}")
    return cands


def gmgn_smart_count(chain_id, address):
    """Smart-money holder count from GMGN via headless Chromium (fresh session each call).
    Returns the count, or None on failure."""
    gmgn_chain = GMGN_CHAIN[chain_id]
    url = GMGN_SMART.format(chain=gmgn_chain, address=address)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox",
                      "--disable-dev-shm-usage", "--disable-gpu"])
            ctx = browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/126.0.0.0 Safari/537.36"),
                viewport={"width": 1920, "height": 1080},
                locale="en-US")
            page = ctx.new_page()
            resp = page.goto(url, timeout=30000)
            status = resp.status if resp else 0
            data = None
            if status == 200:
                try:
                    data = resp.json()
                except Exception:
                    data = None
            browser.close()
            if data is None:
                log(f"GMGN {status} for {address[:8]}… (no JSON)")
                return None
            d = data.get("data")
            if d is None:
                log(f"GMGN 200 but no data for {address[:8]}…")
                return None
            count = d.get("smart_money_count")
            log(f"GMGN {status} for {address[:8]}… → smart_money_count={count}")
            return count
    except Exception as e:
        log(f"GMGN error for {address[:8]}…: {e}")
        return None


def build_rows():
    cands = fetch_trending()
    rows, seen = [], set()
    for p in cands:
        if len(rows) >= TOP_N:
            break
        bt = p.get("baseToken", {})
        addr = bt.get("address", "")
        chain_id = p.get("chainId", "").lower()
        key = (chain_id, addr)
        if not addr or key in seen:
            continue
        seen.add(key)
        sc = gmgn_smart_count(chain_id, addr)
        if sc is None or sc <= 0:
            log(f"skip {bt.get('symbol','?')}: no smart money (or GMGN failed)")
            time.sleep(GMGN_DELAY)
            continue
        chg = p.get("priceChange") or {}
        vol = p.get("volume") or {}
        rows.append({
            "token": bt.get("symbol", "?"),
            "name": bt.get("name", ""),
            "address": addr,
            "chain": chain_id,
            "price": float(p.get("priceUsd") or 0),
            "mcap": p.get("marketCap") or p.get("fdv") or 0,
            "smart_wallets": sc,
            "vol24": vol.get("h24") or 0,
            "h1": chg.get("h1") or 0,
            "h6": chg.get("h6") or 0,
            "h24": chg.get("h24") or 0,
        })
        log(f"  ✓ {bt.get('symbol','?')} ({chain_id}) — {sc} smart wallets, "
            f"vol ${vol.get('h24',0):,.0f}")
        time.sleep(GMGN_DELAY)
    return rows


# ---------------- 2. Render ----------------

def font(size):
    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans-Bold", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def price_fmt(p):
    s = f"{p:.6f}".rstrip("0")
    if s.endswith("."):
        s += "0"
    return f"${s}"


def usd(v):
    if v >= 1_000_000:
        return f"${v/1_000_000:.1f}M".replace(".0M", "M")
    if v >= 1_000:
        return f"${v/1_000:.0f}K"
    return f"${v:.0f}"


def pct(v):
    label = f"{v:+.1f}%"
    return label, GREEN if v >= 0 else RED


COLS = [("TOKEN", 160, "center"), ("CHAIN", 100, "center"), ("PRICE", 130, "center"),
        ("MCAP", 110, "center"), ("24H VOL", 120, "center"),
        ("1H %", 100, "center"), ("24H %", 110, "center"), ("SMART\nWALLET", 130, "center")]


def cell_text(row, idx):
    if idx == 0: return row["token"], TEXT              # single token name only
    if idx == 1: return row["chain"], TEXT
    if idx == 2: return price_fmt(row["price"]), TEXT
    if idx == 3: return usd(row["mcap"]), TEXT
    if idx == 4: return usd(row["vol24"]), TEXT
    if idx == 5: return pct(row["h1"])
    if idx == 6: return pct(row["h24"])
    return str(row["smart_wallets"]), TEXT              # SMART WALLET = last column


def render_table(rows, path, W, H):
    img = Image.new("RGB", (W, H)); d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / max(1, H - 1)
        d.line([(0, y), (W, y)], fill=tuple(int(NAVY_TOP[i] + (NAVY_BOT[i] - NAVY_TOP[i]) * t) for i in range(3)))

    # ---- vertical centering: measure the whole content block first ----
    header_h = 70
    row_h = 95
    title_h = 175                      # two title lines + date
    gap = 40                           # space between date and table
    table_h = header_h + len(rows) * row_h
    top = max(30, (H - title_h - gap - table_h) // 2)

    # ---- title block ----
    t1, tf1 = "24 HOUR TRENDING TOKENS", font(56)
    tw = d.textlength(t1, font=tf1)
    d.text(((W - tw) / 2, top), t1, font=tf1, fill=(255, 255, 255))
    t2, tf2 = "with SMART MONEY", font(56)
    tw = d.textlength(t2, font=tf2)
    d.text(((W - tw) / 2, top + 65), t2, font=tf2, fill=GOLD)
    dt = NOW.strftime("%d %b %Y").upper()
    fdt = font(30)
    tw = d.textlength(dt, font=fdt)
    d.text(((W - tw) / 2, top + 135), dt, font=fdt, fill=(190, 200, 230))

    # ---- table geometry (columns centered on the canvas) ----
    total_w = sum(w for _, w, _ in COLS)
    x0 = (W - total_w) // 2
    xs, x = [], x0
    for _, w, _ in COLS:
        xs.append(x); x += w
    x1 = x0 + total_w
    y0 = top + title_h + gap
    y1 = y0 + table_h

    # ---- header ----
    d.rectangle([x0, y0, x1, y0 + header_h], fill=GOLD)
    hf = font(18)
    for (label, w, _), xpos in zip(COLS, xs):
        lines = label.split("\n")
        for li, ln in enumerate(lines):
            tw = d.textlength(ln, font=hf)
            ty = y0 + header_h / 2 + (li - (len(lines) - 1) / 2) * 22 - 11
            d.text((xpos + w / 2 - tw / 2, ty), ln,
                   font=hf, fill=(12, 14, 40))

    # ---- data rows ----
    cf = font(19)
    for ri, row in enumerate(rows):
        ry = y0 + header_h + ri * row_h
        for ci, ((label, w, align), xpos) in enumerate(zip(COLS, xs)):
            txt, color = cell_text(row, ci)
            lines = txt.split("\n")[:2]
            for li, ln in enumerate(lines):
                tw = d.textlength(ln, font=cf)
                d.text((xpos + w / 2 - tw / 2, ry + row_h / 2 - (len(lines) - li - 0.5) * 22 + 1),
                       ln, font=cf, fill=color)

    # ---- full grid: borders around every cell ----
    GRID = (70, 82, 130)
    d.rectangle([x0, y0, x1, y1], outline=GRID, width=3)                       # outer border
    for xpos in xs[1:]:                                                        # vertical lines
        d.line([(xpos, y0), (xpos, y1)], fill=GRID, width=2)
    d.line([(x0, y0 + header_h), (x1, y0 + header_h)], fill=GRID, width=2)     # under header
    for ri in range(1, len(rows)):                                             # row separators
        yy = y0 + header_h + ri * row_h
        d.line([(x0, yy), (x1, yy)], fill=GRID, width=2)

    img.save(path, "JPEG", quality=92)
    log(f"rendered {path}")


# ---------------- 3. Caption ----------------

def llm(messages, max_tokens=400):
    """Single OpenRouter chat call (google/gemini-2.5-flash, cheap+fast)."""
    r = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                 "Content-Type": "application/json",
                 "HTTP-Referer": "https://github.com/kitbrianleung/onchain-daily",
                 "X-Title": "Trending x Smart Money"},
        json={"model": "google/gemini-2.5-flash", "messages": messages,
              "max_tokens": max_tokens, "temperature": 0.5},
        timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"LLM {r.status_code}: {r.text[:300]}")
    return r.json()["choices"][0]["message"]["content"]


def build_caption(rows):
    data = json.dumps(rows, ensure_ascii=False)
    prompt = f"""Write a short Instagram caption (max 6 lines + hashtags) analyzing this table of the top 10
Dexscreener 24H trending tokens that are held by smart-money wallets (per GMGN data).
Table (JSON): {data}
Call out the biggest movers (24H %) and which tokens have the most smart-money wallets.
Do NOT include token contract addresses — they are appended automatically.
End with a blank line then hashtags: #dexscreener #smartmoney #onchain #crypto plus one #TICKER hashtag per token (use the token symbols, without $)."""
    # ---- token address footer (post caption only) ----
    addr_lines = []
    for r in rows:
        a = r.get("address")
        if a:
            addr_lines.append(f"${r['token']}\n{a}")
    addr_block = "\n\n".join(addr_lines)
    budget = 2000 - len(addr_block)          # keep total under IG's 2200-char caption limit
    try:
        cap = llm([{"role": "user", "content": prompt}], max_tokens=700).strip()[:budget]
        idx = cap.find("#")
        if idx == -1:
            cap = cap + "\n\nData from DEX Screener"
        else:
            cap = cap[:idx].rstrip() + "\n\nData from DEX Screener\n\n" + cap[idx:]
    except Exception:
        cap = "📊 Top 24H trending tokens held by smart money.\n\nData from DEX Screener\n\n#dexscreener #smartmoney #onchain #crypto"
    if addr_block:
        cap += "\n\n" + addr_block
    return cap


# ---------------- 4. Instagram ----------------

def ig_create(image_url, caption=None, is_story=False):
    payload = {"image_url": image_url, "access_token": IG_ACCESS_TOKEN}
    if is_story:
        payload["media_type"] = "STORIES"
    elif caption is not None:
        payload["caption"] = caption
    r = requests.post(f"{GRAPH}/{IG_USER_ID}/media", data=payload, timeout=60)
    if r.status_code == 200:
        return r.json()["id"]
    raise RuntimeError(f"IG container failed: {r.status_code}: {r.text[:300]}")


def ig_publish(cid):
    """Publish with retry: IG processes the container asynchronously; 9007 = not ready yet."""
    last = ""
    for attempt in range(8):
        r = requests.post(f"{GRAPH}/{IG_USER_ID}/media_publish",
                          data={"creation_id": cid, "access_token": IG_ACCESS_TOKEN}, timeout=60)
        if r.status_code == 200:
            return r.json()["id"]
        last = f"{r.status_code}: {r.text[:300]}"
        wait = min(5 * (attempt + 1), 30)          # 5s, 10s, 15s ... max 30s
        log(f"publish attempt {attempt + 1} failed ({last[:100]}), retrying in {wait}s")
        time.sleep(wait)
    raise RuntimeError(f"IG publish failed after retries: {last}")


# ---------------- 5. Orchestration ----------------

def cmd_generate():
    rows = build_rows()
    if len(rows) < 3:
        notify(f"⚠️ Trending x Smart Money: only {len(rows)} tokens with smart money — skipping today.")
        sys.exit(0)

    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    story_path = IMAGES_DIR / f"{DAY}-story.jpg"
    post_path = IMAGES_DIR / f"{DAY}-post.jpg"
    render_table(rows, story_path, 1080, 1920)
    render_table(rows, post_path, 1080, 1350)

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({
        "date": DAY,
        "rows": rows,
        "story_image": f"trending/{DAY}-story.jpg",
        "post_image": f"trending/{DAY}-post.jpg",
    }, ensure_ascii=False, indent=2))

    push_state_and_images()
    notify(f"🛠 Trending x Smart Money: {len(rows)} tokens, images pushed for {DAY}. "
           "Next step: publish job.")
    log("generate done")


def cmd_publish():
    state = json.loads(STATE_FILE.read_text())
    if state.get("date") != DAY or "story_image" not in state:
        notify(f"⏭️ Trending x Smart Money: no fresh table for {DAY} "
               f"(state file is from '{state.get('date', '?')}'). Skipping publish.")
        log("stale or old-format state — nothing to publish")
        return
    story_url = f"{RAW}/{state['story_image']}"
    post_url = f"{RAW}/{state['post_image']}"
    wait_for_raw(state["story_image"])
    wait_for_raw(state["post_image"])

    story_cid = ig_create(story_url, is_story=True)
    story_id = ig_publish(story_cid)
    log(f"story published: {story_id}")

    caption = build_caption(state["rows"])
    post_cid = ig_create(post_url, caption=caption)
    post_id = ig_publish(post_cid)
    log(f"post published: {post_id}")

    notify(f"✅ Trending x Smart Money published! (story {story_id}, post {post_id})")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("generate", "publish"):
        print("usage: python trending.py [generate|publish]")
        sys.exit(1)
    try:
        {"generate": cmd_generate, "publish": cmd_publish}[sys.argv[1]]()
    except Exception:
        import traceback
        notify("❌ Trending x Smart Money FAILED:\n" + traceback.format_exc()[-3500:])
        raise
