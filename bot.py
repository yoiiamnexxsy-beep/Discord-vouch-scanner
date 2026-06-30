"""
Vouch Tracker Discord Bot
-------------------------
Two ways to use this bot:

1. LIVE TRACKING (per-message, going forward)
   Watches your vouch channel for messages like:
       legit got <amount> <owo|ltc> <@username>
   and logs each one to a database so you can pull per-user stats later
   with !vouchstats / !leaderboard / !serverstats.

2. FULL CHANNEL SCAN ("Vouch Scan Complete" style report)
   Run !scanvouches in (or pointed at) a channel and the bot will read
   through the channel's entire message history, add up every OWO amount
   and every LTC amount it finds, convert the LTC total to USD and INR
   using a live rate, and post a summary embed — the same style as:

       Vouch Scan Complete!
       Channel: #proof
       Messages Scanned: 533
       Total OWO: 48,005,137
       Total LTC: 1.0123  (~$125.88 / ~₹10,532.10)

SETUP
-----
1. pip install -r requirements.txt
2. Create a bot at https://discord.com/developers/applications
   - Enable the "Message Content Intent" under Bot settings.
3. Copy .env.example to .env and fill in:
   - DISCORD_TOKEN=your-bot-token
   - VOUCH_CHANNEL_ID=the channel id to watch for live tracking (optional,
     only needed for feature 1 above)
4. Run: python bot.py

COMMANDS
--------
!scanvouches [#channel] [limit]  -> scans message history and posts a full report
!vouchstats @user                -> live-tracked totals for one user
!leaderboard                     -> live-tracked top 10 users
!serverstats                     -> live-tracked server-wide totals
!recentvouches @user             -> last 10 live-tracked vouches for a user

NOTE ON PARSING
----------------
The scan looks for any number directly followed by "owo" (e.g. "5000 owo",
"+5000owo") and any number directly followed by "ltc" (e.g. "0.5 ltc"),
anywhere in a message — it does not require the "legit got" phrasing, so it
should pick up most real vouch messages. If it's missing or over-counting
messages in your server, send me a few real examples and I'll tighten the
patterns (OWO_AMOUNT_PATTERN / LTC_AMOUNT_PATTERN below).
"""

import os
import re
import sqlite3
import asyncio
from datetime import datetime, timezone

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
VOUCH_CHANNEL_ID = int(os.getenv("VOUCH_CHANNEL_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "vouches.db")

# Matches: "legit got <amount> <owo|ltc> @mention" (case-insensitive, flexible spacing)
# Used for LIVE per-user tracking.
VOUCH_PATTERN = re.compile(
    r"legit\s+got\s+(\d+(?:\.\d+)?)\s*(owo|ltc)\b.*?(<@!?(\d+)>)",
    re.IGNORECASE | re.DOTALL,
)

# Loose patterns used for FULL CHANNEL SCANS — catches "5000 owo", "+5000owo",
# "0.5 ltc", "0.5LTC", etc. anywhere in a message, regardless of phrasing.
OWO_AMOUNT_PATTERN = re.compile(r"([\d,]+(?:\.\d+)?)\s*owo\b", re.IGNORECASE)
LTC_AMOUNT_PATTERN = re.compile(r"([\d,]+(?:\.\d+)?)\s*ltc\b", re.IGNORECASE)

CACHED_RATE = {"usd": None, "inr": None, "fetched_at": 0}
RATE_CACHE_SECONDS = 300  # re-fetch LTC rates at most every 5 minutes


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vouches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER UNIQUE,
            vouchee_id INTEGER NOT NULL,
            voucher_id INTEGER,
            amount REAL NOT NULL,
            currency TEXT NOT NULL,        -- 'owo' or 'ltc'
            inr_value REAL NOT NULL,       -- 0 for owo, converted value for ltc
            ltc_inr_rate REAL,             -- rate used at the time, null for owo
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def save_vouch(message_id, vouchee_id, voucher_id, amount, currency, inr_value, rate):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """INSERT INTO vouches
               (message_id, vouchee_id, voucher_id, amount, currency, inr_value, ltc_inr_rate, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                message_id,
                vouchee_id,
                voucher_id,
                amount,
                currency,
                inr_value,
                rate,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass  # message already logged (e.g. bot restarted and re-saw it)
    finally:
        conn.close()


def get_user_stats(user_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        """SELECT currency, SUM(amount), SUM(inr_value), COUNT(*)
           FROM vouches WHERE vouchee_id = ? GROUP BY currency""",
        (user_id,),
    )
    rows = cur.fetchall()
    conn.close()
    stats = {"owo": 0.0, "ltc": 0.0, "inr": 0.0, "count": 0}
    for currency, total_amount, total_inr, count in rows:
        stats[currency] = total_amount or 0.0
        stats["inr"] += total_inr or 0.0
        stats["count"] += count
    return stats


def get_leaderboard(limit=10):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        """SELECT vouchee_id,
                  SUM(CASE WHEN currency='owo' THEN amount ELSE 0 END) AS owo_total,
                  SUM(CASE WHEN currency='ltc' THEN amount ELSE 0 END) AS ltc_total,
                  SUM(inr_value) AS inr_total,
                  COUNT(*) AS vouch_count
           FROM vouches
           GROUP BY vouchee_id
           ORDER BY inr_total DESC
           LIMIT ?""",
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_server_totals():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        """SELECT
              SUM(CASE WHEN currency='owo' THEN amount ELSE 0 END),
              SUM(CASE WHEN currency='ltc' THEN amount ELSE 0 END),
              SUM(inr_value),
              COUNT(*)
           FROM vouches"""
    )
    row = cur.fetchone()
    conn.close()
    owo_total, ltc_total, inr_total, count = row
    return {
        "owo": owo_total or 0.0,
        "ltc": ltc_total or 0.0,
        "inr": inr_total or 0.0,
        "count": count or 0,
    }


def get_recent_vouches(user_id, limit=10):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        """SELECT amount, currency, inr_value, created_at
           FROM vouches WHERE vouchee_id = ?
           ORDER BY id DESC LIMIT ?""",
        (user_id, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# LTC -> INR rate fetching (CoinGecko, cached for RATE_CACHE_SECONDS)
# ---------------------------------------------------------------------------
async def get_ltc_rates():
    """Returns (usd_rate, inr_rate) for 1 LTC, cached for RATE_CACHE_SECONDS."""
    now = asyncio.get_event_loop().time()
    if CACHED_RATE["usd"] is not None and (now - CACHED_RATE["fetched_at"] < RATE_CACHE_SECONDS):
        return CACHED_RATE["usd"], CACHED_RATE["inr"]

    url = "https://api.coingecko.com/api/v3/simple/price?ids=litecoin&vs_currencies=usd,inr"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                data = await resp.json()
                usd = float(data["litecoin"]["usd"])
                inr = float(data["litecoin"]["inr"])
                CACHED_RATE["usd"] = usd
                CACHED_RATE["inr"] = inr
                CACHED_RATE["fetched_at"] = now
                return usd, inr
    except Exception as e:
        print(f"[warn] failed to fetch LTC rates: {e}")
        return CACHED_RATE["usd"], CACHED_RATE["inr"]  # may be None if never fetched


async def get_ltc_inr_rate():
    """Back-compat helper used by the live per-message tracker."""
    _, inr = await get_ltc_rates()
    return inr


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    init_db()
    print(f"Logged in as {bot.user} | watching channel ID {VOUCH_CHANNEL_ID}")


@bot.event
async def on_message(message: discord.Message):
    # Always let commands still work
    await bot.process_commands(message)

    if message.author.bot:
        return
    if VOUCH_CHANNEL_ID and message.channel.id != VOUCH_CHANNEL_ID:
        return

    match = VOUCH_PATTERN.search(message.content)
    if not match:
        return

    amount = float(match.group(1))
    currency = match.group(2).lower()
    vouchee_id = int(match.group(4))

    inr_value = 0.0
    rate = None
    if currency == "ltc":
        rate = await get_ltc_inr_rate()
        if rate is not None:
            inr_value = amount * rate

    save_vouch(
        message_id=message.id,
        vouchee_id=vouchee_id,
        voucher_id=message.author.id,
        amount=amount,
        currency=currency,
        inr_value=inr_value,
        rate=rate,
    )

    confirm = f"✅ Logged: {amount} {currency.upper()} for <@{vouchee_id}>"
    if currency == "ltc" and rate:
        confirm += f" (≈ ₹{inr_value:,.2f})"
    elif currency == "ltc":
        confirm += " (INR rate unavailable right now, will show as ₹0 until recalculated)"
    await message.channel.send(confirm)


@bot.command(name="scanvouches")
async def scanvouches(ctx, channel: discord.TextChannel = None, limit: int = None):
    """
    Scans a channel's full message history and totals up every OWO and LTC
    amount found, then posts a summary report (mirrors the
    "Vouch Scan Complete!" style report).

    Usage:
      !scanvouches                -> scans the current channel, all messages
      !scanvouches #proof         -> scans #proof, all messages
      !scanvouches #proof 1000    -> scans #proof, most recent 1000 messages
    """
    channel = channel or ctx.channel
    status_msg = await ctx.send(f"🔍 Scanning {channel.mention}... this may take a moment.")

    total_owo = 0.0
    total_ltc = 0.0
    messages_scanned = 0

    async for message in channel.history(limit=limit):
        messages_scanned += 1
        content = message.content or ""

        for m in OWO_AMOUNT_PATTERN.finditer(content):
            total_owo += float(m.group(1).replace(",", ""))
        for m in LTC_AMOUNT_PATTERN.finditer(content):
            total_ltc += float(m.group(1).replace(",", ""))

    usd_rate, inr_rate = await get_ltc_rates()
    usd_value = total_ltc * usd_rate if usd_rate else None
    inr_value = total_ltc * inr_rate if inr_rate else None

    embed = discord.Embed(
        title="🐶 Vouch Scan Complete!",
        color=discord.Color.from_rgb(255, 255, 255),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="📌 Channel", value=channel.mention, inline=False)
    embed.add_field(name="📊 Messages Scanned", value=f"{messages_scanned:,}", inline=False)
    embed.add_field(name="🪙 Total OWO", value=f"{total_owo:,.0f}", inline=False)

    ltc_line = f"{total_ltc:,.4f} LTC"
    if usd_value is not None:
        ltc_line += f"  (≈ ${usd_value:,.2f})"
    embed.add_field(name="🔷 Total LTC", value=ltc_line, inline=False)

    if inr_value is not None:
        embed.add_field(name="🇮🇳 Total INR", value=f"₹{inr_value:,.2f}", inline=False)
    else:
        embed.add_field(name="🇮🇳 Total INR", value="rate unavailable right now", inline=False)

    await status_msg.delete()
    await ctx.send(embed=embed)


@bot.command(name="vouchstats")
async def vouchstats(ctx, member: discord.Member = None):
    member = member or ctx.author
    stats = get_user_stats(member.id)

    embed = discord.Embed(
        title=f"Vouch Stats — {member.display_name}",
        color=discord.Color.green(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Total Vouches", value=str(stats["count"]), inline=False)
    embed.add_field(name="OWO", value=f"{stats['owo']:,.0f} owo", inline=True)
    embed.add_field(name="LTC", value=f"{stats['ltc']:.6f} LTC", inline=True)
    embed.add_field(name="INR (from LTC)", value=f"₹{stats['inr']:,.2f}", inline=True)
    await ctx.send(embed=embed)


@bot.command(name="leaderboard")
async def leaderboard(ctx):
    rows = get_leaderboard(10)
    if not rows:
        await ctx.send("No vouches logged yet.")
        return

    embed = discord.Embed(title="🏆 Vouch Leaderboard", color=discord.Color.gold())
    lines = []
    for i, (user_id, owo_total, ltc_total, inr_total, count) in enumerate(rows, start=1):
        lines.append(
            f"**{i}. <@{user_id}>** — {owo_total:,.0f} owo | {ltc_total:.4f} LTC | "
            f"₹{inr_total:,.2f} | {count} vouches"
        )
    embed.description = "\n".join(lines)
    await ctx.send(embed=embed)


@bot.command(name="serverstats")
async def serverstats(ctx):
    totals = get_server_totals()
    embed = discord.Embed(title="📊 Server Vouch Totals", color=discord.Color.blue())
    embed.add_field(name="Total Vouches Logged", value=str(totals["count"]), inline=False)
    embed.add_field(name="Total OWO Given", value=f"{totals['owo']:,.0f} owo", inline=True)
    embed.add_field(name="Total LTC Given", value=f"{totals['ltc']:.6f} LTC", inline=True)
    embed.add_field(name="Total INR (from LTC)", value=f"₹{totals['inr']:,.2f}", inline=True)
    await ctx.send(embed=embed)


@bot.command(name="recentvouches")
async def recentvouches(ctx, member: discord.Member = None):
    member = member or ctx.author
    rows = get_recent_vouches(member.id, 10)
    if not rows:
        await ctx.send(f"No vouches found for {member.display_name}.")
        return
    lines = []
    for amount, currency, inr_value, created_at in rows:
        extra = f" (₹{inr_value:,.2f})" if currency == "ltc" else ""
        lines.append(f"`{created_at[:19]}` — {amount} {currency.upper()}{extra}")
    embed = discord.Embed(
        title=f"Recent Vouches — {member.display_name}",
        description="\n".join(lines),
        color=discord.Color.purple(),
    )
    await ctx.send(embed=embed)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    bot.run(TOKEN)
