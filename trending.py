#!/usr/bin/env python3
"""24H Trending Tokens with Smart Money:
Dexscreener trending -> GMGN smart-money filter -> table image -> IG story + post."""
import os, re, json, sys, time, datetime, traceback
import requests
from PIL import Image, ImageDraw, ImageFont

# ---------------- Environment ----------------
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
MODEL              = os.environ.get("MODEL", "moonshotai/kimi-k3")
IG_ACCESS_TOKEN    = os.environ.get("IG_ACCESS_TOKEN", "")
IG_USER_ID         = os.environ.get("IG_USER_ID", "")
IG_API_VERSION     = os.environ.get("IG_API_VERSION", "v23.0")
GRAPH              = f"https://graph.instagram.com/{IG_API_VERSION}"
DISCORD_TOKEN      = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL    = os.environ.get("DISCORD_CHANNEL_ID", "")
GMGN_API_KEY       = os.environ.get("GMGN_API_KEY", "")
GMGN_HOST          = os.environ.get("GMGN_HOST", "https://openapi.gmgn.ai")
IMAGE_BASE_URL     = os.environ.get("IMAGE_BASE_URL", "").rstrip("/")
NOW                = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)
STATE_FILE         = "state/trending.json"
DEX_API            = "https://api.dexscreener.com"
CHAIN_MAP          = {"solana": "sol", "ethereum": "eth", "bsc": "bsc", "base": "base",
                      "arbitrum": "arb", "avalanche": "avax", "robinhood": "robinhood"}
CANDIDATE_LIMIT    = 25
TOP_N              = 10
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}

def log(m): print(m, flush=True)

def notify(msg):
    log(msg)
    if not (DISCORD_TOKEN and DISCORD_CHANNEL): return
    for i in range(0, len(msg), 1900):
        try:
            requests.post(f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL}/messages",
                          headers={"Authorization": f"Bot {DISCORD_TOKEN}"},
                          json={"content": msg[i:i + 1900]}, timeout=30)
        except Exception as e: log(f"discord notify failed: {e}")

def llm(messages, max_tokens=800, temperature=0.4):
    r = requests.post("https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "X-Title": "OnchainDailyTrending"},
        json={"model": MODEL, "messages": messages, "max_tokens": max_tokens,
              "temperature": temperature}, timeout=120)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

def pct_str(v):
    return f"{v:+.1f}%" if isinstance(v, (int, float)) else "—"

# ---------------- 1. Fetch Dexscreener trending ----------------
def _chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]

def _as_list(data):
    """API responses may be a raw list or wrapped in an object."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("pairs", "boosts", "tokens", "data"):
            if isinstance(data.get(key), list):
                return data[key]
    return []

def _fetch_batch(chain, addrs, endpoint):
    """endpoint: 'tokens' or 'token-pairs'. Returns (pairs, status_note)."""
    url = f"{DEX_API}/{endpoint}/v1/{chain}/{','.join(addrs)}"
    rr = requests.get(url, headers=UA, timeout=30)
    if rr.status_code != 200:
        return [], f"HTTP {rr.status_code}"
    try:
        lst = _as_list(rr.json())
    except Exception:
        return [], "non-JSON response"
    return [p for p in lst if isinstance(p, dict) and p.get("priceUsd")], f"HTTP 200, {len(lst)} raw"

def fetch_dex_pairs():
    """Trending = top boosted tokens. Full pair data via the official multi-token endpoint."""
    # 1) Top boosted tokens (Dexscreener's trending universe)
    r = requests.get(f"{DEX_API}/token-boosts/top/v1", headers=UA, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"dexscreener boosts HTTP {r.status_code}")
    boosts = _as_list(r.json())

    seen, tokens = set(), []
    for b in boosts:
        if not isinstance(b, dict):
            continue
        key = (b.get("chainId"), b.get("tokenAddress"))
        if key not in seen and b.get("tokenAddress") and b.get("chainId"):
            seen.add(key)
            tokens.append({"chainId": b["chainId"], "address": b["tokenAddress"]})
    log(f"dexscreener: {len(tokens)} unique boosted tokens across {len({t['chainId'] for t in tokens})} chains")

    # 2) Full pair data — try /tokens/v1 first (documented multi-token endpoint),
    #    fall back to /token-pairs/v1 for any chain that returned nothing.
    by_chain = {}
    for t in tokens:
        by_chain.setdefault(t["chainId"], []).append(t["address"])

    pairs, empty_chains = [], []
    for chain, addrs in by_chain.items():
        got = 0
        for batch in _chunks(addrs[:60], 30):
            try:
                found, note = _fetch_batch(chain, batch, "tokens")
                pairs.extend(found); got += len(found)
                log(f"dex /tokens/v1 {chain} batch({len(batch)} addrs): {note}, kept {len(found)}")
                time.sleep(0.25)
            except Exception as ex:
                log(f"dex /tokens/v1 {chain} batch failed: {ex}")
        if got == 0:
            empty_chains.append(chain)

    # Fallback pass with the older endpoint for chains that gave nothing
    for chain in empty_chains:
        for batch in _chunks(by_chain[chain][:60], 30):
            try:
                found, note = _fetch_batch(chain, batch, "token-pairs")
                pairs.extend(found)
                log(f"dex /token-pairs/v1 {chain} batch({len(batch)} addrs): {note}, kept {len(found)}")
                time.sleep(0.25)
            except Exception as ex:
                log(f"dex /token-pairs/v1 {chain} batch failed: {ex}")

    # 3) Dedupe by pair address, rank by 24H volume (our trending proxy)
    uniq = {}
    for p in pairs:
        key = (p.get("chainId"), p.get("pairAddress"))
        if key not in uniq:
            uniq[key] = p
    pairs = list(uniq.values())

    def vol24(p):
        try: return float((p.get("volume") or {}).get("h24") or 0)
        except Exception: return 0

    pairs.sort(key=vol24, reverse=True)
    log(f"dexscreener: {len(pairs)} pairs with price, top vol24={vol24(pairs[0]):,.0f}" if pairs
        else "dexscreener: 0 pairs with price")
    return pairs

# ---------------- 2. GMGN smart-money count ----------------
_gmgn_debugged = False

import uuid

_gmgn_debugged = False
_gmgn_429_count = 0

def gmgn_smart_wallets(chain_id, address):
    """GMGN OpenAPI. Auth = client_id + timestamp, where client_id must be UNIQUE per request
    (reused client_ids are rejected as 'replayed'). Respects rate-limit reset_at."""
    global _gmgn_debugged, _gmgn_429_count
    chain = CHAIN_MAP.get(chain_id)
    if not chain:
        return None
    params = {"chain": chain, "address": address,
              "timestamp": str(int(time.time())),
              "client_id": f"ocd-{uuid.uuid4().hex[:12]}"}   # unique nonce per call
    headers = dict(UA)
    if GMGN_API_KEY:
        headers["X-APIKEY"] = GMGN_API_KEY
    for attempt in range(2):
        try:
            r = requests.get(f"{GMGN_HOST}/v1/token/info",
                             headers=headers, params=params, timeout=30)
            if r.status_code == 429:
                _gmgn_429_count += 1
                if _gmgn_429_count > 3:
                    raise RuntimeError("GMGN rate limit persists — aborting to avoid extending the IP ban")
                try: reset_at = r.json().get("reset_at")
                except Exception: reset_at = None
                wait = max(5, min(120, int(reset_at - time.time()) + 1)) if reset_at else 60
                log(f"gmgn rate-limited, backing off {wait}s")
                time.sleep(wait)
                continue
            if r.status_code != 200:
                log(f"gmgn {chain}:{address[:8]} -> HTTP {r.status_code}: {r.text[:150]}")
                return None
            d = r.json().get("data", r.json())
            if not _gmgn_debugged:   # one-time dump to verify the smart-money field name
                log(f"gmgn sample response: {json.dumps(d, ensure_ascii=False)[:1500]}")
                _gmgn_debugged = True
            wts = d.get("wallet_tags_stat") or {}
            for cand in (wts.get("smart_wallets"), d.get("smart_degen_count"),
                         d.get("smart_money_count"), d.get("smart_money"),
                         (d.get("token_info") or {}).get("smart_degen_count")):
                if isinstance(cand, (int, float)):
                    return int(cand)
            return 0
        except RuntimeError:
            raise
        except Exception as ex:
            log(f"gmgn failed for {address}: {ex}")
            return None
    return None

def num(x):
    try: return float(x)
    except (TypeError, ValueError): return None

def build_rows(pairs):
    rows, gmgn_calls = [], 0
    for p in pairs[:CANDIDATE_LIMIT]:
        bt  = p.get("baseToken", {})
        addr, chain_id = bt.get("address"), p.get("chainId", "")
        smart = gmgn_smart_wallets(chain_id, addr) if addr else None
        gmgn_calls += 1
        if not smart:
            log(f"skip ${bt.get('symbol','?')} ({chain_id}) — no smart money / unsupported chain")
            continue
        rows.append({
            "token":  bt.get("symbol", "?"),
            "name":   bt.get("name", ""),
            "address": addr,
            "chain":  chain_id,
            "price":  num(p.get("priceUsd")),
            "mcap":   num(p.get("marketCap") or p.get("fdv")),
            "smart_wallets": smart,
            "vol24":  num((p.get("volume") or {}).get("h24")),
            "h1":     num((p.get("priceChange") or {}).get("h1")),
            "h6":     num((p.get("priceChange") or {}).get("h6")),
            "h24":    num((p.get("priceChange") or {}).get("h24")),
        })
        time.sleep(1.2)
        if len(rows) >= TOP_N: break
    if len(rows) < 3:
        raise RuntimeError(f"only {len(rows)} tokens with smart money found "
                           f"({gmgn_calls} GMGN lookups — check the gmgn logs above: "
                           f"HTTP 401 = bad key, missing field = field name changed)")
    return rows

# ---------------- 3. Render the table ----------------
COLS = [("TOKEN", 160, "center"), ("CHAIN", 100, "center"), ("PRICE", 130, "center"),
        ("MCAP", 110, "center"), ("24H VOL", 120, "center"),
        ("1H %", 100, "center"), ("24H %", 110, "center"), ("SMART\nWALLET", 130, "center")]
NAVY_TOP, NAVY_BOT = (8, 12, 38), (14, 18, 55)
GOLD  = (232, 186, 76)
GREEN = (90, 215, 130)
RED   = (255, 105, 120)
TEXT  = (225, 230, 245)

def font(size, bold=True):
    paths = (["fonts/Orbitron-Bold.ttf"] if bold else []) + [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    for p in paths:
        try: return ImageFont.truetype(p, size)
        except Exception: continue
    return ImageFont.load_default()

def usd(v):
    if v is None: return "—"
    if v >= 1e9: return f"${v/1e9:.1f}B"
    if v >= 1e6: return f"${v/1e6:.0f}M"
    if v >= 1e3: return f"${v/1e3:.0f}K"
    return f"${v:.0f}"

def price_fmt(p):
    if p is None: return "—"
    if p >= 1: return f"${p:,.2f}"
    return f"${p:.6f}".rstrip("0")

def pct(v):
    if v is None: return "—", TEXT
    c = GREEN if v >= 0 else RED
    return f"{v:+.1f}%", c

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
            d.text((xpos + w / 2 - tw / 2, y0 + header_h / 2 - (len(lines) - li - 0.5) * 22), ln,
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

# ---------------- 4. Caption ----------------
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
  
Dexscreener 24H trending tokens that are held by smart-money wallets (per GMGN data).
Table (JSON): {data}
Call out the biggest movers (24H %) and which tokens have the most smart-money wallets.
End with a blank line then hashtags: #dexscreener #smartmoney #onchain #crypto plus one #TICKER hashtag per token (use the token symbols, without $)."""
    try:
        cap = llm([{"role": "user", "content": prompt}], max_tokens=700).strip()[:2100]
        idx = cap.find("#")
        if idx == -1:
            return cap + "\n\nData from DEX Screener"
        return cap[:idx].rstrip() + "\n\nData from DEX Screener\n\n" + cap[idx:]
    except Exception:
        return "📊 Top 24H trending tokens held by smart money.\n\nData from DEX Screener\n\n#dexscreener #smartmoney #onchain #crypto"

# ---------------- 5. Publish ----------------
def resolve_ig_user_id():
    r = requests.get(f"{GRAPH}/me", params={"fields": "user_id,username",
                                            "access_token": IG_ACCESS_TOKEN}, timeout=30)
    r.raise_for_status()
    d = r.json()
    log(f"IG token belongs to @{d.get('username')}")
    return str(d["user_id"])

def ig_container(image_url, caption=None, story=False):
    data = {"image_url": image_url, "access_token": IG_ACCESS_TOKEN}
    if story: data["media_type"] = "STORIES"
    if caption: data["caption"] = caption
    r = requests.post(f"{GRAPH}/{IG_USER_ID}/media", data=data, timeout=60)
    if r.status_code == 200: return r.json()["id"]
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

# ---------------- Commands ----------------
def cmd_generate():
    if not GMGN_API_KEY:
        raise RuntimeError("GMGN_API_KEY secret is missing — get a free key at https://gmgn.ai/ai")
    pairs = fetch_dex_pairs()
    if not pairs:
        raise RuntimeError("dexscreener returned 0 usable pairs — check the per-batch logs above")
    rows  = build_rows(pairs)
    os.makedirs("state", exist_ok=True); os.makedirs("images", exist_ok=True)
    json.dump({"date": NOW.strftime("%B %d, %Y"), "rows": rows}, open(STATE_FILE, "w"), ensure_ascii=False, indent=2)
    render_table(rows, "images/trending-story.jpg", 1080, 1920)
    render_table(rows, "images/trending-post.jpg", 1080, 1350)
    notify(f"📊 Trending x Smart Money: {len(rows)} tokens from {min(len(pairs), CANDIDATE_LIMIT)} candidates:\n"
           + "\n".join(f"{i+1}. ${x['token']} ({x['chain']}) — {x['smart_wallets']} SM wallets, {pct_str(x['h24'])} 24H"
                       for i, x in enumerate(rows)))

def cmd_publish():
    global IG_USER_ID
    IG_USER_ID = resolve_ig_user_id()
    st  = json.load(open(STATE_FILE)); rows = st["rows"]
    ts  = int(time.time())
    story_id = ig_publish(ig_container(f"{IMAGE_BASE_URL}/trending-story.jpg?cb={ts}", story=True))
    post_id  = ig_publish(ig_container(f"{IMAGE_BASE_URL}/trending-post.jpg?cb={ts}",
                                       caption=build_caption(rows)))
    notify(f"✅ Trending x Smart Money {st['date']} published! (story {story_id}, post {post_id})")

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "generate"
    try:
        {"generate": cmd_generate, "publish": cmd_publish}[cmd]()
    except Exception:
        notify("❌ Trending/Smart-Money post FAILED:\n" + traceback.format_exc()[-3000:])
        raise
