#!/usr/bin/env python3
"""
Hive SupportBot – AIM/Small Cap knowledge bot
Live data from Notion + #stockpick capture
Strict group behaviour: only responds on @mention + #ticker
or #ticker + intent keywords (summary, snapshot, thesis, etc.)
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
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")       # for #stockpick captures
NOTION_TICKERS_DB_ID = os.getenv("NOTION_TICKERS_DB_ID")   # UK AIM Micro-Cap database

notion = Client(auth=NOTION_TOKEN) if NOTION_TOKEN else None

if not notion:
    logger.warning("Notion credentials missing – live lookup and #stockpick write disabled")

# ------------------------------------------------------------
# Caches
# ------------------------------------------------------------
_ticker_cache: dict[str, dict] = {}
CACHE_TTL_SECONDS = 600  # 10 minutes

_authorized_cache: dict = {"usernames": set(), "expires": 0}
AUTH_CACHE_TTL = 300  # 5 minutes

# Keywords that indicate the user wants a ticker lookup
INTENT_KEYWORDS = {
    "summary", "snapshot", "thesis", "overview",
    "red flags", "red flag", "risks", "catalyst",
    "next", "update", "view", "thoughts", "take",
    "stockpick", "lookup", "info", "details",
}

# ------------------------------------------------------------
# Notion helpers
# ------------------------------------------------------------
def _get_plain_text(prop: dict) -> str:
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


async def get_authorized_usernames() -> set[str]:
    """Return a set of authorised Telegram usernames (without @)."""
    if not notion or not NOTION_DATABASE_ID:
        return set()

    now = time.time()
    if _authorized_cache["expires"] > now:
        return _authorized_cache["usernames"]

    try:
        usernames = set()
        cursor = None

        while True:
            kwargs = {"database_id": NOTION_DATABASE_ID, "page_size": 100}
            if cursor:
                kwargs["start_cursor"] = cursor

            response = notion.databases.query(**kwargs)

            for page in response.get("results", []):
                props = page.get("properties", {})
                for key in ("Telegram Username", "Username", "TG Username", "Telegram", "User"):
                    if key in props:
                        val = _get_plain_text(props[key]).strip().lstrip("@").lower()
                        if val:
                            usernames.add(val)
                        break

            if not response.get("has_more"):
                break
            cursor = response.get("next_cursor")

        _authorized_cache["usernames"] = usernames
        _authorized_cache["expires"] = now + AUTH_CACHE_TTL
        logger.info("Loaded %d authorised usernames from Notion", len(usernames))
        return usernames

    except Exception as e:
        logger.error("Failed to load authorised users: %s", e)
        return _authorized_cache["usernames"]


async def is_authorized(update: Update) -> bool:
    user = update.effective_user
    if not user or not user.username:
        return False
    authorized = await get_authorized_usernames()
    return user.username.lower() in authorized


async def get_ticker_from_notion(ticker: str) -> dict | None:
    if not notion or not NOTION_TICKERS_DB_ID:
        return None

    ticker = ticker.upper().strip()

    cached = _ticker_cache.get(ticker)
    if cached and cached["expires"] > time.time():
        return cached["data"]

    try:
        filters_to_try = [
            {"property": "Ticker", "title": {"equals": ticker}},
            {"property": "Ticker", "rich_text": {"equals": ticker}},
            {"property": "Ticker", "rich_text": {"contains": ticker}},
        ]

        results = []
        for f in filters_to_try:
            response = notion.databases.query(
                database_id=NOTION_TICKERS_DB_ID,
                filter=f,
                page_size=5,
            )
            results = response.get("results", [])
            if results:
                break

        if not results:
            logger.info("No Notion page found for ticker: %s", ticker)
            return None

        props = results[0]["properties"]

        def find_prop(*names):
            for name in names:
                if name in props:
                    val = _get_plain_text(props[name])
                    if val:
                        return val
            return ""

        data = {
            "company": find_prop("Company", "Name", "Company Name"),
            "summary": find_prop("Summary & Next Catalyst", "Summary", "Overview", "Thesis"),
            "red_flags": find_prop("Red Flags", "Risks", "Red Flag", "Key Risks"),
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
    if not notion or not NOTION_DATABASE_ID:
        return False

    try:
        properties = {
            "Name": {"title": [{"text": {"content": text[:100]}}]},
            "Message": {"rich_text": [{"text": {"content": text[:2000]}}]},
            "User": {"rich_text": [{"text": {"content": user_name or "Unknown"}}]},
            "Date": {"date": {"start": datetime.now(timezone.utc).isoformat()}},
            "Source": {"rich_text": [{"text": {"content": "Telegram"}}]},
        }
        if ticker:
            properties["Ticker"] = {"rich_text": [{"text": {"content": ticker}}]}

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
def extract_hashtag_tickers(text: str) -> list[str]:
    """Only extract tickers that appear as #TICKER (2-5 letters)."""
    if not text:
        return []
    # Strict: must be a hashtag
    matches = re.findall(r"#([A-Za-z]{2,5})\b", text)
    found = []
    for m in matches:
        t = m.upper()
        if t not in found and t.isalpha():
            found.append(t)
    return found


def has_intent_keyword(text: str) -> bool:
    """Return True if the message contains an intent keyword."""
    lower = text.lower()
    return any(kw in lower for kw in INTENT_KEYWORDS)


def format_reply(ticker: str, data: dict) -> str:
    return (
        f"🔖📑 *#{ticker}* – {data.get('company') or 'N/A'}\n\n"
        f"*Snapshot Summary:*\n{data.get('summary') or 'No summary available.'}\n\n"
        f"*Red Flags:*\n{data.get('red_flags') or 'None noted.'}\n\n"
        f"_🔋🪫 Powered by: The Hive 🐝 BuzzBot Knowledge Hub. Not financial advice. DYOR._"
    )


# ------------------------------------------------------------
# Command handlers
# ------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = update.effective_user.first_name or "there"
    await update.message.reply_text(
        f"Hi {name}! 👋\n\n"
        "It's 🐝 BuzzBot here, I’m your Hive Group SupportBot.\n"
        "In the group: @mention me + #TICKER, or use #TICKER with words like "
        "summary / snapshot / thesis / stockpick.\n"
        "Use /help for more commands."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🐝 *Hive SupportBot commands*\n\n"
        "/start – Welcome message\n"
        "/help – This help\n"
        "/faq – Quick FAQ\n"
        "/tickers – How to request a ticker\n\n"
        "*In the group:*\n"
        "• `@Bot #KEFI summary` → lookup\n"
        "• `#KEFI snapshot` → lookup\n"
        "• `#stockpick my idea...` → save to Notion\n"
        "Normal chat is ignored.",
        parse_mode="Markdown",
    )


async def faq(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📌 *FAQ*\n\n"
        "• Data is pulled live from the curated UK AIM Micro-Cap database.\n"
        "• This is *not* financial advice – always DYOR.\n"
        "• Use `#stockpick` in the group to log ideas.\n"
        "• Contact a human admin in The Hive group if something looks wrong.",
        parse_mode="Markdown",
    )


async def tickers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "In the group use:\n"
        "`@Bot #KEFI summary` or `#KEFI snapshot`\n\n"
        "I’ll look it up in the live UK AIM Micro-Cap snapshot.",
        parse_mode="Markdown",
    )


# ------------------------------------------------------------
# Message handling – STRICT
# ------------------------------------------------------------
async def should_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Group rules (strict):
    1. User must be authorised (if auth is configured)
    2. AND one of:
       - Bot is @mentioned AND message contains #TICKER
       - Message contains #TICKER + an intent keyword (summary, snapshot, thesis...)
       - Message contains #stockpick
    Private chats: always allowed.
    """
    msg = update.message
    if not msg or not msg.text:
        return False

    text = msg.text
    lower = text.lower()

    # Private chats always allowed
    if msg.chat.type == "private":
        return True

    # --- Authorisation (groups) ---
    # Comment out the next 2 lines if you want to temporarily disable auth
    if not await is_authorized(update):
        return False

    # --- Group trigger rules ---
    bot_username = (context.bot.username or "").lower()
    has_mention = False
    if bot_username and msg.entities:
        for entity in msg.entities:
            if entity.type == "mention":
                mention = text[entity.offset : entity.offset + entity.length].lower()
                if mention == f"@{bot_username}":
                    has_mention = True
                    break

    hashtag_tickers = extract_hashtag_tickers(text)
    has_stockpick = "#stockpick" in lower
    has_intent = has_intent_keyword(text)

    # Rule 1: #stockpick is always allowed (for authorised users)
    if has_stockpick:
        return True

    # Rule 2: @mention + at least one #TICKER
    if has_mention and hashtag_tickers:
        return True

    # Rule 3: #TICKER + intent keyword (summary / snapshot / thesis etc.)
    if hashtag_tickers and has_intent:
        return True

    # Everything else → stay silent
    return False


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await should_reply(update, context):
        return

    text = (update.message.text or "").strip()
    lower = text.lower()

    # Clean bot mention out of the text
    bot_username = (context.bot.username or "").lower()
    clean_text = re.sub(rf"@{re.escape(bot_username)}\b", "", text, flags=re.IGNORECASE).strip()
    clean_lower = clean_text.lower()

    # 1. #stockpick capture
    if "#stockpick" in clean_lower:
        user = update.effective_user
        user_name = user.full_name if user else "Unknown"
        tickers = extract_hashtag_tickers(clean_text)
        ticker = tickers[0] if tickers else None

        success = await save_stockpick_to_notion(clean_text, user_name, ticker)
        if success:
            reply = "✅ Captured your #stockpick"
            if ticker:
                reply += f" ({ticker})"
            reply += " and saved to Notion."
            await update.message.reply_text(reply)
        else:
            await update.message.reply_text(
                "✅ Captured your #stockpick.\n(Could not write to Notion – check logs.)"
            )
        return

    # 2. Ticker lookup – ONLY from hashtags
    tickers = extract_hashtag_tickers(clean_text)
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
                    f"I don’t have *#{t}* in the current UK AIM Micro-Cap snapshot.",
                    parse_mode="Markdown",
                )
        return

    # 3. Pure @mention with no #ticker
    await update.message.reply_text(
        "Hi! To look up a ticker use:\n"
        "`@Bot #KEFI summary` or `#KEFI snapshot`\n"
        "Or use `#stockpick` to save an idea.",
        parse_mode="Markdown",
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Error while handling update: %s", context.error)


async def debug_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Temporary diagnostic."""
    if not notion or not NOTION_TICKERS_DB_ID:
        await update.message.reply_text("Notion client or NOTION_TICKERS_DB_ID is missing.")
        return

    try:
        response = notion.databases.query(
            database_id=NOTION_TICKERS_DB_ID,
            page_size=5,
        )
        results = response.get("results", [])

        if not results:
            await update.message.reply_text("Database accessible but returned 0 pages.")
            return

        lines = [f"Found {len(results)} page(s):\n"]
        for page in results:
            props = page.get("properties", {})
            prop_names = list(props.keys())
            title = ""
            for key in prop_names:
                val = _get_plain_text(props[key])
                if val:
                    title = f"{key}: {val}"
                    break
            lines.append(f"• {title or 'No text'} | {prop_names}")

        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Error:\n`{e}`", parse_mode="Markdown")


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------
def main() -> None:
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("faq", faq))
    app.add_handler(CommandHandler("tickers", tickers_cmd))
    app.add_handler(CommandHandler("debug", debug_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Hive SupportBot starting (strict group mode)...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()