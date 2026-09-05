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
    user = update.effective_user
    name = user.first_name if user else "there"

    authorised = await is_authorized(update)

    if authorised:
        await update.message.reply_text(
            f"Hi {name}! 👋\n\n"
            "It's 🐝 BuzzBot here.\n"
            "✅ You are *Authorised* and can use the bot.\n\n"
            "In the group use:\n"
            "• `@Bot #TICKER` to look up a stock\n"
            "• `#stockpick your idea` to save an idea\n\n"
            "Type /help for more commands.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"Hi {name}! 👋\n\n"
            "It's 🐝 BuzzBot here.\n"
            "❌ You are *not authorised* yet.\n\n"
            "👉 *Next step:* tap the command below to request access:\n"
            "/request\n\n"
            "An admin will review it in Notion and approve you.\n"
            "You can check your status anytime with /status.",
            parse_mode="Markdown",
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

async def create_access_request(user) -> bool:
    """Create a Pending access request in Notion."""
    if not notion or not NOTION_AUTH_DB_ID:
        logger.error("Notion or NOTION_AUTH_DB_ID missing")
        return False

    try:
        # Base properties – adjust names if yours are different
        properties = {
            "Name": {
                "title": [{"text": {"content": (user.full_name or "Unknown")[:100]}}]
            },
            "Status": {
                "select": {"name": "Pending"}
            },
        }

        # Telegram Username (Text / rich_text)
        if user.username:
            properties["Telegram Username"] = {
                "rich_text": [{"text": {"content": user.username}}]
            }

        # Telegram User ID – try as rich_text first (most common)
        properties["Telegram User ID"] = {
            "rich_text": [{"text": {"content": str(user.id)}}]
        }

        notion.pages.create(
            parent={"database_id": NOTION_AUTH_DB_ID},
            properties=properties,
        )
        logger.info("Access request created for user %s (%s)", user.id, user.username)
        return True

    except Exception as e:
        logger.error("Failed to create access request: %s", e)
        # One retry with alternative property names if the first attempt fails
        try:
            properties = {
                "Name": {
                    "title": [{"text": {"content": (user.full_name or "Unknown")[:100]}}]
                },
                "Status": {
                    "select": {"name": "Pending"}
                },
                "Username": {
                    "rich_text": [{"text": {"content": user.username or ""}}]
                },
                "User ID": {
                    "rich_text": [{"text": {"content": str(user.id)}}]
                },
            }
            notion.pages.create(
                parent={"database_id": NOTION_AUTH_DB_ID},
                properties=properties,
            )
            logger.info("Access request created on retry for user %s", user.id)
            return True
        except Exception as e2:
            logger.error("Retry also failed: %s", e2)
            return False
            
async def request_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    if await is_authorized(update):
        await update.message.reply_text("You are already authorised. You can use the bot.")
        return

    success = await create_access_request(user)

    if success:
        await update.message.reply_text(
            "✅ Your access request has been submitted.\n\n"
            f"Name: {user.full_name}\n"
            f"Username: @{user.username or 'N/A'}\n"
            f"User ID: `{user.id}`\n\n"
            "An admin will change your Status to *Authorised* in Notion.\n"
            "Check progress anytime with /status.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "Sorry, I could not submit your request right now.\n\n"
            "Possible reasons:\n"
            "• Notion property names don’t match\n"
            "• Status option “Pending” doesn’t exist\n"
            "• Integration not connected to the database\n\n"
            "Please contact an admin."
        )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    authorised = await is_authorized(update)

    if authorised:
        await update.message.reply_text(
            f"✅ You are **Authorised**.\n\n"
            f"Name: {user.full_name}\n"
            f"Username: @{user.username or 'N/A'}\n"
            f"User ID: `{user.id}`",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"❌ You are **not authorised** yet.\n\n"
            f"Name: {user.full_name}\n"
            f"Username: @{user.username or 'N/A'}\n"
            f"User ID: `{user.id}`\n\n"
            "Send /request to submit an access request.",
            parse_mode="Markdown",
        )
        
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Let a user check whether they are authorised."""
    user = update.effective_user
    if not user:
        return

    authorised = await is_authorized(update)

    if authorised:
        await update.message.reply_text(
            f"✅ You are **Authorised**.\n\n"
            f"Name: {user.full_name}\n"
            f"Username: @{user.username or 'N/A'}\n"
            f"User ID: `{user.id}`",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"❌ You are **not authorised** yet.\n\n"
            f"Name: {user.full_name}\n"
            f"Username: @{user.username or 'N/A'}\n"
            f"User ID: `{user.id}`\n\n"
            "Send /request to submit an access request.",
            parse_mode="Markdown",
        )
        
# ------------------------------------------------------------
# Authorisation (Status = "Authorised" required)
# ------------------------------------------------------------
_authorized_cache: dict = {"users": {}, "expires": 0}
AUTH_CACHE_TTL = 300  # 5 minutes

NOTION_AUTH_DB_ID = os.getenv("NOTION_AUTH_DB_ID") or os.getenv("NOTION_DATABASE_ID")


async def get_authorized_users() -> dict:
    """
    Returns a dict of authorised users:
    {
        "usernames": {"john_smith", ...},
        "user_ids": {"123456789", ...}
    }
    Only rows where Status == "Authorised" are included.
    """
    if not notion or not NOTION_AUTH_DB_ID:
        return {"usernames": set(), "user_ids": set()}

    now = time.time()
    if _authorized_cache["expires"] > now:
        return _authorized_cache["users"]

    try:
        usernames = set()
        user_ids = set()
        cursor = None

        while True:
            kwargs = {
                "database_id": NOTION_AUTH_DB_ID,
                "page_size": 100,
                "filter": {
                    "property": "Status",
                    "select": {"equals": "Authorised"}
                }
            }
            if cursor:
                kwargs["start_cursor"] = cursor

            response = notion.databases.query(**kwargs)

            for page in response.get("results", []):
                props = page.get("properties", {})

                # Username
                for key in ("Telegram Username", "Username", "TG Username"):
                    if key in props:
                        val = _get_plain_text(props[key]).strip().lstrip("@").lower()
                        if val:
                            usernames.add(val)
                        break

                # User ID
                for key in ("Telegram User ID", "User ID", "Telegram ID", "ID"):
                    if key in props:
                        val = _get_plain_text(props[key]).strip()
                        if val:
                            user_ids.add(val)
                        break

            if not response.get("has_more"):
                break
            cursor = response.get("next_cursor")

        result = {"usernames": usernames, "user_ids": user_ids}
        _authorized_cache["users"] = result
        _authorized_cache["expires"] = now + AUTH_CACHE_TTL
        logger.info("Loaded %d authorised usernames and %d user IDs", len(usernames), len(user_ids))
        return result

    except Exception as e:
        logger.error("Failed to load authorised users: %s", e)
        return _authorized_cache.get("users", {"usernames": set(), "user_ids": set()})


async def is_authorized(update: Update) -> bool:
    """True only if the user is in the Authorised list (by username or user ID)."""
    user = update.effective_user
    if not user:
        return False

    auth = await get_authorized_users()

    # Check by username
    if user.username and user.username.lower() in auth["usernames"]:
        return True

    # Check by numeric user ID
    if str(user.id) in auth["user_ids"]:
        return True

    return False


async def create_access_request(user) -> bool:
    """Create a Pending access request in Notion."""
    if not notion or not NOTION_AUTH_DB_ID:
        return False

    try:
        properties = {
            "Name": {
                "title": [{"text": {"content": user.full_name or "Unknown"}}]
            },
            "Status": {
                "select": {"name": "Pending"}
            },
            "Telegram User ID": {
                "rich_text": [{"text": {"content": str(user.id)}}]
            },
        }

        if user.username:
            properties["Telegram Username"] = {
                "rich_text": [{"text": {"content": user.username}}]
            }

        notion.pages.create(
            parent={"database_id": NOTION_AUTH_DB_ID},
            properties=properties,
        )
        return True
    except Exception as e:
        logger.error("Failed to create access request: %s", e)
        return False

# ------------------------------------------------------------
# Message handling – STRICT
# ------------------------------------------------------------

async def should_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    msg = update.message
    if not msg or not msg.text:
        return False

    text = msg.text
    lower = text.lower()
    chat_type = msg.chat.type

    # Private chats always allowed
    if chat_type == "private":
        return True

    # --- Authorisation (must be Status = Authorised) ---
    if not await is_authorized(update):
        return False

    # --- Trigger rules ---
    bot_username = (context.bot.username or "").lower()

    has_mention = False
    if bot_username:
        if msg.entities:
            for entity in msg.entities:
                if entity.type == "mention":
                    mention = text[entity.offset : entity.offset + entity.length].lower()
                    if mention == f"@{bot_username}":
                        has_mention = True
                        break
        if f"@{bot_username}" in lower:
            has_mention = True

    hashtag_tickers = extract_hashtag_tickers(text)
    has_stockpick = "#stockpick" in lower

    if has_stockpick:
        return True

    if has_mention and hashtag_tickers:
        return True

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
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("request", request_access))
    app.add_handler(CommandHandler("debug", debug_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Hive SupportBot starting...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
    
if __name__ == "__main__":
    main()