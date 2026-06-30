# Vouch Tracker Discord Bot

Two modes:

1. **Full channel scan** (`!scanvouches`) — reads through a channel's entire
   message history, totals up every OWO and LTC amount mentioned, and posts
   a "Vouch Scan Complete!" style report (matches the format you showed me),
   now including an INR total too.
2. **Live tracking** — watches new messages going forward and logs them
   per-user so you can pull stats with `!vouchstats`, `!leaderboard`, etc.

## What gets detected

**Scan mode** picks up any number directly followed by `owo` or `ltc`,
anywhere in a message — e.g. `5000 owo`, `+5000owo`, `0.5 ltc`, `0.5LTC`.
It doesn't require any specific phrasing, so it should catch most real
vouch messages as-is.

**Live tracking mode** (optional, for per-user stats) looks for:
```
legit got <amount> <owo|ltc> @<user>
```

If either mode is missing real vouches in your server, send me a few
example messages and I'll tighten the patterns in `bot.py`
(`OWO_AMOUNT_PATTERN` / `LTC_AMOUNT_PATTERN` for scans,
`VOUCH_PATTERN` for live tracking).

## Setup

1. **Create the bot application**
   - Go to https://discord.com/developers/applications → New Application
   - Bot tab → Add Bot → copy the token
   - Under "Privileged Gateway Intents", enable **Message Content Intent**
   - OAuth2 → URL Generator → scopes: `bot`, permissions: `Send Messages`,
     `Read Message History`, `Embed Links` → use the generated URL to invite
     the bot to your server

2. **Get your vouch channel ID** (only needed for live tracking)
   - Discord Settings → Advanced → enable Developer Mode
   - Right-click the vouch channel → Copy Channel ID

3. **Install and configure**
   ```bash
   pip install -r requirements.txt
   cp .env.example .env
   ```
   Edit `.env`:
   ```
   DISCORD_TOKEN=your-bot-token-here
   VOUCH_CHANNEL_ID=your-channel-id-here   # optional, only for live tracking
   ```

4. **Run it**
   ```bash
   python bot.py
   ```

## Commands

| Command | What it does |
|---|---|
| `!scanvouches` | Scans the current channel's full history and posts a totals report |
| `!scanvouches #channel` | Scans a specific channel |
| `!scanvouches #channel 1000` | Scans only the most recent 1000 messages (faster, for very large channels) |
| `!vouchstats @user` | Live-tracked totals for one user (OWO, LTC, INR, vouch count) |
| `!leaderboard` | Live-tracked top 10 users ranked by INR-equivalent value |
| `!serverstats` | Live-tracked server-wide totals |
| `!recentvouches @user` | Last 10 live-tracked vouches for that user |

## How the INR / USD conversion works

- OWO has no real-world value, so it's tracked as a raw count and never converted.
- For `!scanvouches`, the LTC total is converted to both USD and INR using the
  **current live rate** at the moment you run the scan (via CoinGecko) — this
  matches how the report you showed me works (it shows "Total LTC / $").
- For live tracking (`!vouchstats` etc.), each LTC vouch is converted to INR
  using the rate **at the time it was logged**, and that value is stored
  permanently, so historical totals don't shift later.
- Rates are cached for 5 minutes to avoid hitting the CoinGecko API too often.

## Data storage

Live-tracked vouches are saved in a local SQLite file (`vouches.db` by
default), so that data survives bot restarts. `!scanvouches` doesn't store
anything — it recalculates from message history fresh each time you run it.

## Notes / things you may want to adjust

- Large channels (many thousands of messages) can take a little while to
  scan since Discord rate-limits history fetching — for those, pass a
  `limit` to `!scanvouches` to scan only the most recent N messages.
- Want `!scanvouches` to also break totals down per-user (not just channel-wide)?
  Easy to add — just ask.
- Want only staff/admins to be able to run `!scanvouches`? I can add a role check.

