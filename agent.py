#!/usr/bin/env python3
"""Onchain Daily: fetch altcoin news -> Kimi picks top 5 -> render template -> post to IG -> Discord."""
import os, re, json, sys, time, datetime, traceback
import requests
import feedparser
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ---------------- Environment (from GitHub Secrets) ----------------
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
IG_ACCESS_TOKEN    = os.environ.get("IG_ACCESS_TOKEN", "")
IG_USER_ID         = os.environ.get("IG_USER_ID", "")
IG_API_VERSION     = os.environ.get("IG_API_VERSION", "v23.0")
GRAPH              = f"https://graph.instagram.com/{IG_API_VERSION}"
DISCORD_TOKEN      = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL    = os.environ.get("DISCORD_CHANNEL_ID", "")
IMAGE_BASE_URL     = os.environ.get("IMAGE_BASE_URL", "").rstrip("/")
NOW                = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)  # HK time
STATE_FILE         = "state/posted.json"
NEWS_FILE          = "state/news.json"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}

# ---------------- Editable config (managed via the Discord bot) ----------------
DEFAULT_CONFIG = {
    "criteria": """You curate "Onchain Daily", a crypto altcoin news digest for Instagram.
Pick the 5 most interesting stories from the CANDIDATES using ALL of these rules:
- GOAL: (a) discover NEW altcoin launches, preferably before/early surge; (b) big altcoin-ecosystem
  events: new chain/protocol launches gaining attention, new DeFi use cases (like the Ethena Pay app),
  sudden DEX volume surges, hacks/exploits, hot surging memecoins; (c) whales accumulating niche
  altcoins or building sizable leveraged positions.
- EXCLUDE stories mainly about USDT, XRP, USDC, TRON. Focus on altcoins & DeFi.
- PREFER smaller-but-growing tokens (e.g. PONS, CASHCAT) over large caps like SOL or AAVE.
- Only items from today or yesterday. NEVER reuse a URL from the PREVIOUSLY USED list.
- Each of the 5 stories must feature a DIFFERENT token/protocol.
Return ONLY a JSON array of exactly 5 objects, best first:
[{"headline":"...", "summary":"...", "url":"...", "token":"$TICKER"}]
TEXT RULES:
- headline: max 60 chars, catchy, one line, MUST mention the token with a $ prefix (e.g. $PONS, $DOGE, $AAVE).
- summary: 1-2 short sentences (max 240 chars), use $TICKER for any token mentioned.
- English only, unless the token name itself is Chinese (e.g. $未来).""",
    "rss_feeds": {
        "The Block":   "https://www.theblock.co/rss.xml",
        "The Defiant": "https://thedefiant.io/feed/",
    },
    "scrape_pages": {
        "PANews Newsflash":     "https://www.panewslab.com/zh-hant/newsflash",
        "PANews EN Newsflash":  "https://www.panews.io/newsflash",
        "The Defiant Converge": "https://thedefiant.io/converge/blockchains",
        "The Block Web3":       "https://www.theblock.co/news/web3",
        "The Block DeFi":       "https://www.theblock.co/news/defi",
        "BlockBeats Newsflash": "https://en.theblockbeats.news/newsflash",
        "CryptoNews DeFi":      "https://cryptonews.net/news/defi/",
        "CryptoNews Altcoins":  "https://cryptonews.net/news/altcoins/",
    },
    "model": "moonshotai/kimi-k3",
    "caption_hashtags": "#altcoins #defi #onchain #crypto #alpha",
}

def load_config():
    """Effective config = defaults, overridden by config.json (written by the Discord bot)."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open("config.json") as f:
            cfg.update({k: v for k, v in json.load(f).items() if k in DEFAULT_CONFIG})
    except FileNotFoundError:
        pass
    if os.environ.get("PANEWS_RSS"):
        cfg["rss_feeds"].setdefault("PANews", os.environ["PANEWS_RSS"])
    return cfg

CFG          = load_config()
CRITERIA     = CFG["criteria"]
RSS_FEEDS    = CFG["rss_feeds"]
SCRAPE_PAGES = CFG["scrape_pages"]
MODEL        = os.environ.get("MODEL", CFG["model"])
HASHTAGS     = CFG["caption_hashtags"]

def log(m): print(m, flush=True)

def notify(msg):
    """Post to Discord (2000-char limit -> send in chunks)."""
    log(msg)
    if not (DISCORD_TOKEN and DISCORD_CHANNEL):
        return
    for i in range(0, len(msg), 1900):
        try:
            requests.post(
                f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL}/messages",
                headers={"Authorization": f"Bot {DISCORD_TOKEN}"},
                json={"content": msg[i:i + 1900]}, timeout=30)
        except Exception as e:
            log(f"discord notify failed: {e}")

def llm(messages, max_tokens=8000, temperature=0.3):
    """Call the model via OpenRouter.
    Retries once with double the token budget if the model returns empty
    content (common with reasoning models that spend the budget 'thinking')."""
    payload = {"model": MODEL, "messages": messages, "temperature": temperature}
    effort = os.environ.get("REASONING_EFFORT", "")
    if effort:
        payload["reasoning"] = {"effort": effort}
    finish = None
    for attempt in range(2):
        payload["max_tokens"] = max_tokens * (attempt + 1)
        r = requests.post("https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "X-Title": "OnchainDaily"},
            json=payload, timeout=300)
        r.raise_for_status()
        data = r.json()
        if "error" in data:                       # OpenRouter sometimes returns 200 + error body
            raise RuntimeError(f"OpenRouter error: {data['error']}")
        choice = data["choices"][0]
        msg = choice.get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):             # some providers return content as parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        finish = choice.get("finish_reason")
        log(f"llm: finish={finish} usage={data.get('usage')}")
        if content and content.strip():
            return content
        log(f"llm: empty content on attempt {attempt + 1} (finish={finish})")
    raise RuntimeError(
        f"Model returned empty content twice (finish_reason={finish}). "
        "Set REASONING_EFFORT=low in the workflow env or pick a non-reasoning model.")

def parse_json(text):
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m: text = m.group(1)
    start = min([i for i in (text.find("["), text.find("{")) if i != -1], default=-1)
    if start == -1: raise ValueError("no JSON in LLM output")
    for end in range(len(text), start, -1):
        try: return json.loads(text[start:end])
        except Exception: pass
    raise ValueError("unparseable JSON")

def norm_url(u):
    u = re.sub(r"^https?://(www\.)?", "", u.strip().lower())
    return u.split("?")[0].rstrip("/")

def load_state():
    try: return json.load(open(STATE_FILE))
    except Exception: return {"used_urls": []}
def save_state(s):
    os.makedirs("state", exist_ok=True)
    json.dump(s, open(STATE_FILE, "w"), indent=1)

def get_cutoff():
    """Morning run (before noon HKT): last 48h. Evening run: today only (since midnight HKT)."""
    if os.environ.get("WINDOW_HOURS"):
        return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=float(os.environ["WINDOW_HOURS"]))
    if NOW.hour < 12:
        return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=48)
    return NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    
# ---------------- 1. Fetch news ----------------
def fetch_rss(name, url, cutoff):
    out = []
    try:
        feed = feedparser.parse(url, request_headers=UA)
        for e in feed.entries[:40]:
            pub = None
            for k in ("published_parsed", "updated_parsed"):
                v = getattr(e, k, None)
                if v: pub = datetime.datetime(*v[:6], tzinfo=datetime.timezone.utc); break
            if pub and pub < cutoff: continue
            hint = BeautifulSoup(e.get("summary", ""), "html.parser").get_text()[:300]
            out.append({"source": name, "title": e.get("title", "").strip(),
                        "url": e.get("link", ""), "time": pub.isoformat() if pub else "",
                        "summary_hint": hint})
    except Exception as ex: log(f"RSS {name} failed: {ex}")
    return out

def fetch_blockbeats(cutoff):
    out, key = [], os.environ.get("BLOCKBEATS_API_KEY", "")
    try:
        if key:
            r = requests.get("https://api-pro.theblockbeats.info/v1/newsflash",
                             headers={"api-key": key, **UA},
                             params={"page": 1, "size": 50, "lang": "en"}, timeout=30)
            d = r.json().get("data", [])
            rows = d if isinstance(d, list) else d.get("data") or d.get("list") or []
        else:
            r = requests.get("https://api.theblockbeats.news/v1/open-api/open-flash",
                             headers=UA, params={"page": 1, "size": 50, "lang": "en"}, timeout=30)
            d = r.json().get("data", {})
            rows = d.get("list") if isinstance(d, dict) else d
        for it in (rows or []):
            t, ts = it.get("create_time") or it.get("add_time") or "", None
            if isinstance(t, (int, float)):
                ts = datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
            else:
                try: ts = datetime.datetime.strptime(str(t)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc)
                except Exception: pass
            if ts and ts < cutoff: continue
            link = it.get("url") or it.get("link") or ""
            if link.startswith("/"): link = "https://www.theblockbeats.info" + link
            title = it.get("title", "").strip()
            if title:
                out.append({"source": "BlockBeats", "title": title, "url": link,
                            "time": ts.isoformat() if ts else "",
                            "summary_hint": BeautifulSoup(it.get("content", ""), "html.parser").get_text()[:300]})
    except Exception as ex: log(f"BlockBeats failed: {ex}")
    return out

def fetch_page_text(url, limit=16000):
    try:
        r = requests.get(url, headers=UA, timeout=40)
        if r.status_code != 200: log(f"{url} -> HTTP {r.status_code}"); return ""
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup.find_all(["style", "noscript", "svg", "footer", "nav", "header", "form", "aside"]):
            tag.decompose()
        nd = soup.find("script", id="__NEXT_DATA__")
        nd_json = nd.string[:8000] if nd and nd.string else ""
        for tag in soup.find_all("script"): tag.decompose()
        text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
        if nd_json: text += "\nJSON_DATA: " + nd_json
        return text[:limit]
    except Exception as ex: log(f"page {url} failed: {ex}"); return ""

def extract_with_llm(source, url, text):
    prompt = f"""Current date/time: {NOW:%Y-%m-%d %H:%M} (UTC+8).
Below is raw text from the crypto news page "{source}" ({url}).
Extract up to 15 distinct crypto news items published today or yesterday.
Return ONLY a JSON array:
[{{"title":"...","url":"https://...","time":"YYYY-MM-DD HH:MM or ''","summary_hint":"one short sentence"}}]
Rules: absolute URLs only; skip ads/navigation/sponsored/price-prediction filler.
PAGE TEXT:
{text}"""
    try:
        arr = parse_json(llm([{"role": "user", "content": prompt}], max_tokens=8000))
        return [{"source": source, **it} for it in arr if str(it.get("url", "")).startswith("http")]
    except Exception as ex: log(f"extract {source} failed: {ex}"); return []

# ---------------- 2. Pick top 5 ----------------
def pick_top5(cands, used_urls):
    listing = json.dumps(cands, ensure_ascii=False)[:22000]
    prompt = f"""{CRITERIA}

Current date: {NOW:%Y-%m-%d} (UTC+8).
PREVIOUSLY USED URLs (never reuse):
{chr(10).join(used_urls[-300:])}

CANDIDATES (JSON):
{listing}"""
    items = parse_json(llm([{"role": "user", "content": prompt}], max_tokens=8000, temperature=0.4))
    items = [i for i in items if i.get("headline") and str(i.get("url", "")).startswith("http")]
    seen, out = set(), []
    for i in items:
        k = norm_url(i["url"])
        if k not in seen: seen.add(k); out.append(i)
    return out[:5]

# ---------------- 3. Render the neon template ----------------
NEON = [(0, 229, 255), (255, 64, 200), (153, 102, 255), (77, 148, 255), (255, 102, 178)]

def font(size, bold=True):
    for p in (["fonts/Orbitron-Bold.ttf"] if bold else []) + [
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]:
        try: return ImageFont.truetype(p, size)
        except Exception: continue
    return ImageFont.load_default()

def wrap_px(text, fnt, maxw, draw):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=fnt) <= maxw: cur = t
        else: lines.append(cur); cur = w
    if cur: lines.append(cur)
    return lines

def render(items, path, W, H):
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    top, bot = (10, 12, 40), (36, 16, 70)
    for y in range(H):
        t = y / max(1, H - 1)
        d.line([(0, y), (W, y)], fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    margin, date_f = 64, font(44)
    date_txt = f"{NOW:%d %b %Y}".upper()
    tw = d.textlength(date_txt, font=date_f)
    d.text(((W - tw) / 2, 70), date_txt, font=date_f, fill=(170, 190, 255))
    title, tf = "ONCHAIN DAILY", font(96 if H > 1500 else 64)
    while d.textlength(title, font=tf) > W - 2 * margin:
        tf = font(tf.size - 4)
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0)); g = ImageDraw.Draw(glow)
    tw = d.textlength(title, font=tf)
    g.text(((W - tw) / 2, 130), title, font=tf, fill=(0, 229, 255, 255))
    glow = glow.filter(ImageFilter.GaussianBlur(10))
    img.paste(Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB"), (0, 0))
    d = ImageDraw.Draw(img)
    d.text(((W - tw) / 2, 130), title, font=tf, fill=(235, 255, 255))
    n = len(items)
    if H > 1500: y0, bh, gap, hf, nf = 350, 225, 45, font(42), font(64)
    else:        y0, bh, gap, hf, nf = 280, 165, 25, font(34), font(48)
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0)); o = ImageDraw.Draw(overlay)
    for i, it in enumerate(items):
        y = y0 + i * (bh + gap); c = NEON[i % len(NEON)]
        o.rounded_rectangle([margin, y, W - margin, y + bh], radius=26,
                            fill=(16, 20, 44, 210), outline=c, width=3)
        o.text((margin + 30, y + bh / 2 - nf.size * 0.7), f"{i+1}.", font=nf, fill=c)
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    d = ImageDraw.Draw(img)
    for i, it in enumerate(items):
        y = y0 + i * (bh + gap)
        lines = wrap_px(it["headline"], hf, W - 2 * margin - 150, d)[:3]
        ty = y + (bh - len(lines) * (hf.size + 8)) / 2
        for ln in lines:
            d.text((margin + 130, ty), ln, font=hf, fill=(230, 235, 255)); ty += hf.size + 8
    foot, ff = "More details in BIO", font(40)
    fw = d.textlength(foot, font=ff)
    d.rounded_rectangle([(W - fw) / 2 - 30, H - 120, (W + fw) / 2 + 30, H - 55],
                        radius=24, outline=(0, 229, 255), width=3)
    d.text(((W - fw) / 2, H - 108), foot, font=ff, fill=(200, 230, 255))
    img.save(path, "JPEG", quality=92)
    log(f"rendered {path}")

# ---------------- 4. Publish to Instagram ----------------
def ig_container(image_url, caption=None, story=False):
    data = {"image_url": image_url, "access_token": IG_ACCESS_TOKEN}
    if story: data["media_type"] = "STORIES"
    if caption: data["caption"] = caption
    last = ""
    for attempt in range(4):
        r = requests.post(f"{GRAPH}/{IG_USER_ID}/media", data=data, timeout=60)
        if r.status_code == 200: return r.json()["id"]
        last = f"{r.status_code}: {r.text[:400]}"; time.sleep(20)
    raise RuntimeError(f"IG container failed: {last}")

def ig_publish(cid):
    last = ""
    for attempt in range(6):
        r = requests.post(f"{GRAPH}/{IG_USER_ID}/media_publish",
                          data={"creation_id": cid, "access_token": IG_ACCESS_TOKEN}, timeout=60)
        if r.status_code == 200: return r.json()["id"]
        last = f"{r.status_code}: {r.text[:400]}"; time.sleep(10)
    raise RuntimeError(f"IG publish failed: {last}")

def build_caption(items):
    lines = [f"📡 ONCHAIN DAILY — {NOW:%d %b %Y}".upper(), ""]
    for i, it in enumerate(items):
        lines += [f"{i+1}. {it['summary']}", f"🔗 {it['url']}", ""]
    lines.append(HASHTAGS)
    return "\n".join(lines)[:2100]

def resolve_ig_user_id():
    """Ask Instagram which account this token belongs to (prevents wrong-ID errors)."""
    r = requests.get(f"{GRAPH}/me", params={"fields": "user_id,username",
                                            "access_token": IG_ACCESS_TOKEN}, timeout=30)
    r.raise_for_status()
    d = r.json()
    log(f"IG token belongs to @{d.get('username')} (user_id={d.get('user_id')})")
    if str(d.get("user_id")) != str(IG_USER_ID):
        log(f"NOTE: IG_USER_ID secret ({IG_USER_ID}) differs from token's account ({d.get('user_id')}) — using the token's account.")
    return str(d["user_id"])

# ---------------- Commands ----------------
def cmd_generate():
    cutoff = get_cutoff()
    log(f"news window: since {cutoff:%Y-%m-%d %H:%M} UTC ({'morning/48h' if NOW.hour < 12 else 'evening/today-only'} run)")
    cands = []
    for name, url in RSS_FEEDS.items():
        if url: cands += fetch_rss(name, url, cutoff)
    cands += fetch_blockbeats(cutoff)
    for name, url in SCRAPE_PAGES.items():
        t = fetch_page_text(url)
        if t: cands += extract_with_llm(name, url, t)
    uniq = {}
    for c in cands:
        k = norm_url(c.get("url", ""))
        if k and k not in uniq: uniq[k] = c
    st = load_state()
    used = {norm_url(u) for u in st["used_urls"]}
    fresh = [c for c in uniq.values() if norm_url(c["url"]) not in used]
    if len(fresh) < 5: fresh = list(uniq.values())
    items = pick_top5(fresh, st["used_urls"])
    if len(items) < 5: raise RuntimeError(f"only {len(items)} stories found from {len(cands)} candidates")
    os.makedirs("state", exist_ok=True); os.makedirs("images", exist_ok=True)
    json.dump({"date": f"{NOW:%d %b %Y}".upper(), "items": items},
              open(NEWS_FILE, "w"), ensure_ascii=False, indent=2)
    render(items, "images/story.jpg", 1080, 1920)
    render(items, "images/post.jpg", 1080, 1350)
    notify(f"🛠 Onchain Daily: picked {len(items)} stories from {len(cands)} candidates:\n"
           + "\n".join(f"{i+1}. {x['headline']}" for i, x in enumerate(items)))

def cmd_publish():
    global IG_USER_ID
    IG_USER_ID = resolve_ig_user_id()
    news = json.load(open(NEWS_FILE)); items = news["items"]
    ts = int(time.time())
    story_id = ig_publish(ig_container(f"{IMAGE_BASE_URL}/story.jpg?cb={ts}", story=True))
    post_id  = ig_publish(ig_container(f"{IMAGE_BASE_URL}/post.jpg?cb={ts}",
                                       caption=build_caption(items)))
    st = load_state()
    st["used_urls"] = (st["used_urls"] + [i["url"] for i in items])[-600:]
    save_state(st)
    notify(f"✅ ONCHAIN DAILY {news['date']} published! (story {story_id}, post {post_id})\n\n"
           + "\n".join(f"{i+1}. {x['headline']}\n{x['url']}" for i, x in enumerate(items)))

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "generate"
    try:
        {"generate": cmd_generate, "publish": cmd_publish}[cmd]()
    except Exception:
        notify("❌ Onchain Daily FAILED:\n" + traceback.format_exc()[-3500:])
        raise
