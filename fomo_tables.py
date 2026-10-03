#!/usr/bin/env python3
"""
FOMO Leaderboards — daily Instagram story + post.

Most Held Tokens on FOMO  +  Trending Tokens on FOMO (fomoapi.io)
-> Pillow-rendered tables in the trending.py house style (1080x1920 story + 1080x1350 post)
-> pushed to repo, published to Instagram (analysis lives in the post caption)

Cron-friendly two-step CLI:
  python fomo_tables.py generate   # fetch -> render -> push images + state
  python fomo_tables.py publish    # publish story + post to Instagram
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent
STATE_FILE = REPO_ROOT / "state" / "fomo.json"
IMAGES_DIR = REPO_ROOT / "images" / "fomo"

# ---- palette identical to trending.py ----
NAVY_TOP = (16, 20, 52)
NAVY_BOT = (8, 10, 32)
GOLD = (255, 214, 92)
GREEN = (72, 219, 120)
RED = (255, 104, 116)
TEXT = (235, 238, 255)
GRID = (70, 82, 130)
MUTED = (190, 200, 230)
DARK = (12, 14, 40)

FOMO_API_KEY = os.environ.get("FOMO_API_KEY", "")
IG_ACCESS_TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
IG_USER_ID = os.environ.get("IG_USER_ID", "")
IG_API_VERSION = os.environ.get("IG_API_VERSION", "v23.0")
GRAPH = f"https://graph.instagram.com/{IG_API_VERSION}"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN", "")

REPO_NAME = os.environ.get("GITHUB_REPOSITORY", "kitbrianleung/onchain-daily")
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")
RAW = f"https://raw.githubusercontent.com/{REPO_NAME}/{BRANCH}/images/fomo"

# Confirmed against https://fomoapi.io/docs
FOMO_BASE = "https://api.fomoapi.io"
FOMO_MOST_HELD_PATH = "/v2/leaderboard/tokens/most-held"
FOMO_TRENDING_PATH = "/v2/leaderboard/tokens/trending"
TOP_N = 7

NOW = datetime.now(timezone.utc)
DAY = NOW.strftime("%Y-%m-%d")


def log(msg):
    print(f"[fomo {datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def notify(msg):
    log("discord: " + msg.splitlines()[0])
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": msg[:1900]}, timeout=15)
    except Exception as e:
        log(f"discord notify failed: {e}")


# ---------------- git (same pattern as trending.py) ----------------

def git(*args):
    r = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True)
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
        "commit", "-m", f"fomo leaderboards {DAY} [skip ci]")
    git("push", "origin", f"HEAD:{BRANCH}")


def wait_for_raw(path, retries=40, delay=5):
    """Poll raw.githubusercontent until the pushed file is served."""
    url = f"{RAW}/{path}"
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(delay)
    raise RuntimeError(f"raw URL never became available: {url}")


# ---------------- 1. Fetch FOMO boards ----------------

def _ascii(s):
    return re.sub(r"[^\x20-\x7E]", "", s or "").strip()


def fomo_get(path):
    r = requests.get(
        f"{FOMO_BASE}{path}",
        headers={"User-Agent": "onchain-daily/1.0",
                 "Authorization": f"Bearer {FOMO_API_KEY}"},
        params={"limit": TOP_N},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"FOMO API failed: {r.status_code}: {r.text[:300]}")
    log(f"{path} OK · credits left: {r.headers.get('x-credits-remaining', '?')}")

    data = r.json()
    if data.get("available") is False:
        raise RuntimeError(f"FOMO board not available yet: {path}")
    tokens = data.get("tokens")
    if not isinstance(tokens, list) or not tokens:
        raise RuntimeError(f"FOMO API: unexpected response: {str(data)[:200]}")

    rows = []
    for t in tokens[:TOP_N]:
        tok = t.get("token") or {}
        name = _ascii(tok.get("name"))
        symbol = _ascii(tok.get("symbol")).lstrip("$")
        rows.append({
            "name": name,
            "symbol": symbol,
            # table cell: symbol preferred (matches trending.py), name as fallback
            "label": (symbol or name)[:18] or "?",
            # prose label for the caption analysis
            "full": (f"{name} ({symbol})" if symbol and symbol.lower() != name.lower()
                     else (name or symbol))[:40] or "?",
            "mcap": t.get("marketCapUsd"),
            "pct": t.get("change24h"),
        })
    return rows


# ---------------- 2. Analysis (goes in the post caption) ----------------

def analyse(held, trending):
    paras = []

    if held:
        leader = held[0]
        pct_txt, _ = pct(leader["pct"])
        paras.append(
            f"Most-held positioning is anchored by {leader['full']} "
            f"({usd(leader['mcap'])} mcap, {pct_txt} 24h) — a read on where "
            f"longer-term conviction currently sits among FOMO traders."
        )
        ups = sum(1 for t in held if isinstance(t["pct"], (int, float)) and t["pct"] > 0)
        paras.append(
            f"{ups}/{len(held)} most-held names are green on the day — "
            + ("broad risk appetite across established positions."
               if ups >= len(held) / 2 else
               "held positions under pressure despite sticky ownership.")
        )

    if trending:
        hot = max(trending,
                  key=lambda t: t["pct"] if isinstance(t["pct"], (int, float)) else -999)
        pct_txt, _ = pct(hot["pct"])
        paras.append(
            f"Today's momentum leader is {hot['full']} at {pct_txt} (24h), "
            f"flagging where fresh speculative flow is rotating."
        )

    if held and trending:
        held_names = {t["full"] for t in held}
        overlap = [t["full"] for t in trending if t["full"] in held_names]
        paras.append(
            f"Overlap: {len(overlap)}/{len(trending)} trending tokens are also widely held"
            + (" — momentum is converting into ownership, a constructive signal."
               if overlap else
               " — flow is chasing new names rather than existing holdings, "
               "suggesting short-horizon rotation.")
        )
    return paras


def build_caption(held, trending):
    tickers, seen = [], set()
    for t in held + trending:
        s = re.sub(r"[^A-Za-z0-9]", "", t["symbol"] or "")
        if s and s.casefold() not in seen:
            seen.add(s.casefold())
            tickers.append(s)
    tags = "#fomo #onchain #crypto #altcoins" + "".join(f" #{t}" for t in tickers[:10])
    cap = "\n\n".join(analyse(held, trending))
    return (cap + "\n\nData from FOMO (fomoapi.io)\n\n" + tags)[:2100]


# ---------------- 3. Render (trending.py house style) ----------------

def font(size):
    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans-Bold", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def usd(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    if v >= 1_000_000:
        return f"${v/1_000_000:.1f}M".replace(".0M", "M")
    if v >= 1_000:
        return f"${v/1_000:.0f}K"
    return f"${v:.0f}"


def pct(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—", MUTED
    return f"{v:+.1f}%", GREEN if v >= 0 else RED


COLS = [("TOKEN", 480), ("MCAP", 240), ("24H %", 240)]


def render_boards(held, trending, path, W, H):
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / max(1, H - 1)
        d.line([(0, y), (W, y)],
               fill=tuple(int(NAVY_TOP[i] + (NAVY_BOT[i] - NAVY_TOP[i]) * t) for i in range(3)))

    # geometry: roomier on the story, compact on the 4:5 post
    if H >= 1600:
        row_h, header_h = 72, 64
        title_sz, sec_sz, hf_sz, cf_sz, dt_sz = 56, 34, 26, 26, 30
        gap, table_gap = 40, 56
    else:
        row_h, header_h = 58, 54
        title_sz, sec_sz, hf_sz, cf_sz, dt_sz = 48, 28, 22, 22, 26
        gap, table_gap = 30, 42

    n = max(len(held), len(trending))
    sec_h = sec_sz + 22                       # section title + its breathing room
    table_h = header_h + n * row_h
    title_h = title_sz + 18 + dt_sz
    total = title_h + gap + sec_h + table_h + table_gap + sec_h + table_h
    top = max(30, (H - total) // 2)

    # ---- title block ----
    t1, tf1 = "FOMO LEADERBOARDS", font(title_sz)
    tw = d.textlength(t1, font=tf1)
    d.text(((W - tw) / 2, top), t1, font=tf1, fill=(255, 255, 255))
    dt = NOW.strftime("%d %b %Y").upper()
    fdt = font(dt_sz)
    tw = d.textlength(dt, font=fdt)
    d.text(((W - tw) / 2, top + title_sz + 14), dt, font=fdt, fill=MUTED)

    # ---- table geometry (centered, same 960px total width as trending.py) ----
    total_w = sum(w for _, w in COLS)
    x0 = (W - total_w) // 2
    xs, x = [], x0
    for _, w in COLS:
        xs.append(x)
        x += w
    x1 = x0 + total_w

    def draw_section(y, title, rows):
        fs = font(sec_sz)
        tw = d.textlength(title, font=fs)
        d.text(((W - tw) / 2, y), title, font=fs, fill=(255, 255, 255))
        y += sec_h

        y0, y1 = y, y + header_h + len(rows) * row_h

        # header — gold bar, dark text
        d.rectangle([x0, y0, x1, y0 + header_h], fill=GOLD)
        hf = font(hf_sz)
        for (label, w), xpos in zip(COLS, xs):
            tw = d.textlength(label, font=hf)
            d.text((xpos + w / 2 - tw / 2, y0 + header_h / 2 - hf_sz * 0.38),
                   label, font=hf, fill=DARK)

        # data rows
        cf = font(cf_sz)
        for ri, row in enumerate(rows):
            ry = y0 + header_h + ri * row_h
            vals = [(row["label"], TEXT), (usd(row["mcap"]), TEXT), pct(row["pct"])]
            for (txt, color), (label, w), xpos in zip(vals, COLS, xs):
                tw = d.textlength(txt, font=cf)
                d.text((xpos + w / 2 - tw / 2, ry + row_h / 2 - cf_sz * 0.38),
                       txt, font=cf, fill=color)

        # full grid borders, same as trending.py
        d.rectangle([x0, y0, x1, y1], outline=GRID, width=3)
        for xpos in xs[1:]:
            d.line([(xpos, y0), (xpos, y1)], fill=GRID, width=2)
        d.line([(x0, y0 + header_h), (x1, y0 + header_h)], fill=GRID, width=2)
        for ri in range(1, len(rows)):
            yy = y0 + header_h + ri * row_h
            d.line([(x0, yy), (x1, yy)], fill=GRID, width=2)
        return y1

    y = top + title_h + gap
    y = draw_section(y, "MOST HELD TOKENS", held)
    y = draw_section(y + table_gap, "TRENDING TOKENS", trending)

    img.save(path, "JPEG", quality=92)
    log(f"rendered {path}")


# ---------------- 4. Instagram (same as trending.py) ----------------

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
                          data={"creation_id": cid, "access_token": IG_ACCESS_TOKEN},
                          timeout=60)
        if r.status_code == 200:
            return r.json()["id"]
        last = f"{r.status_code}: {r.text[:300]}"
        wait = min(5 * (attempt + 1), 30)
        log(f"publish attempt {attempt + 1} failed ({last[:100]}), retrying in {wait}s")
        time.sleep(wait)
    raise RuntimeError(f"IG publish failed after retries: {last}")


# ---------------- 5. Orchestration ----------------

def cmd_generate():
    if not FOMO_API_KEY:
        raise SystemExit("Missing env var: FOMO_API_KEY")

    held = fomo_get(FOMO_MOST_HELD_PATH)
    trending = fomo_get(FOMO_TRENDING_PATH)
    log(f"most-held: {len(held)} rows · trending: {len(trending)} rows")

    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    story_path = IMAGES_DIR / f"{DAY}-story.jpg"
    post_path = IMAGES_DIR / f"{DAY}-post.jpg"
    render_boards(held, trending, story_path, 1080, 1920)
    render_boards(held, trending, post_path, 1080, 1350)

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({
        "date": DAY,
        "most_held": held,
        "trending": trending,
        "story_image": f"fomo/{DAY}-story.jpg",
        "post_image": f"fomo/{DAY}-post.jpg",
    }, ensure_ascii=False, indent=2))

    push_state_and_images()
    notify(f"🛠 FOMO Leaderboards: {len(held)}+{len(trending)} tokens, images pushed for {DAY}.")
    log("generate done")


def cmd_publish():
    if not IG_ACCESS_TOKEN or not IG_USER_ID:
        raise SystemExit("Missing env vars: IG_ACCESS_TOKEN, IG_USER_ID")

    state = json.loads(STATE_FILE.read_text())
    if state.get("date") != DAY or "story_image" not in state:
        notify(f"⏭️ FOMO Leaderboards: no fresh board for {DAY} "
               f"(state file is from '{state.get('date', '?')}'). Skipping publish.")
        log("stale or old-format state — nothing to publish")
        return

    story_rel = state["story_image"].removeprefix("fomo/")
    post_rel = state["post_image"].removeprefix("fomo/")
    story_url = f"{RAW}/{story_rel}"
    post_url = f"{RAW}/{post_rel}"
    wait_for_raw(story_rel)
    wait_for_raw(post_rel)

    story_cid = ig_create(story_url, is_story=True)
    story_id = ig_publish(story_cid)
    log(f"story published: {story_id}")

    caption = build_caption(state["most_held"], state["trending"])
    post_cid = ig_create(post_url, caption=caption)
    post_id = ig_publish(post_cid)
    log(f"post published: {post_id}")

    notify(f"✅ FOMO Leaderboards published! (story {story_id}, post {post_id})")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("generate", "publish"):
        print("usage: python fomo_tables.py [generate|publish]")
        sys.exit(1)
    try:
        {"generate": cmd_generate, "publish": cmd_publish}[sys.argv[1]]()
    except Exception:
        import traceback
        notify("❌ FOMO Leaderboards FAILED:\n" + traceback.format_exc()[-3500:])
        raise
