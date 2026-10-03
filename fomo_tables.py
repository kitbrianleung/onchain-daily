#!/usr/bin/env python3
"""fomo_tables.py — FOMO 'Most Held' + 'Trending' tokens → one IG story (1080x1920).

Mirrors trending.py's architecture:
  generate → render PNG, save to images/fomo/story.png, git commit + push
  publish  → fetch image (Actions cache → raw URL → local), publish IG story

Secrets needed: FOMO_API_KEY, IG_ACCESS_TOKEN, IG_USER_ID
(GITHUB_TOKEN is provided automatically by Actions)
"""

import base64
import json
import os
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

# ──────────────────────────── CONFIG ────────────────────────────

FOMO_API_KEY = os.environ.get("FOMO_API_KEY", "")
IG_ACCESS_TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
IG_USER_ID = os.environ.get("IG_USER_ID", "")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

IMAGE_BASE_URL = os.environ.get(
    "IMAGE_BASE_URL",
    f"https://raw.githubusercontent.com/{GITHUB_REPOSITORY}/main/images",
).rstrip("/")

IG_API = "https://graph.instagram.com/v26.0"

# Confirmed against https://fomoapi.io/docs
FOMO_BASE = "https://api.fomoapi.io"
FOMO_MOST_HELD_PATH = "/v2/leaderboard/tokens/most-held"
FOMO_TRENDING_PATH = "/v2/leaderboard/tokens/trending"

STORY_W, STORY_H = 1080, 1920
TOP_N = 7
UA = "onchain-daily/1.0 (+github.com/kitbrianleung/onchain-daily)"

FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# ──────────────────────────── HELPERS ───────────────────────────

def log(msg: str) -> None:
    print(f"[fomo {datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def notify(msg: str) -> None:
    log(f"discord: {msg[:90]}")
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": msg[:1900]}, timeout=15)
    except Exception as e:
        log(f"discord error: {e}")


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_BOLD if bold else FONT_REGULAR, size)


def fmt_mcap(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    if v >= 1e9:
        return f"${v/1e9:.2f}B"
    if v >= 1e6:
        return f"${v/1e6:.1f}M"
    if v >= 1e3:
        return f"${v/1e3:.0f}K"
    return f"${v:.0f}"


def fmt_pct(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—", (180, 180, 190)
    colour = (46, 204, 113) if v >= 0 else (231, 76, 60)
    return f"{v:+.1f}%", colour

# ─────────────────────── FOMO API FETCH ─────────────────────────

def fomo_get(path: str) -> list[dict]:
    url = f"{FOMO_BASE}{path}"
    r = requests.get(
        url,
        headers={"User-Agent": UA, "Authorization": f"Bearer {FOMO_API_KEY}"},
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

    out = []
    for t in tokens[:TOP_N]:
        tok = t.get("token") or {}
        name = (tok.get("name") or "").strip()
        symbol = (tok.get("symbol") or "").strip().lstrip("$")
        label = (f"{name} ({symbol})" if symbol and symbol.lower() != name.lower()
                 else (name or symbol))
        out.append({
            "name": label[:24],
            "mcap": t.get("marketCapUsd"),
            "pct": t.get("change24h"),
        })
    return out

# ─────────────────────── ANALYSIS ───────────────────────────────

def analyse(held: list[dict], trending: list[dict]) -> list[str]:
    lines = []

    if held:
        leader = held[0]
        pct_txt, _ = fmt_pct(leader["pct"])
        lines.append(
            f"Most-held positioning is anchored by {leader['name']} "
            f"({fmt_mcap(leader['mcap'])} mcap, {pct_txt} 24h) — a read on where "
            f"longer-term conviction currently sits among FOMO traders."
        )
        ups = sum(1 for t in held if isinstance(t["pct"], (int, float)) and t["pct"] > 0)
        lines.append(
            f"{ups}/{len(held)} most-held names are green on the day — "
            + ("broad risk appetite across established positions."
               if ups >= len(held) / 2 else
               "held positions under pressure despite sticky ownership.")
        )

    if trending:
        hot = max(trending, key=lambda t: t["pct"] if isinstance(t["pct"], (int, float)) else -999)
        pct_txt, _ = fmt_pct(hot["pct"])
        lines.append(
            f"Today's momentum leader is {hot['name']} at {pct_txt} (24h), "
            f"flagging where fresh speculative flow is rotating."
        )

    if held and trending:
        held_names = {t["name"] for t in held}
        overlap = [t["name"] for t in trending if t["name"] in held_names]
        lines.append(
            f"Overlap: {len(overlap)}/{len(trending)} trending tokens are also widely held"
            + (" — momentum is converting into ownership, a constructive signal."
               if overlap else
               " — flow is chasing new names rather than existing holdings, "
               "suggesting short-horizon rotation.")
        )

    wrapped = []
    for para in lines:
        wrapped.extend(textwrap.wrap(para, 55) or [""])
        wrapped.append("")
    return wrapped[:-1]

# ─────────────────────── RENDER STORY ───────────────────────────

def draw_table(d: ImageDraw.ImageDraw, y: int, title: str, rows: list[dict]) -> int:
    x0, x1 = 60, STORY_W - 60
    col_name, col_mcap, col_pct = x0 + 20, 640, 860

    d.text((x0, y), title, font=_font(44, bold=True), fill=(255, 255, 255))
    y += 70

    d.rectangle([x0, y, x1, y + 52], fill=(38, 42, 58))
    f_head = _font(30, bold=True)
    d.text((col_name, y + 10), "TOKEN", font=f_head, fill=(150, 158, 180))
    d.text((col_mcap, y + 10), "MCAP", font=f_head, fill=(150, 158, 180))
    d.text((col_pct, y + 10), "24H %", font=f_head, fill=(150, 158, 180))
    y += 52

    f_row = _font(30)
    for i, t in enumerate(rows):
        if i % 2 == 0:
            d.rectangle([x0, y, x1, y + 56], fill=(24, 27, 38))
        pct_txt, pct_col = fmt_pct(t["pct"])
        d.text((col_name, y + 12), t["name"], font=f_row, fill=(235, 238, 245))
        d.text((col_mcap, y + 12), fmt_mcap(t["mcap"]), font=f_row, fill=(235, 238, 245))
        d.text((col_pct, y + 12), pct_txt, font=f_row, fill=pct_col)
        y += 56
    return y


def render_story(held: list[dict], trending: list[dict]) -> Image.Image:
    img = Image.new("RGB", (STORY_W, STORY_H), (13, 15, 22))
    d = ImageDraw.Draw(img)

    d.text((60, 70), "FOMO Leaderboards", font=_font(64, bold=True), fill=(255, 255, 255))
    d.text((60, 155), datetime.now(timezone.utc).strftime("%A, %d %B %Y  ·  UTC"),
           font=_font(28), fill=(130, 138, 160))
    d.line([(60, 215), (STORY_W - 60, 215)], fill=(50, 55, 75), width=2)

    y = draw_table(d, 250, "Most Held Tokens", held)
    y = draw_table(d, y + 60, "Trending Tokens", trending)

    y += 55
    d.text((60, y), "Analysis", font=_font(40, bold=True), fill=(255, 255, 255))
    y += 62
    for line in analyse(held, trending):
        d.text((60, y), line, font=_font(27), fill=(205, 210, 225))
        y += 40

    d.text((60, STORY_H - 80), "Data: fomoapi.io  ·  @onchain_daily_wrap",
           font=_font(24), fill=(100, 106, 128))
    return img

# ─────────────────────── GIT PUBLISH ────────────────────────────
# Same pattern as trending.py: commit image to repo, serve via raw.githubusercontent.

def _git(*args) -> None:
    subprocess.run(["git", *args], check=True)


def git_push_images() -> None:
    _git("config", "user.name", "github-actions[bot]")
    _git("config", "user.email",
         "41898282+github-actions[bot]@users.noreply.github.com")
    _git("add", "images/fomo/")
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        log("no image changes to commit")
        return
    _git("commit", "-m", "fomo: update leaderboard images [skip ci]")
    _git("push")

# ─────────────────── INSTAGRAM (same as trending.py) ────────────

def ig_create(image_url: str) -> str:
    r = requests.post(
        f"{IG_API}/{IG_USER_ID}/media",
        data={
            "image_url": image_url,
            "media_type": "STORIES",
            "access_token": IG_ACCESS_TOKEN,
        },
        timeout=60,
    )
    if r.status_code != 200:
        raise RuntimeError(f"IG container failed: {r.status_code}: {r.text[:300]}")
    return r.json()["id"]


def ig_wait_ready(cid: str, timeout_s: int = 300) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = requests.get(
            f"{IG_API}/{cid}",
            params={"fields": "status_code", "access_token": IG_ACCESS_TOKEN},
            timeout=30,
        )
        if r.status_code != 200:
            raise RuntimeError(f"IG status failed: {r.status_code}: {r.text[:200]}")
        status = r.json().get("status_code")
        if status == "FINISHED":
            return
        if status == "ERROR":
            raise RuntimeError(f"IG container error: {r.text[:200]}")
        time.sleep(4)
    raise RuntimeError("IG container timeout")


def ig_publish(cid: str) -> str:
    r = requests.post(
        f"{IG_API}/{IG_USER_ID}/media_publish",
        data={"creation_id": cid, "access_token": IG_ACCESS_TOKEN},
        timeout=60,
    )
    if r.status_code != 200:
        raise RuntimeError(f"IG publish failed: {r.status_code}: {r.text[:300]}")
    return r.json()["id"]

# ──────────────────────────── COMMANDS ──────────────────────────

def cmd_generate() -> None:
    if not FOMO_API_KEY:
        raise SystemExit("Missing env var: FOMO_API_KEY")

    log("Fetching FOMO leaderboards…")
    held = fomo_get(FOMO_MOST_HELD_PATH)
    trending = fomo_get(FOMO_TRENDING_PATH)
    log(f"most-held: {len(held)} rows · trending: {len(trending)} rows")

    img = render_story(held, trending)

    out_dir = Path("images/fomo")
    out_dir.mkdir(parents=True, exist_ok=True)
    img.save(out_dir / "story.png")
    (out_dir / "meta.json").write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "most_held": held,
        "trending": trending,
    }, indent=2))
    log("rendered images/fomo/story.png")

    git_push_images()


def cmd_publish() -> None:
    if not IG_ACCESS_TOKEN or not IG_USER_ID:
        raise SystemExit("Missing env vars: IG_ACCESS_TOKEN, IG_USER_ID")

    # Cache-bust GitHub's raw-file CDN, same as trending.py
    story_url = f"{IMAGE_BASE_URL}/fomo/story.png?ts={int(time.time())}"
    log(f"story url: {story_url}")

    cid = ig_create(story_url)
    log(f"story container: {cid}")
    ig_wait_ready(cid)
    story_mid = ig_publish(cid)
    log(f"published story: {story_mid}")

    notify(f"✅ FOMO leaderboards story posted (media id {story_mid})\n{story_url}")


if __name__ == "__main__":
    cmd = {"generate": cmd_generate, "publish": cmd_publish}.get(sys.argv[1] if len(sys.argv) > 1 else "")
    if not cmd:
        raise SystemExit("Usage: python fomo_tables.py [generate|publish]")
    try:
        cmd()
    except Exception as e:
        notify(f"❌ FOMO leaderboards FAILED:\n```{e}```")
        raise
