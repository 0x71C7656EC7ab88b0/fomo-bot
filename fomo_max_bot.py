#!/usr/bin/env python3
"""
FOMO Max Profit Alert Bot – Smart Version
=========================================
Only high-quality alerts. Clear buy/sell language.
/pause and /resume support.

Requirements:
  pip install websockets requests python-telegram-bot
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Set, Optional

import requests
import websockets
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes

# ====================== CONFIG ======================
TELEGRAM_BOT_TOKEN = "PASTE_YOUR_BOT_TOKEN_HERE"
TELEGRAM_CHAT_ID   = "PASTE_YOUR_CHAT_ID_HERE"
FOMO_API_KEY       = "PASTE_YOUR_FOMOAPI_KEY_HERE"

# Smart filters
MIN_USD_VALUE      = 1200         # ignore small noise
WATCH_TOP_N        = 8
PREFERRED_CHAINS   = {"solana", "base", "bnb", "robinhood"}
# ====================================================

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
log = logging.getLogger("fomo-max")

watched_traders: Set[str] = set()
seen_event_ids: Set[str] = set()
bot: Optional[Bot] = None
is_paused: bool = False          # for /pause /resume

FOMO_BASE = "https://api.fomoapi.io"
WS_URL = "wss://api.fomoapi.io/ws/alerts"


def fomo_get(path: str, params: dict = None) -> dict:
    headers = {"Authorization": f"Bearer {FOMO_API_KEY}"}
    r = requests.get(f"{FOMO_BASE}{path}", headers=headers, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


async def send_tg(text: str):
    if not bot or is_paused:
        return
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
    except Exception as e:
        log.error(f"Telegram send failed: {e}")


# -------------------- Commands --------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 <b>FOMO Smart Bot online</b>\n\n"
        "Only high-quality signals now.\n\n"
        "<b>Commands:</b>\n"
        "/top [24h|7d] – leaderboard\n"
        "/trader <handle> – check a trader\n"
        "/add <handle> – watch this trader\n"
        "/del <handle> – stop watching\n"
        "/list – show watched traders\n"
        "/pause – stop all alerts\n"
        "/resume – start alerts again\n"
        "/status – bot status",
        parse_mode="HTML"
    )


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_paused
    is_paused = True
    await update.message.reply_text("⏸ Alerts paused. Use /resume to turn them back on.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_paused
    is_paused = False
    await update.message.reply_text("▶️ Alerts resumed.")


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    window = "7d"
    if context.args and context.args[0] in ("24h", "7d", "30d"):
        window = context.args[0]
    try:
        data = fomo_get(f"/v2/leaderboard/{window}", {"limit": 12})
        traders = data.get("traders") or data.get("data") or []
        lines = [f"<b>🏆 FOMO Leaderboard ({window})</b>\n"]
        for i, t in enumerate(traders[:10], 1):
            handle = t.get("handle") or t.get("username") or "?"
            pnl = t.get("realizedPnlUsd") or t.get("pnl") or 0
            lines.append(f"{i}. @{handle}  →  ${pnl:,.0f}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_trader(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /trader <handle>")
        return
    handle = context.args[0].lstrip("@")
    try:
        data = fomo_get(f"/v2/users/{handle}")
        profile = data.get("data") or data
        pnl = profile.get("realizedPnlUsd") or profile.get("pnl") or "N/A"
        text = f"<b>@{handle}</b>\nPnL: {pnl}\nUse /add {handle} to watch."
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


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = "⏸ PAUSED" if is_paused else "▶️ ACTIVE"
    await update.message.reply_text(
        f"Status: {status}\n"
        f"Watching: {len(watched_traders)} traders\n"
        f"Min size: ${MIN_USD_VALUE}\n"
        f"Time: {datetime.utcnow().strftime('%H:%M UTC')}"
    )


# -------------------- Smart Alerts --------------------

async def handle_alert(msg: dict):
    if msg.get("type") != "alert" or is_paused:
        return

    event_id = str(msg.get("eventId") or msg.get("ts") or msg)
    if event_id in seen_event_ids:
        return
    seen_event_ids.add(event_id)
    if len(seen_event_ids) > 4000:
        seen_event_ids.clear()

    trader = (msg.get("trader") or msg.get("handle") or "").lower()
    alert_type = (msg.get("alertType") or msg.get("type_") or msg.get("notificationType") or "").lower()
    token = msg.get("token") or "?"
    token_addr = msg.get("tokenAddress") or ""
    chain = (msg.get("chain") or "").lower()
    usd = float(msg.get("usdValue") or 0)
    text = msg.get("text") or ""

    # Strict filters – only high quality
    is_watched = trader in watched_traders
    is_large = usd >= 2500

    if not is_watched and not is_large:
        return
    if usd < MIN_USD_VALUE:
        return
    if PREFERRED_CHAINS and chain and chain not in PREFERRED_CHAINS:
        return

    # Clear useful language
    if "buy" in alert_type:
        if is_watched:
            header = f"⭐ <b>STRONG BUY SIGNAL</b>\nWatched trader @{trader} just bought"
        else:
            header = f"🟢 <b>Large Buy</b>\n@{trader}"
    elif "sell" in alert_type:
        if is_watched:
            header = f"🔴 <b>WATCHED TRADER SELLING</b>\n@{trader} is selling – consider taking profit"
        else:
            header = f"🔴 Large Sell\n@{trader}"
    else:
        header = f"📝 @{trader}"

    body = (
        f"{header}\n\n"
        f"Token: <b>{token}</b>\n"
        f"Size: ${usd:,.0f}\n"
        f"Chain: {chain}\n"
    )
    if token_addr:
        body += f"<code>{token_addr}</code>\n"
    if text:
        body += f"\n{text}\n"

    body += "\n💡 Max $3–$4 per trade with your capital"

    await send_tg(body)


async def load_top_traders():
    global watched_traders
    try:
        data = fomo_get("/v2/leaderboard/7d", {"limit": WATCH_TOP_N})
        traders = data.get("traders") or data.get("data") or []
        for t in traders:
            handle = (t.get("handle") or t.get("username") or "").lower()
            if handle:
                watched_traders.add(handle)
        log.info(f"Loaded {len(watched_traders)} top traders")
        await send_tg(f"✅ Watching top {len(watched_traders)} traders from 7d leaderboard")
    except Exception as e:
        log.warning(f"Could not load leaderboard: {e}")


async def ws_loop():
    while True:
        try:
            url = f"{WS_URL}?key={FOMO_API_KEY}"
            log.info("Connecting to FOMO WebSocket...")
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                log.info("WebSocket connected")
                await send_tg("🟢 Smart FOMO feed connected")
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        await handle_alert(msg)
                    except Exception:
                        pass
        except Exception as e:
            log.error(f"WebSocket error: {e}. Reconnecting in 10s...")
            await asyncio.sleep(10)


async def main():
    global bot
    if "PASTE_" in TELEGRAM_BOT_TOKEN or "PASTE_" in FOMO_API_KEY:
        print("ERROR: Fill in your keys first!")
        return

    bot = Bot(token=TELEGRAM_BOT_TOKEN)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("trader", cmd_trader))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("status", cmd_status))

    await load_top_traders()

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    log.info("Smart bot started")
    await ws_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped")
