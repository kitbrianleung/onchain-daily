#!/usr/bin/env python3
"""Discord control panel for Onchain Daily — change the agent's config by chatting."""
import os, json, traceback, subprocess
import requests
from agent import load_config, parse_json, llm

DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
CHANNEL_ID    = os.environ["DISCORD_CHANNEL_ID"]
MY_USER_ID    = str(os.environ["DISCORD_USER_ID"])
API           = "https://discord.com/api/v10"
HEADERS       = {"Authorization": f"Bot {DISCORD_TOKEN}"}
CONFIG_FILE   = "config.json"
BACKUP_FILE   = "state/config_backup.json"
STATE_FILE    = "state/bot_state.json"

SYSTEM = """You are the configuration manager for "Onchain Daily", an automated crypto-news Instagram agent. The owner chats with you on Discord to change how the agent behaves.

CURRENT CONFIG (JSON):
__CONFIG__

CONFIG SCHEMA:
- "criteria": string — the complete instruction text the news-picking AI follows (goals, focus, exclusions, formatting rules).
- "rss_feeds": object of name -> RSS feed URL.
- "scrape_pages": object of name -> webpage URL (for sites without RSS; an AI reads the page and extracts news).
- "model": string — OpenRouter model id.
- "caption_hashtags": string — hashtags appended to the Instagram caption.

YOUR JOB:
- If the owner asks to CHANGE something (add/remove a website, change focus, add/remove a requirement, change model or hashtags...), return the COMPLETE new config with every key, plus a short summary of what changed.
- If the owner asks a QUESTION or just chats, reply helpfully without changing anything.

RULES:
- New website: if the URL contains "rss", "feed", or ends in ".xml", put it in "rss_feeds"; otherwise put it in "scrape_pages". Use a short sensible name as the key.
- When rewriting "criteria", keep every existing rule the owner didn't ask to change (especially: exactly 5 stories, different token/protocol each, $TICKER formatting, English-only, no URL reuse, today/yesterday only).
- Copy URLs exactly as given. Never invent URLs.
- Never ask for or mention API keys — those are stored separately.

RESPOND WITH ONLY ONE OF THESE JSON OBJECTS:
{"config": {...complete new config...}, "summary": "short plain-English summary of the changes"}
or
{"message": "your reply to the owner"}"""

HELP = """🤖 **Onchain Daily control bot**

Just tell me in plain English what to change, e.g.:
• "Add https://example.com/news to my sources"
• "Focus more on memecoins and airdrop news"
• "Remove The Block from my sources"
• "Stop covering hacks"
• "What are my current sources?"

Commands (plain text, not slash commands):
`!config` — show current settings
`!undo` — revert the last change
`!help` — this message"""

def send(text):
    for i in range(0, len(text), 1900):
        requests.post(f"{API}/channels/{CHANNEL_ID}/messages",
                      headers=HEADERS, json={"content": text[i:i + 1900]}, timeout=30)

def load_last_id():
    try: return str(json.load(open(STATE_FILE))["last_id"])
    except Exception: return "0"

def save_last_id(mid):
    os.makedirs("state", exist_ok=True)
    json.dump({"last_id": str(mid)}, open(STATE_FILE, "w"))

def git_push(msg):
    subprocess.run(["git", "config", "user.name", "onchain-bot"], check=True)
    subprocess.run(["git", "config", "user.email", "bot@users.noreply.github.com"], check=True)
    subprocess.run(["git", "add", CONFIG_FILE, BACKUP_FILE], check=True)
    subprocess.run(["git", "commit", "-m", msg], check=True)
    subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=True)
    subprocess.run(["git", "push"], check=True)

def validate(cfg):
    assert isinstance(cfg.get("criteria"), str) and len(cfg["criteria"]) > 50, "criteria missing/too short"
    for key in ("rss_feeds", "scrape_pages"):
        assert isinstance(cfg.get(key), dict), f"{key} must be an object"
        for name, url in cfg[key].items():
            assert isinstance(name, str) and str(url).startswith("http"), f"bad entry: {name}"

def apply_config(new_cfg):
    validate(new_cfg)
    old = load_config()
    os.makedirs("state", exist_ok=True)
    json.dump(old, open(BACKUP_FILE, "w"), ensure_ascii=False, indent=2)
    json.dump(new_cfg, open(CONFIG_FILE, "w"), ensure_ascii=False, indent=2)
    git_push("config update via discord")

def show_config():
    cfg = load_config()
    lines = ["⚙️ **Current Onchain Daily config**", "", "📰 **RSS feeds:**"]
    lines += [f"• {k}: {v}" for k, v in cfg["rss_feeds"].items() if v]
    lines += ["", "🌐 **Scraped pages:**"]
    lines += [f"• {k}: {v}" for k, v in cfg["scrape_pages"].items() if v]
    lines += ["", f"🧠 **Model:** {cfg['model']}", f"#️⃣ **Hashtags:** {cfg['caption_hashtags']}",
              "", "📋 **Criteria:**", cfg["criteria"]]
    return "\n".join(lines)

def undo():
    if not os.path.exists(BACKUP_FILE):
        return "Nothing to undo yet."
    json.dump(json.load(open(BACKUP_FILE)), open(CONFIG_FILE, "w"), ensure_ascii=False, indent=2)
    git_push("config undo via discord")
    return "↩️ Reverted to the previous config."

def handle(text):
    low = text.strip().lower()
    if low.startswith(("!help", "!start")): return HELP
    if low.startswith("!config"): return show_config()
    if low.startswith("!undo"):   return undo()
    if low.startswith("!ping"):   return "pong 🏓"
    cfg = load_config()
    prompt = SYSTEM.replace("__CONFIG__", json.dumps(cfg, ensure_ascii=False, indent=2))
    out = llm([{"role": "system", "content": prompt},
               {"role": "user", "content": text}], max_tokens=12000, temperature=0.2)
    data = parse_json(out)
    if isinstance(data, dict) and "config" in data:
        apply_config(data["config"])
        return ("✅ Done! " + data.get("summary", "Config updated.")
                + "\n\nApplies from the next daily run. Send `!undo` to revert.")
    if isinstance(data, dict) and "message" in data:
        return data["message"]
    return "🤔 I couldn't understand that. Try rephrasing, or send `!help`."

def main():
    first_run = not os.path.exists(STATE_FILE)
    last_id = load_last_id()
    r = requests.get(f"{API}/channels/{CHANNEL_ID}/messages",
                     headers=HEADERS, params={"after": last_id, "limit": 50}, timeout=30)
    r.raise_for_status()
    msgs = r.json()  # newest first
    if not msgs:
        print("no new messages"); return
    newest = str(msgs[0]["id"])
    if first_run:
        # Don't reply to old channel history on the very first run
        save_last_id(newest)
        print("initialized; skipping history"); return
    for m in reversed(msgs):  # oldest first
        author = m.get("author", {})
        if author.get("bot"):
            continue                       # ignore ourselves / other bots
        if str(author.get("id")) != MY_USER_ID:
            send(f"⛔ <@{author.get('id')}> This is a private bot.")
            continue
        text = (m.get("content") or "").strip()
        if not text:
            continue
        try:
            send(handle(text))
        except Exception:
            send("❌ Something went wrong:\n" + traceback.format_exc()[-1200:])
    save_last_id(newest)

if __name__ == "__main__":
    main()
