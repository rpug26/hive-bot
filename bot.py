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

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
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

# user_id -> last stockpick Notion page_id this month
_last_stockpick_page: dict[int, str] = {}
# user_id -> waiting field name ("Summary" | "Next Catalyst" | "Target Price" | "Change")
_awaiting_field: dict[int, str] = {}
# user_id -> "add" | "change" | "delete"
_awaiting_watchlist: dict[int, str] = {}

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
# Admin commands (only for authorised admin)
# ------------------------------------------------------------
ADMIN_USER_IDS = {1670138803}  # your Telegram user ID

def is_admin(user) -> bool:
    return bool(user and user.id in ADMIN_USER_IDS)


async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List users with Status = Pending."""
    user = update.effective_user
    if not is_admin(user):
        await update.message.reply_text("This command is for admins only.")
        return

    db_id = os.getenv("NOTION_AUTH_DB_ID") or os.getenv("NOTION_DATABASE_ID")
    if not notion or not db_id:
        await update.message.reply_text("Notion is not configured.")
        return

    try:
        response = notion.databases.query(
            database_id=db_id,
            filter={
                "property": "Status",
                "select": {"equals": "Pending"}
            },
            page_size=20,
        )
        results = response.get("results", [])

        if not results:
            await update.message.reply_text("No pending requests.")
            return

        lines = ["⏳ *Pending access requests:*\n"]
        for page in results:
            props = page.get("properties", {})
            uid = _get_plain_text(props.get("Telegram User ID")) or "—"
            name = _get_plain_text(props.get("Full Name")) or "—"
            uname = _get_plain_text(props.get("Username")) or "—"
            lines.append(f"• `{uid}` — {name} (@{uname})")

        lines.append("\nApprove with:\n`/approve <user_id>`")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    except Exception as e:
        logger.error("pending_cmd failed: %s", e)
        await update.message.reply_text(f"Error loading pending list:\n`{e}`", parse_mode="Markdown")


async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Approve a user: /approve <telegram_user_id>"""
    user = update.effective_user
    if not is_admin(user):
        await update.message.reply_text("This command is for admins only.")
        return

    if not context.args:
        await update.message.reply_text("Usage: `/approve <telegram_user_id>`", parse_mode="Markdown")
        return

    target_id = context.args[0].strip()

    db_id = os.getenv("NOTION_AUTH_DB_ID") or os.getenv("NOTION_DATABASE_ID")
    if not notion or not db_id:
        await update.message.reply_text("Notion is not configured.")
        return

    try:
        # Find the page with this Telegram User ID (Title)
        response = notion.databases.query(
            database_id=db_id,
            filter={
                "property": "Telegram User ID",
                "title": {"equals": target_id}
            },
            page_size=1,
        )
        results = response.get("results", [])

        if not results:
            await update.message.reply_text(
                f"No request found for User ID `{target_id}`.",
                parse_mode="Markdown",
            )
            return

        page_id = results[0]["id"]

        # Update Status → Authorised
        notion.pages.update(
            page_id=page_id,
            properties={
                "Status": {"select": {"name": "Authorised"}}
            },
        )

        # Clear auth cache so the change takes effect immediately
        _authorized_cache["expires"] = 0

        await update.message.reply_text(
            f"✅ User `{target_id}` is now *Authorised*.",
            parse_mode="Markdown",
        )

    except Exception as e:
        logger.error("approve_cmd failed: %s", e)
        await update.message.reply_text(f"Error approving user:\n`{e}`", parse_mode="Markdown")


async def reject_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Optional: reject a user – /reject <telegram_user_id>"""
    user = update.effective_user
    if not is_admin(user):
        await update.message.reply_text("This command is for admins only.")
        return

    if not context.args:
        await update.message.reply_text("Usage: `/reject <telegram_user_id>`", parse_mode="Markdown")
        return

    target_id = context.args[0].strip()
    db_id = os.getenv("NOTION_AUTH_DB_ID") or os.getenv("NOTION_DATABASE_ID")

    if not notion or not db_id:
        await update.message.reply_text("Notion is not configured.")
        return

    try:
        response = notion.databases.query(
            database_id=db_id,
            filter={
                "property": "Telegram User ID",
                "title": {"equals": target_id}
            },
            page_size=1,
        )
        results = response.get("results", [])

        if not results:
            await update.message.reply_text(
                f"No request found for User ID `{target_id}`.",
                parse_mode="Markdown",
            )
            return

        page_id = results[0]["id"]
        notion.pages.update(
            page_id=page_id,
            properties={
                "Status": {"select": {"name": "Rejected"}}
            },
        )
        _authorized_cache["expires"] = 0

        await update.message.reply_text(
            f"🚫 User `{target_id}` has been *Rejected*.",
            parse_mode="Markdown",
        )

    except Exception as e:
        logger.error("reject_cmd failed: %s", e)
        await update.message.reply_text(f"Error:\n`{e}`", parse_mode="Markdown")
        
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

async def save_stockpick_to_notion(
    text: str,
    user_name: str,
    ticker: str | None = None,
    period_type: str | None = None,
    period_value: str | None = None,
    user_id: int | None = None,
) -> bool:
    if not notion:
        logger.error("Notion client is None – NOTION_TOKEN missing?")
        return False

    db_id = os.getenv("NOTION_DATABASE_ID") or os.getenv("NOTION_STOCKPICKS_DB_ID")
    if not db_id:
        logger.error("NOTION_DATABASE_ID is missing")
        return False

    try:
        # Notes: period + uid for monthly limit checks
        notes_parts = []
        if period_type and period_value:
            notes_parts.append(f"{period_type}: {period_value}")
        if user_id:
            notes_parts.append(f"uid:{user_id}")
        notes = " | ".join(notes_parts)

        name = f"#{ticker}" if ticker else text[:80]
        if period_value:
            name = f"{name} ({period_value})"

        properties = {
            "Name": {
                "title": [{"text": {"content": name[:100]}}]
            },
            "Message": {
                "rich_text": [{"text": {"content": text[:2000]}}]
            },
            "Posted By": {
                "rich_text": [{"text": {"content": (user_name or "Unknown")[:200]}}]
            },
            "Source Group": {
                "rich_text": [{"text": {"content": "Telegram"}}]
            },
            "Status": {
                "select": {"name": "New"}
            },
            "Telegram Date": {
                "date": {"start": datetime.now(timezone.utc).date().isoformat()}
            },
        }

        if ticker:
            properties["Ticker"] = {
                "rich_text": [{"text": {"content": ticker}}]
            }

        if notes:
            properties["Notes"] = {
                "rich_text": [{"text": {"content": notes[:2000]}}]
            }

        page = notion.pages.create(
            parent={"database_id": db_id},
            properties=properties,
        )
        logger.info(
            "Saved #stockpick (ticker=%s, period=%s %s)",
            ticker, period_type, period_value,
        )
        return page.get("id")   # return page id instead of True
    except Exception as e:
        logger.error("Failed to write #stockpick to Notion: %s", e)
        return None             # return None instead of False
        
    except Exception as e:
        logger.error("Failed to write #stockpick to Notion: %s", e)
        return False

async def has_submitted_this_month(user) -> bool:
    """
    Return True if this Telegram user already has a #stockpick
    in the Hive Stock Picks database for the current calendar month.
    """
    if not notion or not user:
        return False

    db_id = os.getenv("NOTION_DATABASE_ID") or os.getenv("NOTION_STOCKPICKS_DB_ID")
    if not db_id:
        return False

    try:
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1).date().isoformat()

        # Next month start (for the date filter upper bound)
        if now.month == 12:
            next_month = now.replace(year=now.year + 1, month=1, day=1)
        else:
            next_month = now.replace(month=now.month + 1, day=1)
        month_end = next_month.date().isoformat()

        response = notion.databases.query(
            database_id=db_id,
            filter={
                "and": [
                    {
                        "property": "Telegram Date",
                        "date": {"on_or_after": month_start},
                    },
                    {
                        "property": "Telegram Date",
                        "date": {"before": month_end},
                    },
                ]
            },
            page_size=100,
        )

        results = response.get("results", [])
        uid_marker = f"uid:{user.id}"
        user_name = (user.full_name or "").strip().lower()

        for page in results:
            props = page.get("properties", {})

            # Prefer matching on uid stored in Notes
            notes = _get_plain_text(props.get("Notes")).lower()
            if uid_marker in notes:
                return True

            # Fallback: match Posted By name
            posted_by = _get_plain_text(props.get("Posted By")).strip().lower()
            if user_name and posted_by == user_name:
                return True

        return False

    except Exception as e:
        logger.error("has_submitted_this_month failed: %s", e)
        return False  # fail open so a Notion glitch doesn't block everyone
        
MONTH_HASHTAGS = {
    "january": "January", "jan": "January",
    "february": "February", "feb": "February",
    "march": "March", "mar": "March",
    "april": "April", "apr": "April",
    "may": "May",
    "june": "June", "jun": "June",
    "july": "July", "jul": "July",
    "august": "August", "aug": "August",
    "september": "September", "sep": "September", "sept": "September",
    "october": "October", "oct": "October",
    "november": "November", "nov": "November",
    "december": "December", "dec": "December",
}

def extract_period(text: str):
    """Returns (period_type, period_value) e.g. ("Monthly", "September") or ("Annual", "2027")."""
    if not text:
        return None, None
    tags = re.findall(r"#(\w+)", text.lower())
    month = None
    year = None
    for tag in tags:
        if tag in MONTH_HASHTAGS:
            month = MONTH_HASHTAGS[tag]
        elif re.fullmatch(r"20[2-9]\d", tag):
            year = tag
    if month:
        return "Monthly", month
    if year:
        return "Annual", year
    return None, None
    
async def stockpick_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = query.from_user
    if not user:
        return

    data = query.data or ""
    if not data.startswith("sp:"):
        return

    field = data[3:]
    page_id = _last_stockpick_page.get(user.id)
    if not page_id:
        await query.message.reply_text(
            "No recent stockpick found. Submit one with #stockpick first, "
            "or open 📌 My Stockpicks."
        )
        return

    # Lock past months (admins can still edit)
    if not is_admin(user) and notion:
        try:
            page = notion.pages.retrieve(page_id=page_id)
            props = page.get("properties", {})
            date_prop = (props.get("Telegram Date") or {}).get("date") or {}
            date_str = date_prop.get("start", "")
            if date_str and not _is_current_month(date_str):
                await query.message.reply_text(
                    "🔒 That stockpick is from a **previous month** and is locked.\n"
                    "Only this month’s pick can be changed (or ask an admin).",
                    parse_mode="Markdown",
                )
                return
        except Exception as e:
            logger.warning("Could not verify stockpick month: %s", e)

    _awaiting_field[user.id] = field

    if field == "Change":
        await query.message.reply_text(
            "Send your **new** stockpick text now (include #TICKER).\n"
            "This updates **this month’s** pick only.",
            parse_mode="Markdown",
        )
    else:
        await query.message.reply_text(
            f"Send your **{field}** now and I’ll add it to your stockpick.",
            parse_mode="Markdown",
        )
        
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

def _is_current_month(date_str: str) -> bool:
    if not date_str or date_str == "—":
        return False
    try:
        d = datetime.fromisoformat(date_str[:10]).date()
        now = datetime.now(timezone.utc).date()
        return d.year == now.year and d.month == now.month
    except Exception:
        return False


def _hub_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Add Summary", callback_data="sp:Summary"),
                InlineKeyboardButton("Next Catalyst", callback_data="sp:Next Catalyst"),
            ],
            [
                InlineKeyboardButton("Target Price", callback_data="sp:Target Price"),
                InlineKeyboardButton("Change (this month)", callback_data="sp:Change"),
            ],
            [
                InlineKeyboardButton("📌 My Stockpicks", callback_data="hub:mypicks"),
                InlineKeyboardButton("👀 My Watchlist", callback_data="hub:watchlist"),
            ],
        ]
    )
    
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

def main_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("📋 Menu"),
                KeyboardButton("📌 My Stockpick"),
                KeyboardButton("👀 My Watchlist"),
            ],
        ],
        resize_keyboard=True,
    )

def menu_inline_keyboard() -> InlineKeyboardMarkup:
    """Inline buttons for /menu."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🏠 Start", callback_data="cmd:start"),
                InlineKeyboardButton("❓ FAQ", callback_data="cmd:faq"),
            ],
            [
                InlineKeyboardButton("📊 Snap (ticker help)", callback_data="cmd:snap"),
                InlineKeyboardButton("📌 My🐝 Stockpick", callback_data="cmd:mystockpick"),
            ],
        ]
    )

# ------------------------------------------------------------
# Command handlers
# ------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    text = (update.message.text or "").strip()

    # Persistent keyboard shortcuts (match full label from buttons)
    if text in ("📋 Menu", "Menu"):
        await menu_cmd(update, context)
        return

    if text in (
        "📌 My Stockpick",
        "📌 My🐝 Stockpick",
        "My Stockpick",
        "My🐝 Stockpick",
    ):
        await mystockpick_cmd(update, context)
        return

    # My Watchlist (tolerate emoji variants)
    if text in ("👀 My Watchlist", "My Watchlist") or (
        "watchlist" in text.lower() and len(text) < 40
    ):
        try:
            await show_watchlist(update, context, edit=False)
        except Exception as e:
            logger.error("My Watchlist button failed: %s", e)
            await update.message.reply_text(
                f"Could not open watchlist.\n`{e}`",
                parse_mode="Markdown",
            )
        return

    # Watchlist follow-up (add / change / delete / list actions)
    if user and user.id in _awaiting_watchlist:
        action = _awaiting_watchlist.pop(user.id)
        await handle_watchlist_text(update, context, action, text)
        return

    # Stockpick field follow-up
    if user and user.id in _awaiting_field:
        field = _awaiting_field.pop(user.id)
        page_id = _last_stockpick_page.get(user.id)
        if not page_id or not notion:
            await update.message.reply_text("Could not update your stockpick. Please try again.")
            return

        try:
            if field == "Change":
                props = {
                    "Message": {"rich_text": [{"text": {"content": text[:2000]}}]},
                }
                tickers = extract_hashtag_tickers(text)
                if tickers:
                    props["Ticker"] = {"rich_text": [{"text": {"content": tickers[0]}}]}
                    props["Name"] = {
                        "title": [{"text": {"content": f"#{tickers[0]}"[:100]}}]
                    }
                notion.pages.update(page_id=page_id, properties=props)
                await update.message.reply_text("✅ Your stockpick has been updated.")
            else:
                notion.pages.update(
                    page_id=page_id,
                    properties={
                        field: {"rich_text": [{"text": {"content": text[:2000]}}]}
                    },
                )
                await update.message.reply_text(
                    f"✅ Added **{field}** to your stockpick.",
                    parse_mode="Markdown",
                )
        except Exception as e:
            logger.error("Failed to update stockpick field %s: %s", field, e)
            await update.message.reply_text(
                "Could not save that update. Please try again later."
            )
        return

    if not await should_reply(update, context):
        return

    # ... rest of handle_message (ticker / #stockpick) ...
    
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    name = user.first_name if user else "there"
    authorised = await is_authorized(update)

    if authorised:
        text = (
            f"Hi {name}! 👋\n\n"
            "It's 🐝 BuzzBot here.\n"
            "✅ You are *Authorised* and can use the bot.\n\n"
            "Use the buttons below, or in the group:\n"
            "• `@Bot #KEFI summary` → lookup\n"
            "• `#KEFI snapshot` → lookup\n"
            "• `#stockpick my idea...` → save your pick"
        )
    else:
        text = (
            f"Hi {name}! 👋\n\n"
            "It's 🐝 BuzzBot here.\n"
            "❌ You are *not authorised* yet.\n\n"
            "Send /request to ask for access.\n"
            "Check status with /status."
        )

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=main_reply_keyboard(),
    )

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the main menu."""
    await update.message.reply_text(
        "🐝 *BuzzBot Menu*\n\n"
        "• /start – Welcome & status\n"
        "• /status – Check if you are authorised\n"
        "• /request – Request access\n"
        "• /snap – How to look up a ticker\n"
        "• /mystockpick – Your stockpick this month\n"
        "• /faq – FAQ\n\n"
        "In the group: `@Bot #TICKER` to look up a stock\n"
        "Or use `#stockpick your idea` to save one.",
        parse_mode="Markdown",
        reply_markup=menu_inline_keyboard() if "menu_inline_keyboard" in dir() else None,
    )

async def menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = (query.data or "").replace("cmd:", "")

    # Reuse existing handlers by faking a simple flow
    class _FakeMsg:
        def __init__(self, q):
            self._q = q
        async def reply_text(self, *a, **k):
            return await self._q.message.reply_text(*a, **k)

    fake_update = update
    # Easier: just reply based on command
    if data == "start":
        await query.message.reply_text("Send /start to refresh the welcome screen.")
    elif data == "faq":
        await faq(update, context)
    elif data == "snap":
        await snap_cmd(update, context)
    elif data == "mystockpick":
        await mystockpick_cmd(update, context)

async def faq(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📌 *FAQ*\n\n"
        "• Data is pulled live from the curated UK AIM Micro-Cap database.\n"
        "• This is *not* financial advice – always DYOR.\n"
        "• Use `#stockpick` in the group to log ideas.\n"
        "• Contact a human admin in The Hive group if something looks wrong.",
        parse_mode="Markdown",
    )

async def snap_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📊 *Company snapshot*\n\n"
        "In the group or here, use a hashtag ticker:\n"
        "• `#KEFI summary`\n"
        "• `#ALRT snapshot`\n"
        "• `@Bot #AVCT thesis`\n\n"
        "I’ll pull the live UK AIM Micro-Cap snapshot.",
        parse_mode="Markdown",
        reply_markup=main_reply_keyboard(),
    )

async def mystockpick_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    if not await is_authorized(update):
        await update.message.reply_text(
            "🔒 Only authorised members can use this.\n"
            "Send /request to ask for access."
        )
        return

    await update.message.reply_text(
        "📌 *My Stockpick hub*\n\n"
        "• Edit **this month’s** pick with the buttons below\n"
        "• Open **My Stockpicks** for your full history\n"
        "• Open **My Watchlist** to manage tickers\n\n"
        "_Past months’ stockpicks are locked (admin can still change them)._",
        parse_mode="Markdown",
        reply_markup=_hub_keyboard(),
    )

async def show_my_stockpicks(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool = False
) -> None:
    user = update.effective_user
    msg = update.callback_query.message if update.callback_query else update.message

    if not await is_authorized(update):
        text = "🔒 Authorised members only."
        if edit:
            await msg.edit_text(text)
        else:
            await msg.reply_text(text)
        return

    db_id = os.getenv("NOTION_DATABASE_ID") or os.getenv("NOTION_STOCKPICKS_DB_ID")
    if not notion or not db_id:
        await msg.reply_text("Stockpick database is not configured.")
        return

    try:
        response = notion.databases.query(database_id=db_id, page_size=50)
        uid_marker = f"uid:{user.id}"
        user_name = (user.full_name or "").strip().lower()
        rows = []

        for page in response.get("results", []):
            props = page.get("properties", {})
            notes = _get_plain_text(props.get("Notes")).lower()
            posted_by = _get_plain_text(props.get("Posted By")).strip().lower()
            if uid_marker not in notes and posted_by != user_name:
                continue

            date_prop = (props.get("Telegram Date") or {}).get("date") or {}
            date_str = date_prop.get("start", "—")
            rows.append(
                {
                    "page_id": page["id"],
                    "date": date_str,
                    "ticker": _get_plain_text(props.get("Ticker")) or "—",
                    "summary": _get_plain_text(props.get("Summary")) or "—",
                    "catalyst": _get_plain_text(props.get("Next Catalyst")) or "—",
                    "target": _get_plain_text(props.get("Target Price")) or "—",
                    "current": _is_current_month(date_str),
                }
            )

        if not rows:
            text = (
                "You have no stockpicks yet.\n"
                "Post `#stockpick #TICKER your idea` in the group."
            )
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("« Back", callback_data="hub:home")]]
            )
            if edit:
                await msg.edit_text(text, reply_markup=kb)
            else:
                await msg.reply_text(text, reply_markup=kb)
            return

        rows.sort(key=lambda r: r["date"], reverse=True)
        lines = [
            "📌 *My Stockpicks*\n",
            "_🔒 = previous month (locked). Only this month can be edited._\n",
        ]
        for r in rows[:12]:
            lock = "" if r["current"] else " 🔒"
            lines.append(
                f"• *{r['date']}* | `#{r['ticker']}`{lock}\n"
                f"  Summary: {r['summary'][:80]}\n"
                f"  Catalyst: {r['catalyst'][:60]}\n"
                f"  Target: {r['target']}\n"
            )

        current = next((r for r in rows if r["current"]), None)
        keyboard = []
        if current:
            _last_stockpick_page[user.id] = current["page_id"]
            keyboard.extend(
                [
                    [
                        InlineKeyboardButton("Add Summary", callback_data="sp:Summary"),
                        InlineKeyboardButton(
                            "Next Catalyst", callback_data="sp:Next Catalyst"
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "Target Price", callback_data="sp:Target Price"
                        ),
                        InlineKeyboardButton(
                            "Change (this month)", callback_data="sp:Change"
                        ),
                    ],
                ]
            )
        elif is_admin(user) and rows:
            _last_stockpick_page[user.id] = rows[0]["page_id"]
            keyboard.append(
                [
                    InlineKeyboardButton(
                        "Admin: edit latest", callback_data="sp:Change"
                    )
                ]
            )

        keyboard.append(
            [InlineKeyboardButton("« Back to hub", callback_data="hub:home")]
        )

        text = "\n".join(lines)
        markup = InlineKeyboardMarkup(keyboard)
        if edit:
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=markup)
        else:
            await msg.reply_text(text, parse_mode="Markdown", reply_markup=markup)

    except Exception as e:
        logger.error("show_my_stockpicks failed: %s", e)
        await msg.reply_text("Could not load your stockpicks right now.")

async def show_watchlist(
        if update.callback_query:
        msg = update.callback_query.message
    else:
        msg = update.message

    if not msg:
        return
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool = False
) -> None:
    user = update.effective_user
    msg = update.callback_query.message if update.callback_query else update.message

    if not await is_authorized(update):
        text = "🔒 Authorised members only."
        if edit:
            await msg.edit_text(text)
        else:
            await msg.reply_text(text)
        return

    db_id = (os.getenv("NOTION_WATCHLIST_DB_ID") or "").strip()
    if not notion or not db_id:
        await msg.reply_text(
            "Watchlist is not configured.\n"
            "Admin: set NOTION_WATCHLIST_DB_ID in Railway."
        )
        return

    try:
        # Prefer filtered query; fall back to unfiltered if filter type fails
        try:
            response = notion.databases.query(
                database_id=db_id,
                filter={
                    "property": "Telegram User ID",
                    "rich_text": {"equals": str(user.id)},
                },
                page_size=30,
            )
        except Exception:
            response = notion.databases.query(database_id=db_id, page_size=50)

        results = []
        for page in response.get("results", []):
            props = page.get("properties", {})
            owner = _get_plain_text(props.get("Telegram User ID")).strip()
            if owner and owner != str(user.id):
                continue
            results.append(page)

        lines = [
            "My Watchlist",
            "",
            "Ticker | Name | Link",
            "",
        ]
        if not results:
            lines.append("Empty — use Edit Watchlist to add tickers.")
        else:
            for page in results:
                props = page.get("properties", {})
                ticker = _get_plain_text(props.get("Ticker")) or "-"
                name = _get_plain_text(props.get("Name")) or "-"
                url = ((props.get("Group Link") or {}).get("url") or "").strip()
                link_txt = url if url else "-"
                lines.append(f"#{ticker} | {name} | {link_txt}")

        keyboard = [
            [
                InlineKeyboardButton("Create New Watchlist", callback_data="wl:create"),
                InlineKeyboardButton("Edit Watchlist", callback_data="wl:edit_menu"),
            ],
            [
                InlineKeyboardButton("Rename Watchlist", callback_data="wl:rename"),
                InlineKeyboardButton("Delete Watchlist", callback_data="wl:delete_list"),
            ],
            [
                InlineKeyboardButton("👀 My Watchlist", callback_data="hub:watchlist"),
                InlineKeyboardButton("« Back to Hub", callback_data="hub:home"),
            ],
        ]

        text = "\n".join(lines)
        markup = InlineKeyboardMarkup(keyboard)

        if edit:
            await msg.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
        else:
            await msg.reply_text(text, reply_markup=markup, disable_web_page_preview=True)

    except Exception as e:
        logger.error("show_watchlist failed: %s", e)
        await msg.reply_text(
            "Could not load watchlist.\n\n"
            f"Error: {str(e)[:300]}\n\n"
            "Check: integration shared with Watchlist DB + NOTION_WATCHLIST_DB_ID."
        )
        
async def hub_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data == "hub:watchlist":
        await show_watchlist(update, context, edit=True)
    elif data == "hub:mypicks":
        await show_my_stockpicks(update, context, edit=True)
    elif data == "hub:home":
        await query.edit_message_text(
            "📌 *My Stockpick hub*\n\nChoose an option:",
            parse_mode="Markdown",
            reply_markup=_hub_keyboard(),
        )

async def watchlist_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = query.from_user
    if not user:
        return

    action = (query.data or "").replace("wl:", "")

    # --- List-level actions ---
    if action == "create":
        _awaiting_watchlist[user.id] = "create_list"
        await query.message.reply_text(
            "🆕 *Create New Watchlist*\n\n"
            "Send a name for the list, e.g.\n"
            "`UK AIM Growth`",
            parse_mode="Markdown",
        )
        return

    if action == "rename":
        _awaiting_watchlist[user.id] = "rename_list"
        await query.message.reply_text(
            "✏️ *Rename Watchlist*\n\n"
            "Send: `Old Name | New Name`",
            parse_mode="Markdown",
        )
        return

    if action == "delete_list":
        _awaiting_watchlist[user.id] = "delete_list"
        await query.message.reply_text(
            "🗑 *Delete Watchlist*\n\n"
            "Send the list name to delete, e.g. `UK AIM Growth`\n"
            "This removes **all tickers** in that list.",
            parse_mode="Markdown",
        )
        return

    if action == "edit_menu":
        keyboard = [
            [
                InlineKeyboardButton("➕ Add ticker", callback_data="wl:add"),
                InlineKeyboardButton("✏️ Change ticker", callback_data="wl:change"),
            ],
            [
                InlineKeyboardButton("🗑 Delete ticker", callback_data="wl:delete"),
            ],
            [
                InlineKeyboardButton("👀 My Watchlist", callback_data="hub:watchlist"),
                InlineKeyboardButton("« Back to Hub", callback_data="hub:home"),
            ],
        ]
        await query.edit_message_text(
            "✏️ *Edit Watchlist*\n\nChoose what to change:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # --- Ticker-level actions ---
    if action not in ("add", "change", "delete"):
        return

    _awaiting_watchlist[user.id] = action

    if action == "add":
        await query.message.reply_text(
            "➕ *Add ticker*\n\n"
            "Send one line:\n"
            "`#TICKER | Company Name | https://t.me/+invite`\n\n"
            "Example:\n"
            "`#ALRT | Defence Holdings | https://t.me/+abc123`",
            parse_mode="Markdown",
        )
    elif action == "change":
        await query.message.reply_text(
            "✏️ *Change ticker*\n\n"
            "Send:\n"
            "`#TICKER | New Name | https://t.me/+newlink`",
            parse_mode="Markdown",
        )
    else:
        await query.message.reply_text(
            "🗑 *Delete ticker*\n\nSend the ticker only, e.g. `#ALRT`",
            parse_mode="Markdown",
        )

async def handle_watchlist_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, text: str
) -> None:
    user = update.effective_user
    db_id = os.getenv("NOTION_WATCHLIST_DB_ID")
    if not notion or not db_id or not user:
        await update.message.reply_text("Watchlist is not available right now.")
        return

    tickers = extract_hashtag_tickers(text)
    ticker = tickers[0] if tickers else None

    try:
        if action == "delete":
            if not ticker:
                await update.message.reply_text("Send a ticker like `#ALRT`.")
                return
            response = notion.databases.query(
                database_id=db_id,
                filter={
                    "and": [
                        {
                            "property": "Telegram User ID",
                            "rich_text": {"equals": str(user.id)},
                        },
                        {"property": "Ticker", "title": {"equals": ticker}},
                    ]
                },
                page_size=5,
            )
            results = response.get("results", [])
            if not results:
                await update.message.reply_text(
                    f"No watchlist item `#{ticker}` found.", parse_mode="Markdown"
                )
                return
            for page in results:
                notion.pages.update(page_id=page["id"], archived=True)
            await update.message.reply_text(
                f"🗑 Removed `#{ticker}` from your watchlist.", parse_mode="Markdown"
            )
            return

        parts = [p.strip() for p in text.split("|")]
        name = parts[1] if len(parts) > 1 else ""
        link = parts[2] if len(parts) > 2 else ""

        if not ticker:
            await update.message.reply_text(
                "Include a ticker, e.g. `#ALRT | Name | https://t.me/+...`"
            )
            return

        if action == "add":
            props["List Name"] = {
    			"rich_text": [{"text": {"content": "Default"}}]
				}
            props = {
                "Ticker": {"title": [{"text": {"content": ticker}}]},
                "Name": {"rich_text": [{"text": {"content": name[:200]}}]},
                "Telegram User ID": {
                    "rich_text": [{"text": {"content": str(user.id)}}]
                },
            }
            if link.startswith("http"):
                props["Group Link"] = {"url": link}
            if user.username:
                props["Username"] = {
                    "rich_text": [{"text": {"content": user.username}}]
                }
            notion.pages.create(parent={"database_id": db_id}, properties=props)
            await update.message.reply_text(
                f"✅ Added `#{ticker}` to your watchlist.", parse_mode="Markdown"
            )
            return

        # change
        response = notion.databases.query(
            database_id=db_id,
            filter={
                "and": [
                    {
                        "property": "Telegram User ID",
                        "rich_text": {"equals": str(user.id)},
                    },
                    {"property": "Ticker", "title": {"equals": ticker}},
                ]
            },
            page_size=1,
        )
        results = response.get("results", [])
        if not results:
            await update.message.reply_text(
                f"No watchlist item `#{ticker}` found.", parse_mode="Markdown"
            )
            return
        props = {}
        if name:
            props["Name"] = {"rich_text": [{"text": {"content": name[:200]}}]}
        if link.startswith("http"):
            props["Group Link"] = {"url": link}
        if props:
            notion.pages.update(page_id=results[0]["id"], properties=props)
        await update.message.reply_text(
            f"✅ Updated `#{ticker}`.", parse_mode="Markdown"
        )

    except Exception as e:
        logger.error("handle_watchlist_text failed: %s", e)
        await update.message.reply_text(
            "Could not update watchlist. Check Notion property names / types."
        )
        
async def create_access_request(user) -> tuple[bool, str]:
    """Create a Pending access request in Notion."""
    if not notion:
        return False, "Notion client is not initialised (NOTION_TOKEN missing?)"

    db_id = os.getenv("NOTION_AUTH_DB_ID") or os.getenv("NOTION_DATABASE_ID")
    if not db_id:
        return False, "NOTION_AUTH_DB_ID / NOTION_DATABASE_ID is missing"

    try:
        properties = {
            # Title property
            "Telegram User ID": {
                "title": [{"text": {"content": str(user.id)}}]
            },
            "Status": {
                "select": {"name": "Pending"}
            },
            "Full Name": {
                "rich_text": [{"text": {"content": (user.full_name or "Unknown")[:100]}}]
            },
        }

        if user.username:
            properties["Username"] = {
                "rich_text": [{"text": {"content": user.username}}]
            }

        # Optional: Date Added
        properties["Date Added"] = {
            "date": {"start": datetime.now(timezone.utc).date().isoformat()}
        }

        notion.pages.create(
            parent={"database_id": db_id},
            properties=properties,
        )
        return True, "OK"

    except Exception as e:
        logger.error("Failed to create access request: %s", e)
        return False, str(e)

async def request_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    if await is_authorized(update):
        await update.message.reply_text("You are already authorised. You can use the bot.")
        return

    success, info = await create_access_request(user)

    if success:
        await update.message.reply_text(
            "✅ *Access request submitted*\n\n"
            f"• Name: {user.full_name}\n"
            f"• Username: @{user.username or 'N/A'}\n"
            f"• Telegram User ID: `{user.id}`\n\n"
            "Your request is now *Pending*.\n\n"
            "⏳ *Next step:*\n"
            "Please wait for an admin to change your Status to *Authorised* in Notion.\n\n"
            "Check your status anytime with /status.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "❌ Could not submit your request.\n\n"
            f"Error:\n`{info}`",
            parse_mode="Markdown",
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
        
async def schema_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Temporary: list properties of the auth database."""
    db_id = os.getenv("NOTION_AUTH_DB_ID") or os.getenv("NOTION_DATABASE_ID")
    if not notion or not db_id:
        await update.message.reply_text("Notion or database ID missing.")
        return

    try:
        db = notion.databases.retrieve(database_id=db_id)
        props = db.get("properties", {})
        lines = ["📋 *Database properties:*\n"]
        for name, info in props.items():
            lines.append(f"• `{name}`  ({info.get('type')})")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Error:\n`{e}`", parse_mode="Markdown")        
        
# ------------------------------------------------------------
# Authorisation (Status = "Authorised" required)
# ------------------------------------------------------------
_authorized_cache: dict = {"users": {}, "expires": 0}
AUTH_CACHE_TTL = 300  # 5 minutes

NOTION_AUTH_DB_ID = os.getenv("NOTION_AUTH_DB_ID") or os.getenv("NOTION_DATABASE_ID")

async def get_authorized_users() -> dict:
    """Load users where Status == Authorised."""
    if not notion:
        return {"usernames": set(), "user_ids": set()}

    db_id = os.getenv("NOTION_AUTH_DB_ID") or os.getenv("NOTION_DATABASE_ID")
    if not db_id:
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
                "database_id": db_id,
                "page_size": 100,
                "filter": {
                    "property": "Status",
                    "select": {"equals": "Authorised"}
                },
            }
            if cursor:
                kwargs["start_cursor"] = cursor

            response = notion.databases.query(**kwargs)

            for page in response.get("results", []):
                props = page.get("properties", {})

                # Title = Telegram User ID
                uid = _get_plain_text(props.get("Telegram User ID"))
                if uid:
                    user_ids.add(uid.strip())

                # Username
                uname = _get_plain_text(props.get("Username"))
                if uname:
                    usernames.add(uname.strip().lstrip("@").lower())

            if not response.get("has_more"):
                break
            cursor = response.get("next_cursor")

        result = {"usernames": usernames, "user_ids": user_ids}
        _authorized_cache["users"] = result
        _authorized_cache["expires"] = now + AUTH_CACHE_TTL
        logger.info("Loaded %d authorised usernames, %d user IDs", len(usernames), len(user_ids))
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

async def find_this_month_stockpick_page(user) -> str | None:
    """Return Notion page id for this user's stockpick in the current month."""
    if not notion or not user:
        return None
    db_id = os.getenv("NOTION_DATABASE_ID") or os.getenv("NOTION_STOCKPICKS_DB_ID")
    if not db_id:
        return None
    try:
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1).date().isoformat()
        if now.month == 12:
            next_month = now.replace(year=now.year + 1, month=1, day=1)
        else:
            next_month = now.replace(month=now.month + 1, day=1)
        month_end = next_month.date().isoformat()

        response = notion.databases.query(
            database_id=db_id,
            filter={
                "and": [
                    {"property": "Telegram Date", "date": {"on_or_after": month_start}},
                    {"property": "Telegram Date", "date": {"before": month_end}},
                ]
            },
            page_size=100,
        )
        uid_marker = f"uid:{user.id}"
        user_name = (user.full_name or "").strip().lower()

        for page in response.get("results", []):
            props = page.get("properties", {})
            notes = _get_plain_text(props.get("Notes")).lower()
            if uid_marker in notes:
                return page["id"]
            posted_by = _get_plain_text(props.get("Posted By")).strip().lower()
            if user_name and posted_by == user_name:
                return page["id"]
        return None
    except Exception as e:
        logger.error("find_this_month_stockpick_page failed: %s", e)
        return None

        user_name = user.full_name if user else "Unknown"
        tickers = extract_hashtag_tickers(clean_text)
        ticker = tickers[0] if tickers else None
        period_type, period_value = extract_period(clean_text)

        page_id = await save_stockpick_to_notion(
            clean_text,
            user_name,
            ticker,
            period_type,
            period_value,
            user_id=user.id if user else None,
        )

        if page_id:
            _last_stockpick_page[user.id] = page_id

            reply = "✅ Captured your #stockpick"
            if ticker:
                reply += f" (#{ticker})"
            if period_type and period_value:
                reply += f"\n📅 {period_type}: *{period_value}*"
            reply += "\nYour pick has been saved.\n\nWhat would you like to do next?"

            keyboard = [
                [
                    InlineKeyboardButton("Add Summary", callback_data="sp:Summary"),
                    InlineKeyboardButton("Next Catalyst", callback_data="sp:Next Catalyst"),
                ],
                [
                    InlineKeyboardButton("Target Price", callback_data="sp:Target Price"),
                    InlineKeyboardButton("Change my stockpick", callback_data="sp:Change"),
                ],
            ]
            await update.message.reply_text(
                reply,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        else:
            await update.message.reply_text(
                "✅ Received your #stockpick.\n"
                "(Could not save it right now – please try again later or contact an admin.)"
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

    # Public commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("faq", faq))
    app.add_handler(CommandHandler("snap", snap_cmd))
    app.add_handler(CommandHandler("mystockpick", mystockpick_cmd))
    # keep /help as alias if you want
    app.add_handler(CommandHandler("help", menu_cmd))
    app.add_handler(CommandHandler("tickers", snap_cmd))
    app.add_handler(CallbackQueryHandler(stockpick_button, pattern=r"^sp:"))
    app.add_handler(CallbackQueryHandler(hub_button, pattern=r"^hub:"))
    app.add_handler(CallbackQueryHandler(watchlist_button, pattern=r"^wl:"))
    app.add_handler(CallbackQueryHandler(menu_button, pattern=r"^cmd:"))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("request", request_access))
    app.add_handler(CommandHandler("debug", debug_cmd))
    app.add_handler(CommandHandler("schema", schema_cmd))
    
    # Admin commands
    app.add_handler(CommandHandler("pending", pending_cmd))
    app.add_handler(CommandHandler("approve", approve_cmd))
    app.add_handler(CommandHandler("reject", reject_cmd))

    # Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Hive SupportBot starting...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
    
