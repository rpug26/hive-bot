#!/usr/bin/env python3
"""
Hive SupportBot – AIM/Small Cap knowledge bot
Live data from Notion + #stockpick capture
"""

import os
import re
import time
import logging
from datetime import datetime, timezone

from dotenv import load_dotenv
from notion_client import Client
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# Environment
# ------------------------------------------------------------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN missing")

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")          # for #stockpick captures
NOTION_TICKERS_DB_ID = os.getenv("NOTION_TICKERS_DB_ID")      # UK AIM Micro-Cap database

notion = Client(auth=NOTION_TOKEN) if NOTION_TOKEN else None

if not notion:
    logger.warning("Notion credentials missing – live lookup and #stockpick write disabled")

# ------------------------------------------------------------
# Simple in-memory cache for ticker lookups
# ------------------------------------------------------------
_ticker_cache: dict[str, dict] = {}
CACHE_TTL_SECONDS = 600  # 10 minutes


# ------------------------------------------------------------
# Notion helpers
# ------------------------------------------------------------
def _get_plain_text(prop: dict) -> str:
    """Extract plain text from a Notion property."""
    if not prop:
        return ""
    ptype = prop.get("type")
    if ptype == "title":
        return "".join(t.get("plain_text", "") for t in prop.get("title", [])).strip()
    if ptype == "rich_text":
        return "".join(t.get("plain_text", "") for t in prop.get("rich_text", [])).strip()
    if ptype == "select":
        sel = prop.get("select")
        return sel.get("name", "") if sel else ""
    return ""


async def get_ticker_from_notion(ticker: str) -> dict | None:
    """Fetch a single ticker from the UK AIM Micro-Cap Notion database."""
    if not notion or not NOTION_TICKERS_DB_ID:
        return None

    ticker = ticker.upper().strip()

    # Cache hit?
    cached = _ticker_cache.get(ticker)
    if cached and cached["expires"] > time.time():
        return cached["data"]

    try:
        response = notion.databases.query(
            database_id=NOTION_TICKERS_DB_ID,
            filter={
                "property": "Ticker",
                "title": {"equals": ticker},
            },
            page_size=1,
        )

        results = response.get("results", [])
        if not results:
            return None

        props = results[0]["properties"]

        data = {
            "company": _get_plain_text(props.get("Company")),
            "status": _get_plain_text(props.get("Status")),
            "mcap": _get_plain_text(props.get("Mkt Cap")),
            "summary": _get_plain_text(props.get("Summary")),
            "red_flags": _get_plain_text(props.get("Red Flags")),
            "next": _get_plain_text(props.get("Next")),
        }

        _ticker_cache[ticker] = {
            "data": data,
            "expires": time.time() + CACHE_TTL_SECONDS,
        }
        return data

    except Exception as e:
        logger.error("Notion ticker lookup failed for %s: %s", ticker, e)
        return None


async def save_stockpick_to_notion(text: str, user_name: str, ticker: str | None = None) -> bool:
    """Write a #stockpick message into the captures database."""
    if not notion or not NOTION_DATABASE_ID:
        return False

    try:
        properties = {
            "Name": {
                "title": [{"text": {"content": text[:100]}}]
            },
            "Message": {
                "rich_text": [{"text": {"content": text[:2000]}}]
            },
            "User": {
                "rich_text": [{"text": {"content": user_name or "Unknown"}}]
            },
            "Date": {
                "date": {"start": datetime.now(timezone.utc).isoformat()}
            },
            "Source": {
                "rich_text": [{"text": {"content": "Telegram"}}]
            },
        }

        if ticker:
            properties["Ticker"] = {
                "rich_text": [{"text": {"content": ticker}}]
            }

        notion.pages.create(
            parent={"database_id": NOTION_DATABASE_ID},
            properties=properties,
        )
        return True
    except Exception as e:
        logger.error("Failed to write #stockpick to Notion: %s", e)
        return False


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def extract_tickers(text: str) -> list[str]:
    """Extract plausible tickers (2–5 alphanumeric) from text or hashtags."""
    if not text:
        return []
    pattern = r"(?:^|[\s$#])([A-Za-z]{2,5})(?=[\s.,!?;:\)]|$)"
    candidates = re.findall(pattern, text)
    found = []
    for c in candidates:
        t = c.upper()
        if t not in found and t.isalpha():
            found.append(t)
    return found


def format_reply(ticker: str, data: dict) -> str:
    return (
        f"📊 *{ticker}* – {data.get('company') or 'N/A'}\n"
        f"Status: {data.get('status') or 'N/A'}  |  Mkt Cap: {data.get('mcap') or 'N/A'}\n\n"
        f"*Summary:*\n{data.get('summary') or 'No summary available.'}\n\n"
        f"*Red Flags:*\n{data.get('red_flags') or 'None noted.'}\n\n"
        f"*Next catalyst:*\n{data.get('next') or 'N/A'}\n\n"
        f"_Hive knowledge snapshot (live from Notion). Not financial advice. DYOR._"
    )


# ------------------------------------------------------------
# Command handlers
# ------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = update.effective_user.first_name or "there"
    await update.message.reply_text(
        f"Hi {name}! 👋\n\n"
        "I’m the Hive SupportBot.\n"
        "Send a ticker (e.g. KEFI or #ALRT) and I’ll look it up from the latest curated snapshot.\n"
        "Use /help for more commands."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🐝 *Hive SupportBot commands*\n\n"
        "/start – Welcome message\n"
        "/help – This help\n"
        "/faq – Quick FAQ\n"
        "/tickers – How to request a ticker\n\n"
        "Just post a ticker (KEFI, #AXL, etc.) and I’ll reply with the latest summary.\n"
        "In groups you can also use #stockpick to capture ideas.",
        parse_mode="Markdown",
    )


async def faq(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📌 *FAQ*\n\n"
        "• Data is pulled live from the curated UK AIM Micro-Cap Notion page.\n"
        "• This is *not* financial advice – always DYOR.\n"
        "• Use #stockpick in the group to log ideas.\n"
        "• Contact a human admin in The Hive if something looks wrong.",
        parse_mode="Markdown",
    )


async def tickers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Just send any ticker you’re interested in (e.g. `KEFI` or `#ALRT`).\n"
        "I’ll look it up in the live UK AIM Micro-Cap snapshot.",
        parse_mode="Markdown",
    )


# ------------------------------------------------------------
# Message handling
# ------------------------------------------------------------
def should_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Decide whether the bot should respond to this message."""
    msg = update.message
    if not msg or not msg.text:
        return False

    # Always reply in private chats
    if msg.chat.type == "private":
        return True

    # In groups: reply if bot is mentioned, replied to, or message contains a ticker / #stockpick
    bot_username = (context.bot.username or "").lower()
    if msg.entities:
        for e in msg.entities:
            if e.type == "mention":
                mention = msg.text[e.offset : e.offset + e.length].lower()
                if mention == f"@{bot_username}":
                    return True

    if msg.reply_to_message and msg.reply_to_message.from_user:
        if msg.reply_to_message.from_user.id == context.bot.id:
            return True

    if extract_tickers(msg.text) or "#stockpick" in msg.text.lower():
        return True

    return False


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not should_reply(update, context):
        return

    text = (update.message.text or "").strip()
    lower = text.lower()

    # Simple greetings
    if any(w in lower for w in ("hi", "hello", "hey", "good morning", "good evening")) and len(text) < 25:
        await update.message.reply_text(
            "Hi! 👋 Send a ticker (e.g. KEFI or #ALRT) and I’ll look it up."
        )
        return

    if any(w in lower for w in ("thank", "thanks", "cheers")):
        await update.message.reply_text("You're welcome! 🐝")
        return

    # #stockpick capture
    if "#stockpick" in lower:
        user = update.effective_user
        user_name = user.full_name if user else "Unknown"
        tickers = extract_tickers(text)
        ticker = tickers[0] if tickers else None

        success = await save_stockpick_to_notion(text, user_name, ticker)

        if success:
            reply = "✅ Captured your #stockpick"
            if ticker:
                reply += f" ({ticker})"
            reply += " and saved to Notion."
            await update.message.reply_text(reply)
        else:
            await update.message.reply_text(
                "✅ Captured your #stockpick.\n"
                "(Could not write to Notion – check the bot logs.)"
            )
        return

    # Ticker lookup
    tickers = extract_tickers(text)
    if tickers:
        for t in tickers:
            data = await get_ticker_from_notion(t)
            if data:
                await update.message.reply_text(
                    format_reply(t, data),
                    parse_mode="Markdown",
                )
            else:
                await update.message.reply_text(
                    f"I don’t have *{t}* in the current UK AIM Micro-Cap snapshot.\n"
                    "It may not be curated yet, or the name is slightly different.",
                    parse_mode="Markdown",
                )
        return

    # Fallback
    await update.message.reply_text(
        "I don’t recognise that as a ticker I know.\n"
        "Try sending a ticker (e.g. KEFI) or use /help."
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Error while handling update: %s", context.error)


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------
def main() -> None:
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("faq", faq))
    app.add_handler(CommandHandler("tickers", tickers_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Hive SupportBot starting (live Notion mode)...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
