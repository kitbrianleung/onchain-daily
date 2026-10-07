# Onchain Daily 🌐📲

An automated crypto media pipeline that **finds** the news, **writes** the analysis, **draws** the graphics, and **posts** to Instagram — three times a day, with zero human input.

Every post is rendered locally with [Pillow](https://python-pillow.org/) and published through the **Instagram Graph API**. The whole thing runs on **GitHub Actions** (no servers, no hosting bills), keeps its memory in `state/*.json` inside the repo, and can be re-configured by chatting with a **Discord bot** in plain English.

![Onchain Daily](https://github.com/kitbrianleung/onchain-daily/actions/workflows/daily.yml/badge.svg)
![Trending x Smart Money](https://github.com/kitbrianleung/onchain-daily/actions/workflows/trending.yml/badge.svg)
![FOMO Leaderboards](https://github.com/kitbrianleung/onchain-daily/actions/workflows/fomo.yml/badge.svg)
![Discord bot](https://github.com/kitbrianleung/onchain-daily/actions/workflows/bot.yml/badge.svg)
![Token refresh](https://github.com/kitbrianleung/onchain-daily/actions/workflows/refresh.yml/badge.svg)

---

## What it posts

| Time (Hong Kong) | Workflow | Content |
|---|---|---|
| 08:00 | `daily.yml` | **Onchain Daily Wrap #1** — 5 curated altcoin/DeFi stories (last 48h) |
| 12:00 | `trending.yml` | **Trending x Smart Money** — top 10 low-cap trending tokens + GMGN smart-money wallet counts |
| 12:30 | `fomo.yml` | **FOMO Leaderboards** — Most Held + Trending tokens from [fomoapi.io](https://fomoapi.io/docs) with written analysis |
| 20:00 | `daily.yml` | **Onchain Daily Wrap #2** — 5 curated stories (same-day only) |
| every 5 min | `bot.yml` | Listens for Discord commands to re-configure the agent |
| 08:00 | `refresh.yml` | Refreshes the long-lived Instagram token and writes it back to repo secrets |

Each pipeline produces a **1080×1920 story** and a **1080×1350 feed post**. The story is a clean graphic; the analysis/links live in the feed post's caption.

---

## How it works

```
                 ┌─────────────────────────────────────────────┐
  RSS + news     │  agent.py                                   │
  sites  ───────►│  fetch → dedupe → LLM picks top 5 → Pillow  │──► images/story.jpg
  (8 sources)    │  render → commit to repo → IG Graph API     │    images/post.jpg
                 └─────────────────────────────────────────────┘
                 ┌─────────────────────────────────────────────┐
  Dexscreener    │  trending.py                                │
  boosts ───────►│  filter (<$2M mcap) → GMGN smart money →    │──► images/trending/*.jpg
                 │  Pillow table → IG story + feed post        │
                 └─────────────────────────────────────────────┘
                 ┌─────────────────────────────────────────────┐
  fomoapi.io     │  fomo_tables.py                             │
  leaderboards ─►│  2 tables → Pillow render → analysis in     │──► images/fomo/*.jpg
                 │  caption → IG story + feed post             │
                 └─────────────────────────────────────────────┘
```

**The publish trick:** Instagram cannot accept an uploaded file — it needs a **public URL**. So every run commits its images to `images/` with `GITHUB_TOKEN`, polls `raw.githubusercontent.com` until the file is live (`wait_for_raw`), then creates an IG media container from that URL and publishes it.

**The two-step CLI:** every pipeline runs as `generate` (fetch → render → push images + state) then `publish` (create + publish IG containers). Splitting them means a failed publish never forces a re-scrape, and state files (`state/news.json`, `state/trending.json`, `state/fomo.json`) are the hand-off between the two steps.

---

## Repository layout

| Path | Purpose |
|---|---|
| `agent.py` | News pipeline: fetch → LLM picks top 5 → render → Instagram → Discord |
| `trending.py` | Trending x Smart Money table (Dexscreener + `gmgn-cli`) |
| `fomo_tables.py` | FOMO Leaderboards (two tables + written analysis) |
| `bot.py` | Discord control panel — edits `config.json` by natural language |
| `refresh_token.py` | Refreshes `IG_ACCESS_TOKEN` (60-day long-lived token) |
| `config.json` | Live agent config: criteria, sources, model, hashtags (bot-managed) |
| `.github/workflows/` | `daily.yml`, `trending.yml`, `fomo.yml`, `bot.yml`, `refresh.yml` |
| `images/` | Rendered JPEGs, served publicly via `raw.githubusercontent.com` |
| `state/` | Persisted memory: used URLs, last Discord message ID, per-day payloads |
| `requirements.txt` | `requests`, `feedparser`, `beautifulsoup4`, `pillow`, `PyNaCl`, `playwright` |

---

## Setup

### 1. Instagram (Business or Creator account)

1. Create a Meta app → add the **Instagram** product → *API setup with Instagram business login*.
2. Assign yourself the **Instagram Tester** role, accept the invite, then generate a token for the account you want to post to.
3. Convert it to a **long-lived token** and confirm which account it belongs to:

```bash
curl "https://graph.instagram.com/v23.0/me?fields=user_id,username&access_token=YOUR_TOKEN"
```

The `user_id` that comes back **is** your `IG_USER_ID`. `agent.py` re-derives it on every publish (`resolve_ig_user_id`) so a mismatch can't silently break you.

### 2. Repository secrets

`Settings → Secrets and variables → Actions → New repository secret`

| Secret | Required by | Notes |
|---|---|---|
| `IG_ACCESS_TOKEN` | all publishers | Long-lived IG token; rotated by `refresh.yml` |
| `IG_USER_ID` | all publishers | The `user_id` from the `/me` call above |
| `OPENROUTER_API_KEY` | `agent.py`, `bot.py`, `trending.py` | [openrouter.ai](https://openrouter.ai) key for LLM calls |
| `GMGN_API_KEY` | `trending.py` | [gmgn.ai/ai](https://gmgn.ai/ai) — **no IP whitelist** (Actions IPs rotate) |
| `FOMO_API_KEY` | `fomo_tables.py` | [fomoapi.io](https://fomoapi.io/docs) API key |
| `GH_PAT` | `refresh_token.py` | PAT with **repo** + **Secrets: read/write** to update `IG_ACCESS_TOKEN` |
| `DISCORD_BOT_TOKEN` | `bot.py` | Bot token from the Discord Developer Portal |
| `DISCORD_CHANNEL_ID` | `bot.py` | Channel the bot listens in |
| `DISCORD_USER_ID` | `bot.py` | *Your* Discord user ID (only you may command the bot) |
| `DISCORD_WEBHOOK_URL` | `trending.py`, `fomo_tables.py` | Optional: webhook for success/failure notifications |
| `PANEWS_RSS` | `agent.py` | Optional extra feed |
| `BLOCKBEATS_API_KEY` | `agent.py` | Optional — free endpoint is used if unset |

> ⚠️ **Paste tokens with no trailing newline.** A newline pasted into `IG_ACCESS_TOKEN` is the single most common cause of `Cannot parse access token` (see Troubleshooting).

### 3. Workflow permissions

Each workflow declares `permissions: contents: write` so the runner can commit images and state back to `main`. Leave it in place.

### 4. Local run

```bash
pip install -r requirements.txt
npm install -g gmgn-cli                 # only needed for trending.py

export OPENROUTER_API_KEY=...  IG_ACCESS_TOKEN=...  IG_USER_ID=...
export GMGN_API_KEY=...        FOMO_API_KEY=...

python agent.py generate && python agent.py publish
python trending.py generate && python trending.py publish
python fomo_tables.py generate && python fomo_tables.py publish
```

---

## Configuration (`config.json`)

`bot.py` rewrites this file when you ask it to, and commits the change.

| Key | Meaning |
|---|---|
| `criteria` | The full instruction block the picking model follows (goals, exclusions, formatting, hashtag rules) |
| `rss_feeds` | `name → RSS URL` sources |
| `scrape_pages` | `name → URL` for sites without RSS (an LLM reads the page and extracts items) |
| `model` | OpenRouter model id used for curation |
| `caption_hashtags` | Base hashtags prepended to token hashtags in the caption |

### Discord commands

```
!config   show current settings
!undo     revert the last change
!help     usage
```

Or just talk to it: *"Add https://example.com/news to my sources"*, *"Focus more on memecoins"*, *"Stop covering hacks"*.

---

## Troubleshooting

<details>
<summary><strong><code>Invalid OAuth access token - Cannot parse access token</code> (code 190)</strong></summary>

`IG_ACCESS_TOKEN` is empty, expired, truncated, or contains whitespace/a newline. Fixes:
1. Re-mint a long-lived token and re-paste it (no trailing newline).
2. Make sure `refresh.yml` is passing — it rotates the secret every day at 00:00 UTC and is what keeps a 60-day token alive indefinitely.
3. Confirm the secret is passed into the job: every workflow needs `IG_ACCESS_TOKEN: ${{ secrets.IG_ACCESS_TOKEN }}` under `env:`.

</details>

<details>
<summary><strong><code>Object with ID "…" does not exist, cannot be loaded due to missing permissions</code> (code 100, subcode 33)</strong></summary>

The token and `IG_USER_ID` point at **different accounts**. This is very common when mixing the two Instagram APIs: `graph.instagram.com` (Instagram Login) uses the **IG-scoped** user id, while `graph.facebook.com` uses the **Page-linked** id. Fix:

```bash
curl "https://graph.instagram.com/v23.0/me?fields=user_id,username&access_token=YOUR_TOKEN"
```

Set `IG_USER_ID` to the returned `user_id`. Also confirm the target account is **Business or Creator** and that the app has the token for that account.

</details>

<details>
<summary><strong><code>Missing env vars: FOMO_API_KEY, IG_ACCESS_TOKEN, …</code></strong></summary>

The script reads `os.environ[...]`, so the name must match **exactly** and must be forwarded by the workflow's `env:` block. Check spelling and case (`FOMO_API_KEY`, not `FOMO_KEY`), then re-run. Secrets added after a run are not injected into that run.

</details>

<details>
<summary><strong>LLM returns empty content / no stories found</strong></summary>

Reasoning models can spend the whole token budget "thinking". The workflows set `REASONING_EFFORT: low`; keep it, or switch `model` in `config.json` to a non-reasoning model.

</details>

<details>
<summary><strong>GMGN <code>AUTH_IP_BLOCKED</code> / <code>AUTH_KEY_INVALID</code></strong></summary>

Remove the IP whitelist from your GMGN key — GitHub Actions runner IPs change on every run, so whitelisting can never work. An invalid key makes `trending.py` fail fast instead of retrying 10 times.

</details>

<details>
<summary><strong>Images render but Instagram can't fetch them</strong></summary>

Instagram downloads the image from `raw.githubusercontent.com`, so the repo must be **public** and the push must have succeeded. If a run renders images locally but never publishes, check the commit step and the `wait_for_raw` polling in the logs.

</details>

---

## Design notes

- **Two-step jobs** (`generate` / `publish`) keep scraping and publishing independent.
- **State in git** replaces a database: `state/posted.json` (last 600 URLs, prevents repeats), `state/bot_state.json` (Discord cursor), `state/{news,trending,fomo}.json` (today's payload between steps).
- **Fail loudly, twice:** every script wraps `main` in a `try/except` that pushes the traceback to Discord before re-raising, so the Actions failure and the Discord ping agree.
- **Idempotent by date:** publishers refuse to post if the state file's date isn't today — a re-run can't double-post yesterday's graphic.

## Disclaimer

Not financial advice. Market data comes from Dexscreener, GMGN and FOMO; news comes from public sources. Always verify before acting.

## License

MIT
