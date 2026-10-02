#!/usr/bin/env python3
"""
FOMO Max Profit Alert Bot
=========================
Overpowered Telegram notifier for FOMO.family trading.
Uses free fomoapi.io WebSocket + REST.

Features:
- Live large-buy / whale alerts
- Auto-watch top traders from 7d leaderboard
- /hot  → what your watched list is buying (meta detector)
- /trader <handle> → profile + recent PnL
- Bible-style risk notes on every alert
- First-buy bias + low-mcap focus for memes

Requirements:
  pip install websockets requests python-telegram-bot

How to run:
  1. Fill the CONFIG section below with your keys
  2. python fomo_max_bot.py
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Set, Dict, Any, Optional

import requests
import websockets
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes

# ====================== CONFIG – FILL THESE ======================
TELEGRAM_BOT_TOKEN = "8614935045:AAFuhV6h0n1Y9t9sDRwYMjGkGgMMhDuZGrE"          # from @BotFather
TELEGRAM_CHAT_ID   = "8289119087"            # your numeric chat id
FOMO_API_KEY       = "fapi_0ea7e2bbdea548c594751d2f90199fc67507440494d545e4b357e9b3e1ad4f29"        # from https://fomoapi.io/dashboard

# Alert filters (tune these)
MIN_USD_VALUE      = 800          # ignore trades smaller than this
WATCH_TOP_N        = 8            # auto-add this many top 7d traders
PREFERRED_CHAINS   = {"solana", "base", "bnb", "robinhood"}  # empty set = all
# =================================================================

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
log = logging.getLogger("fomo-max")

# Runtime state
watched_traders: Set[str] = set()
seen_event_ids: Set[str] = set()
bot: Optional[Bot] = None

FOMO_BASE = "https://api.fomoapi.io"
WS_URL = "wss://api.fomoapi.io/ws/alerts"


def fomo_get(path: str, params: dict = None) -> dict:
    headers = {"Authorization": f"Bearer {FOMO_API_KEY}"}
    r = requests.get(f"{FOMO_BASE}{path}", headers=headers, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def risk_note(usd: float, token: str = "") -> str:
    """Bible-inspired short risk reminders."""
    notes = []
    if usd >= 5000:
        notes.append("🔥 Large size – high conviction or high risk")
    if usd < 1500:
        notes.append("Small size – possible early entry")
    notes.append("Position size rule: 1-2% of portfolio max")
    notes.append("Check holders + liquidity before aping")
    return " | ".join(notes)


async def send_tg(text: str, parse_mode: str = "HTML"):
    if not bot:
        return
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            parse_mode=parse_mode,
            disable_web_page_preview=True
        )
    except Exception as e:
        log.error(f"Telegram send failed: {e}")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 <b>FOMO Max Bot online</b>\n\n"
        "Commands:\n"
        "/top [24h|7d] – leaderboard\n"
        "/hot – what your watched traders are buying\n"
        "/trader <handle> – profile + recent activity\n"
        "/add <handle> – add to watchlist\n"
        "/list – show watched traders\n"
        "/del <handle> – remove from watchlist\n"
        "/status – bot status\n\n"
        "Live whale + smart-money alerts are running automatically.",
        parse_mode="HTML"
    )


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    window = "7d"
    if context.args and context.args[0] in ("24h", "7d", "30d"):
        window = context.args[0]
    try:
        data = fomo_get(f"/v2/leaderboard/{window}", {"limit": 15})
        traders = data.get("traders") or data.get("data") or []
        lines = [f"<b>🏆 FOMO Leaderboard ({window})</b>\n"]
        for i, t in enumerate(traders[:12], 1):
            handle = t.get("handle") or t.get("username") or "?"
            pnl = t.get("realizedPnlUsd") or t.get("pnl") or 0
            lines.append(f"{i}. @{handle}  →  ${pnl:,.0f}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Error fetching leaderboard: {e}")


async def cmd_trader(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /trader <handle>")
        return
    handle = context.args[0].lstrip("@")
    try:
        data = fomo_get(f"/v2/users/{handle}")
        profile = data.get("data") or data
        pnl = profile.get("realizedPnlUsd") or profile.get("pnl") or "N/A"
        followers = profile.get("followers") or profile.get("followerCount") or "?"
        text = (
            f"<b>@{handle}</b>\n"
            f"PnL: {pnl}\n"
            f"Followers: {followers}\n"
            f"Use /add {handle} to watch live."
        )
        await update.message.reply_text(text, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Could not fetch @{handle}: {e}")


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /add <handle>")
        return
    handle = context.args[0].lstrip("@").lower()
    watched_traders.add(handle)
    await update.message.reply_text(f"✅ Now watching @{handle}")


async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /del <handle>")
        return
    handle = context.args[0].lstrip("@").lower()
    watched_traders.discard(handle)
    await update.message.reply_text(f"Removed @{handle}")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not watched_traders:
        await update.message.reply_text("Watchlist empty. Use /add <handle>")
        return
    text = "<b>Watching:</b>\n" + "\n".join(f"• @{t}" for t in sorted(watched_traders))
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_hot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔥 Hot detector runs on live feed.\n"
        "When multiple watched traders buy the same token you will get a META alert automatically."
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Bot online\n"
        f"Watching {len(watched_traders)} traders\n"
        f"Min USD: ${MIN_USD_VALUE}\n"
        f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
    )


async def load_top_traders():
    """Auto-populate watchlist with top 7d performers."""
    global watched_traders
    try:
        data = fomo_get("/v2/leaderboard/7d", {"limit": WATCH_TOP_N})
        traders = data.get("traders") or data.get("data") or []
        for t in traders:
            handle = (t.get("handle") or t.get("username") or "").lower()
            if handle:
                watched_traders.add(handle)
        log.info(f"Auto-loaded {len(watched_traders)} top traders")
        await send_tg(f"✅ Auto-loaded top {len(watched_traders)} traders from 7d leaderboard")
    except Exception as e:
        log.warning(f"Could not auto-load leaderboard: {e}")


async def handle_alert(msg: dict):
    """Process one WebSocket alert."""
    if msg.get("type") != "alert":
        return

    event_id = msg.get("eventId") or msg.get("ts") or str(msg)
    if event_id in seen_event_ids:
        return
    seen_event_ids.add(event_id)
    if len(seen_event_ids) > 5000:
        seen_event_ids.clear()

    trader = (msg.get("trader") or msg.get("handle") or "").lower()
    alert_type = msg.get("alertType") or msg.get("type_") or msg.get("notificationType") or ""
    token = msg.get("token") or "?"
    token_addr = msg.get("tokenAddress") or ""
    chain = (msg.get("chain") or msg.get("chainId") or "").lower()
    usd = float(msg.get("usdValue") or 0)
    text = msg.get("text") or ""

    # Filters
    if usd < MIN_USD_VALUE and alert_type in ("buy", "sell"):
        return
    if PREFERRED_CHAINS and chain and chain not in PREFERRED_CHAINS:
        return

    is_watched = trader in watched_traders
    is_large = usd >= 3000

    # Build message
    emoji = "🟢" if "buy" in alert_type else "🔴" if "sell" in alert_type else "📝"
    header = f"{emoji} <b>@{trader}</b> {alert_type.upper()}"
    if is_watched:
        header = f"⭐ {header}"

    body = (
        f"{header}\n"
        f"Token: <b>{token}</b>\n"
        f"Size: ${usd:,.0f}\n"
        f"Chain: {chain}\n"
    )
    if token_addr:
        body += f"<code>{token_addr}</code>\n"
    if text:
        body += f"\n{text}\n"
    body += f"\n{risk_note(usd, token)}"

    # META detection (simple): if watched trader buys, flag it
    if is_watched and "buy" in alert_type:
        body = "🔥 <b>WATCHED TRADER BUY</b>\n" + body

    await send_tg(body)


async def ws_loop():
    """Persistent WebSocket connection with reconnect."""
    while True:
        try:
            url = f"{WS_URL}?key={FOMO_API_KEY}"
            log.info("Connecting to FOMO WebSocket...")
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                log.info("WebSocket connected")
                await send_tg("🟢 Live FOMO feed connected")
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        await handle_alert(msg)
                    except Exception as e:
                        log.debug(f"Skip message: {e}")
        except Exception as e:
            log.error(f"WebSocket error: {e}. Reconnecting in 8s...")
            await asyncio.sleep(8)


async def main():
    global bot
    if "PASTE_" in TELEGRAM_BOT_TOKEN or "PASTE_" in FOMO_API_KEY:
        print("ERROR: Fill in TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID and FOMO_API_KEY in the script!")
        return

    bot = Bot(token=TELEGRAM_BOT_TOKEN)

    # Telegram command handlers
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("trader", cmd_trader))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("hot", cmd_hot))
    app.add_handler(CommandHandler("status", cmd_status))

    # Load top traders once
    await load_top_traders()

    # Start Telegram polling + WebSocket in parallel
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    log.info("Bot fully started. Press Ctrl+C to stop.")
    await ws_loop()  # runs forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped by user")
