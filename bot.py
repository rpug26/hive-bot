#!/usr/bin/env python3
"""
Hive SupportBot – AIM/Small Cap knowledge bot
Live data from Notion + #stockpick capture
Strict group behaviour: only responds on @mention + #ticker
or #ticker + intent keywords (summary, snapshot, thesis, etc.)
"""

import os
import re
import asyncio
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
NOTION_TICKERS_DATA_SOURCE_ID = (
    os.getenv("NOTION_TICKERS_DATA_SOURCE_ID") or "09a9cf15-ffef-4f86-8767-db104ea66e11"
).strip()
# Auth DB container + data source (required for multi-source Notion API 2025-09-03+)
NOTION_AUTH_DB_ID_ENV = (
    os.getenv("NOTION_AUTH_DB_ID") or os.getenv("NOTION_DATABASE_ID") or ""
).strip()
# Hive Bot Authorised Users data source id (from collection://…)
NOTION_AUTH_DATA_SOURCE_ID = (
    os.getenv("NOTION_AUTH_DATA_SOURCE_ID") or "fd1e050c-5396-448d-a2d5-c4749a0cc69e"
).strip()

# Standalone request history (one row per request)
NOTION_HISTORY_DB_ID = (
    os.getenv("NOTION_HISTORY_DB_ID") or "3dbe81bb-7bfb-4def-ab05-80eac9b0c009"
).strip()
NOTION_HISTORY_DATA_SOURCE_ID = (
    os.getenv("NOTION_HISTORY_DATA_SOURCE_ID") or "aee62b48-46a5-4cbc-8237-af8912a3f22c"
).strip()

# Hive Bot Watchlist
NOTION_WATCHLIST_DB_ID = (
    os.getenv("NOTION_WATCHLIST_DB_ID") or "d587a125-e6c3-4597-af2d-440d09cc49ac"
).strip()
NOTION_WATCHLIST_DATA_SOURCE_ID = (
    os.getenv("NOTION_WATCHLIST_DATA_SOURCE_ID")
    or "dedbbebf-df4a-4c06-8ffb-0e5945315350"
).strip()

# RNS News Log (latest regulatory news per ticker)
NOTION_RNS_DB_ID = (
    os.getenv("NOTION_RNS_DB_ID") or "a7931699-9ab9-4fe6-8a81-74d86146ae1a"
).strip()
NOTION_RNS_DATA_SOURCE_ID = (
    os.getenv("NOTION_RNS_DATA_SOURCE_ID") or "1ccae0d5-0186-4e9b-87d2-6e42f9273ec5"
).strip()

# ticker -> (fetched_at_epoch, rns_dict|None)
_rns_cache: dict[str, tuple[float, dict | None]] = {}
RNS_CACHE_TTL_SECONDS = 120

notion = Client(auth=NOTION_TOKEN) if NOTION_TOKEN else None

if not notion:
    logger.warning("Notion credentials missing – live lookup and #stockpick write disabled")


def _notion_http(method: str, path: str, body: dict | None = None) -> dict:
    """
    Raw Notion REST call with API version that supports multi-source databases.
    path is relative, e.g. 'data_sources/{id}/query'
    """
    import json as _json
    import urllib.error
    import urllib.request

    if not NOTION_TOKEN:
        raise RuntimeError("NOTION_TOKEN missing")

    url = f"https://api.notion.com/v1/{path.lstrip('/')}"
    data = None if body is None else _json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method.upper(),
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Notion-Version": "2025-09-03",
            "Content-Type": "application/json",
        },
    )
    try:
        # Keep interactive bot calls snappy (was 30s → multi-minute hangs)
        with urllib.request.urlopen(req, timeout=12) as resp:
            return _json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Notion HTTP {e.code}: {err_body}") from e


def notion_query_data_source(
    *,
    data_source_id: str | None = None,
    database_id: str | None = None,
    **kwargs,
) -> dict:
    """
    Query a Notion table in a way that works with multi-source databases.
    Prefer data_sources/{id}/query (API 2025-09-03+); fall back to databases.query.
    """
    if not notion and not NOTION_TOKEN:
        raise RuntimeError("Notion client is not initialised")

    ds_id = (data_source_id or "").strip() or None
    db_id = (database_id or "").strip() or None

    if ds_id:
        try:
            body = dict(kwargs)
            body.pop("database_id", None)
            return _notion_http("POST", f"data_sources/{ds_id}/query", body)
        except Exception as e:
            logger.warning(
                "data_sources.query failed for %s: %s – trying databases.query",
                ds_id,
                e,
            )

    if not db_id:
        raise ValueError("Need data_source_id or database_id for Notion query")
    # Always use HTTP helper (has timeout) — SDK can hang with no timeout
    body = dict(kwargs)
    return _notion_http("POST", f"databases/{db_id}/query", body)


def notion_create_page_in_data_source(
    *,
    properties: dict,
    data_source_id: str | None = None,
    database_id: str | None = None,
) -> dict:
    """Create a page under a data source (multi-source safe)."""
    if not notion and not NOTION_TOKEN:
        raise RuntimeError("Notion client is not initialised")

    ds_id = (data_source_id or "").strip() or None
    db_id = (database_id or "").strip() or None

    if ds_id:
        try:
            return _notion_http(
                "POST",
                "pages",
                {
                    "parent": {
                        "type": "data_source_id",
                        "data_source_id": ds_id,
                    },
                    "properties": properties,
                },
            )
        except Exception as e:
            logger.warning(
                "pages.create with data_source_id failed (%s): %s – trying database_id",
                ds_id,
                e,
            )

    if not db_id:
        raise ValueError("Need data_source_id or database_id to create Notion page")
    if notion:
        return notion.pages.create(
            parent={"database_id": db_id},
            properties=properties,
        )
    return _notion_http(
        "POST",
        "pages",
        {"parent": {"database_id": db_id}, "properties": properties},
    )


# ------------------------------------------------------------
# Caches
# ------------------------------------------------------------
_ticker_cache: dict[str, dict] = {}
CACHE_TTL_SECONDS = 600  # 10 minutes

# user_id -> last stockpick Notion page_id this month
_last_stockpick_page: dict[int, str] = {}
# Minimal UI: track the single "active panel" message per user to delete on next nav
_nav_panel: dict[int, dict] = {}  # user_id -> {chat_id, message_id}
# My Stockpick hub expand/collapse + month cursor
_msp_ui: dict[int, dict] = {}  # user_id -> flags + league_ym / hist_ym
# user_id -> waiting field name ("Summary" | "Next Catalyst" | "Target Price" | "Change")
_awaiting_field: dict[int, str] = {}
# user_id -> "add" | "change" | "delete"
_awaiting_watchlist: dict[int, str] = {}
# user_id -> True while waiting for link search query
_awaiting_link: dict[int, bool] = {}
# user_id -> waiting for ticker input after Stock Snapshot button
_awaiting_snapshot: dict[int, bool] = {}
# admin_id -> Group Links request they are fulfilling (paste URL next)
_awaiting_admin_glink: dict[int, dict] = {}
# short req_id -> pending Group Links request details
_glink_requests: dict[str, dict] = {}
# Stock of the Day RNS likes: "YYYY-MM-DD:TICKER" -> set of user_ids who liked
_sotd_rns_likes: dict[str, set[int]] = {}
# Cache last top-5 RNS payload per day so Like can refresh the board
_sotd_rns_top_cache: dict[str, list[dict]] = {}
# Throttle Notion Group Member sync (user_id -> last unix ts)
_group_member_sync_at: dict[int, float] = {}
_GROUP_MEMBER_SYNC_TTL = 3600.0  # seconds — do not rewrite Notion every request
# Live Telegram membership cache (user_id -> (expires_ts, is_member, detail)
_group_member_cache: dict[int, tuple[float, bool, str]] = {}
_GROUP_MEMBER_CACHE_TTL = 300.0  # 5 minutes — avoid hanging getChatMember every tap
# Daily Brief caches (per UTC day)
_daily_brief_rns: dict[str, list[dict]] = {}  # day -> ranked RNS rows
_daily_brief_pct: dict[str, list[tuple[str, str, float]]] = {}  # day -> ranked %


def _sotd_like_key(day: str, ticker: str) -> str:
    return f"{day}:{(ticker or '').upper().strip()}"


def _sotd_like_count(day: str, ticker: str) -> int:
    return len(_sotd_rns_likes.get(_sotd_like_key(day, ticker), set()))


def _sotd_user_liked(day: str, ticker: str, user_id: int) -> bool:
    return user_id in _sotd_rns_likes.get(_sotd_like_key(day, ticker), set())


def _sotd_add_like(day: str, ticker: str, user_id: int) -> tuple[bool, int]:
    """
    Record one like per user per ticker per day.
    Returns (newly_added, total_count).
    """
    key = _sotd_like_key(day, ticker)
    bucket = _sotd_rns_likes.setdefault(key, set())
    if user_id in bucket:
        return False, len(bucket)
    bucket.add(user_id)
    return True, len(bucket)
_active_watchlist_name: dict[int, str] = {}
# user_id -> {chat_id, panel_msg_id} for seamless in-place watchlist UI
_watchlist_ui: dict[int, dict] = {}  # panel msg tracking: chat_id, panel_msg_id, ...
_watchlist_page: dict[int, int] = {}  # user_id -> page index (0-based)
_watchlist_sort: dict[int, str] = {}  # user_id -> rns | pct | priority | name
# Expand/collapse flags for My Watchlist keyboard sections (separate from panel ids)
_watchlist_kb: dict[int, dict] = {}  # user_id -> {sort_open, manage_open, lists_open}
WATCHLIST_PAGE_SIZE = 3
MAX_WATCHLISTS = 3


def _wl_ui(user_id: int) -> dict:
    """Keyboard expand/collapse state for My Watchlist (not panel message ids)."""
    st = _watchlist_kb.get(user_id)
    if not st:
        st = {"sort_open": False, "manage_open": False, "lists_open": False}
        _watchlist_kb[user_id] = st
    return st
# Single auth cache: usernames + user_ids where Notion Status = Authorised
_authorized_cache: dict = {
    "usernames": set(),
    "user_ids": set(),
    "expires": 0,
}
AUTH_CACHE_TTL = 60  # short TTL so admin approvals apply quickly

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

async def chatid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    await update.message.reply_text(
        f"Chat title: {chat.title if chat else 'N/A'}\n"
        f"Chat type: {chat.type if chat else 'N/A'}\n"
        f"Chat ID: {chat.id if chat else 'N/A'}\n"
        f"Your user ID: {user.id if user else 'N/A'}"
    )
    
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
        response = notion_query_data_source(
            data_source_id=NOTION_AUTH_DATA_SOURCE_ID,
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
        response = notion_query_data_source(
            data_source_id=NOTION_AUTH_DATA_SOURCE_ID,
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

async def notify_admins_of_request(
    context: ContextTypes.DEFAULT_TYPE,
    user,
    *,
    in_group: bool,
) -> None:
    """DM all admins with a new access request + Approve / Denied / Pending buttons."""
    text = (
        "🔔 *New Bot Access request*\n\n"
        f"• Name: {user.full_name or '—'}\n"
        f"• Username: @{user.username or 'N/A'}\n"
        f"• Telegram ID: `{user.id}`\n"
        f"• Group member: {'Yes' if in_group else 'No'}\n"
        f"• Status: *Pending*\n\n"
        "Choose an action:\n"
        "• ✅ *Approved* – grant access (Status = Authorised)\n"
        "• 🚫 *Denied* – reject access (Status = Blocked)\n"
        "• ⏳ *Pending* – leave in queue for later review\n\n"
        "Commands:\n"
        f"`/approve {user.id}`  `/reject {user.id}`  `/pending`"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Approved", callback_data=f"admin:approve:{user.id}"
                ),
                InlineKeyboardButton(
                    "🚫 Denied", callback_data=f"admin:reject:{user.id}"
                ),
                InlineKeyboardButton(
                    "⏳ Pending", callback_data=f"admin:keeppending:{user.id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    "📋 All pending", callback_data="admin:pending"
                ),
            ],
        ]
    )
    for admin_id in ADMIN_USER_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.error("Failed to notify admin %s: %s", admin_id, e)
            
async def admin_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = query.from_user

    if not is_admin(user):
        await query.answer("Admins only.", show_alert=True)
        return

    data = query.data or ""
    parts = data.split(":")
    # admin:pending | admin:approve:123 | admin:reject:123
    if len(parts) < 2:
        return

    action = parts[1]
    db_id = os.getenv("NOTION_AUTH_DB_ID") or os.getenv("NOTION_DATABASE_ID")

    # ----- Group Links request management -----
    if action in ("glinkadd", "glinkdone", "glinkpending"):
        if len(parts) < 3:
            return
        req_id = parts[2].strip()
        req = _glink_requests.get(req_id)
        if not req:
            await query.message.reply_text(
                "This Group Links request expired or is unknown. Ask the user to search again."
            )
            return

        target_id = req["user_id"]
        search_q = req.get("query") or req.get("ticker") or ""

        if action == "glinkpending":
            await query.message.reply_text(
                f"⏳ Marked *Pending* for request `{req_id}` (`{search_q}`).\n"
                f"User `{target_id}` has not been notified yet.",
                parse_mode="Markdown",
            )
            return

        if action == "glinkdone":
            try:
                await context.bot.send_message(
                    chat_id=int(target_id),
                    text=(
                        "🔗 Your Group Links request has been updated.\n\n"
                        + (f"Search again for: {search_q}\n" if search_q else "")
                        + "Tap 🔗 Group Links to look it up."
                    ),
                )
                await query.message.reply_text(
                    f"✅ User `{target_id}` notified for `{search_q}`.",
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.error("glinkdone notify failed: %s", e)
                await query.message.reply_text(f"Could not notify user: {e}")
            return

        if action == "glinkadd":
            _awaiting_admin_glink[user.id] = {
                "req_id": req_id,
                "user_id": target_id,
                "query": search_q,
                "page_id": req.get("page_id"),
                "ticker": req.get("ticker") or search_q,
            }
            await query.message.reply_text(
                f"➕ *Add new group link*\n\n"
                f"Request: `{req_id}`\n"
                f"Search / ticker: `{search_q}`\n"
                f"Requester: `{target_id}`\n\n"
                "Send the Telegram group invite link now (e.g. `https://t.me/+xxxx`).\n"
                "It will be saved to Notion and the user will be notified.",
                parse_mode="Markdown",
            )
            return

    # ----- List all pending -----
    if action == "pending":
        if not notion or not db_id:
            await query.message.reply_text("Notion is not configured.")
            return
        try:
            response = notion.databases.query(
                database_id=db_id,
                filter={
                    "property": "Status",
                    "select": {"equals": "Pending"},
                },
                page_size=20,
            )
            results = response.get("results", [])
            if not results:
                await query.message.reply_text("No pending requests.")
                return

            lines = ["⏳ *Pending access requests:*\n"]
            rows = []
            for page in results:
                props = page.get("properties", {})
                uid = _get_plain_text(props.get("Telegram User ID")) or "—"
                name = _get_plain_text(props.get("Full Name")) or "—"
                uname = _get_plain_text(props.get("Username")) or "—"
                lines.append(f"• `{uid}` — {name} (@{uname})")
                if uid.isdigit():
                    rows.append(
                        [
                            InlineKeyboardButton(
                                f"✅ {uid}", callback_data=f"admin:approve:{uid}"
                            ),
                            InlineKeyboardButton(
                                f"🚫 {uid}", callback_data=f"admin:reject:{uid}"
                            ),
                        ]
                    )

            lines.append("\nTap a button to Approve / Reject:")
            await query.message.reply_text(
                "\n".join(lines),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(rows) if rows else None,
            )
        except Exception as e:
            logger.error("admin pending list failed: %s", e)
            await query.message.reply_text(f"Error loading pending list:\n`{e}`", parse_mode="Markdown")
        return

    # ----- Approve / Denied / keep Pending -----
    if action not in ("approve", "reject", "keeppending") or len(parts) < 3:
        return

    target_id = parts[2].strip()
    if not notion or not db_id:
        await query.message.reply_text("Notion is not configured.")
        return

    try:
        response = notion_query_data_source(
            data_source_id=NOTION_AUTH_DATA_SOURCE_ID,
            database_id=db_id,
            filter={
                "property": "Telegram User ID",
                "title": {"equals": target_id},
            },
            page_size=1,
        )
        results = response.get("results", [])
        if not results:
            await query.message.reply_text(f"No request found for `{target_id}`.")
            return

        page_id = results[0]["id"]

        if action == "keeppending":
            notion.pages.update(
                page_id=page_id,
                properties={"Status": {"select": {"name": "Pending"}}},
            )
            await query.message.reply_text(
                f"⏳ User `{target_id}` remains *Pending*.\n"
                "You can approve or deny later with /pending or the buttons.",
                parse_mode="Markdown",
            )
            return

        new_status = "Authorised" if action == "approve" else "Blocked"
        notion.pages.update(
            page_id=page_id,
            properties={"Status": {"select": {"name": new_status}}},
        )
        _authorized_cache["expires"] = 0

        if action == "approve":
            await query.message.reply_text(
                f"✅ User `{target_id}` is now *Authorised*.",
                parse_mode="Markdown",
            )
            try:
                await context.bot.send_message(
                    chat_id=int(target_id),
                    text=(
                        "✅ Your access request was *approved*.\n\n"
                        "Send /start to begin using the bot."
                    ),
                    parse_mode="Markdown",
                )
            except Exception:
                pass
        else:
            await query.message.reply_text(
                f"🚫 User `{target_id}` has been *Denied*.",
                parse_mode="Markdown",
            )
            try:
                await context.bot.send_message(
                    chat_id=int(target_id),
                    text=(
                        "Your access request was not approved.\n"
                        "Contact a Hive admin if you think this is a mistake."
                    ),
                )
            except Exception:
                pass
    except Exception as e:
        logger.error("admin_button failed: %s", e)
        await query.message.reply_text(f"Error: {e}")
        
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
        response = notion_query_data_source(
            data_source_id=NOTION_AUTH_DATA_SOURCE_ID,
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
                "Status": {"select": {"name": "Blocked"}}
            },
        )
        _authorized_cache["expires"] = 0

        await update.message.reply_text(
            f"🚫 User `{target_id}` has been *Blocked*.",
            parse_mode="Markdown",
        )

    except Exception as e:
        logger.error("reject_cmd failed: %s", e)
        await update.message.reply_text(f"Error:\n`{e}`", parse_mode="Markdown")


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin control panel: access approval + Group Links requests."""
    user = update.effective_user
    if not is_admin(user):
        await update.message.reply_text("This command is for admins only.")
        return

    open_glinks = len(_glink_requests)
    lines = [
        "🛠 *Admin control panel*\n",
        "*1. Bot Access requests (Authorised Users)*",
        "When a member sends /request you receive a DM with 3 buttons:",
        "• ✅ *Approved* – set Status = Authorised, notify user",
        "• 🚫 *Denied* – set Status = Blocked, notify user",
        "• ⏳ *Pending* – keep in queue for later review",
        "Commands: `/pending`  `/approve <id>`  `/reject <id>`\n",
        "*2. Group Links / Security Summary requests*",
        "When a member searches 🔗 Group Links and no link is saved, you get a DM with:",
        "• ➕ *Add new group links* – paste `https://t.me/...`, save to Notion, notify user",
        "• ✅ *Added – Notify User* – notify only (if you already updated Notion)",
        "• ⏳ *Pending* – mark as pending (no user notify)",
        "• `/cancelglink` – cancel if you started Add but change your mind\n",
        f"Open Group Links requests in memory: *{open_glinks}*",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


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
    if ptype == "url":
        return (prop.get("url") or "").strip()
    return ""

async def get_authorized_usernames() -> set[str]:
    """Return authorised usernames (wrapper around get_authorized_users)."""
    auth = await get_authorized_users()
    return auth.get("usernames", set())

# Hardcoded Hive Telegram group (fallback if TELEGRAM_GROUP_ID env missing)
# Chat ID confirmed in project: Hive support / AIM group
HIVE_GROUP_CHAT_ID = -1001891936451
_MEMBER_STATUSES = frozenset(
    {"creator", "administrator", "member", "restricted"}
)
_NON_MEMBER_STATUSES = frozenset({"left", "kicked"})


def get_hive_group_chat_id() -> int:
    """Env TELEGRAM_GROUP_ID wins; otherwise hardcoded Hive group."""
    raw = (os.getenv("TELEGRAM_GROUP_ID") or "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            logger.warning("Invalid TELEGRAM_GROUP_ID=%s – using hardcoded", raw)
    return HIVE_GROUP_CHAT_ID


async def is_group_member(
    context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> tuple[bool, str]:
    """
    Live Telegram membership check against the Hive group.
    Cached 5 minutes. Hard 3s timeout on getChatMember (was hanging indefinitely).
    Returns (is_member, detail).
    """
    if not context or not context.bot:
        return False, "error: no bot context"

    now_ts = time.time()
    cached = _group_member_cache.get(user_id)
    if cached and cached[0] > now_ts:
        return cached[1], cached[2] + " (cached)"

    chat_id = get_hive_group_chat_id()
    try:
        member = await asyncio.wait_for(
            context.bot.get_chat_member(chat_id=chat_id, user_id=user_id),
            timeout=3.0,
        )
        status = getattr(member, "status", None) or "unknown"
        if status in _MEMBER_STATUSES:
            detail = f"status={status}"
            _group_member_cache[user_id] = (now_ts + _GROUP_MEMBER_CACHE_TTL, True, detail)
            return True, detail
        detail = f"status={status}"
        _group_member_cache[user_id] = (now_ts + _GROUP_MEMBER_CACHE_TTL, False, detail)
        return False, detail
    except asyncio.TimeoutError:
        logger.warning(
            "get_chat_member TIMEOUT chat_id=%s user_id=%s", chat_id, user_id
        )
        return False, "error: get_chat_member timeout"
    except Exception as e:
        logger.warning(
            "get_chat_member failed chat_id=%s user_id=%s err=%s",
            chat_id,
            user_id,
            e,
        )
        return False, f"error: {e}"


async def _notion_group_member_flag(user) -> str | None:
    """
    Read Notion Group Member for this user: 'Yes' | 'No' | None (unknown).
    """
    if not user or (not notion and not NOTION_TOKEN):
        return None
    db_id = NOTION_AUTH_DB_ID_ENV or os.getenv("NOTION_AUTH_DB_ID") or os.getenv(
        "NOTION_DATABASE_ID"
    )
    ds_id = NOTION_AUTH_DATA_SOURCE_ID
    if not db_id and not ds_id:
        return None
    try:
        results = []
        for filt in (
            {
                "property": "Telegram User ID",
                "title": {"equals": str(user.id)},
            },
            {
                "property": "Telegram User ID",
                "rich_text": {"equals": str(user.id)},
            },
        ):
            try:
                response = notion_query_data_source(
                    data_source_id=ds_id,
                    database_id=db_id,
                    filter=filt,
                    page_size=1,
                )
                results = response.get("results", [])
                if results:
                    break
            except Exception:
                continue
        if not results:
            return None
        props = results[0].get("properties", {}) or {}
        gm = props.get("Group Member") or {}
        if isinstance(gm, dict):
            if gm.get("type") == "select" and gm.get("select"):
                return (gm["select"].get("name") or "").strip() or None
            # plain text fallback
            text = _get_plain_text(gm).strip()
            return text or None
        return None
    except Exception as e:
        logger.warning("_notion_group_member_flag failed for %s: %s", user.id, e)
        return None


async def is_authorized(
    update: Update, context: ContextTypes.DEFAULT_TYPE | None = None
) -> bool:
    """
    Hard dual-gate on every request (auto-sync):

      1) Hive group membership (live Telegram getChatMember)
         - left / kicked → DENY + sync Notion Group Member = No
         - API error → fall back to Notion Group Member (Yes only)
      2) Notion Hive Bot Authorised Users Status = Authorised
         (Telegram User ID preferred, else Username)

    Admins always pass (still best-effort membership sync).
    """
    user = update.effective_user
    if not user:
        return False

    # --- Admins: always allowed; membership sync in background only ---
    if is_admin(user):
        if context is not None:
            try:
                asyncio.create_task(sync_group_member_to_notion(context, user))
            except Exception:
                pass
        return True

    uid = str(user.id).strip()
    uname = (user.username or "").strip().lstrip("@").lower()

    # ========== 1) Hive group membership (mandatory) ==========
    in_group = False
    group_detail = "unknown"
    if context is not None:
        in_group, group_detail = await is_group_member(context, user.id)
        # Persist to Notion only when status is clear — throttled (not every request)
        if group_detail.startswith("status="):
            status = group_detail.replace("status=", "", 1)
            now_ts = time.time()
            last_sync = _group_member_sync_at.get(user.id, 0)
            must_sync = status in _NON_MEMBER_STATUSES or (
                now_ts - last_sync > _GROUP_MEMBER_SYNC_TTL
            )
            if must_sync:
                try:
                    # Background so auth is not blocked on Notion write
                    asyncio.create_task(
                        mark_group_member_in_notion(
                            user, is_member=(status in _MEMBER_STATUSES)
                        )
                    )
                    _group_member_sync_at[user.id] = now_ts
                except Exception as e:
                    logger.warning(
                        "Auth sync Group Member schedule failed user=%s: %s",
                        user.id,
                        e,
                    )
            if status in _NON_MEMBER_STATUSES:
                logger.info(
                    "Auth DENY user=%s – not in Hive group (%s)",
                    user.id,
                    group_detail,
                )
                return False
            if status not in _MEMBER_STATUSES:
                logger.info(
                    "Auth DENY user=%s – unexpected group status (%s)",
                    user.id,
                    group_detail,
                )
                return False
        elif group_detail.startswith("error:"):
            # Telegram API failed – fall back to last known Notion flag
            notion_gm = await _notion_group_member_flag(user)
            if notion_gm and notion_gm.lower() in ("no", "n", "false", "0"):
                logger.info(
                    "Auth DENY user=%s – TG API error and Notion Group Member=No "
                    "(%s)",
                    user.id,
                    group_detail,
                )
                return False
            if not notion_gm or notion_gm.lower() not in ("yes", "y", "true", "1"):
                # Unknown membership after API error → deny (fail closed)
                logger.info(
                    "Auth DENY user=%s – TG API error and no Notion Group Member=Yes "
                    "(%s / notion=%s)",
                    user.id,
                    group_detail,
                    notion_gm,
                )
                return False
            logger.warning(
                "Auth group check API error user=%s – allowing via Notion "
                "Group Member=Yes (%s)",
                user.id,
                group_detail,
            )
            in_group = True
        else:
            if not in_group:
                logger.info(
                    "Auth DENY user=%s – not in Hive group (%s)",
                    user.id,
                    group_detail,
                )
                return False
    else:
        # No context → cannot live-check Telegram; use Notion Group Member
        notion_gm = await _notion_group_member_flag(user)
        if not notion_gm or notion_gm.lower() not in ("yes", "y", "true", "1"):
            logger.info(
                "Auth DENY user=%s – no context and Notion Group Member not Yes "
                "(notion=%s)",
                user.id,
                notion_gm,
            )
            return False
        in_group = True

    if not in_group:
        logger.info(
            "Auth DENY user=%s – group membership failed (%s)",
            user.id,
            group_detail,
        )
        return False

    # ========== 2) Notion Authorised status ==========
    auth = await get_authorized_users()
    in_notion = uid in auth.get("user_ids", set()) or (
        uname and uname in auth.get("usernames", set())
    )
    if not in_notion:
        logger.info(
            "Auth DENY user=%s username=%s – in group but not Notion Authorised "
            "(ids=%d usernames=%d)",
            uid,
            uname or "N/A",
            len(auth.get("user_ids", set())),
            len(auth.get("usernames", set())),
        )
        return False

    logger.debug(
        "Auth OK user=%s group=%s notion=Authorised",
        user.id,
        group_detail,
    )
    return True


async def require_authorized(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """
    Hard gate: Hive group member AND Notion Authorised.
    Replies with a clear reason when denied.
    """
    if await is_authorized(update, context):
        return True
    msg = update.effective_message
    if not msg:
        return False

    user = update.effective_user
    reason_lines = [
        "🔒 Access denied.\n",
        "This bot only works if *both* are true:",
        "1. You are a *member of the Hive Telegram group*",
        "2. An admin has set your status to *Authorised* in Notion",
        "",
    ]
    # Best-effort specific hint
    if user and context:
        try:
            in_group, detail = await is_group_member(context, user.id)
            if not in_group and detail.startswith("status="):
                st = detail.replace("status=", "", 1)
                if st in _NON_MEMBER_STATUSES:
                    reason_lines.append(
                        f"_Reason: you are not in the Hive group "
                        f"(Telegram status: {st})._"
                    )
                else:
                    reason_lines.append(
                        f"_Reason: group check failed ({detail})._"
                    )
            elif not in_group:
                reason_lines.append(
                    f"_Reason: group membership could not be confirmed "
                    f"({detail})._"
                )
            else:
                reason_lines.append(
                    "_Reason: you are in the group, but not Authorised in Notion yet._"
                )
                reason_lines.append("Send /request then wait for admin approval.")
        except Exception:
            reason_lines.append(
                "Send /request for access, then /status to check."
            )
    else:
        reason_lines.append("Send /request for access, then /status to check.")

    reason_lines.append("\nUse /status anytime to see your current checks.")
    try:
        await msg.reply_text(
            "\n".join(reason_lines),
            parse_mode="Markdown",
        )
    except Exception:
        await msg.reply_text(
            "Access denied. You must be a Hive group member and Authorised in Notion. "
            "Send /request and /status."
        )
    return False


async def sync_group_member_to_notion(
    context: ContextTypes.DEFAULT_TYPE,
    user,
) -> bool | None:
    """
    Live Telegram membership → Notion Group Member (Yes/No).
    Returns True/False, or None if check not possible.
    """
    if not user:
        return None
    if not notion and not NOTION_TOKEN:
        return None

    chat_id = get_hive_group_chat_id()
    try:
        member = await context.bot.get_chat_member(
            chat_id=chat_id,
            user_id=user.id,
        )
        is_member = (getattr(member, "status", None) or "") in _MEMBER_STATUSES
    except Exception as e:
        logger.warning("get_chat_member failed for %s: %s", user.id, e)
        return None

    try:
        await mark_group_member_in_notion(user, is_member=is_member)
        logger.info(
            "Updated Notion Group Member=%s for user %s",
            "Yes" if is_member else "No",
            user.id,
        )
    except Exception as e:
        logger.error("Failed to update Group Member in Notion: %s", e)

    return is_member
        
async def get_ticker_from_notion(ticker: str) -> dict | None:
    """Look up a ticker in UK AIM Micro-Cap (NOTION_TICKERS_DB_ID)."""
    if not notion or not ticker:
        return None

    db_id = (os.getenv("NOTION_TICKERS_DB_ID") or "").strip()
    if not db_id:
        logger.error("NOTION_TICKERS_DB_ID is missing")
        return None

    # Normalise UUID if needed
    raw = db_id.replace("-", "")
    if len(raw) == 32 and "-" not in db_id:
        db_id = f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"

    ticker = ticker.upper().strip()

    # Cache
    cached = _ticker_cache.get(ticker)
    if cached and cached.get("expires", 0) > time.time():
        return cached.get("data")

    try:
        filters_to_try = [
            {"property": "Ticker", "title": {"equals": ticker}},
            {"property": "Ticker", "rich_text": {"equals": ticker}},
            {"property": "Ticker", "rich_text": {"contains": ticker}},
        ]

        results = []
        for f in filters_to_try:
            try:
                response = notion.databases.query(
                    database_id=db_id,
                    filter=f,
                    page_size=5,
                )
                results = response.get("results", [])
                if results:
                    break
            except Exception as fe:
                logger.warning("Ticker filter failed %s: %s", f, fe)

        if not results:
            logger.info("No Notion page for ticker=%s db=%s", ticker, db_id)
            return None

        # --- THIS BLOCK WAS MISSING ---
        props = results[0]["properties"]

        def find_prop(*names):
            for name in names:
                if name in props:
                    val = _get_plain_text(props[name])
                    if val:
                        return val
            return ""

        def find_number(*names):
            for name in names:
                prop = props.get(name)
                if not prop or not isinstance(prop, dict):
                    continue
                if prop.get("type") == "number" and prop.get("number") is not None:
                    return float(prop["number"])
                if prop.get("number") is not None:
                    try:
                        return float(prop["number"])
                    except (TypeError, ValueError):
                        pass
                # sometimes stored as text
                raw = _get_plain_text(prop).replace("%", "").replace(",", "").strip()
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    pass
            return None

        data = {
            "company": find_prop("Company", "Name", "Company Name"),
            "summary": find_prop(
                "Summary & Next Catalyst", "Summary", "Overview", "Thesis"
            ),
            "red_flags": find_prop("Red Flags", "Risks", "Red Flag", "Key Risks"),
            "company_overview": find_prop("Company Overview", "Investment Thesis"),
            "status": find_prop("Status"),
            "day_change_pct": find_number(
                "Day Change %",
                "% Change",
                "Change %",
                "Price Change %",
                "1D %",
                "Daily Change %",
                "Change",
            ),
        }

        _ticker_cache[ticker] = {
            "data": data,
            "expires": time.time() + CACHE_TTL_SECONDS,
        }
        logger.info("Found ticker %s – company=%s", ticker, data.get("company"))
        return data
        # --- END ---

    except Exception as e:
        logger.error("Notion ticker lookup failed for %s: %s", ticker, e)
        return None
        
async def get_stockpickers_for_ticker(ticker: str) -> list[str]:
    """
    Return unique Posted By names from Hive Stock Picks for this ticker.
    """
    if not notion or not ticker:
        return []

    # Prefer dedicated stockpicks DB only
    db_id = (os.getenv("NOTION_STOCKPICKS_DB_ID") or "").strip()
    if not db_id:
        db_id = "9095ded4-ad6a-4b25-9887-19a77baba12f"
        logger.warning(
            "NOTION_STOCKPICKS_DB_ID missing – using default Hive Stock Picks id"
        )

    raw = db_id.replace("-", "")
    if len(raw) == 32:
        db_id = f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"

    ticker = ticker.upper().strip()
    names: list[str] = []
    seen: set[str] = set()

    filters_to_try = [
        {"property": "Ticker", "rich_text": {"equals": ticker}},
        {"property": "Ticker", "rich_text": {"contains": ticker}},
        {"property": "Stockpick & Month", "title": {"contains": ticker}},
        {"property": "Message", "rich_text": {"contains": f"#{ticker}"}},
    ]

    try:
        results = []
        for f in filters_to_try:
            response = notion.databases.query(
                database_id=db_id,
                filter=f,
                page_size=100,
            )
            results = response.get("results", [])
            if results:
                logger.info(
                    "Stockpickers filter hit for %s via %s (%d rows)",
                    ticker,
                    f.get("property"),
                    len(results),
                )
                break

        for page in results:
            props = page.get("properties", {})
            posted_by = _get_plain_text(props.get("Posted By")).strip()
            if not posted_by:
                # fallback: title
                posted_by = _get_plain_text(props.get("Stockpick & Month")).strip()
            if not posted_by:
                continue
            key = posted_by.lower()
            if key in seen:
                continue
            seen.add(key)
            names.append(posted_by)

        if not names:
            logger.info(
                "No stockpickers found for %s in db %s", ticker, db_id
            )
    except Exception as e:
        logger.error("Stockpickers lookup failed for %s db=%s: %s", ticker, db_id, e)

    return names
    
async def save_stockpick_to_notion(
    text: str,
    user_name: str,
    ticker: str | None = None,
    period_type: str | None = None,
    period_value: str | None = None,
    user_id: int | None = None,
):
    """
    Save a #stockpick into Hive Stock Picks.
    Returns (page_id, error_message). page_id is None on failure.
    """
    if not notion:
        msg = "Notion client is None – NOTION_TOKEN missing?"
        logger.error(msg)
        return None, msg

    # Prefer dedicated stockpicks DB — never write picks into AIM research DB
    db_id = (
        os.getenv("NOTION_STOCKPICKS_DB_ID")
        or os.getenv("NOTION_DATABASE_ID")
        or "9095ded4ad6a4b25988719a77baba12f"
    )
    db_id = db_id.strip().replace("-", "")
    # Notion accepts ids with or without dashes; keep dashed form for API
    if len(db_id) == 32:
        db_id = f"{db_id[:8]}-{db_id[8:12]}-{db_id[12:16]}-{db_id[16:20]}-{db_id[20:]}"

    logger.info("Saving #stockpick to database_id=%s", db_id)

    try:
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
            "Stockpick & Month": {"title": [{"text": {"content": name[:100]}}]},
            "Message": {"rich_text": [{"text": {"content": text[:2000]}}]},
            "Posted By": {
                "rich_text": [{"text": {"content": (user_name or "Unknown")[:200]}}]
            },
            "Source Group": {
                "rich_text": [{"text": {"content": "Telegram"}}]
            },
            "Status": {"select": {"name": "New"}},
            "Telegram Date": {
                "date": {"start": datetime.now(timezone.utc).date().isoformat()}
            },
        }

        if ticker:
            properties["Ticker"] = {
                "rich_text": [{"text": {"content": ticker[:50]}}]
            }

        if notes:
            properties["Notes"] = {
                "rich_text": [{"text": {"content": notes[:2000]}}]
            }

        page = notion.pages.create(
            parent={"database_id": db_id},
            properties=properties,
        )
        page_id = page.get("id")
        logger.info(
            "Saved #stockpick page_id=%s ticker=%s",
            page_id, ticker,
        )
        return page_id, None
    except Exception as e:
        msg = str(e)
        logger.error("Failed to write #stockpick to Notion: %s", msg)
        return None, msg

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
    # Replace hub panel with a short prompt (minimal footprint)
    prompt = (
        "Send your *new* stockpick text now (include #TICKER).\n"
        "This updates *this month’s* pick only."
        if field == "Change"
        else f"Send your *{field}* now and I’ll add it to your stockpick."
    )
    try:
        await query.edit_message_text(
            prompt,
            parse_mode="Markdown",
            reply_markup=hub_back_keyboard(),
        )
        await remember_nav_panel(user.id, query.message)
    except Exception:
        sent = await query.message.reply_text(
            prompt,
            parse_mode="Markdown",
            reply_markup=hub_back_keyboard(),
        )
        await remember_nav_panel(user.id, sent)
    await ensure_home_keyboard(
        context.bot, query.message.chat_id if query.message else None
    )
        
# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def extract_hashtag_tickers(text: str) -> list[str]:
    """Extract hashtag tickers: 1-5 chars, letters and/or digits (e.g. KEFI, 80M, RRR)."""
    if not text:
        return []
    # Letters and digits, 1–5 characters after #
    matches = re.findall(r"#([A-Za-z0-9]{1,5})\b", text)
    found = []
    for m in matches:
        t = m.upper()
        # Skip pure noise / common non-tickers if needed
        if t not in found:
            found.append(t)
    return found

MAX_WATCHLISTS = 3


def _fetch_pct_on_day_live(ticker: str) -> float | None:
    """
    Live day % change for AIM/LSE ticker via Yahoo Finance (TICKER.L).
    Returns percent points e.g. 13.70 meaning +13.70%, or None.

    Source priority (validated vs LSE prints):
      1) meta.regularMarketChangePercent  (official session % — most reliable)
      2) last two daily closes from the chart series
      3) price / previousClose (NOT chartPreviousClose — that can lag and inflate %)
    """
    if not ticker:
        return None
    symbol = f"{ticker.upper().strip()}.L"
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?range=5d&interval=1d"
    )
    try:
        import json as _json
        import urllib.request

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; HiveBot/1.0)",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        result = (data.get("chart") or {}).get("result") or []
        if not result:
            return None
        meta = result[0].get("meta") or {}

        # 1) Official session day-change % (e.g. EPP → 13.70, not 17.73)
        if meta.get("regularMarketChangePercent") is not None:
            try:
                return round(float(meta["regularMarketChangePercent"]), 2)
            except (TypeError, ValueError):
                pass

        # 2) Last two daily closes on the chart
        closes = (
            ((result[0].get("indicators") or {}).get("quote") or [{}])[0].get("close")
            or []
        )
        closes = [c for c in closes if c is not None]
        if len(closes) >= 2 and closes[-2]:
            try:
                return round(
                    (float(closes[-1]) / float(closes[-2]) - 1.0) * 100.0, 2
                )
            except (TypeError, ValueError, ZeroDivisionError):
                pass

        # 3) Price vs previousClose only (skip chartPreviousClose — often wrong)
        price = meta.get("regularMarketPrice")
        prev = meta.get("previousClose")
        if price is not None and prev:
            try:
                return round((float(price) / float(prev) - 1.0) * 100.0, 2)
            except (TypeError, ValueError, ZeroDivisionError):
                pass
    except Exception as e:
        logger.warning("live %% on day failed for %s: %s", ticker, e)
    return None


async def _user_stockpick_tickers_this_month(user) -> set[str]:
    """
    Tickers this Telegram user has as a #stockpick in Hive Stock Picks
    for the current calendar month. Used to badge My Watchlist rows.
    """
    out: set[str] = set()
    if not notion or not user:
        return out
    db_id = (
        os.getenv("NOTION_STOCKPICKS_DB_ID")
        or os.getenv("NOTION_DATABASE_ID")
        or ""
    ).strip()
    if not db_id:
        return out
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
        uid_marker = f"uid:{user.id}"
        user_name = (user.full_name or "").strip().lower()
        uname = (user.username or "").strip().lower()
        for page in response.get("results", []):
            props = page.get("properties", {})
            notes = _get_plain_text(props.get("Notes")).lower()
            posted_by = _get_plain_text(props.get("Posted By")).strip().lower()
            is_mine = uid_marker in notes
            if not is_mine and user_name and posted_by == user_name:
                is_mine = True
            if not is_mine and uname and uname in posted_by:
                is_mine = True
            if not is_mine:
                continue
            # Ticker property (rich_text) or parse from title
            t = _get_plain_text(props.get("Ticker")).lstrip("#").upper().strip()
            if not t:
                title = _get_plain_text(
                    props.get("Stockpick & Month") or props.get("Name")
                )
                m = re.search(r"#([A-Z0-9]{2,6})\b", (title or "").upper())
                if m:
                    t = m.group(1)
            if t:
                out.add(t)
    except Exception as e:
        logger.warning("_user_stockpick_tickers_this_month failed: %s", e)
    return out


def _month_picks_label() -> str:
    """e.g. 'Sept Picks' for current calendar month."""
    now = datetime.now(timezone.utc)
    # Short month labels
    labels = {
        1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
        7: "Jul", 8: "Aug", 9: "Sept", 10: "Oct", 11: "Nov", 12: "Dec",
    }
    return f"{labels.get(now.month, now.strftime('%b'))} Picks"


async def _fetch_user_watchlist_pages(user_id: int) -> list[dict]:
    """All Notion rows for this Telegram user (multi-source safe)."""
    db_id = NOTION_WATCHLIST_DB_ID or (os.getenv("NOTION_WATCHLIST_DB_ID") or "").strip()
    ds_id = NOTION_WATCHLIST_DATA_SOURCE_ID
    if not (notion or NOTION_TOKEN) or (not db_id and not ds_id):
        return []

    try:
        response = notion_query_data_source(
            data_source_id=ds_id,
            database_id=db_id,
            filter={
                "property": "Telegram User ID",
                "rich_text": {"equals": str(user_id)},
            },
            page_size=100,
        )
        return response.get("results", [])
    except Exception as e:
        logger.error("_fetch_user_watchlist_pages failed: %s", e)
        return []


def _strip_html(text: str) -> str:
    if not text:
        return ""
    # Light cleanup for AI Summary snippets from Investegate/Notion
    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


async def get_latest_rns_for_ticker(
    ticker: str, *, force: bool = False
) -> dict | None:
    """
    Latest RNS row for a ticker from RNS News Log.
    Returns dict: title, date, summary, link, company, source — or None.
    """
    t = (ticker or "").lstrip("#").upper().strip()
    if not t:
        return None
    if not (notion or NOTION_TOKEN):
        return None
    ds_id = NOTION_RNS_DATA_SOURCE_ID
    db_id = NOTION_RNS_DB_ID
    if not ds_id and not db_id:
        return None

    now = time.time()
    if not force and t in _rns_cache:
        ts, cached = _rns_cache[t]
        if now - ts < RNS_CACHE_TTL_SECONDS:
            return cached

    try:
        response = notion_query_data_source(
            data_source_id=ds_id,
            database_id=db_id,
            filter={
                "property": "Ticker",
                "rich_text": {"equals": t},
            },
            sorts=[{"property": "RNS Date", "direction": "descending"}],
            page_size=3,
        )
        results = response.get("results", [])
        # Prefer exact ticker match (case-insensitive)
        best = None
        for page in results:
            props = page.get("properties", {})
            row_t = (_get_plain_text(props.get("Ticker")) or "").upper().strip()
            if row_t != t:
                continue
            title = _get_plain_text(props.get("Title")) or "RNS"
            summary = _strip_html(_get_plain_text(props.get("AI Summary")))
            company = _get_plain_text(props.get("Company")) or ""
            link = ""
            link_prop = props.get("Link") or {}
            if isinstance(link_prop, dict):
                link = (link_prop.get("url") or "").strip()
            date_str = ""
            date_prop = props.get("RNS Date") or {}
            if isinstance(date_prop, dict):
                d = date_prop.get("date") or {}
                if isinstance(d, dict):
                    date_str = (d.get("start") or "")[:10]
            source = ""
            src = props.get("Source") or {}
            if isinstance(src, dict) and src.get("select"):
                source = (src["select"] or {}).get("name") or ""
            best = {
                "ticker": t,
                "title": title[:120],
                "summary": summary[:280],
                "company": company[:80],
                "link": link,
                "date": date_str,
                "source": source,
            }
            break

        _rns_cache[t] = (now, best)
        return best
    except Exception as e:
        logger.warning("get_latest_rns_for_ticker(%s) failed: %s", t, e)
        _rns_cache[t] = (now, None)
        return None


async def get_latest_rns_for_tickers(
    tickers: list[str], *, force: bool = False
) -> dict[str, dict]:
    """Map ticker -> latest RNS dict for a list of tickers."""
    out: dict[str, dict] = {}
    seen: set[str] = set()
    for raw in tickers:
        t = (raw or "").lstrip("#").upper().strip()
        if not t or t in seen:
            continue
        seen.add(t)
        rns = await get_latest_rns_for_ticker(t, force=force)
        if rns:
            out[t] = rns
    return out

def _list_names_from_pages(pages: list[dict]) -> list[str]:
    names = []
    for page in pages:
        props = page.get("properties", {})
        n = _get_plain_text(props.get("List Name")).strip() or "Default"
        if n not in names:
            names.append(n)
    return names[:MAX_WATCHLISTS]

def _tab_keyboard(list_names: list[str], active: str) -> list[list]:
    """One row of tab buttons (max 3)."""
    row = []
    for name in list_names:
        label = f"[{name}]" if name == active else name
        # callback data must stay short
        safe = name[:40]
        row.append(
            InlineKeyboardButton(label, callback_data=f"wl:tab:{safe}")
        )
    return [row] if row else []
    
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

def format_reply(ticker: str, data: dict, stockpickers: list[str] | None = None) -> str:
    text = (
        f"🔖📑 *#{ticker}* – {data.get('company') or 'N/A'}\n\n"
        f"*Snapshot Summary:*\n{data.get('summary') or 'No summary available.'}\n\n"
        f"*Red Flags:*\n{data.get('red_flags') or 'None noted.'}\n"
    )

    if stockpickers:
        quoted = ", ".join(f'"{n}"' for n in stockpickers)
        text += f"\n*#{ticker} This Month Hive Stockpicker:* {quoted}\n"
    else:
        text += f"\n*#{ticker} This Month Hive Stockpicker:* _None yet_\n"

    text += (
        "\n_🔋🪫 Powered by: The Hive 🐝 BuzzBot Knowledge Hub. "
        "Not financial advice. DYOR._"
    )
    return text

def main_reply_keyboard() -> ReplyKeyboardMarkup:
    """
    Home navigation – always-on persistent reply keyboard.
    Stays visible after /start and after every menu selection.
    """
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("👀 My Watchlist"),
                KeyboardButton("📌 My Stockpick"),
            ],
            [
                KeyboardButton("📊 Stock Brief"),
                KeyboardButton("🔗 Group Links"),
            ],
            [
                KeyboardButton("📈 Top 10"),
                KeyboardButton("📰 RNS Brief"),
            ],
            [
                KeyboardButton("📋 Menu"),
                KeyboardButton("🙈 Hide"),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False,
    )


def hidden_reply_keyboard() -> ReplyKeyboardMarkup:
    """Collapsed keyboard – single button to restore Home."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("☰ Show menu")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False,
    )


async def ensure_home_keyboard(bot, chat_id: int | None) -> None:
    """
    Re-assert the persistent Home reply keyboard.

    Never delete the message that carries ReplyKeyboardMarkup — many Telegram
    clients drop the keyboard when that message is removed.
    """
    if not bot or chat_id is None:
        return
    try:
        await bot.send_message(
            chat_id,
            "🏠 Home",
            reply_markup=main_reply_keyboard(),
        )
    except Exception as e:
        logger.debug("ensure_home_keyboard failed: %s", e)


async def send_home_menu(bot, chat_id: int | None) -> None:
    """
    Land on Home with the full 7-button reply keyboard.
    Message is kept on purpose so the keyboard stays visible.
    """
    if not bot or chat_id is None:
        return
    text = (
        "🏠 *Home*\n\n"
        "• 👀 My Watchlist\n"
        "• 📌 My Stockpick\n"
        "• 📊 Stock Brief\n"
        "• 🔗 Group Links\n"
        "• 📈 Top 10\n"
        "• 📰 RNS Brief\n"
        "• 📋 Menu\n"
        "• 🙈 Hide"
    )
    try:
        await bot.send_message(
            chat_id,
            text,
            parse_mode="Markdown",
            reply_markup=main_reply_keyboard(),
        )
    except Exception as e:
        logger.error("send_home_menu failed: %s", e)
        try:
            await bot.send_message(
                chat_id,
                "Home",
                reply_markup=main_reply_keyboard(),
            )
        except Exception:
            pass


def menu_inline_keyboard() -> InlineKeyboardMarkup:
    """Simplified Menu – access + help only."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🏠 Start", callback_data="cmd:start"),
                InlineKeyboardButton("🔐 Status", callback_data="cmd:status"),
            ],
            [
                InlineKeyboardButton("📨 Request access", callback_data="cmd:request"),
                InlineKeyboardButton("❓ FAQ", callback_data="cmd:faq"),
            ],
            [
                InlineKeyboardButton("« Hub", callback_data="hub:home"),
            ],
        ]
    )


def hub_back_keyboard() -> InlineKeyboardMarkup:
    """Single « Hub row for any submenu."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("« Hub", callback_data="hub:home")]]
    )


def brief_action_keyboard(ticker: str, *, rns_page: int = 0) -> InlineKeyboardMarkup:
    """
    Stock Brief panel (consolidated design):
      Row 1: Stock Summary | Snap Shot | RNS News
      Row 2: Save to My Watchlist
      Row 3: « Hub
    """
    t = (ticker or "").upper()[:20]
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📋 Stock Summary",
                    callback_data=f"sbrief:sum:{t}",
                ),
                InlineKeyboardButton(
                    "⚡ Snap Shot",
                    callback_data=f"sbrief:cat:{t}",
                ),
                InlineKeyboardButton(
                    "📰 RNS News",
                    callback_data=f"sbrief:rns:{t}:{int(rns_page)}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "➕ Save to My Watchlist",
                    callback_data=f"snap:save:{t}",
                )
            ],
            [
                InlineKeyboardButton("« Hub", callback_data="hub:home"),
            ],
        ]
    )


def snapshot_action_keyboard(ticker: str) -> InlineKeyboardMarkup:
    """Back-compat alias → Stock Brief keyboard."""
    return brief_action_keyboard(ticker)


async def sync_microcap_with_latest_rns(ticker: str) -> dict | None:
    """
    Pull latest RNS for ticker from Hive RNS News Log and update
    UK AIM Micro-Cap (Last RNS Date + Last 3 RNS head) when possible.
    """
    t = (ticker or "").lstrip("#").upper().strip()
    if not t:
        return None
    latest = await get_latest_rns_for_ticker(t, force=True)
    if not latest:
        return None
    meta = await get_ticker_from_notion(t)
    page_id = (meta or {}).get("page_id")
    if not page_id or not (notion or NOTION_TOKEN):
        return latest
    title = latest.get("title") or "RNS"
    date_str = latest.get("date") or ""
    summary = (latest.get("summary") or "")[:500]
    link = latest.get("link") or ""
    head = f"• {date_str} | {title}"
    if summary:
        head += f"\n  {summary[:220]}"
    if link:
        head += f"\n  {link}"
    old_last = (meta or {}).get("last_rns") or ""
    blocks = [b.strip() for b in old_last.split("• ") if b.strip()]
    new_blocks = [head.lstrip("• ").strip()]
    for b in blocks:
        if title.lower() in b.lower() and (not date_str or date_str in b):
            continue
        new_blocks.append(b)
        if len(new_blocks) >= 3:
            break
    last3 = "\n\n".join(f"• {b}" for b in new_blocks)
    props: dict = {}
    if date_str:
        props["Last RNS Date"] = {"date": {"start": date_str}}
    props["Last 3 RNS"] = {"rich_text": [{"text": {"content": last3[:1900]}}]}
    try:
        if notion:
            try:
                notion.pages.update(page_id=page_id, properties=props)
            except Exception as e1:
                logger.warning("sync RNS props failed %s: %s", t, e1)
                if date_str:
                    try:
                        notion.pages.update(
                            page_id=page_id,
                            properties={"Last RNS Date": {"date": {"start": date_str}}},
                        )
                    except Exception as e2:
                        logger.warning("sync RNS date failed %s: %s", t, e2)
        _ticker_cache.pop(t, None)
        logger.info("Synced latest RNS into Micro-Cap for %s (%s)", t, date_str)
    except Exception as e:
        logger.error("sync_microcap_with_latest_rns failed %s: %s", t, e)
    return latest


def format_brief_header(ticker: str, data: dict, *, pct: float | None = None) -> str:
    company = data.get("company") or "N/A"
    lines = [f"📊 *Stock Brief* · *#{ticker}* — {company}"]
    if pct is not None:
        sign = "+" if pct >= 0 else ""
        arrow = "🟢" if pct > 0 else ("🔴" if pct < 0 else "⚪")
        lines.append(f"{arrow} Session: *{sign}{pct:.2f}%*")
    rns_d = data.get("last_rns_date") or ""
    if rns_d:
        lines.append(f"Last RNS date: *{rns_d}*")
    lines.append("")
    lines.append("_Choose a view below._")
    return "\n".join(lines)


def format_stock_summary(
    ticker: str,
    data: dict,
    stockpickers: list[str] | None = None,
    latest_rns: dict | None = None,
) -> str:
    """Stock Summary view — latest RNS first when available."""
    company = data.get("company") or "N/A"
    lines: list[str] = [
        f"📋 *Stock Summary* · *#{ticker}* — {company}",
        "",
    ]
    rns = latest_rns or data.get("latest_rns")
    lines.append("*Latest RNS*")
    if rns:
        date_bit = rns.get("date") or data.get("last_rns_date") or "—"
        lines.append(f"*{date_bit}* · {rns.get('title') or 'RNS'}")
        if rns.get("summary"):
            lines.append(rns["summary"])
        if rns.get("link"):
            lines.append(rns["link"])
    else:
        fallback = (data.get("last_rns") or "").strip()
        if fallback:
            lines.append(fallback[:500])
        else:
            lines.append("_No RNS in News Log for this ticker yet._")
    lines.append("")
    lines.append("*Snapshot Summary*")
    lines.append(data.get("summary") or "No summary available.")
    lines.append("")
    lines.append("*Red Flags*")
    lines.append(data.get("red_flags") or "None noted.")
    if data.get("company_overview"):
        lines.append("")
        lines.append("*Company Overview*")
        lines.append(data["company_overview"])
    lines.append("")
    lines.append(f"*#{ticker} This Month Hive Stockpicker*")
    if stockpickers:
        quoted = ", ".join(f'"{n}"' for n in stockpickers)
        lines.append(quoted)
    else:
        lines.append("_None yet_")
    lines.append("")
    lines.append("_🔋 Powered by The Hive 🐝 BuzzBot. Not financial advice. DYOR._")
    return "\n".join(lines)


def format_catalyst_snapshot(ticker: str, data: dict) -> str:
    company = data.get("company") or "N/A"
    score = data.get("catalyst_score")
    score_s = f"*{score:g}*" if isinstance(score, (int, float)) else "_n/a_"
    nxt = data.get("next_catalyst") or ""
    if not nxt and data.get("summary"):
        s = data["summary"]
        low = s.lower()
        for marker in ("next catalysts", "next catalyst", "catalysts"):
            idx = low.find(marker)
            if idx >= 0:
                nxt = s[idx:][:600]
                break
    if not nxt:
        nxt = "_No catalyst detail on file._"
    return "\n".join(
        [
            f"⚡ *Snap Shot* · *#{ticker}* — {company}",
            "",
            "*Catalyst Score*",
            score_s,
            "",
            "*Next Catalyst*",
            nxt,
            "",
            "_Not financial advice. DYOR._",
        ]
    )


async def _fetch_rns_history_for_ticker(ticker: str, limit: int = 12) -> list[dict]:
    """Newest RNS rows for a ticker from Hive RNS News Log."""
    t = (ticker or "").lstrip("#").upper().strip()
    if not t or not (notion or NOTION_TOKEN):
        return []
    ds_id = NOTION_RNS_DATA_SOURCE_ID
    db_id = NOTION_RNS_DB_ID
    out: list[dict] = []
    try:
        resp = notion_query_data_source(
            data_source_id=ds_id or None,
            database_id=db_id or None,
            filter={"property": "Ticker", "rich_text": {"equals": t}},
            sorts=[{"property": "RNS Date", "direction": "descending"}],
            page_size=min(24, max(limit, 12)),
        )
        for page in resp.get("results", []):
            props = page.get("properties") or {}
            row_t = (_get_plain_text(props.get("Ticker")) or "").upper().strip()
            if row_t and row_t != t:
                continue
            title = _get_plain_text(props.get("Title")) or "RNS"
            summary = _strip_html(_get_plain_text(props.get("AI Summary")) or "")
            link = ""
            lp = props.get("Link") or {}
            if isinstance(lp, dict):
                link = (lp.get("url") or "").strip()
            date_str = ""
            dp = props.get("RNS Date") or {}
            if isinstance(dp, dict) and dp.get("date"):
                date_str = (dp["date"].get("start") or "")[:10]
            out.append({
                "title": title[:140],
                "summary": summary[:320],
                "link": link,
                "date": date_str,
            })
            if len(out) >= limit:
                break
    except Exception as e:
        logger.error("_fetch_rns_history_for_ticker(%s) failed: %s", t, e)
    return out


def hub_home_keyboard() -> InlineKeyboardMarkup:
    """
    Lightweight inline mirror of Home (optional).
    Prefer main_reply_keyboard() for the real 7-button Home bar.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "👀 My Watchlist", callback_data="hub:watchlist"
                ),
                InlineKeyboardButton(
                    "📌 My Stockpick", callback_data="hub:mypicks"
                ),
            ],
            [
                InlineKeyboardButton(
                    "📊 Snapshot", callback_data="cmd:snap"
                ),
                InlineKeyboardButton(
                    "🏆 Stock of Day", callback_data="cmd:sotd"
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔗 Links", callback_data="cmd:link"
                ),
                InlineKeyboardButton("📋 Menu", callback_data="cmd:menu"),
            ],
        ]
    )


def _is_private(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type == "private")


async def safe_delete_message(
    bot, chat_id: int | None, message_id: int | None
) -> bool:
    """Best-effort delete. Works in private chats; groups need delete rights."""
    if not bot or chat_id is None or message_id is None:
        return False
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except Exception as e:
        logger.debug("delete_message failed chat=%s msg=%s: %s", chat_id, message_id, e)
        return False


async def cleanup_trigger_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove the user's button/command message so the chat stays clean."""
    msg = update.effective_message or update.message
    if not msg or not context or not context.bot:
        return
    # Prefer private chats (always allowed for bot↔user)
    if not _is_private(update) and not is_admin(update.effective_user):
        return
    await safe_delete_message(context.bot, msg.chat_id, msg.message_id)


def with_command_cleanup(handler):
    """
    Wrap a CommandHandler callback: run it, then delete the /command message
    in private chat so only the bot's reply remains.
    """
    async def _wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            return await handler(update, context)
        finally:
            try:
                # Don't strip admin tooling in groups; private chat only
                if _is_private(update):
                    await cleanup_trigger_message(update, context)
            except Exception as e:
                logger.debug("command cleanup failed: %s", e)

    _wrapped.__name__ = getattr(handler, "__name__", "wrapped_cmd")
    return _wrapped


async def cleanup_callback_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, only_if_private: bool = True
) -> None:
    """Delete the message that held the inline keyboard (after a button press)."""
    query = update.callback_query
    if not query or not query.message or not context or not context.bot:
        return
    if only_if_private and not _is_private(update):
        return
    await safe_delete_message(
        context.bot, query.message.chat_id, query.message.message_id
    )


async def remember_nav_panel(user_id: int | None, msg) -> None:
    """Remember the bot panel message so the next navigation can wipe it."""
    if not user_id or not msg:
        return
    try:
        _nav_panel[user_id] = {
            "chat_id": msg.chat_id,
            "message_id": msg.message_id,
        }
    except Exception:
        pass


async def clear_nav_panel(bot, user_id: int | None) -> None:
    """Delete the last remembered panel for a minimal footprint."""
    if not bot or not user_id:
        return
    info = _nav_panel.pop(user_id, None)
    if not info:
        return
    await safe_delete_message(bot, info.get("chat_id"), info.get("message_id"))


def _msp_state(user_id: int) -> dict:
    st = _msp_ui.get(user_id)
    if not st:
        now = datetime.now(timezone.utc)
        st = {
            "league_open": False,
            "mine_open": False,
            "hist_open": False,
            "league_y": now.year,
            "league_m": now.month,
            "hist_y": now.year,
            "hist_m": now.month,
        }
        _msp_ui[user_id] = st
    return st


async def send_clean(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    delete_trigger: bool = True,
    delete_reply_after: float | None = None,
    **reply_kwargs,
):
    """
    Send text via chat_id (safe after deleting the trigger message),
    and optionally delete this bot reply after a few seconds.
    """
    chat = update.effective_chat
    if delete_trigger:
        await cleanup_trigger_message(update, context)

    msg = None
    if chat and context and context.bot:
        msg = await context.bot.send_message(chat.id, text, **reply_kwargs)
    elif update.effective_message:
        msg = await update.effective_message.reply_text(text, **reply_kwargs)

    if delete_reply_after and msg and context and context.bot:
        async def _later():
            try:
                await asyncio.sleep(float(delete_reply_after))
                await safe_delete_message(context.bot, msg.chat_id, msg.message_id)
            except Exception as e:
                logger.debug("ephemeral delete failed: %s", e)

        try:
            asyncio.create_task(_later())
        except Exception as e:
            logger.debug("Could not schedule ephemeral delete: %s", e)

    return msg



def _watchlist_nav_keyboard() -> list[list]:
    """Consistent bottom nav for watchlist screens."""
    return [
        [
            InlineKeyboardButton("👀 My Watchlist", callback_data="hub:watchlist"),
            InlineKeyboardButton("« Hub", callback_data="hub:home"),
        ]
    ]


async def _remember_watchlist_panel(user_id: int, msg) -> None:
    if not user_id or not msg:
        return
    try:
        _watchlist_ui[user_id] = {
            "chat_id": msg.chat_id,
            "panel_msg_id": msg.message_id,
        }
    except Exception:
        pass


async def _delete_watchlist_prompt(context, user_id: int) -> None:
    """Remove stored prompt message if any."""
    ui = _watchlist_ui.get(user_id) or {}
    pid = ui.get("prompt_msg_id")
    chat_id = ui.get("chat_id")
    if pid and chat_id and context and context.bot:
        await safe_delete_message(context.bot, chat_id, pid)
        ui.pop("prompt_msg_id", None)
        _watchlist_ui[user_id] = ui


async def _watchlist_show_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    awaiting: str,
) -> None:
    """
    Show an input prompt by editing the current panel (clean), with Back.
    Keeps main reply keyboard by sending a fleeting keyboard pulse.
    """
    query = update.callback_query
    user = update.effective_user
    if not user:
        return
    _awaiting_watchlist[user.id] = awaiting
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("« Back to Watchlist", callback_data="hub:watchlist")],
            *_watchlist_nav_keyboard(),
        ]
    )
    msg = None
    if query and query.message:
        try:
            msg = await query.message.edit_text(
                text, parse_mode="Markdown", reply_markup=kb
            )
        except Exception:
            try:
                msg = await query.message.edit_text(text, reply_markup=kb)
            except Exception:
                msg = await context.bot.send_message(
                    query.message.chat_id, text, parse_mode="Markdown", reply_markup=kb
                )
    elif update.effective_chat:
        msg = await context.bot.send_message(
            update.effective_chat.id,
            text,
            parse_mode="Markdown",
            reply_markup=kb,
        )
    if msg:
        await _remember_watchlist_panel(user.id, msg)
    # Re-assert persistent bottom keyboard without leaving clutter
    chat = update.effective_chat
    if chat and context and context.bot:
        try:
            pulse = await context.bot.send_message(
                chat.id,
                "⋯",
                reply_markup=main_reply_keyboard(),
            )
            await safe_delete_message(context.bot, pulse.chat_id, pulse.message_id)
        except Exception:
            pass


async def _watchlist_finish_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    summary: str,
) -> None:
    """
    After add/edit/delete: wipe user input, show brief result, return to watchlist panel.
    """
    user = update.effective_user
    if user:
        await _delete_watchlist_prompt(context, user.id)
    await cleanup_trigger_message(update, context)

    chat = update.effective_chat
    if not chat or not context or not context.bot:
        return

    # Short confirmation then auto-remove
    try:
        conf = await context.bot.send_message(
            chat.id,
            summary,
            parse_mode="Markdown",
            reply_markup=main_reply_keyboard(),
        )

        async def _later():
            try:
                await asyncio.sleep(2.5)
                await safe_delete_message(context.bot, conf.chat_id, conf.message_id)
            except Exception:
                pass

        asyncio.create_task(_later())
    except Exception:
        pass

    # Rebuild clean watchlist view
    class _Msg:
        def __init__(self, chat_id, bot):
            self.chat_id = chat_id
            self._bot = bot
            self.message_id = None

        async def reply_text(self, *a, **k):
            m = await self._bot.send_message(self.chat_id, *a, **k)
            self.message_id = m.message_id
            return m

        async def edit_text(self, *a, **k):
            ui = _watchlist_ui.get(user.id if user else 0) or {}
            mid = ui.get("panel_msg_id")
            if mid:
                try:
                    m = await self._bot.edit_message_text(
                        chat_id=self.chat_id, message_id=mid, *a, **k
                    )
                    return m
                except Exception:
                    pass
            return await self.reply_text(*a, **k)

    class _Up:
        def __init__(self, orig, msg):
            self.effective_user = orig.effective_user
            self.effective_chat = orig.effective_chat
            self.message = msg
            self.callback_query = None

    proxy_msg = _Msg(chat.id, context.bot)
    # Prefer edit existing panel
    ui = _watchlist_ui.get(user.id) if user else None
    if ui and ui.get("panel_msg_id"):
        # Fake callback-style edit path
        class _Q:
            def __init__(self, chat_id, mid, bot, from_user):
                self.message = type("M", (), {})()
                self.message.chat_id = chat_id
                self.message.message_id = mid
                self.message.edit_text = lambda *a, **k: bot.edit_message_text(
                    chat_id=chat_id, message_id=mid, *a, **k
                )
                self.from_user = from_user
                self.data = "hub:watchlist"

            async def answer(self, *a, **k):
                return None

        class _Up2:
            def __init__(self, orig, q):
                self.effective_user = orig.effective_user
                self.effective_chat = orig.effective_chat
                self.message = q.message
                self.callback_query = q

        # Use show_watchlist with edit via stored panel — simpler: always send fresh + delete old panel
        old_mid = ui.get("panel_msg_id")
        if old_mid:
            await safe_delete_message(context.bot, chat.id, old_mid)
        await show_watchlist(_Up(update, proxy_msg), context, edit=False, force_rns=False)
    else:
        await show_watchlist(_Up(update, proxy_msg), context, edit=False, force_rns=False)


def _extract_telegram_link(props: dict) -> str:
    """Pull Telegram group URL from Notion properties (URL or text)."""
    # Prefer known property names (including trailing-space variant)
    for key in (
        "Telegram group ",
        "Telegram group",
        "Telegram Group",
        "Telegram Group Link",
        "Group Link",
        "Telegram",
    ):
        if key in props:
            val = _get_plain_text(props.get(key)).strip()
            if val:
                return val
    # Fallback: any URL-type property containing t.me
    for key, prop in props.items():
        if not isinstance(prop, dict):
            continue
        if prop.get("type") == "url":
            url = (prop.get("url") or "").strip()
            if "t.me" in url.lower() or "telegram" in url.lower():
                return url
        val = _get_plain_text(prop).strip()
        if val and ("t.me" in val.lower() or "telegram.me" in val.lower()):
            return val
    return ""


async def lookup_telegram_group_links(query: str) -> list[dict]:
    """Search UK AIM Micro-Cap by ticker or company; return Telegram group links."""
    if not notion or not query:
        return []

    db_id = (os.getenv("NOTION_TICKERS_DB_ID") or "").strip()
    if not db_id:
        logger.error("NOTION_TICKERS_DB_ID missing for link lookup")
        return []

    raw = db_id.replace("-", "")
    if len(raw) == 32 and "-" not in db_id:
        db_id = f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"

    q = query.strip()
    q_upper = q.lstrip("#").upper()

    # Same robust filter style as get_ticker_from_notion
    filters_to_try = [
        {"property": "Ticker", "title": {"equals": q_upper}},
        {"property": "Ticker", "rich_text": {"equals": q_upper}},
        {"property": "Ticker", "title": {"contains": q_upper}},
        {"property": "Ticker", "rich_text": {"contains": q_upper}},
        {"property": "Company", "title": {"contains": q}},
        {"property": "Company", "rich_text": {"contains": q}},
    ]

    results: list[dict] = []
    seen: set[str] = set()

    try:
        for f in filters_to_try:
            try:
                response = notion.databases.query(
                    database_id=db_id,
                    filter=f,
                    page_size=10,
                )
            except Exception as e:
                logger.warning("Link search filter failed %s: %s", f, e)
                continue

            for page in response.get("results", []):
                pid = page.get("id")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                props = page.get("properties", {})
                ticker = _get_plain_text(props.get("Ticker")).strip().upper()
                company = (
                    _get_plain_text(props.get("Company")).strip()
                    or _get_plain_text(props.get("Name")).strip()
                )
                link = _extract_telegram_link(props)
                results.append(
                    {
                        "ticker": ticker or "—",
                        "company": company or "—",
                        "link": link,
                        "page_id": pid,
                    }
                )
            if results:
                break
    except Exception as e:
        logger.error("lookup_telegram_group_links failed: %s", e)

    logger.info(
        "Link search query=%r matches=%d with_link=%d",
        query,
        len(results),
        sum(1 for r in results if r.get("link")),
    )
    return results


async def save_telegram_group_link_to_notion(
    page_id: str | None, query: str, link_url: str
) -> tuple[bool, str]:
    """Save Telegram group URL onto UK AIM Micro-Cap page. Returns (ok, message)."""
    if not notion:
        return False, "Notion is not configured."
    db_id = (os.getenv("NOTION_TICKERS_DB_ID") or "").strip()
    if not db_id:
        return False, "NOTION_TICKERS_DB_ID missing."

    raw = db_id.replace("-", "")
    if len(raw) == 32 and "-" not in db_id:
        db_id = f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"

    # Resolve page if not provided
    if not page_id:
        rows = await lookup_telegram_group_links(query)
        if rows and rows[0].get("page_id"):
            page_id = rows[0]["page_id"]
    if not page_id:
        return False, f"No Notion page found for `{query}`. Add the ticker in UK AIM Micro-Cap first."

    try:
        page = notion.pages.retrieve(page_id=page_id)
        props = page.get("properties", {})
        prop_name = None
        prop_type = "url"
        for key in (
            "Telegram group ",
            "Telegram group",
            "Telegram Group",
            "Telegram Group Link",
            "Group Link",
        ):
            if key in props:
                prop_name = key
                prop_type = props[key].get("type") or "url"
                break
        if not prop_name:
            # Fallback: any url property mentioning telegram
            for key, prop in props.items():
                if isinstance(prop, dict) and prop.get("type") == "url":
                    prop_name = key
                    prop_type = "url"
                    break
        if not prop_name:
            prop_name = "Telegram group "
            prop_type = "url"

        if prop_type == "url":
            update_props = {prop_name: {"url": link_url}}
        elif prop_type == "rich_text":
            update_props = {
                prop_name: {"rich_text": [{"text": {"content": link_url[:2000]}}]}
            }
        elif prop_type == "title":
            update_props = {
                prop_name: {"title": [{"text": {"content": link_url[:2000]}}]}
            }
        else:
            update_props = {prop_name: {"url": link_url}}

        notion.pages.update(page_id=page_id, properties=update_props)
        # Clear ticker cache so next lookup is fresh
        q_upper = query.lstrip("#").upper().strip()
        _ticker_cache.pop(q_upper, None)
        return True, f"Saved on Notion page for `{query}` (property: {prop_name})."
    except Exception as e:
        logger.error("save_telegram_group_link_to_notion failed: %s", e)
        return False, str(e)


async def notify_admin_missing_group_link(
    context: ContextTypes.DEFAULT_TYPE,
    user,
    query: str,
    rows: list[dict],
) -> None:
    """DM admin R with Group Links request + management buttons."""
    if not context or not user:
        return
    missing = [r for r in (rows or []) if not (r.get("link") or "").strip()]
    if rows and not missing:
        return

    if not rows:
        detail = f"No match in UK AIM Micro-Cap for `{query}`"
        page_id = None
        ticker = query.lstrip("#").upper().strip()
    else:
        detail = "No Telegram group link saved for:\n" + "\n".join(
            f"• `#{r.get('ticker') or '—'}` – {r.get('company') or '—'}"
            for r in missing[:8]
        )
        page_id = missing[0].get("page_id")
        ticker = (missing[0].get("ticker") or query).lstrip("#").upper().strip()

    req_id = f"{int(time.time()) % 1000000:06d}"
    _glink_requests[req_id] = {
        "user_id": user.id,
        "query": query,
        "page_id": page_id,
        "ticker": ticker,
        "name": user.full_name or "—",
        "username": user.username or "N/A",
    }

    text = (
        "🔗 *New Group Links request*\n\n"
        f"• Name: {user.full_name or '—'}\n"
        f"• Username: @{user.username or 'N/A'}\n"
        f"• Telegram ID: `{user.id}`\n"
        f"• Search: `{query}`\n"
        f"• Request ID: `{req_id}`\n\n"
        f"{detail}\n\n"
        "Choose an action:"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "➕ Add new group links",
                    callback_data=f"admin:glinkadd:{req_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "✅ Added – Notify User",
                    callback_data=f"admin:glinkdone:{req_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "⏳ Pending",
                    callback_data=f"admin:glinkpending:{req_id}",
                )
            ],
        ]
    )
    for admin_id in ADMIN_USER_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.error("Failed to notify admin %s of missing group link: %s", admin_id, e)


async def _send_link_results(
    update: Update,
    query: str,
    rows: list[dict],
    context: ContextTypes.DEFAULT_TYPE | None = None,
) -> None:
    # Exactly one activity log + history row per group-link search
    if update.effective_user:
        try:
            await log_member_activity(
                update.effective_user,
                REQUEST_TYPE_TG_LINK,
                notes=f"Group link search: {query}",
            )
        except Exception as e:
            logger.error("group-link activity log failed: %s", e)

    submitted = (
        "Request submitted to the admin group.\n"
        "You will be notified once it is updated."
    )

    if not rows:
        await update.message.reply_text(
            f"(No Telegram Group link yet)\n\n{submitted}",
            reply_markup=main_reply_keyboard(),
        )
        if context:
            await notify_admin_missing_group_link(
                context, update.effective_user, query, rows
            )
        return

    lines = [f"🔗 Group links for {query}\n"]
    any_missing = False
    for r in rows[:8]:
        link = r.get("link") or ""
        ticker = r.get("ticker") or "—"
        company = r.get("company") or "—"
        if link:
            lines.append(f"• #{ticker} – {company}\n  {link}")
        else:
            any_missing = True
            lines.append(f"• #{ticker} – {company}\n  (No Telegram Group link yet)")
    if any_missing:
        lines.append(f"\n{submitted}")
    else:
        lines.append("\nTap 🔗 Group Links to search again.")
    await update.message.reply_text(
        "\n".join(lines),
        disable_web_page_preview=False,
        reply_markup=main_reply_keyboard(),
    )
    if context and any_missing:
        await notify_admin_missing_group_link(
            context, update.effective_user, query, rows
        )


async def link_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Private-only: search Telegram group links from UK AIM Micro-Cap.

    Supports:
      /link
      /link ALRT
      button 🔗 Link  (then type ticker)
    """
    if not update.message:
        return

    try:
        logger.info(
            "link_cmd invoked chat=%s private=%s user=%s args=%s",
            update.effective_chat.id if update.effective_chat else None,
            _is_private(update),
            update.effective_user.id if update.effective_user else None,
            getattr(context, "args", None),
        )

        if not _is_private(update):
            await update.message.reply_text(
                "Group Link only works in a private 1-to-1 chat with me.\n\n"
                "Open a private chat, tap 🔗 Link (or send /link), then type a ticker or company name."
            )
            return

        if not await require_authorized(update, context):
            return

        # One-step: /link ALRT  or  /link Defence Holdings
        args = getattr(context, "args", None) or []
        if args:
            query = " ".join(args).strip()
            rows = await lookup_telegram_group_links(query)
            await _send_link_results(update, query, rows, context)
            return

        user = update.effective_user
        if user:
            _awaiting_link[user.id] = True

        await update.message.reply_text(
            "🔗 Group Link lookup\n\n"
            "Type a ticker or company name and send it, for example:\n"
            "• ALRT\n"
            "• KEFI\n"
            "• Defence Holdings\n\n"
            "Or in one step: /link ALRT\n\n"
            "I will search UK AIM Micro-Cap and return the Telegram group link if one is saved.",
            reply_markup=main_reply_keyboard(),
        )
    except Exception as e:
        logger.error("link_cmd failed: %s", e, exc_info=True)
        try:
            await update.message.reply_text(
                f"Could not start Group Link lookup.\nError: {e}"
            )
        except Exception:
            pass

# ------------------------------------------------------------
# Command handlers
# ------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    text = (update.message.text or "").strip()
    lower = text.lower()

    # --- Admin: paste Telegram group link after "Add new group links" ---
    if user and is_admin(user) and user.id in _awaiting_admin_glink:
        state = _awaiting_admin_glink.pop(user.id)
        link_url = text.strip()
        if not (
            link_url.startswith("http://")
            or link_url.startswith("https://")
            or link_url.startswith("t.me/")
        ):
            # Put state back if they sent something else by mistake
            _awaiting_admin_glink[user.id] = state
            await update.message.reply_text(
                "Please send a valid Telegram link (https://t.me/...).\n"
                "Or send /cancelglink to abort."
            )
            return
        if link_url.startswith("t.me/"):
            link_url = "https://" + link_url

        ok, msg = await save_telegram_group_link_to_notion(
            state.get("page_id"),
            state.get("query") or state.get("ticker") or "",
            link_url,
        )
        target_id = state.get("user_id")
        search_q = state.get("query") or state.get("ticker") or ""
        if ok:
            try:
                if target_id:
                    await context.bot.send_message(
                        chat_id=int(target_id),
                        text=(
                            "🔗 Your Group Links request has been updated.\n\n"
                            f"Search again for: {search_q}\n"
                            "Tap 🔗 Group Links to look it up."
                        ),
                    )
                await update.message.reply_text(
                    f"✅ Group link saved.\n{msg}\n"
                    f"User `{target_id}` has been notified.",
                    parse_mode="Markdown",
                )
            except Exception as e:
                await update.message.reply_text(
                    f"✅ Saved to Notion.\n{msg}\n"
                    f"Could not notify user: {e}"
                )
        else:
            await update.message.reply_text(
                f"❌ Could not save to Notion.\n{msg}\n\n"
                "You can still add it manually in UK AIM Micro-Cap, then use "
                "✅ Added – Notify User on the request."
            )
        return

    if user and is_admin(user) and lower in ("/cancelglink", "cancelglink"):
        _awaiting_admin_glink.pop(user.id, None)
        await update.message.reply_text("Cancelled Group Links add.")
        return

    # --- Persistent keyboard shortcuts (delete trigger text so chat stays clean) ---
    if text in ("🙈 Hide", "Hide") or lower in ("hide", "🙈 hide"):
        await send_clean(
            update,
            context,
            "Keyboard hidden. Tap ☰ Show menu to bring it back.",
            delete_trigger=True,
            delete_reply_after=3.0,
            reply_markup=hidden_reply_keyboard(),
        )
        return

    if text in ("☰ Show menu", "Show menu", "Unhide") or lower in (
        "show menu",
        "☰ show menu",
        "unhide",
    ):
        await send_clean(
            update,
            context,
            "Menu restored.",
            delete_trigger=True,
            delete_reply_after=2.5,
            reply_markup=main_reply_keyboard(),
        )
        return

    if text in ("📋 Menu", "Menu") or lower == "menu":
        await cleanup_trigger_message(update, context)
        await menu_cmd(update, context)
        return

    # Stock Brief – Home keyboard (legacy: Stock Snapshot)
    if text in (
        "📊 Stock Brief",
        "Stock Brief",
        "📊 Stock Snapshot",
        "Stock Snapshot",
        "📊 Snapshot",
        "Snapshot",
    ) or lower in (
        "stock brief",
        "📊 stock brief",
        "stock snapshot",
        "📊 stock snapshot",
        "snapshot",
    ):
        await stock_snapshot_prompt(update, context)
        return

    # Top 10 — session % Top/Bottom performers only
    if text in (
        "📈 Top 10",
        "Top 10",
        "Top10",
        "🏆 Stock of the Day",
        "Stock of the Day",
    ) or lower in (
        "top 10",
        "📈 top 10",
        "stock of the day",
        "🏆 stock of the day",
    ):
        await cleanup_trigger_message(update, context)
        await top10_cmd(update, context)
        return

    # RNS Brief — curated RNS from Hive News Log only
    if text in (
        "📰 RNS Brief",
        "RNS Brief",
        "RNS brief",
        "📰 Daily Brief",
        "Daily Brief",
    ) or lower in (
        "rns brief",
        "📰 rns brief",
        "daily brief",
        "📰 daily brief",
    ):
        await cleanup_trigger_message(update, context)
        await rns_brief_cmd(update, context)
        return

    # Match Link button even if emoji/spacing differs
    if (
        text in (
            "🔗 Group Links",
            "🔗 Group Link",
            "🔗 Link",
            "Group Links",
            "Group Link",
            "Link",
        )
        or lower in (
            "🔗 group links",
            "group links",
            "🔗 group link",
            "group link",
            "🔗 link",
            "link",
        )
        or (lower.replace("🔗", "").strip() in ("link", "group link", "group links"))
        or (len(text) <= 24 and "link" in lower and "stock" not in lower)
    ):
        logger.info("Link button matched text=%r", text)
        await link_cmd(update, context)
        await cleanup_trigger_message(update, context)
        return

    # Stock Snapshot follow-up – user typed a ticker after tapping the button
    if user and user.id in _awaiting_snapshot:
        _awaiting_snapshot.pop(user.id, None)
        raw = text.strip()
        tickers = extract_hashtag_tickers(raw)
        if not tickers:
            t = raw.lstrip("#").upper().strip()
            if t and re.fullmatch(r"[A-Z0-9]{1,6}", t):
                tickers = [t]
        if not tickers:
            await update.message.reply_text(
                "Please send a ticker like `ALRT` or `#KEFI`.",
                parse_mode="Markdown",
                reply_markup=main_reply_keyboard(),
            )
            return
        for t in tickers[:3]:
            await deliver_stock_snapshot(update, context, t)
        return

    # Link search follow-up (private only) after tapping 🔗 Link
    if user and user.id in _awaiting_link:
        if not _is_private(update):
            _awaiting_link.pop(user.id, None)
            return

        _awaiting_link.pop(user.id, None)
        query = text.strip()
        # Keep the search query visible (useful history); only strip pure UI taps
        if not query:
            await update.message.reply_text(
                "Please send a ticker or company name (e.g. ALRT or Defence Holdings)."
            )
            return

        try:
            rows = await lookup_telegram_group_links(query)
            await _send_link_results(update, query, rows, context)
        except Exception as e:
            logger.error("Link follow-up failed: %s", e, exc_info=True)
            await update.message.reply_text(
                f"Could not search group links right now.\nError: {e}",
                reply_markup=main_reply_keyboard(),
            )
        return

    if "my stockpick" in lower or "my🐝 stockpick" in lower:
        try:
            await mystockpick_cmd(update, context)
        except Exception as e:
            logger.error("My Stockpick button failed: %s", e)
            await update.message.reply_text(
                f"Could not open My Stockpick.\n`{e}`",
                parse_mode="Markdown",
            )
        await cleanup_trigger_message(update, context)
        return

    if "watchlist" in lower and len(text) < 40:
        try:
            await show_watchlist(update, context, edit=False)
        except Exception as e:
            logger.error("My Watchlist button failed: %s", e)
            await update.message.reply_text(
                f"Could not open watchlist.\n`{e}`",
                parse_mode="Markdown",
            )
        await cleanup_trigger_message(update, context)
        return

    # Watchlist follow-up
    if user and user.id in _awaiting_watchlist:
        action = _awaiting_watchlist.pop(user.id)
        await handle_watchlist_text(update, context, action, text)
        return

    # Stockpick field follow-up (Summary / Catalyst / Target / Change)
    if user and user.id in _awaiting_field:
        field = _awaiting_field.pop(user.id)
        page_id = _last_stockpick_page.get(user.id)
        # Clear user's typed text + any prior panel for a clean UI
        await cleanup_trigger_message(update, context)
        await clear_nav_panel(context.bot, user.id)
        chat_id = update.effective_chat.id if update.effective_chat else None
        if not page_id or not notion:
            if chat_id:
                await context.bot.send_message(
                    chat_id,
                    "Could not update your stockpick. Please try again.",
                    reply_markup=main_reply_keyboard(),
                )
            return

        try:
            if field == "Change":
                props = {
                    "Message": {
                        "rich_text": [{"text": {"content": text[:2000]}}]
                    },
                }
                tickers = extract_hashtag_tickers(text)
                if tickers:
                    props["Ticker"] = {
                        "rich_text": [{"text": {"content": tickers[0]}}]
                    }
                    props["Stockpick & Month"] = {
                        "title": [{"text": {"content": f"#{tickers[0]}"[:100]}}]
                    }
                notion.pages.update(page_id=page_id, properties=props)
                confirm = "✅ Stockpick updated."
            else:
                notion.pages.update(
                    page_id=page_id,
                    properties={
                        field: {
                            "rich_text": [{"text": {"content": text[:2000]}}]
                        }
                    },
                )
                confirm = f"✅ Added {field}."
            if chat_id:
                sent = await context.bot.send_message(
                    chat_id,
                    confirm,
                    reply_markup=main_reply_keyboard(),
                )
                await remember_nav_panel(user.id, sent)
                # Refresh My Stockpick hub in place (clean)
                try:
                    await show_stockpick_hub(update, context, edit=False)
                    # Remove the brief confirm once hub is up
                    await safe_delete_message(
                        context.bot, sent.chat_id, sent.message_id
                    )
                except Exception:
                    pass
            await ensure_home_keyboard(context.bot, chat_id)
        except Exception as e:
            logger.error("Failed to update stockpick field %s: %s", field, e)
            if chat_id:
                await context.bot.send_message(
                    chat_id,
                    "Could not save that update. Please try again later.",
                    reply_markup=main_reply_keyboard(),
                )
        return

    if not await should_reply(update, context):
        return

    bot_username = (context.bot.username or "").lower()
    clean_text = re.sub(
        rf"@{re.escape(bot_username)}\b", "", text, flags=re.IGNORECASE
    ).strip()
    clean_lower = clean_text.lower()

    # 1. #stockpick capture
    if "#stockpick" in clean_lower:
        if not await require_authorized(update, context):
            return

        if await has_submitted_this_month(user):
            month_name = datetime.now(timezone.utc).strftime("%B")
            page_id = _last_stockpick_page.get(user.id)
            if not page_id:
                page_id = await find_this_month_stockpick_page(user)
                if page_id:
                    _last_stockpick_page[user.id] = page_id

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
            await cleanup_trigger_message(update, context)
            chat_id = update.effective_chat.id if update.effective_chat else None
            if chat_id:
                sent = await context.bot.send_message(
                    chat_id,
                    f"⚠️ You already submitted a #stockpick for *{month_name}*.\n"
                    "One pick per month — add details or change it:",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
                await remember_nav_panel(user.id, sent)
                await ensure_home_keyboard(context.bot, chat_id)
            return

        user_name = user.full_name if user else "Unknown"
        tickers = extract_hashtag_tickers(clean_text)
        ticker = tickers[0] if tickers else None
        period_type, period_value = extract_period(clean_text)

        page_id, save_error = await save_stockpick_to_notion(
            clean_text,
            user_name,
            ticker,
            period_type,
            period_value,
            user_id=user.id if user else None,
        )

        # Wipe the user's #stockpick message for a clean chat
        await cleanup_trigger_message(update, context)
        await clear_nav_panel(context.bot, user.id if user else None)
        chat_id = update.effective_chat.id if update.effective_chat else None

        if page_id:
            _last_stockpick_page[user.id] = page_id
            await log_member_activity(user, REQUEST_TYPE_STOCKPICK)
            reply = "✅ Captured your #stockpick"
            if ticker:
                reply += f" (#{ticker})"
            if period_type and period_value:
                reply += f"\n📅 {period_type}: *{period_value}*"
            reply += "\nSaved. Expand *My pick this month* to add details."

            keyboard = [
                [
                    InlineKeyboardButton(
                        "✏️ My pick this month ▾", callback_data="msp:ui_mine"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📌 Open My Stockpick", callback_data="hub:mypicks"
                    )
                ],
                [InlineKeyboardButton("« Hub", callback_data="hub:home")],
            ]
            if chat_id:
                sent = await context.bot.send_message(
                    chat_id,
                    reply,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
                await remember_nav_panel(user.id, sent)
                await ensure_home_keyboard(context.bot, chat_id)
        else:
            if chat_id:
                await context.bot.send_message(
                    chat_id,
                    "Could not save your #stockpick right now.\n"
                    f"Error: {save_error or 'unknown'}",
                    reply_markup=main_reply_keyboard(),
                )
        return

    # 2. Ticker lookup (snapshot / summary) – authorised only
    tickers = extract_hashtag_tickers(clean_text)
    if tickers:
        if not await require_authorized(update, context):
            return
        for t in tickers:
            try:
                data = await get_ticker_from_notion(t)
                if data:
                    await log_member_activity(user, REQUEST_TYPE_SNAPSHOT)
                    try:
                        stockpickers = await get_stockpickers_for_ticker(t)
                    except Exception as e:
                        logger.error("stockpickers failed for %s: %s", t, e)
                        stockpickers = []
                    body = format_reply(t, data, stockpickers)
                    try:
                        await update.message.reply_text(body, parse_mode="Markdown")
                    except Exception:
                        await update.message.reply_text(body)
                else:
                    await update.message.reply_text(
                        f"I don’t have #{t} in the current UK AIM Micro-Cap snapshot."
                    )
            except Exception as e:
                logger.error("Ticker lookup path failed for %s: %s", t, e)
                await update.message.reply_text(
                    f"Lookup failed for #{t}. Please try again.\n`{e}`"
                )
        return

    # 3. Fallback help (private vs group)
    if _is_private(update):
        await update.message.reply_text(
            "Use the buttons below, or try:\n"
            "• /link ALRT – Group Link lookup\n"
            "• #KEFI snapshot – ticker summary\n"
            "• #stockpick my idea – save a pick\n"
            "• /menu – full command list",
            reply_markup=main_reply_keyboard(),
        )
    else:
        await update.message.reply_text(
            "Hi! To look up a ticker use:\n"
            "@Bot #KEFI summary or #KEFI snapshot\n"
            "Or use #stockpick to save an idea."
        )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    name = user.first_name if user else "there"
    authorised = await is_authorized(update, context)

    if authorised:
        text = (
            f"Hi {name}! 👋\n\n"
            "It's 🐝 BuzzBot here.\n"
            "✅ You are *Authorised* and can use the bot.\n\n"
            "Home keyboard (always available):\n"
            "• 👀 My Watchlist\n"
            "• 📌 My Stockpick\n"
            "• 📊 Stock Brief\n"
            "• 🔗 Group Links\n"
            "• 📈 Top 10\n"
            "• 📰 RNS Brief\n"
            "• 📋 Menu\n"
            "• 🙈 Hide\n\n"
            "In the group: `@Bot #KEFI summary` or `#stockpick …`"
        )
    else:
        text = (
            f"Hi {name}! 👋\n\n"
            "It's 🐝 BuzzBot here.\n"
            "❌ You are *not authorised* yet.\n\n"
            "Send /request to ask for access.\n"
            "Check status with /status."
        )

    # Remove /start (or Start button text) so only the welcome stays
    await cleanup_trigger_message(update, context)
    chat = update.effective_chat
    if chat:
        await context.bot.send_message(
            chat_id=chat.id,
            text=text,
            parse_mode="Markdown",
            reply_markup=main_reply_keyboard(),
        )
    elif update.message:
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=main_reply_keyboard(),
        )


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Simplified Menu – access & help only; features live on Home keyboard."""
    text = (
        "🐝 *BuzzBot Menu*\n\n"
        "• /start – Welcome & status\n"
        "• /status – Check if you are authorised\n"
        "• /request – Request access\n"
        "• /faq – FAQ\n\n"
        "Features are on the Home keyboard:\n"
        "My Watchlist · My Stockpick · Stock Brief · Group Links"
    )
    await cleanup_trigger_message(update, context)
    chat = update.effective_chat
    markup_inline = menu_inline_keyboard()
    if chat:
        await context.bot.send_message(
            chat_id=chat.id,
            text=text,
            parse_mode="Markdown",
            reply_markup=markup_inline,
        )
        # Keep persistent Home keyboard on top
        try:
            pulse = await context.bot.send_message(
                chat.id, "⋯", reply_markup=main_reply_keyboard()
            )
            await safe_delete_message(context.bot, pulse.chat_id, pulse.message_id)
        except Exception:
            pass
    elif update.message:
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=markup_inline,
        )


async def _load_microcap_tickers(limit: int | None = None) -> list[tuple[str, str]]:
    """
    Return [(ticker, company), ...] from the full UK AIM Micro-Cap universe.
    Paginate until exhausted (or optional hard limit).
    """
    out: list[tuple[str, str]] = []
    if not (notion or NOTION_TOKEN):
        return out
    db_id = (os.getenv("NOTION_TICKERS_DB_ID") or NOTION_TICKERS_DB_ID or "").strip()
    ds_id = (os.getenv("NOTION_TICKERS_DATA_SOURCE_ID") or NOTION_TICKERS_DATA_SOURCE_ID or "").strip()
    if not db_id and not ds_id:
        return out
    hard_cap = limit if limit is not None else 5000
    try:
        cursor = None
        while len(out) < hard_cap:
            page_size = min(100, hard_cap - len(out))
            kwargs = {"page_size": page_size}
            if cursor:
                kwargs["start_cursor"] = cursor
            try:
                resp = notion_query_data_source(
                    data_source_id=ds_id or None,
                    database_id=db_id or None,
                    **kwargs,
                )
            except Exception:
                # Classic client fallback
                if not notion or not db_id:
                    break
                qkwargs = {"database_id": db_id, "page_size": page_size}
                if cursor:
                    qkwargs["start_cursor"] = cursor
                resp = notion.databases.query(**qkwargs)
            for page in resp.get("results", []):
                props = page.get("properties") or {}
                t = (
                    _get_plain_text(props.get("Ticker"))
                    or _get_plain_text(props.get("Name"))
                    or ""
                ).lstrip("#").upper().strip()
                if not t or not re.fullmatch(r"[A-Z0-9]{1,6}", t):
                    continue
                company = (
                    _get_plain_text(props.get("Company"))
                    or _get_plain_text(props.get("Company Name"))
                    or _get_plain_text(props.get("Name"))
                    or t
                )
                out.append((t, company[:80]))
                if len(out) >= hard_cap:
                    break
            if not resp.get("has_more"):
                break
            cursor = resp.get("next_cursor")
            if not cursor:
                break
    except Exception as e:
        logger.error("_load_microcap_tickers failed: %s", e)
    seen = set()
    uniq: list[tuple[str, str]] = []
    for t, c in out:
        if t in seen:
            continue
        seen.add(t)
        uniq.append((t, c))
    logger.info("_load_microcap_tickers loaded %d unique names", len(uniq))
    return uniq


async def _rank_tickers_by_live_pct(
    pairs: list[tuple[str, str]],
    *,
    concurrency: int = 20,
    overall_timeout: float = 40.0,
) -> list[tuple[str, str, float]]:
    """
    Fetch Yahoo session % concurrently; return quotes sorted by % desc.
    Hard overall timeout so Daily Brief / SOTD never hangs for minutes.
    """
    if not pairs:
        return []
    # Cap universe size for interactive boards
    if len(pairs) > 150:
        pairs = pairs[:150]
    sem = asyncio.Semaphore(max(1, concurrency))
    ranked: list[tuple[str, str, float]] = []

    async def _one(t: str, company: str) -> tuple[str, str, float] | None:
        async with sem:
            try:
                pct = await asyncio.wait_for(
                    asyncio.to_thread(_fetch_pct_on_day_live, t),
                    timeout=5.0,
                )
            except Exception:
                return None
        if pct is None:
            return None
        return (t, company, float(pct))

    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                *[_one(t, c) for t, c in pairs],
                return_exceptions=True,
            ),
            timeout=overall_timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "_rank_tickers_by_live_pct timed out after %.0fs (%d pairs)",
            overall_timeout,
            len(pairs),
        )
        results = []

    for r in results or []:
        if isinstance(r, Exception):
            continue
        if r is None:
            continue
        ranked.append(r)
    ranked.sort(key=lambda x: x[2], reverse=True)
    logger.info(
        "_rank_tickers_by_live_pct got %d quotes from %d pairs",
        len(ranked),
        len(pairs),
    )
    return ranked


async def _ensure_daily_brief_rns(day: str) -> list[dict]:
    """Load & cache today's ranked RNS only (Notion) with a hard timeout."""
    rns = _daily_brief_rns.get(day)
    if rns is not None:
        return rns
    try:
        rows = await asyncio.wait_for(_load_rns_for_day(day), timeout=15.0)
    except asyncio.TimeoutError:
        logger.error("_ensure_daily_brief_rns timed out day=%s", day)
        rows = []
    except Exception as e:
        logger.error("_ensure_daily_brief_rns failed day=%s: %s", day, e)
        rows = []
    ticker_counts: dict[str, int] = {}
    for r in rows:
        if r.get("ticker"):
            ticker_counts[r["ticker"]] = ticker_counts.get(r["ticker"], 0) + 1
    for r in rows:
        t = r.get("ticker") or ""
        if ticker_counts.get(t, 0) >= 2:
            r["score"] = min(100.0, float(r.get("score") or 0) + 5)
    rows.sort(key=lambda r: float(r.get("score") or 0), reverse=True)
    _daily_brief_rns[day] = rows
    return rows


async def _ensure_daily_brief_pct(day: str) -> list[tuple[str, str, float]]:
    """
    Load & cache session % ranking (slower — Yahoo).
    Only call when user opens Performers, not on Daily Brief open.
    """
    pct = _daily_brief_pct.get(day)
    if pct is not None:
        return pct
    # Cap at 120 names so board loads in ~15–30s not 5+ minutes
    pairs = await _load_microcap_tickers(limit=120)
    pct = (
        await _rank_tickers_by_live_pct(
            pairs, concurrency=20, overall_timeout=35.0
        )
        if pairs
        else []
    )
    _daily_brief_pct[day] = pct
    return pct


async def _ensure_daily_brief_data(
    day: str,
) -> tuple[list[dict], list[tuple[str, str, float]]]:
    """
    Back-compat: RNS always; % only if already cached (else empty list).
    Prefer _ensure_daily_brief_rns / _ensure_daily_brief_pct separately.
    """
    rns = await _ensure_daily_brief_rns(day)
    pct = _daily_brief_pct.get(day) or []
    return rns, pct


def _format_daily_brief_news(
    day: str, rns: list[dict], page: int
) -> tuple[str, InlineKeyboardMarkup]:
    """Page 0 = ranks 1–5, page 1 = ranks 6–10."""
    page = 0 if page < 0 else (1 if page > 1 else page)
    start = page * 5
    chunk = rns[start : start + 5]
    max_page = 1 if len(rns) > 5 else 0

    lines = [
        "📰 *RNS Brief*",
        f"_Hive RNS News Log · {day}_",
        f"_Page {page + 1}/{max_page + 1} · ranks {start + 1}–{start + (len(chunk) or 0)}_",
        "",
    ]
    if not chunk:
        lines.append("_No RNS items for this page today._")
    else:
        for i, r in enumerate(chunk, start + 1):
            t = (r.get("ticker") or "—").upper()
            company = r.get("company") or ""
            head = f"#{t}" if t != "—" else "RNS"
            if company:
                head = f"{head} {company}"
            lines.append(f"{i}. *{head}*")
            lines.append(f"   📰 {r.get('title') or '—'}")
            if r.get("summary"):
                s = r["summary"]
                if len(s) > 160:
                    s = s[:157] + "…"
                lines.append(f"   _{s}_")
            if r.get("link"):
                lines.append(f"   {r['link']}")
            lines.append("")

    lines.append("_Use ‹ › to flip pages (1–5 / 6–10). Tap ticker for snapshot._")
    lines.append("_Not financial advice. DYOR._")

    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton("‹ Prev", callback_data="brief:news:0")
        )
    nav.append(
        InlineKeyboardButton(f"{page + 1}/{max_page + 1}", callback_data="brief:noop")
    )
    if page < max_page:
        nav.append(
            InlineKeyboardButton("Next ›", callback_data="brief:news:1")
        )

    kb = [
        nav,
        [InlineKeyboardButton("« Hub", callback_data="hub:home")],
    ]
    # Snapshot shortcuts for visible tickers
    for r in chunk:
        t = (r.get("ticker") or "")[:12].upper()
        if t:
            kb.insert(
                -1,
                [
                    InlineKeyboardButton(
                        f"#{t}", callback_data=f"sotd:snap:{t}"
                    )
                ],
            )
    return "\n".join(lines).strip(), InlineKeyboardMarkup(kb)


def _format_daily_brief_pct(
    day: str,
    ranked: list[tuple[str, str, float]],
    *,
    board: str = "top",  # "top" | "bot"
    page: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Top 10 or Bottom 10 session % board.
      page 0 → ranks 1–5
      page 1 → ranks 6–10
    """
    page = 0 if page < 0 else (1 if page > 1 else page)
    start = page * 5
    worst = list(reversed(ranked))
    max_page = 1 if len(ranked) > 5 else 0
    board = "bot" if board == "bot" else "top"
    ordered = worst if board == "bot" else ranked
    chunk = ordered[start : start + 5]

    if board == "bot":
        title = "📉 *Bottom 10 — session %*"
        switch_btn = InlineKeyboardButton(
            "📈 Top 10", callback_data="brief:pct:top:0"
        )
    else:
        title = "📈 *Top 10 — session %*"
        switch_btn = InlineKeyboardButton(
            "📉 Bottom 10", callback_data="brief:pct:bot:0"
        )

    lines = [
        title,
        f"_UK AIM Micro-Cap · live % · {day}_",
        f"_Page {page + 1}/{max_page + 1} · ranks {start + 1}–{start + 5}_",
        "",
    ]
    if not chunk:
        lines.append("_No live quotes for this slice._")
        lines.append("")
    else:
        for i, (tkr, company, pct) in enumerate(chunk, start + 1):
            sign = "+" if pct >= 0 else ""
            arrow = "🟢" if pct > 0 else ("🔴" if pct < 0 else "⚪")
            name = (company or "").strip()
            if len(name) > 28:
                name = name[:25] + "…"
            lines.append(f"{i}. *#{tkr}* {name}")
            lines.append(f"   {arrow} *{sign}{pct:.2f}%*")
        lines.append("")

    lines.append("_‹ › ranks 6–10 · switch Top/Bottom · tap ticker for snapshot._")
    lines.append("_Yahoo Finance (LSE). Not FA. DYOR._")

    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton("‹", callback_data=f"brief:pct:{board}:0")
        )
    nav.append(
        InlineKeyboardButton(
            f"{page + 1}/{max_page + 1}", callback_data="brief:noop"
        )
    )
    if page < max_page:
        nav.append(
            InlineKeyboardButton("›", callback_data=f"brief:pct:{board}:1")
        )

    kb: list[list] = [nav] if nav else []
    for tkr, company, pct in chunk:
        sign = "+" if pct >= 0 else ""
        kb.append(
            [
                InlineKeyboardButton(
                    f"#{tkr} {sign}{pct:.1f}%",
                    callback_data=f"sotd:snap:{tkr[:12]}",
                )
            ]
        )
    kb.append([switch_btn])
    kb.append([InlineKeyboardButton("« Hub", callback_data="hub:home")])
    return "\n".join(lines).strip(), InlineKeyboardMarkup(kb)


async def daily_brief_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    RNS Brief — curated news from Hive RNS News Log only.
    Auth + RNS each have hard timeouts so the UI never sits for minutes.
    """
    user = update.effective_user
    msg = update.effective_message
    if not msg:
        return

    # Auth with overall 8s budget — never block RNS Brief for minutes
    try:
        ok = await asyncio.wait_for(is_authorized(update, context), timeout=8.0)
    except asyncio.TimeoutError:
        logger.error("daily_brief_cmd: is_authorized timed out")
        ok = is_admin(user)
    if not ok:
        await msg.reply_text(
            "🔒 Only authorised members can use Daily Brief.\n"
            "Send /request to ask for access.",
            reply_markup=main_reply_keyboard(),
        )
        return

    day = datetime.now(timezone.utc).date().isoformat()

    # IMPORTANT: clear old panels BEFORE posting the loading message.
    # Previous bug: remember(status) then clear_nav_panel() deleted status,
    # so users saw nothing / endless "loading".
    try:
        await clear_nav_panel(context.bot, user.id if user else None)
    except Exception:
        pass
    try:
        await cleanup_trigger_message(update, context)
    except Exception:
        pass

    try:
        status = await msg.reply_text(
            "📰 *Daily Brief*\n\nLoading today’s RNS…",
            parse_mode="Markdown",
            reply_markup=main_reply_keyboard(),
        )
    except Exception as e:
        logger.error("daily_brief_cmd: could not send ack: %s", e)
        return
    await remember_nav_panel(user.id if user else None, status)

    try:
        rns = await _ensure_daily_brief_rns(day)
        if not rns:
            text = (
                f"📰 *RNS Brief*\n"
                f"_Hive RNS News Log · {day}_\n\n"
                "_No RNS rows for today yet "
                "(or Notion timed out under 15s)._\n\n"
                "Try again shortly, or use Top 10 for session %."
            )
            markup = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("« Hub", callback_data="hub:home")],
                ]
            )
        else:
            text, markup = _format_daily_brief_news(day, rns, page=0)
        try:
            await status.edit_text(
                text,
                parse_mode="Markdown",
                reply_markup=markup,
                disable_web_page_preview=True,
            )
        except Exception:
            try:
                await status.edit_text(
                    text.replace("*", "").replace("_", ""),
                    reply_markup=markup,
                    disable_web_page_preview=True,
                )
            except Exception as e2:
                logger.error(
                    "daily_brief_cmd edit failed: %s — sending new message",
                    e2,
                )
                try:
                    sent = await context.bot.send_message(
                        status.chat_id,
                        text.replace("*", "").replace("_", ""),
                        reply_markup=markup,
                        disable_web_page_preview=True,
                    )
                    await remember_nav_panel(user.id if user else None, sent)
                except Exception as e3:
                    logger.error("daily_brief_cmd send fallback failed: %s", e3)
        try:
            await log_member_activity(
                user, REQUEST_TYPE_RNS_BRIEF, notes="RNS Brief opened"
            )
        except Exception as le:
            logger.warning("RNS Brief history log failed: %s", le)
    except Exception as e:
        logger.error("daily_brief_cmd failed: %s", e)
        try:
            await status.edit_text(f"RNS Brief failed: {e}")
        except Exception:
            try:
                await context.bot.send_message(
                    status.chat_id, f"RNS Brief failed: {e}"
                )
            except Exception:
                pass


async def daily_brief_button(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Pagination / section switch for Daily Brief."""
    query = update.callback_query
    user = query.from_user if query else None
    if not query or not user:
        return
    data = query.data or ""
    if data == "brief:noop":
        await query.answer()
        return
    if not await is_authorized(update, context):
        await query.answer("Authorised members only.", show_alert=True)
        return

    day = datetime.now(timezone.utc).date().isoformat()

    # News pages — fast
    if data.startswith("brief:news:"):
        await query.answer()
        try:
            page = int(data.split(":")[-1])
        except ValueError:
            page = 0
        rns = await _ensure_daily_brief_rns(day)
        text, markup = _format_daily_brief_news(day, rns, page)
        try:
            await query.edit_message_text(
                text,
                parse_mode="Markdown",
                reply_markup=markup,
                disable_web_page_preview=True,
            )
        except Exception:
            try:
                await query.edit_message_text(
                    text.replace("*", "").replace("_", ""),
                    reply_markup=markup,
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.warning("daily_brief news edit failed: %s", e)
        await remember_nav_panel(user.id, query.message)
        return

    # Performers — load Yahoo ranking only now (with progress + timeout)
    if data.startswith("brief:pct:"):
        parts = data.split(":")
        board = parts[2] if len(parts) > 2 else "top"
        try:
            page = int(parts[3]) if len(parts) > 3 else 0
        except ValueError:
            page = 0
        if board not in ("top", "bot"):
            board = "top"

        cached = _daily_brief_pct.get(day)
        if cached is None:
            await query.answer("Loading live %…")
            try:
                await query.edit_message_text(
                    "📈📉 *Session performers*\n\n"
                    "Fetching live day-% for AIM Micro-Cap "
                    "(up to ~30s, then results)…",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
            try:
                pct = await _ensure_daily_brief_pct(day)
            except Exception as e:
                logger.error("daily brief pct load failed: %s", e)
                try:
                    await query.edit_message_text(
                        f"Could not load performers: {e}\n\n"
                        "Try again or open Stock of the Day → % Today.",
                        reply_markup=InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton(
                                        "📰 News",
                                        callback_data="brief:news:0",
                                    )
                                ],
                                [
                                    InlineKeyboardButton(
                                        "« Hub", callback_data="hub:home"
                                    )
                                ],
                            ]
                        ),
                    )
                except Exception:
                    pass
                return
        else:
            await query.answer()
            pct = cached

        text, markup = _format_daily_brief_pct(
            day, pct, board=board, page=page
        )
        try:
            await query.edit_message_text(
                text,
                parse_mode="Markdown",
                reply_markup=markup,
                disable_web_page_preview=True,
            )
        except Exception:
            try:
                await query.edit_message_text(
                    text.replace("*", "").replace("_", ""),
                    reply_markup=markup,
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.warning("daily_brief pct edit failed: %s", e)
        await remember_nav_panel(user.id, query.message)
        # Log board open once (page 0 only) to Request History
        if page == 0:
            try:
                board_label = "Top 10" if board == "top" else "Bottom 10"
                await log_member_activity(
                    user,
                    REQUEST_TYPE_TOP10,
                    notes=f"{board_label} board viewed",
                )
            except Exception:
                pass
        return

    await query.answer()
    rns = await _ensure_daily_brief_rns(day)
    text, markup = _format_daily_brief_news(day, rns, 0)
    try:
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    except Exception:
        pass
    await remember_nav_panel(user.id, query.message)


async def top10_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Top 10 keyboard — Top 10 / Bottom 10 session % only.
    """
    user = update.effective_user
    msg = update.effective_message
    if not msg:
        return
    try:
        ok = await asyncio.wait_for(is_authorized(update, context), timeout=8.0)
    except asyncio.TimeoutError:
        ok = is_admin(user)
    if not ok:
        await msg.reply_text(
            "🔒 Only authorised members can use Top 10.\n"
            "Send /request to ask for access.",
            reply_markup=main_reply_keyboard(),
        )
        return

    try:
        await clear_nav_panel(context.bot, user.id if user else None)
    except Exception:
        pass
    try:
        await cleanup_trigger_message(update, context)
    except Exception:
        pass

    body = (
        "📈 *Top 10*\n\n"
        "Session % movers from UK AIM Micro-Cap.\n\n"
        "Choose a board:"
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📈 Top 10", callback_data="brief:pct:top:0"
                ),
                InlineKeyboardButton(
                    "📉 Bottom 10", callback_data="brief:pct:bot:0"
                ),
            ],
            [InlineKeyboardButton("« Hub", callback_data="hub:home")],
        ]
    )
    sent = await msg.reply_text(
        body,
        parse_mode="Markdown",
        reply_markup=kb,
    )
    await remember_nav_panel(user.id if user else None, sent)
    await ensure_home_keyboard(context.bot, msg.chat_id)
    try:
        await log_member_activity(
            user, REQUEST_TYPE_TOP10, notes="Top 10 menu opened"
        )
    except Exception as le:
        logger.warning("Top 10 history log failed: %s", le)


async def stock_of_the_day_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Back-compat alias → Top 10 chooser."""
    await top10_cmd(update, context)


async def rns_brief_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Home button alias → RNS Brief (news only)."""
    await daily_brief_cmd(update, context)


async def stock_of_the_day_pct(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit_msg=None
) -> None:
    """Top movers by live Yahoo session % across full Micro-Cap universe."""
    user = update.effective_user
    msg = edit_msg or update.effective_message
    if not msg:
        return

    async def _set(text: str, markup=None):
        try:
            await msg.edit_text(
                text,
                parse_mode="Markdown",
                reply_markup=markup or hub_back_keyboard(),
                disable_web_page_preview=True,
            )
        except Exception:
            try:
                await msg.edit_text(
                    text.replace("*", "").replace("_", ""),
                    reply_markup=markup or hub_back_keyboard(),
                    disable_web_page_preview=True,
                )
            except Exception:
                pass

    await _set("🏆 Scanning AIM list for live session %…")

    try:
        # Cap + timeout — full universe Yahoo scan was hanging 5+ minutes
        pairs = await _load_microcap_tickers(limit=150)
        if not pairs:
            await _set(
                "🏆 *Stock of the Day — % Today*\n\n"
                "Could not load the UK AIM Micro-Cap list from Notion."
            )
            return

        await _set(f"🏆 Scanning *{len(pairs)}* AIM names for live session %…")
        ranked = await _rank_tickers_by_live_pct(
            pairs, concurrency=20, overall_timeout=40.0
        )
        top = ranked[:5]
        quoted = len(ranked)

        if not top:
            await _set(
                "🏆 *Stock of the Day — % Today*\n\n"
                f"Loaded *{len(pairs)}* names but no live Yahoo quotes. "
                "Try again shortly."
            )
            return

        lines = [
            "🏆 *Stock of the Day — % Today*",
            "_Top movers by session % (UK AIM Micro-Cap sample)_",
            f"_Scanned {len(pairs)} · {quoted} with live Yahoo quotes_",
            "",
        ]
        for i, (t, company, pct) in enumerate(top, 1):
            sign = "+" if pct >= 0 else ""
            arrow = "🟢" if pct > 0 else ("🔴" if pct < 0 else "⚪")
            lines.append(f"{i}. *#{t}* {company}")
            lines.append(f"   {arrow} *{sign}{pct:.2f}%*")
        lines.append("")
        lines.append("_Tap a ticker for the full snapshot._")
        lines.append("_Prices: Yahoo Finance (LSE). Not FA. DYOR._")

        kb_rows = []
        for t, company, pct in top:
            sign = "+" if pct >= 0 else ""
            kb_rows.append(
                [
                    InlineKeyboardButton(
                        f"#{t}  {sign}{pct:.2f}%",
                        callback_data=f"sotd:snap:{t[:20]}",
                    )
                ]
            )
        kb_rows.append(
            [
                InlineKeyboardButton("« Boards", callback_data="sotd:menu"),
                InlineKeyboardButton("« Hub", callback_data="hub:home"),
            ]
        )
        await _set("\n".join(lines), InlineKeyboardMarkup(kb_rows))
        chat_id = getattr(msg, "chat_id", None) or (
            update.effective_chat.id if update.effective_chat else None
        )
        if chat_id:
            await ensure_home_keyboard(context.bot, chat_id)
    except Exception as e:
        logger.error("stock_of_the_day_pct failed: %s", e)
        await _set(f"Stock of the Day (% Today) failed: {e}")


# Significance weights for RNS titles/summaries (no readership field in Notion)
_RNS_SCORE_HIGH = (
    "acquisition",
    "takeover",
    "recommended offer",
    "firm offer",
    "fundraising",
    "placing",
    "subscription",
    "retail offer",
    "open offer",
    "interim results",
    "final results",
    "full year results",
    "half-year",
    "half year",
    "profit warning",
    "trading update",
    "administration",
    "administrators",
    "suspension",
    "restore",
    "contract win",
    "major contract",
    "offtake",
    "resource",
    "drill",
    "feasibility",
    "permitting",
    "licence",
    "license",
    "fda",
    "clinical",
    "partnership",
    "joint venture",
    "disposal",
    "divestment",
    "strategic review",
)
_RNS_SCORE_MED = (
    "appointment",
    "board change",
    "director",
    "holding(s)",
    "holdings in company",
    "agm",
    "egm",
    "general meeting",
    "loan note",
    "convertible",
    "warrant",
    "exercise of options",
)
_RNS_SCORE_LOW = (
    "total voting rights",
    "tvr",
    "block listing",
    "pdmr",
    "form 8",
    "rule 8",
    "second price monitoring",
    "price monitoring extension",
)


def _score_rns_significance(title: str, summary: str) -> tuple[float, str]:
    """
    Heuristic significance score (0–100) + short reason tag.
    Proxy for readership/virality/context until explicit metrics exist in Notion.
    """
    blob = f"{title or ''} {summary or ''}".lower()
    score = 10.0
    tags: list[str] = []

    for kw in _RNS_SCORE_HIGH:
        if kw in blob:
            score += 18
            tags.append(kw)
    for kw in _RNS_SCORE_MED:
        if kw in blob:
            score += 8
            if len(tags) < 3:
                tags.append(kw)
    for kw in _RNS_SCORE_LOW:
        if kw in blob:
            score -= 6

    # Substance: longer AI summary → more context
    s_len = len((summary or "").strip())
    if s_len > 400:
        score += 12
    elif s_len > 180:
        score += 7
    elif s_len > 60:
        score += 3
    elif s_len == 0:
        score -= 5

    # Title punchiness
    t_len = len((title or "").strip())
    if 12 <= t_len <= 80:
        score += 3

    score = max(0.0, min(100.0, score))
    reason = ", ".join(dict.fromkeys(tags[:3])) if tags else "general update"
    return score, reason


async def _load_rns_for_day(day_iso: str | None = None) -> list[dict]:
    """
    RNS News Log rows for a calendar day.
    Uses HTTP helper only (12s timeout) — never the SDK without timeout.
    Runs in a thread so the event loop stays responsive.
    """
    if not day_iso:
        day_iso = datetime.now(timezone.utc).date().isoformat()
    ds_id = (NOTION_RNS_DATA_SOURCE_ID or "").strip() or None
    db_id = (NOTION_RNS_DB_ID or "").strip() or None
    if not NOTION_TOKEN or (not ds_id and not db_id):
        return []

    def _sync_load() -> list[dict]:
        rows: list[dict] = []
        try:
            resp = notion_query_data_source(
                data_source_id=ds_id,
                database_id=db_id,
                filter={
                    "property": "RNS Date",
                    "date": {"equals": day_iso},
                },
                sorts=[{"property": "RNS Date", "direction": "descending"}],
                page_size=50,
            )
            pages = resp.get("results", [])
            if not pages:
                # Lightweight fallback: newest 50 by created_time
                resp = notion_query_data_source(
                    data_source_id=ds_id,
                    database_id=db_id,
                    sorts=[
                        {
                            "timestamp": "created_time",
                            "direction": "descending",
                        }
                    ],
                    page_size=50,
                )
                pages = resp.get("results", [])

            for page in pages:
                props = page.get("properties") or {}
                title = _get_plain_text(props.get("Title")) or "RNS"
                ticker = (
                    _get_plain_text(props.get("Ticker")) or ""
                ).lstrip("#").upper().strip()
                company = _get_plain_text(props.get("Company")) or ""
                summary = _strip_html(
                    _get_plain_text(props.get("AI Summary")) or ""
                )
                link = ""
                lp = props.get("Link") or {}
                if isinstance(lp, dict):
                    link = (lp.get("url") or "").strip()
                date_str = ""
                dp = props.get("RNS Date") or {}
                if isinstance(dp, dict) and dp.get("date"):
                    date_str = (dp["date"].get("start") or "")[:10]
                created = page.get("created_time") or ""
                if date_str and date_str != day_iso:
                    continue
                if not date_str and created and not created.startswith(day_iso):
                    continue
                score, reason = _score_rns_significance(title, summary)
                rows.append(
                    {
                        "title": title[:140],
                        "ticker": ticker,
                        "company": company[:80],
                        "summary": summary[:320],
                        "link": link,
                        "date": date_str or day_iso,
                        "score": score,
                        "reason": reason,
                        "page_id": page.get("id"),
                    }
                )
        except Exception as e:
            logger.error("_load_rns_for_day sync failed: %s", e)
        return rows

    return await asyncio.to_thread(_sync_load)


async def stock_of_the_day_rns(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit_msg=None
) -> None:
    """Top 5 most significant RNS items from today's News Log."""
    user = update.effective_user
    msg = edit_msg or update.effective_message
    if not msg:
        return

    async def _set(text: str, markup=None):
        try:
            await msg.edit_text(
                text,
                parse_mode="Markdown",
                reply_markup=markup or hub_back_keyboard(),
                disable_web_page_preview=True,
            )
        except Exception:
            try:
                await msg.edit_text(
                    text.replace("*", "").replace("_", ""),
                    reply_markup=markup or hub_back_keyboard(),
                    disable_web_page_preview=True,
                )
            except Exception:
                pass

    day = datetime.now(timezone.utc).date().isoformat()
    await _set(f"📰 Loading RNS News Log for *{day}*…")

    try:
        rows = await _load_rns_for_day(day)
        if not rows:
            await _set(
                f"🏆 *Stock of the Day — RNS News*\n\n"
                f"No RNS rows found for *{day}* in Hive RNS News Log.\n"
                "Check the log has today’s date on **RNS Date**."
            )
            return

        # Rank by significance; light boost if same ticker appears often (cluster)
        ticker_counts: dict[str, int] = {}
        for r in rows:
            if r.get("ticker"):
                ticker_counts[r["ticker"]] = ticker_counts.get(r["ticker"], 0) + 1
        for r in rows:
            t = r.get("ticker") or ""
            if ticker_counts.get(t, 0) >= 2:
                r["score"] = min(100.0, r["score"] + 5)

        rows.sort(key=lambda r: r["score"], reverse=True)
        top = rows[:5]
        # Cache for Like refreshes (same process / day)
        _sotd_rns_top_cache[day] = top

        text, markup = _format_sotd_rns_board(
            day, top, total_count=len(rows), viewer_id=user.id if user else None
        )
        await _set(text, markup)
        chat_id = getattr(msg, "chat_id", None) or (
            update.effective_chat.id if update.effective_chat else None
        )
        if chat_id:
            await ensure_home_keyboard(context.bot, chat_id)
    except Exception as e:
        logger.error("stock_of_the_day_rns failed: %s", e)
        await _set(f"Stock of the Day (RNS) failed: {e}")


def _format_sotd_rns_board(
    day: str,
    top: list[dict],
    *,
    total_count: int | None = None,
    viewer_id: int | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Build RNS top-5 board text + buttons including 👍 Like per item
    and aggregate sentiment line.
    """
    lines = [
        "🏆 *Stock of the Day — RNS News*",
        f"_Most significant updates · {day}_",
    ]
    if total_count is not None:
        lines.append(f"_From {total_count} RNS item(s) in today’s News Log_")
    lines.append("")

    total_likes = 0
    for i, r in enumerate(top, 1):
        t = (r.get("ticker") or "—").upper()
        company = r.get("company") or ""
        likes = _sotd_like_count(day, t) if t != "—" else 0
        total_likes += likes
        head = f"#{t}" if t != "—" else "RNS"
        if company:
            head = f"{head} {company}"
        like_bit = f" · 👍 *{likes}*" if likes else ""
        lines.append(f"{i}. *{head}*{like_bit}")
        lines.append(f"   📰 {r.get('title') or '—'}")
        if r.get("summary"):
            summ = r["summary"]
            if len(summ) > 180:
                summ = summ[:177] + "…"
            lines.append(f"   _{summ}_")
        lines.append(
            f"   Significance *{r.get('score', 0):.0f}*/100"
            f" · {r.get('reason') or '—'}"
        )
        if r.get("link"):
            lines.append(f"   {r['link']}")
        lines.append("")

    # Sentiment aggregate
    ranked_likes = sorted(
        (
            ((r.get("ticker") or "").upper(), _sotd_like_count(day, (r.get("ticker") or "")))
            for r in top
            if (r.get("ticker") or "").strip()
        ),
        key=lambda x: x[1],
        reverse=True,
    )
    lines.append(f"📊 *Sentiment (likes today):* {total_likes}")
    if ranked_likes and ranked_likes[0][1] > 0:
        parts = [f"#{t} {n}" for t, n in ranked_likes if n > 0][:5]
        if parts:
            lines.append("   " + " · ".join(parts))
    lines.append("")
    lines.append(
        "_Tap 👍 to Like an RNS (1 like per member). "
        "Snapshot opens company detail._"
    )
    lines.append("_Not financial advice. DYOR._")

    kb_rows: list[list] = []
    for r in top:
        t = (r.get("ticker") or "")[:12].upper()
        if not t:
            continue
        likes = _sotd_like_count(day, t)
        liked = viewer_id and _sotd_user_liked(day, t, viewer_id)
        like_label = f"{'✅ ' if liked else ''}👍 {likes}" if likes else "👍 Like"
        # callback data must stay ≤ 64 bytes
        kb_rows.append(
            [
                InlineKeyboardButton(
                    f"#{t}",
                    callback_data=f"sotd:snap:{t}",
                ),
                InlineKeyboardButton(
                    like_label[:24],
                    callback_data=f"sotd:like:{t}",
                ),
            ]
        )
    kb_rows.append(
        [
            InlineKeyboardButton("« Boards", callback_data="sotd:menu"),
            InlineKeyboardButton("« Hub", callback_data="hub:home"),
        ]
    )
    return "\n".join(lines).strip(), InlineKeyboardMarkup(kb_rows)


async def whats_new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Back-compat alias → Stock of the Day chooser."""
    await stock_of_the_day_cmd(update, context)


async def menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = (query.data or "").replace("cmd:", "")

    # Build a minimal update proxy when only callback_query is present
    def _proxy_update():
        if update.message:
            return update
        chat = update.effective_chat
        if not chat or not query or not query.message:
            return update

        class _MsgProxy:
            def __init__(self, msg, bot):
                self._msg = msg
                self._bot = bot
                self.chat = msg.chat
                self.chat_id = msg.chat_id
                self.message_id = msg.message_id

            async def reply_text(self, *a, **k):
                return await self._bot.send_message(self.chat_id, *a, **k)

        class _Up:
            def __init__(self, orig, msg):
                self.effective_user = orig.effective_user
                self.effective_chat = orig.effective_chat
                self.message = msg
                self.callback_query = orig.callback_query

        return _Up(update, _MsgProxy(query.message, context.bot))

    if data == "start":
        await start(update, context)
    elif data == "faq":
        await faq(_proxy_update(), context)
    elif data == "status":
        await status_cmd(_proxy_update(), context)
    elif data == "request":
        await request_access(_proxy_update(), context)
    elif data == "menu":
        await menu_cmd(_proxy_update(), context)
    elif data == "snap":
        # Same entry as Home "Stock Snapshot" – prompt for ticker
        await stock_snapshot_prompt(_proxy_update(), context)
    elif data == "mystockpick":
        await mystockpick_cmd(_proxy_update(), context)
    elif data == "link":
        await link_cmd(_proxy_update(), context)
    elif data == "whatsnew":
        await stock_of_the_day_cmd(_proxy_update(), context)
    elif data == "sotd":
        await stock_of_the_day_cmd(_proxy_update(), context)

async def faq(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    await msg.reply_text(
        "📌 *FAQ*\n\n"
        "• Data is pulled live from the curated UK AIM Micro-Cap database.\n"
        "• This is *not* financial advice – always DYOR.\n"
        "• 📊 *Stock Brief* on the Home keyboard looks up any AIM ticker.\n"
        "• Use `#stockpick` in the group to log ideas.\n"
        "• Contact a human admin in The Hive group if something looks wrong.",
        parse_mode="Markdown",
        reply_markup=hub_back_keyboard(),
    )
    chat = update.effective_chat
    if chat:
        await ensure_home_keyboard(context.bot, chat.id)

async def stock_snapshot_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Home keyboard → Stock Brief: ask for a ticker."""
    user = update.effective_user
    msg = update.effective_message
    if not msg or not user:
        return
    if not await is_authorized(update, context):
        await msg.reply_text(
            "🔒 Only authorised members can request snapshots.\n"
            "Send /request to ask for access.",
            reply_markup=main_reply_keyboard(),
        )
        return
    _awaiting_snapshot[user.id] = True
    await cleanup_trigger_message(update, context)
    await clear_nav_panel(context.bot, user.id)
    sent = await msg.reply_text(
        "📊 *Stock Brief*\n\n"
        "Send a ticker (e.g. `ALRT` or `#KEFI`).",
        parse_mode="Markdown",
        reply_markup=hub_back_keyboard(),
    )
    await remember_nav_panel(user.id, sent)
    await ensure_home_keyboard(context.bot, msg.chat_id)


async def deliver_stock_snapshot(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    ticker: str,
) -> None:
    """Look up ticker and open Stock Brief on Stock Summary (multi-view UI)."""
    user = update.effective_user
    msg = update.effective_message
    if not msg:
        return
    ticker = (ticker or "").lstrip("#").upper().strip()
    # Wipe prompt + user's ticker message for a clean panel
    if user:
        await clear_nav_panel(context.bot, user.id)
    await cleanup_trigger_message(update, context)
    if not ticker:
        sent = await context.bot.send_message(
            msg.chat_id,
            "Please send a valid ticker (e.g. ALRT).",
            reply_markup=main_reply_keyboard(),
        )
        if user:
            await remember_nav_panel(user.id, sent)
        return
    if not await is_authorized(update, context):
        await context.bot.send_message(
            msg.chat_id,
            "🔒 Only authorised members can request snapshots.\n"
            "Send /request to ask for access.",
            reply_markup=main_reply_keyboard(),
        )
        return
    try:
        # Force-fetch latest RNS and best-effort sync into Micro-Cap
        latest_rns = None
        try:
            latest_rns = await sync_microcap_with_latest_rns(ticker)
        except Exception as se:
            logger.warning("sync_microcap_with_latest_rns: %s", se)
            try:
                latest_rns = await get_latest_rns_for_ticker(ticker, force=True)
            except Exception:
                latest_rns = None

        meta = await get_ticker_from_notion(ticker)
        if not meta:
            sent = await context.bot.send_message(
                msg.chat_id,
                f"No snapshot for #{ticker} in UK AIM Micro-Cap.",
                reply_markup=hub_back_keyboard(),
            )
            if user:
                await remember_nav_panel(user.id, sent)
            await ensure_home_keyboard(context.bot, msg.chat_id)
            return
        try:
            stockpickers = await get_stockpickers_for_ticker(ticker)
        except Exception:
            stockpickers = []
        body = format_stock_summary(
            ticker, meta, stockpickers, latest_rns=latest_rns
        )
        pct = meta.get("day_change_pct")
        if pct is None:
            pct = _fetch_pct_on_day_live(ticker)
        if pct is not None:
            sign = "+" if pct >= 0 else ""
            arrow = "🟢" if pct > 0 else ("🔴" if pct < 0 else "⚪")
            body = f"{arrow} Session: *{sign}{pct:.2f}%*\n\n" + body
        try:
            sent = await context.bot.send_message(
                msg.chat_id,
                body,
                parse_mode="Markdown",
                reply_markup=brief_action_keyboard(ticker),
                disable_web_page_preview=True,
            )
        except Exception:
            sent = await context.bot.send_message(
                msg.chat_id,
                body.replace("*", "").replace("_", ""),
                reply_markup=brief_action_keyboard(ticker),
                disable_web_page_preview=True,
            )
        if user:
            await remember_nav_panel(user.id, sent)
        try:
            await log_member_activity(
                user, REQUEST_TYPE_SNAPSHOT, notes=f"#{ticker} stock brief"
            )
        except Exception:
            pass
        await ensure_home_keyboard(context.bot, msg.chat_id)
    except Exception as e:
        logger.error("deliver_stock_snapshot failed for %s: %s", ticker, e)
        await context.bot.send_message(
            msg.chat_id,
            f"Lookup failed for #{ticker}. Please try again.\n{e}",
            reply_markup=main_reply_keyboard(),
        )


async def snap_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /snap              → prompt (same as Stock Snapshot button)
    /snap #80M         → snapshot for authorised users
    /snap 80M          → same
    """
    user = update.effective_user
    msg = update.effective_message
    if not msg:
        return

    raw = " ".join(context.args) if context.args else ""
    tickers = extract_hashtag_tickers(raw)

    if not tickers and context.args:
        for a in context.args:
            t = a.lstrip("#").upper().strip()
            if t and re.fullmatch(r"[A-Z0-9]{1,5}", t):
                tickers.append(t)

    if not tickers:
        # No args → same UX as Home Stock Snapshot button
        await stock_snapshot_prompt(update, context)
        return

    if not await is_authorized(update, context):
        await msg.reply_text(
            "🔒 Only authorised members can request snapshots.\n"
            "Send /request to ask for access.",
            reply_markup=main_reply_keyboard(),
        )
        return

    for t in tickers:
        await deliver_stock_snapshot(update, context, t)


async def _fetch_stockpicks_month(
    year: int, month: int, *, mine_user=None
) -> list[dict]:
    """
    Load Hive Stock Picks rows for a calendar month.
    If mine_user is set, only that user's rows; else all (for league).
    """
    db_id = os.getenv("NOTION_STOCKPICKS_DB_ID") or os.getenv("NOTION_DATABASE_ID")
    if not notion or not db_id:
        return []
    try:
        month_start = datetime(year, month, 1, tzinfo=timezone.utc).date().isoformat()
        if month == 12:
            month_end = datetime(year + 1, 1, 1, tzinfo=timezone.utc).date().isoformat()
        else:
            month_end = (
                datetime(year, month + 1, 1, tzinfo=timezone.utc).date().isoformat()
            )
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
        uid_marker = f"uid:{mine_user.id}" if mine_user else None
        user_name = (mine_user.full_name or "").strip().lower() if mine_user else ""
        username = (mine_user.username or "").strip().lower() if mine_user else ""
        rows = []
        for page in response.get("results", []):
            props = page.get("properties", {})
            if mine_user:
                notes = _get_plain_text(props.get("Notes")).lower()
                posted_by = _get_plain_text(props.get("Posted By")).strip().lower()
                is_mine = uid_marker and uid_marker in notes
                if not is_mine and user_name and posted_by == user_name:
                    is_mine = True
                if not is_mine and username and username in posted_by:
                    is_mine = True
                if not is_mine:
                    continue
            date_prop = (props.get("Telegram Date") or {}).get("date") or {}
            date_str = date_prop.get("start", "—")
            ticker = (
                _get_plain_text(props.get("Ticker")) or ""
            ).lstrip("#").upper() or "—"
            rows.append(
                {
                    "page_id": page["id"],
                    "date": date_str,
                    "ticker": ticker,
                    "summary": _get_plain_text(props.get("Summary")) or "—",
                    "catalyst": _get_plain_text(props.get("Next Catalyst")) or "—",
                    "target": _get_plain_text(props.get("Target Price")) or "—",
                    "posted_by": _get_plain_text(props.get("Posted By")) or "—",
                    "message": _get_plain_text(props.get("Message")) or "",
                }
            )
        return rows
    except Exception as e:
        logger.error("_fetch_stockpicks_month failed: %s", e)
        return []


def _month_label(year: int, month: int) -> str:
    names = [
        "",
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sept",
        "Oct",
        "Nov",
        "Dec",
    ]
    return f"{names[month]} {year}"


async def show_stockpick_hub(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    edit: bool = False,
) -> None:
    """
    My Stockpick home – two content parts + 3 expandable inline sections:
      1) League table (month nav)
      2) My pick this month (edit actions)
      3) History (month nav)
    """
    user = update.effective_user
    if update.callback_query:
        msg = update.callback_query.message
    else:
        msg = update.message
    if not msg or not user:
        return
    if not await is_authorized(update, context):
        text = "🔒 Not authorised. Send /request then /status."
        if edit:
            try:
                await msg.edit_text(text)
            except Exception:
                await msg.reply_text(text)
        else:
            await msg.reply_text(text)
        return

    st = _msp_state(user.id)
    now = datetime.now(timezone.utc)
    # Default cursors to current month
    ly, lm = st.get("league_y") or now.year, st.get("league_m") or now.month
    hy, hm = st.get("hist_y") or now.year, st.get("hist_m") or now.month

    # --- Part 2 data: user's current month pick ---
    my_rows = await _fetch_stockpicks_month(now.year, now.month, mine_user=user)
    my_pick = my_rows[0] if my_rows else None
    if my_pick:
        _last_stockpick_page[user.id] = my_pick["page_id"]

    # Who else picked the same ticker this month (followers / co-pickers)
    co_pickers: list[str] = []
    if my_pick and my_pick.get("ticker") and my_pick["ticker"] != "—":
        all_month = await _fetch_stockpicks_month(now.year, now.month, mine_user=None)
        for r in all_month:
            if r["ticker"] == my_pick["ticker"]:
                name = (r.get("posted_by") or "").strip()
                if name and name != "—" and name.lower() != (user.full_name or "").lower():
                    if name not in co_pickers:
                        co_pickers.append(name)

    lines = [
        "📌 *My Stockpick*",
        "",
        "• View the *Hive Stockpicker League* for the month",
        "• Enter or edit *your* stockpick this month",
        "• Open *history* by month",
        "",
    ]
    if my_pick:
        lines.append(
            f"*Your pick · {_month_label(now.year, now.month)}:* "
            f"*#{my_pick['ticker']}*"
        )
        lines.append(f"Summary: {my_pick['summary'][:120]}")
        lines.append(f"Catalyst: {my_pick['catalyst'][:80]}")
        lines.append(f"Target: {my_pick['target']}")
        if co_pickers:
            lines.append(
                "Also picked by: " + ", ".join(co_pickers[:8])
            )
    else:
        lines.append(
            f"_No stockpick yet for {_month_label(now.year, now.month)}. "
            "Post `#stockpick #TICKER` in the group._"
        )
    lines.append("")

    keyboard: list[list] = []

    # --- Section 1: League ---
    if st.get("league_open"):
        league_rows = await _fetch_stockpicks_month(ly, lm, mine_user=None)
        # Aggregate by ticker
        counts: dict[str, list[str]] = {}
        for r in league_rows:
            t = r["ticker"]
            if not t or t == "—":
                continue
            counts.setdefault(t, [])
            name = (r.get("posted_by") or "?").strip()
            if name and name not in counts[t]:
                counts[t].append(name)
        ranked = sorted(counts.items(), key=lambda x: len(x[1]), reverse=True)
        lines.append(f"🏆 *League · {_month_label(ly, lm)}*")
        if not ranked:
            lines.append("_No stockpicks logged this month._")
        else:
            for i, (t, names) in enumerate(ranked[:10], 1):
                lines.append(f"{i}. *#{t}* · {len(names)} pick(s)")
                lines.append(f"   {', '.join(names[:6])}")
        lines.append("")
        keyboard.append(
            [
                InlineKeyboardButton("‹", callback_data="msp:league_prev"),
                InlineKeyboardButton(
                    _month_label(ly, lm), callback_data="msp:league_noop"
                ),
                InlineKeyboardButton("›", callback_data="msp:league_next"),
            ]
        )
        keyboard.append(
            [InlineKeyboardButton("▴ Hide league", callback_data="msp:ui_league")]
        )
    else:
        keyboard.append(
            [
                InlineKeyboardButton(
                    "🏆 League table ▾", callback_data="msp:ui_league"
                )
            ]
        )

    # --- Section 2: My pick this month ---
    if st.get("mine_open"):
        keyboard.append(
            [
                InlineKeyboardButton("Summary", callback_data="sp:Summary"),
                InlineKeyboardButton("Catalyst", callback_data="sp:Next Catalyst"),
            ]
        )
        keyboard.append(
            [
                InlineKeyboardButton("Target", callback_data="sp:Target Price"),
                InlineKeyboardButton("Edit pick", callback_data="sp:Change"),
            ]
        )
        keyboard.append(
            [InlineKeyboardButton("▴ Hide my pick", callback_data="msp:ui_mine")]
        )
    else:
        keyboard.append(
            [
                InlineKeyboardButton(
                    "✏️ My pick this month ▾", callback_data="msp:ui_mine"
                )
            ]
        )

    # --- Section 3: History ---
    if st.get("hist_open"):
        hist_rows = await _fetch_stockpicks_month(hy, hm, mine_user=user)
        lines.append(f"📚 *History · {_month_label(hy, hm)}*")
        if not hist_rows:
            lines.append("_No picks in this month._")
        else:
            for r in hist_rows[:8]:
                lines.append(
                    f"• *#{r['ticker']}* · {r['date'][:10]}\n"
                    f"  {r['summary'][:80]}"
                )
        lines.append("")
        keyboard.append(
            [
                InlineKeyboardButton("‹", callback_data="msp:hist_prev"),
                InlineKeyboardButton(
                    _month_label(hy, hm), callback_data="msp:hist_noop"
                ),
                InlineKeyboardButton("›", callback_data="msp:hist_next"),
            ]
        )
        keyboard.append(
            [InlineKeyboardButton("▴ Hide history", callback_data="msp:ui_hist")]
        )
    else:
        keyboard.append(
            [
                InlineKeyboardButton(
                    "📚 History ▾", callback_data="msp:ui_hist"
                )
            ]
        )

    keyboard.append(
        [InlineKeyboardButton("« Hub", callback_data="hub:home")]
    )

    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3900] + "\n…"
    markup = InlineKeyboardMarkup(keyboard)

    if edit:
        try:
            await msg.edit_text(
                text, parse_mode="Markdown", reply_markup=markup
            )
            await remember_nav_panel(user.id, msg)
            return
        except Exception:
            pass
    sent = await msg.reply_text(
        text, parse_mode="Markdown", reply_markup=markup
    )
    await remember_nav_panel(user.id, sent)
    await ensure_home_keyboard(context.bot, msg.chat_id)


async def mystockpick_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    if not await require_authorized(update, context):
        return
    await cleanup_trigger_message(update, context)
    await clear_nav_panel(context.bot, user.id)
    await show_stockpick_hub(update, context, edit=False)

async def show_my_stockpicks(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool = False
) -> None:
    user = update.effective_user
    msg = update.callback_query.message if update.callback_query else update.message

    if not await is_authorized(update, context):
        text = (
            "🔒 You are not authorised to use this bot service yet.\n"
            "Send /request then /status."
        )
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
        username = (user.username or "").strip().lower()
        rows = []

        for page in response.get("results", []):
            props = page.get("properties", {})
            notes = _get_plain_text(props.get("Notes")).lower()
            posted_by = _get_plain_text(props.get("Posted By")).strip().lower()
            message = _get_plain_text(props.get("Message")).lower()

            is_mine = (
                uid_marker in notes
                or (user_name and posted_by == user_name)
                or (username and username in posted_by)
                or (username and f"@{username}" in posted_by)
                or (user_name and user_name in posted_by)
            )
            if not is_mine:
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
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    edit: bool = False,
    force_rns: bool = False,
) -> None:
    """
    Show My Watchlist and auto-sync latest RNS from Notion RNS News Log
    for each ticker on the active list.
    """
    user = update.effective_user
    if update.callback_query:
        msg = update.callback_query.message
    else:
        msg = update.message
    if not msg or not user:
        return

    if not await is_authorized(update, context):
        text = (
            "🔒 You are not authorised to use this bot service yet.\n"
            "Send /request then /status."
        )
        if edit:
            await msg.edit_text(text)
        else:
            await msg.reply_text(text)
        return

    db_id = NOTION_WATCHLIST_DB_ID or (os.getenv("NOTION_WATCHLIST_DB_ID") or "").strip()
    if not (notion or NOTION_TOKEN) or not db_id:
        await msg.reply_text(
            "Watchlist is not configured.\n"
            "Admin: set NOTION_WATCHLIST_DB_ID in Railway."
        )
        return

    try:
        pages = await _fetch_user_watchlist_pages(user.id)
        list_names = _list_names_from_pages(pages)

        # Active tab
        active = _active_watchlist_name.get(user.id)
        if not active or active not in list_names:
            active = list_names[0] if list_names else "Default"
            _active_watchlist_name[user.id] = active

        # Rows for active list only (with priority)
        rows = []
        tickers_for_rns: list[str] = []
        for page in pages:
            props = page.get("properties", {})
            ln = _get_plain_text(props.get("List Name")).strip() or "Default"
            if ln != active:
                continue
            ticker = (_get_plain_text(props.get("Ticker")) or "-").lstrip("#").upper()
            name = _get_plain_text(props.get("Name")) or "-"
            gl = props.get("Group Link") or {}
            url = ""
            if isinstance(gl, dict):
                url = (gl.get("url") or "").strip()
            pri = None
            pr = props.get("Priority") or {}
            if isinstance(pr, dict) and pr.get("number") is not None:
                try:
                    pri = int(pr["number"])
                except (TypeError, ValueError):
                    pri = None
            page_id = page.get("id")
            rows.append(
                {
                    "ticker": ticker,
                    "name": name,
                    "url": url or "-",
                    "priority": pri,
                    "page_id": page_id,
                }
            )
            if ticker and ticker != "-":
                tickers_for_rns.append(ticker)

        # Auto-sync latest RNS for watchlist tickers
        rns_map = await get_latest_rns_for_tickers(
            tickers_for_rns, force=force_rns
        )
        rns_hits = len(rns_map)

        # Day % change – always load for display (and for sort-by-pct)
        # Priority: live Yahoo (session %) → Micro-Cap → Watchlist cache
        # Live is preferred: Micro-Cap day-% is often empty/stale.
        pct_map: dict[str, float | None] = {}
        sort_mode = _watchlist_sort.get(user.id, "rns")
        for t in tickers_for_rns:
            pct_map[t] = None
            live = _fetch_pct_on_day_live(t)
            if live is not None:
                pct_map[t] = live
            else:
                try:
                    data = await get_ticker_from_notion(t)
                    if data and data.get("day_change_pct") is not None:
                        pct_map[t] = data.get("day_change_pct")
                except Exception:
                    pass
            await asyncio.sleep(0.12)
        # User's this-month stockpick tickers (for 📌 badge)
        try:
            my_pick_tickers = await _user_stockpick_tickers_this_month(user)
        except Exception:
            my_pick_tickers = set()
        picks_label = _month_picks_label()
        # 3) Watchlist row cache if still missing
        for page in pages:
            props = page.get("properties", {})
            ln = _get_plain_text(props.get("List Name")).strip() or "Default"
            if ln != active:
                continue
            t = (_get_plain_text(props.get("Ticker")) or "").lstrip("#").upper()
            if not t or pct_map.get(t) is not None:
                continue
            pct_prop = props.get("% On Day") or {}
            if isinstance(pct_prop, dict) and pct_prop.get("number") is not None:
                try:
                    pct_map[t] = float(pct_prop["number"])
                except (TypeError, ValueError):
                    pass
        # On Refresh: persist latest % into Hive Bot Watchlist "% On Day"
        if force_rns and (notion or NOTION_TOKEN):
            for page in pages:
                props = page.get("properties", {})
                ln = _get_plain_text(props.get("List Name")).strip() or "Default"
                if ln != active:
                    continue
                t = (_get_plain_text(props.get("Ticker")) or "").lstrip("#").upper()
                pct = pct_map.get(t)
                if pct is None or not page.get("id"):
                    continue
                try:
                    if notion:
                        notion.pages.update(
                            page_id=page["id"],
                            properties={"% On Day": {"number": float(pct)}},
                        )
                    else:
                        _notion_http(
                            "PATCH",
                            f"pages/{page['id']}",
                            {"properties": {"% On Day": {"number": float(pct)}}},
                        )
                    logger.info("Wrote %% On Day=%.2f for %s", pct, t)
                except Exception as we:
                    logger.warning(
                        "Watchlist %% On Day write failed for %s "
                        "(add Number column '%% On Day' on Hive Bot Watchlist): %s",
                        t,
                        we,
                    )

        # Enrich + sort
        for r in rows:
            t = r["ticker"]
            rns = rns_map.get(t) or {}
            r["rns"] = rns
            r["rns_date"] = rns.get("date") or ""
            r["pct"] = pct_map.get(t)

        def _sort_key(r):
            if sort_mode == "priority":
                # Higher priority first; unset last
                p = r.get("priority")
                return (0 if p is not None else 1, -(p or 0), r["ticker"])
            if sort_mode == "pct":
                v = r.get("pct")
                return (0 if v is not None else 1, -(v or 0), r["ticker"])
            if sort_mode == "name":
                return ((r.get("name") or r["ticker"]).lower(),)
            # default: latest RNS date desc
            d = r.get("rns_date") or ""
            return (0 if d else 1, d, r["ticker"])

        if sort_mode == "rns":
            rows.sort(key=_sort_key, reverse=True)
        else:
            rows.sort(key=_sort_key)

        # Pagination — 3 tickers per page
        total = len(rows)
        page_size = WATCHLIST_PAGE_SIZE
        total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
        page_idx = _watchlist_page.get(user.id, 0)
        if page_idx >= total_pages:
            page_idx = max(0, total_pages - 1)
        if page_idx < 0:
            page_idx = 0
        _watchlist_page[user.id] = page_idx
        start_i = page_idx * page_size
        page_rows = rows[start_i : start_i + page_size]

        sort_labels = {
            "rns": "Latest RNS",
            "pct": "% day change",
            "priority": "Priority 1–10",
            "name": "Name",
        }
        sort_label = sort_labels.get(sort_mode, sort_mode)

        lines = [
            f"👀 *My Watchlist*  ({len(list_names)}/{MAX_WATCHLISTS})",
            f"Active: *{active}* · Sort: *{sort_label}*",
            f"Page *{page_idx + 1}/{total_pages}* · {total} ticker(s)"
            + (f" · RNS {rns_hits}/{len(tickers_for_rns)}" if tickers_for_rns else ""),
            "",
        ]
        if not rows:
            lines.append("Empty — use Edit list to add tickers.")
        else:
            lines.append("Tap a #ticker button to open the company snapshot.")
            lines.append("")
            for r in page_rows:
                ticker, name, url = r["ticker"], r["name"], r["url"]
                # Prefer company name from UK AIM Micro-Cap when list Name is thin
                company = name
                try:
                    meta = await get_ticker_from_notion(ticker)
                    if meta and meta.get("company"):
                        company = meta["company"]
                except Exception:
                    pass
                r["display_name"] = company
                pri = r.get("priority")
                pri_bit = f" · ⭐{pri}" if pri is not None else ""
                pct = r.get("pct")
                if pct is not None:
                    sign = "+" if pct >= 0 else ""
                    arrow = "🟢" if pct > 0 else ("🔴" if pct < 0 else "⚪")
                    pct_bit = f" · {arrow} {sign}{pct:.2f}%"
                else:
                    pct_bit = " · ⚪ —%"
                # Badge if this ticker is the user's stockpick this month
                pick_bit = ""
                if ticker in my_pick_tickers:
                    pick_bit = f" 📌 {picks_label}"
                # e.g. #EPP Energy Pathways · 🟢 +13.70% 📌 Sept Picks
                lines.append(
                    f"*#{ticker}* {company}{pri_bit}{pct_bit}{pick_bit}"
                )
                if url and url != "-":
                    lines.append(f"  🔗 {url}")
                rns = r.get("rns") or {}
                if rns:
                    date_bit = f" · {rns['date']}" if rns.get("date") else ""
                    lines.append(f"  📰 *Latest RNS*{date_bit}")
                    lines.append(f"  {rns.get('title') or '—'}")
                    if rns.get("summary"):
                        lines.append(f"  {rns['summary']}")
                    if rns.get("link"):
                        lines.append(f"  {rns['link']}")
                else:
                    lines.append("  📰 No RNS in News Log yet")
                lines.append("")

        # ----- Inline keyboard in 3 sections (expand / contract) -----
        # 1) Company snapshot list
        # 2) Navigation (page / sort / refresh) — sort expands
        # 3) Manage menu — collapsed by default
        ui = _wl_ui(user.id)
        sort_open = bool(ui.get("sort_open"))
        manage_open = bool(ui.get("manage_open"))
        lists_open = bool(ui.get("lists_open"))

        keyboard = []

        # --- Section 1: company snapshot list ---
        for r in page_rows:
            t = r["ticker"]
            n = (r.get("display_name") or r.get("name") or t)[:28]
            if t and t != "-":
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            f"#{t}  {n}",
                            callback_data=f"wl:snap:{t[:20]}",
                        )
                    ]
                )

        # --- Section 2: navigation ---
        # List switcher (collapsed to one button when >1 lists)
        if len(list_names) > 1:
            if lists_open:
                keyboard.extend(_tab_keyboard(list_names, active))
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            "▴ Hide lists", callback_data="wl:ui_lists"
                        )
                    ]
                )
            else:
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            f"📂 {active} ▾",
                            callback_data="wl:ui_lists",
                        )
                    ]
                )
        elif list_names:
            # Single list – compact label only (no extra chrome)
            pass

        # Page + Refresh on one row
        nav = []
        if page_idx > 0:
            nav.append(InlineKeyboardButton("‹", callback_data="wl:page_prev"))
        nav.append(
            InlineKeyboardButton(
                f"{page_idx + 1}/{total_pages}", callback_data="wl:page_noop"
            )
        )
        if page_idx < total_pages - 1:
            nav.append(InlineKeyboardButton("›", callback_data="wl:page_next"))
        nav.append(InlineKeyboardButton("🔄", callback_data="wl:refresh_rns"))
        keyboard.append(nav)

        # Sort – collapsed dropdown style
        sort_labels = {
            "rns": "📰 RNS",
            "pct": "% Day",
            "priority": "⭐ Pri",
            "name": "Name",
        }
        cur_sort = sort_labels.get(sort_mode, "Sort")
        if sort_open:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        "📰 RNS" + (" ✓" if sort_mode == "rns" else ""),
                        callback_data="wl:sort_rns",
                    ),
                    InlineKeyboardButton(
                        "% Day" + (" ✓" if sort_mode == "pct" else ""),
                        callback_data="wl:sort_pct",
                    ),
                    InlineKeyboardButton(
                        "⭐ Pri" + (" ✓" if sort_mode == "priority" else ""),
                        callback_data="wl:sort_priority",
                    ),
                ]
            )
            keyboard.append(
                [
                    InlineKeyboardButton(
                        "▴ Hide sort", callback_data="wl:ui_sort"
                    )
                ]
            )
        else:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        f"Sort: {cur_sort} ▾",
                        callback_data="wl:ui_sort",
                    )
                ]
            )

        # --- Section 3: manage menu (collapsed by default) ---
        if manage_open:
            keyboard.append(
                [
                    InlineKeyboardButton("➕ Add", callback_data="wl:add"),
                    InlineKeyboardButton(
                        "✏️ Edit", callback_data="wl:edit_menu"
                    ),
                ]
            )
            keyboard.append(
                [
                    InlineKeyboardButton(
                        "⭐ Priority", callback_data="wl:set_priority"
                    ),
                    InlineKeyboardButton(
                        "🆕 New list", callback_data="wl:create"
                    ),
                ]
            )
            keyboard.append(
                [
                    InlineKeyboardButton("Rename", callback_data="wl:rename"),
                    InlineKeyboardButton(
                        "🗑 Delete", callback_data="wl:delete_list"
                    ),
                ]
            )
            keyboard.append(
                [
                    InlineKeyboardButton(
                        "▴ Hide menu", callback_data="wl:ui_manage"
                    )
                ]
            )
        else:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        "☰ Manage ▾", callback_data="wl:ui_manage"
                    ),
                    InlineKeyboardButton("« Hub", callback_data="hub:home"),
                ]
            )

        text = "\n".join(lines).strip()
        # Telegram message limit safety
        if len(text) > 3900:
            text = text[:3900] + "\n\n…truncated"
        markup = InlineKeyboardMarkup(keyboard)
        sent = None
        if edit:
            try:
                sent = await msg.edit_text(
                    text,
                    reply_markup=markup,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            except Exception:
                try:
                    sent = await msg.edit_text(
                        text.replace("*", "").replace("_", ""),
                        reply_markup=markup,
                        disable_web_page_preview=True,
                    )
                except Exception:
                    sent = await context.bot.send_message(
                        msg.chat_id,
                        text.replace("*", "").replace("_", ""),
                        reply_markup=markup,
                        disable_web_page_preview=True,
                    )
        else:
            try:
                sent = await msg.reply_text(
                    text,
                    reply_markup=markup,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            except Exception:
                sent = await msg.reply_text(
                    text.replace("*", "").replace("_", ""),
                    reply_markup=markup,
                    disable_web_page_preview=True,
                )
        # Track panel for in-place navigation + keep bottom keyboard
        if sent:
            await _remember_watchlist_panel(user.id, sent)
        elif edit and msg:
            await _remember_watchlist_panel(user.id, msg)
        try:
            pulse = await context.bot.send_message(
                msg.chat_id, "⋯", reply_markup=main_reply_keyboard()
            )
            await safe_delete_message(context.bot, pulse.chat_id, pulse.message_id)
        except Exception:
            pass

    except Exception as e:
        logger.error("show_watchlist failed: %s", e)
        await msg.reply_text(f"Could not load watchlist.\nError: {str(e)[:300]}")
        
async def msp_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """My Stockpick hub expand/collapse + month navigation."""
    query = update.callback_query
    user = query.from_user if query else None
    if not query or not user:
        return
    await query.answer()
    action = (query.data or "").replace("msp:", "", 1)
    st = _msp_state(user.id)
    now = datetime.now(timezone.utc)

    def _shift(y: int, m: int, delta: int) -> tuple[int, int]:
        m = m + delta
        while m < 1:
            m += 12
            y -= 1
        while m > 12:
            m -= 12
            y += 1
        # Don't go past current month
        if y > now.year or (y == now.year and m > now.month):
            return now.year, now.month
        return y, m

    if action == "ui_league":
        st["league_open"] = not st.get("league_open")
        if st["league_open"]:
            st["mine_open"] = False
            st["hist_open"] = False
    elif action == "ui_mine":
        st["mine_open"] = not st.get("mine_open")
        if st["mine_open"]:
            st["league_open"] = False
            st["hist_open"] = False
    elif action == "ui_hist":
        st["hist_open"] = not st.get("hist_open")
        if st["hist_open"]:
            st["league_open"] = False
            st["mine_open"] = False
    elif action == "league_prev":
        st["league_y"], st["league_m"] = _shift(
            st.get("league_y") or now.year,
            st.get("league_m") or now.month,
            -1,
        )
        st["league_open"] = True
    elif action == "league_next":
        st["league_y"], st["league_m"] = _shift(
            st.get("league_y") or now.year,
            st.get("league_m") or now.month,
            1,
        )
        st["league_open"] = True
    elif action == "hist_prev":
        st["hist_y"], st["hist_m"] = _shift(
            st.get("hist_y") or now.year,
            st.get("hist_m") or now.month,
            -1,
        )
        st["hist_open"] = True
    elif action == "hist_next":
        st["hist_y"], st["hist_m"] = _shift(
            st.get("hist_y") or now.year,
            st.get("hist_m") or now.month,
            1,
        )
        st["hist_open"] = True
    elif action in ("league_noop", "hist_noop"):
        return

    await show_stockpick_hub(update, context, edit=True)


async def sotd_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stock of the Day boards + snapshot."""
    query = update.callback_query
    user = query.from_user if query else None
    if not query or not user:
        return
    data = query.data or ""

    # Chooser again
    if data == "sotd:menu":
        await query.answer()
        body = (
            "🏆 *Stock of the Day*\n\n"
            "Choose a board:\n\n"
            "• *% Today* — top session movers across UK AIM Micro-Cap\n"
            "• *RNS News* — most significant RNS from *today’s* News Log"
        )
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📈 % Today", callback_data="sotd:mode:pct"
                    ),
                    InlineKeyboardButton(
                        "📰 RNS News", callback_data="sotd:mode:rns"
                    ),
                ],
                [InlineKeyboardButton("« Hub", callback_data="hub:home")],
            ]
        )
        try:
            await query.edit_message_text(
                body, parse_mode="Markdown", reply_markup=kb
            )
        except Exception:
            await query.edit_message_text(
                body.replace("*", ""), reply_markup=kb
            )
        await remember_nav_panel(user.id, query.message)
        return

    if data == "sotd:mode:pct":
        await query.answer("Loading % Today…")
        await stock_of_the_day_pct(
            update, context, edit_msg=query.message
        )
        await remember_nav_panel(user.id, query.message)
        return

    if data == "sotd:mode:rns":
        await query.answer("Loading RNS News…")
        await stock_of_the_day_rns(
            update, context, edit_msg=query.message
        )
        await remember_nav_panel(user.id, query.message)
        return

    # 👍 Like on RNS top-5 board
    if data.startswith("sotd:like:"):
        if not await is_authorized(update, context):
            await query.answer("Authorised members only.", show_alert=True)
            return
        ticker = data.replace("sotd:like:", "", 1).strip().upper()
        if not ticker:
            await query.answer("Missing ticker.", show_alert=True)
            return
        day = datetime.now(timezone.utc).date().isoformat()
        added, count = _sotd_add_like(day, ticker, user.id)
        if added:
            await query.answer(f"Liked #{ticker} · {count} like(s)")
        else:
            await query.answer(f"Already liked #{ticker} · {count} like(s)")

        top = _sotd_rns_top_cache.get(day) or []
        if not top:
            # Rebuild board if cache empty
            await stock_of_the_day_rns(
                update, context, edit_msg=query.message
            )
            await remember_nav_panel(user.id, query.message)
            return
        text, markup = _format_sotd_rns_board(
            day, top, viewer_id=user.id
        )
        try:
            await query.edit_message_text(
                text,
                parse_mode="Markdown",
                reply_markup=markup,
                disable_web_page_preview=True,
            )
        except Exception:
            try:
                await query.edit_message_text(
                    text.replace("*", "").replace("_", ""),
                    reply_markup=markup,
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.warning("sotd like refresh failed: %s", e)
        await remember_nav_panel(user.id, query.message)
        try:
            await log_member_activity(
                user,
                "Stock of the Day",
                notes=f"Liked RNS #{ticker} ({count})",
            )
        except Exception:
            pass
        return

    await query.answer()
    if not data.startswith("sotd:snap:"):
        return
    ticker = data.replace("sotd:snap:", "", 1).strip().upper()
    if not ticker:
        return
    # Reuse snapshot delivery into this chat; keep Home keyboard
    try:
        meta = await get_ticker_from_notion(ticker)
        if not meta:
            await query.edit_message_text(
                f"No snapshot for #{ticker} in UK AIM Micro-Cap.",
                reply_markup=hub_back_keyboard(),
            )
            await remember_nav_panel(user.id, query.message)
            return
        try:
            stockpickers = await get_stockpickers_for_ticker(ticker)
        except Exception:
            stockpickers = []
        body = format_reply(ticker, meta, stockpickers)
        pct = meta.get("day_change_pct")
        if pct is None:
            pct = _fetch_pct_on_day_live(ticker)
        if pct is not None:
            sign = "+" if pct >= 0 else ""
            body = f"% on day: *{sign}{pct:.2f}%*\n\n" + body
        try:
            await query.edit_message_text(
                body,
                parse_mode="Markdown",
                reply_markup=snapshot_action_keyboard(ticker),
                disable_web_page_preview=True,
            )
        except Exception:
            await query.edit_message_text(
                body.replace("*", "").replace("_", ""),
                reply_markup=snapshot_action_keyboard(ticker),
                disable_web_page_preview=True,
            )
        await remember_nav_panel(user.id, query.message)
        await ensure_home_keyboard(
            context.bot, query.message.chat_id if query.message else None
        )
    except Exception as e:
        logger.error("sotd:snap failed for %s: %s", ticker, e)
        try:
            await query.edit_message_text(
                f"Snapshot failed: {e}", reply_markup=hub_back_keyboard()
            )
        except Exception:
            pass



async def sbrief_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stock Brief view switcher: Summary / Snap Shot / RNS News (paged)."""
    query = update.callback_query
    user = query.from_user if query else None
    if not query or not user:
        return
    data = query.data or ""
    if not data.startswith("sbrief:"):
        return
    if data == "sbrief:noop":
        await query.answer()
        return
    parts = data.split(":")
    # sbrief:sum:TICKER | sbrief:cat:TICKER | sbrief:rns:TICKER:page
    if len(parts) < 3:
        await query.answer()
        return
    view = parts[1]
    ticker = (parts[2] or "").upper().strip()
    page = 0
    if view == "rns" and len(parts) >= 4:
        try:
            page = max(0, int(parts[3]))
        except ValueError:
            page = 0
    if not ticker:
        await query.answer("Missing ticker.", show_alert=True)
        return
    if not await is_authorized(update, context):
        await query.answer("Authorised members only.", show_alert=True)
        return
    await query.answer()
    try:
        meta = await get_ticker_from_notion(ticker)
        if not meta:
            try:
                await query.edit_message_text(
                    f"No data for #{ticker} in UK AIM Micro-Cap.",
                    reply_markup=hub_back_keyboard(),
                )
            except Exception:
                pass
            return
        latest_rns = None
        try:
            latest_rns = await get_latest_rns_for_ticker(ticker, force=False)
        except Exception:
            latest_rns = None
        try:
            stockpickers = await get_stockpickers_for_ticker(ticker)
        except Exception:
            stockpickers = []

        if view == "sum":
            body = format_stock_summary(
                ticker, meta, stockpickers, latest_rns=latest_rns
            )
            markup = brief_action_keyboard(ticker)
        elif view == "cat":
            body = format_catalyst_snapshot(ticker, meta)
            markup = brief_action_keyboard(ticker)
        elif view == "rns":
            # Last 12 RNS from Hive RNS News Log · 4 per page (‹ ›)
            rows = await _fetch_rns_history_for_ticker(ticker, limit=12)
            page_size = 4
            max_page = max(0, (len(rows) - 1) // page_size) if rows else 0
            if page > max_page:
                page = max_page
            start = page * page_size
            chunk = rows[start : start + page_size]
            company = meta.get("company") or "N/A"
            end_n = start + len(chunk)
            lines = [
                f"📰 *RNS News* · *#{ticker}* — {company}",
                f"_Last {len(rows)} from Hive RNS News Log · "
                f"showing {start + 1}–{end_n} · page {page + 1}/{max_page + 1}_",
                "",
            ]
            if not chunk:
                lines.append("_No RNS rows in Hive RNS News Log for this ticker._")
            else:
                for i, r in enumerate(chunk, start=start + 1):
                    date_bit = r.get("date") or "—"
                    lines.append(f"*{i}. {date_bit}* · {r.get('title') or 'RNS'}")
                    if r.get("summary"):
                        lines.append(r["summary"])
                    if r.get("link"):
                        lines.append(r["link"])
                    lines.append("")
            body = "\n".join(lines).strip()
            # Pagination row for RNS view
            nav = []
            if page > 0:
                nav.append(
                    InlineKeyboardButton(
                        "‹ Prev",
                        callback_data=f"sbrief:rns:{ticker}:{page - 1}",
                    )
                )
            nav.append(
                InlineKeyboardButton(
                    f"{page + 1}/{max_page + 1}",
                    callback_data="sbrief:noop",
                )
            )
            if page < max_page:
                nav.append(
                    InlineKeyboardButton(
                        "Next ›",
                        callback_data=f"sbrief:rns:{ticker}:{page + 1}",
                    )
                )
            kb_rows = [
                [
                    InlineKeyboardButton(
                        "📋 Stock Summary",
                        callback_data=f"sbrief:sum:{ticker}",
                    ),
                    InlineKeyboardButton(
                        "⚡ Snap Shot",
                        callback_data=f"sbrief:cat:{ticker}",
                    ),
                    InlineKeyboardButton(
                        "📰 RNS News",
                        callback_data=f"sbrief:rns:{ticker}:{page}",
                    ),
                ],
            ]
            if nav:
                kb_rows.append(nav)
            kb_rows.append(
                [
                    InlineKeyboardButton(
                        "➕ Save to My Watchlist",
                        callback_data=f"snap:save:{ticker}",
                    )
                ]
            )
            kb_rows.append(
                [InlineKeyboardButton("« Hub", callback_data="hub:home")]
            )
            markup = InlineKeyboardMarkup(kb_rows)
        else:
            await query.answer()
            return

        try:
            await query.edit_message_text(
                body,
                parse_mode="Markdown",
                reply_markup=markup,
                disable_web_page_preview=True,
            )
        except Exception:
            try:
                await query.edit_message_text(
                    body.replace("*", "").replace("_", ""),
                    reply_markup=markup,
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.warning("sbrief edit failed: %s", e)
        await remember_nav_panel(user.id, query.message)
    except Exception as e:
        logger.error("sbrief_button failed %s: %s", data, e)
        try:
            await query.answer("Failed to load view.", show_alert=True)
        except Exception:
            pass


async def snapshot_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle snap:save:TICKER from Stock Snapshot inline actions."""
    query = update.callback_query
    user = query.from_user if query else None
    if not query or not user:
        return
    await query.answer()
    data = query.data or ""
    if not data.startswith("snap:save:"):
        return
    ticker = data.replace("snap:save:", "", 1).strip().upper()
    if not ticker:
        await query.message.reply_text("Missing ticker.")
        return
    if not await is_authorized(update, context):
        await query.message.reply_text(
            "🔒 Only authorised members can use My Watchlist.\n"
            "Send /request to ask for access.",
            reply_markup=main_reply_keyboard(),
        )
        return
    list_name = _active_watchlist_name.get(user.id, "Default")
    company = ""
    try:
        meta = await get_ticker_from_notion(ticker)
        if meta:
            company = meta.get("company") or ""
    except Exception:
        pass
    try:
        status, info = await _watchlist_upsert_ticker(
            user,
            ticker=ticker,
            name=company,
            link="",
            list_name=list_name,
        )
        if status in ("added", "updated"):
            verb = "Added to" if status == "added" else "Updated on"
            msg = (
                f"✅ *#{ticker}* {verb} My Watchlist "
                f"(*{list_name}*)."
            )
        else:
            msg = f"⚠️ Could not save *#{ticker}*: {info}"
        await query.message.reply_text(
            msg,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "👀 Open My Watchlist",
                            callback_data="hub:watchlist",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "« Hub", callback_data="hub:home"
                        )
                    ],
                ]
            ),
        )
        await ensure_home_keyboard(
            context.bot,
            query.message.chat_id if query.message else None,
        )
    except Exception as e:
        logger.error("snap:save failed for %s: %s", ticker, e)
        await query.message.reply_text(
            f"Could not save #{ticker} to watchlist.\n`{e}`",
            parse_mode="Markdown",
            reply_markup=main_reply_keyboard(),
        )


async def hub_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    user = update.effective_user

    if data == "hub:watchlist":
        # Cancel any pending watchlist input and return to clean panel
        if user:
            _awaiting_watchlist.pop(user.id, None)
            _awaiting_snapshot.pop(user.id, None)
        await show_watchlist(update, context, edit=True, force_rns=False)
    elif data == "hub:mypicks":
        await show_stockpick_hub(update, context, edit=True)
    elif data == "hub:home":
        # Wipe previous panel, then land on Home with keyboard that STAYS
        if user:
            _awaiting_snapshot.pop(user.id, None)
            _awaiting_watchlist.pop(user.id, None)
            _awaiting_link.pop(user.id, None)
            _awaiting_field.pop(user.id, None)
            await clear_nav_panel(context.bot, user.id)
        chat_id = query.message.chat_id if query.message else None
        # Remove the inline panel (snapshot / sotd / stockpick / etc.)
        try:
            await query.message.delete()
        except Exception:
            try:
                await query.edit_message_text("…")
            except Exception:
                pass
        # Must send a non-deleted message with ReplyKeyboardMarkup
        # or Telegram clients drop the Home menu entirely
        await send_home_menu(context.bot, chat_id)
        return


async def watchlist_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    if not user:
        return

    data = query.data or ""
    action = data.replace("wl:", "")

    # Force re-query RNS News Log + refresh % On Day into Notion
    if action == "refresh_rns":
        await query.answer("Syncing RNS + % on day…")
        # Clear ticker cache so day_change_pct is re-read from Notion
        try:
            _ticker_cache.clear()
        except Exception:
            pass
        await show_watchlist(update, context, edit=True, force_rns=True)
        return

    # Expand / contract keyboard sections
    if action == "ui_sort":
        st = _wl_ui(user.id)
        st["sort_open"] = not st.get("sort_open")
        # keep manage closed when opening sort (less clutter)
        if st["sort_open"]:
            st["manage_open"] = False
        await query.answer()
        await show_watchlist(update, context, edit=True, force_rns=False)
        return
    if action == "ui_manage":
        st = _wl_ui(user.id)
        st["manage_open"] = not st.get("manage_open")
        if st["manage_open"]:
            st["sort_open"] = False
            st["lists_open"] = False
        await query.answer()
        await show_watchlist(update, context, edit=True, force_rns=False)
        return
    if action == "ui_lists":
        st = _wl_ui(user.id)
        st["lists_open"] = not st.get("lists_open")
        if st["lists_open"]:
            st["manage_open"] = False
        await query.answer()
        await show_watchlist(update, context, edit=True, force_rns=False)
        return

    # « Back to Watchlist from snapshot – restore same panel in place
    if action == "back":
        await query.answer()
        await show_watchlist(update, context, edit=True, force_rns=False)
        return

    # Ticker "hyperlink" → company snapshot in the SAME message (edit in place)
    if action.startswith("snap:"):
        await query.answer()
        ticker = action[5:].strip().upper()
        back_kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "« Back to Watchlist", callback_data="wl:back"
                    )
                ]
            ]
        )
        if not ticker:
            try:
                await query.edit_message_text(
                    "Missing ticker.", reply_markup=back_kb
                )
            except Exception:
                await query.message.reply_text(
                    "Missing ticker.", reply_markup=back_kb
                )
            return
        try:
            meta = await get_ticker_from_notion(ticker)
            if not meta:
                body = f"No snapshot for #{ticker} in UK AIM Micro-Cap."
                try:
                    await query.edit_message_text(body, reply_markup=back_kb)
                except Exception:
                    await query.message.reply_text(body, reply_markup=back_kb)
                return
            try:
                stockpickers = await get_stockpickers_for_ticker(ticker)
            except Exception:
                stockpickers = []
            body = format_reply(ticker, meta, stockpickers)
            pct = meta.get("day_change_pct")
            if pct is None:
                pct = _fetch_pct_on_day_live(ticker)
            if pct is not None:
                sign = "+" if pct >= 0 else ""
                body = f"% on day: *{sign}{pct:.2f}%*\n\n" + body
            # Stay in the same view: replace watchlist message with snapshot
            try:
                await query.edit_message_text(
                    body,
                    parse_mode="Markdown",
                    reply_markup=back_kb,
                    disable_web_page_preview=True,
                )
            except Exception:
                try:
                    await query.edit_message_text(
                        body.replace("*", "").replace("_", ""),
                        reply_markup=back_kb,
                        disable_web_page_preview=True,
                    )
                except Exception as e2:
                    logger.warning("wl:snap edit failed, fallback reply: %s", e2)
                    await query.message.reply_text(
                        body.replace("*", "").replace("_", ""),
                        reply_markup=back_kb,
                        disable_web_page_preview=True,
                    )
        except Exception as e:
            logger.error("wl:snap failed for %s: %s", ticker, e)
            try:
                await query.edit_message_text(
                    f"Snapshot failed: {e}", reply_markup=back_kb
                )
            except Exception:
                await query.message.reply_text(
                    f"Snapshot failed: {e}", reply_markup=back_kb
                )
        return

    # Pagination
    if action == "page_prev":
        await query.answer()
        _watchlist_page[user.id] = max(0, _watchlist_page.get(user.id, 0) - 1)
        await show_watchlist(update, context, edit=True, force_rns=False)
        return
    if action == "page_next":
        await query.answer()
        _watchlist_page[user.id] = _watchlist_page.get(user.id, 0) + 1
        await show_watchlist(update, context, edit=True, force_rns=False)
        return
    if action == "page_noop":
        await query.answer("Use ◀️ / ▶️ to change page")
        return

    # Sort modes
    if action.startswith("sort_"):
        mode = action.replace("sort_", "") or "rns"
        if mode not in ("rns", "pct", "priority", "name"):
            mode = "rns"
        _watchlist_sort[user.id] = mode
        _watchlist_page[user.id] = 0  # reset to first page
        # Collapse sort dropdown after choice
        try:
            _wl_ui(user.id)["sort_open"] = False
        except Exception:
            pass
        labels = {
            "rns": "Latest RNS",
            "pct": "% day change",
            "priority": "Priority",
            "name": "Name",
        }
        await query.answer(f"Sorted by {labels.get(mode, mode)}")
        await show_watchlist(update, context, edit=True, force_rns=False)
        return

    if action == "set_priority":
        await query.answer()
        await _watchlist_show_prompt(
            update,
            context,
            "⭐ *Set priority (1–10)*\n\n"
            "Send: `#TICKER 7`\n"
            "Example: `#ALRT 9`\n\n"
            "10 = highest priority. Clear with `#ALRT 0`.\n"
            "_Input is removed after save._",
            awaiting="set_priority",
        )
        return

    await query.answer()

    # --- Switch list tab (also reloads RNS for that list) ---
    if action.startswith("tab:"):
        tab_name = action[4:].strip() or "Default"
        _active_watchlist_name[user.id] = tab_name
        _watchlist_page[user.id] = 0
        await show_watchlist(update, context, edit=True, force_rns=False)
        return

    # --- List-level actions (edit in place, Back always available) ---
    if action == "create":
        await _watchlist_show_prompt(
            update,
            context,
            "🆕 *Create New Watchlist*\n\n"
            "Send a name for the list, e.g.\n"
            "`UK AIM Growth`\n\n"
            "_Your message will be cleared after create._",
            awaiting="create_list",
        )
        return

    if action == "rename":
        await _watchlist_show_prompt(
            update,
            context,
            "✏️ *Rename active list*\n\n"
            "Send the *new name* only.\n\n"
            "_Your message will be cleared after rename._",
            awaiting="rename_list",
        )
        return

    if action == "delete_list":
        active = _active_watchlist_name.get(user.id, "Default")
        await _watchlist_show_prompt(
            update,
            context,
            f"🗑 *Delete list* `{active}`\n\n"
            "This removes **all tickers** in that list.\n"
            "Send `YES` to confirm, or tap « Back to cancel.\n\n"
            "_Your message will be cleared after._",
            awaiting="delete_list",
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
                InlineKeyboardButton("« Back to Watchlist", callback_data="hub:watchlist"),
            ],
            *_watchlist_nav_keyboard(),
        ]
        try:
            await query.message.edit_text(
                "✏️ *Edit Watchlist*\n\nChoose an action:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            await _remember_watchlist_panel(user.id, query.message)
        except Exception:
            await query.message.reply_text(
                "✏️ *Edit Watchlist*\n\nChoose an action:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        return

    # --- Ticker-level actions ---
    if action not in ("add", "change", "delete"):
        return

    if action == "add":
        await _watchlist_show_prompt(
            update,
            context,
            "➕ *Add ticker(s)*\n\n"
            "Send one or many (saved to Notion):\n\n"
            "`#ALRT | Name | https://t.me/+…`\n"
            "or bulk: `#AAA #BBB #CCC`\n\n"
            "_Input is removed after save._",
            awaiting="add",
        )
    elif action == "change":
        await _watchlist_show_prompt(
            update,
            context,
            "✏️ *Change ticker*\n\n"
            "Send:\n"
            "`#TICKER | New Name | https://t.me/+…`\n\n"
            "_Input is removed after update._",
            awaiting="change",
        )
    else:
        await _watchlist_show_prompt(
            update,
            context,
            "🗑 *Delete ticker*\n\n"
            "Send ticker(s), e.g. `#ALRT` or `#AAA #BBB`\n\n"
            "_Input is removed after delete._",
            awaiting="delete",
        )


async def _watchlist_find_pages(
    user_id: int, ticker: str, *, list_name: str | None = None
) -> list[dict]:
    """Find watchlist rows for user + ticker (optional list filter)."""
    db_id = NOTION_WATCHLIST_DB_ID
    ds_id = NOTION_WATCHLIST_DATA_SOURCE_ID
    t = (ticker or "").lstrip("#").upper().strip()
    filters: list[dict] = [
        {
            "property": "Telegram User ID",
            "rich_text": {"equals": str(user_id)},
        },
        {"property": "Ticker", "title": {"equals": t}},
    ]
    if list_name:
        filters.append(
            {
                "property": "List Name",
                "rich_text": {"equals": list_name},
            }
        )
    response = notion_query_data_source(
        data_source_id=ds_id,
        database_id=db_id,
        filter={"and": filters},
        page_size=20,
    )
    return response.get("results", [])


async def _watchlist_upsert_ticker(
    user,
    *,
    ticker: str,
    name: str = "",
    link: str = "",
    list_name: str = "Default",
) -> tuple[str, str]:
    """
    Create or update one ticker row in Hive Bot Watchlist.
    Returns (status, message) where status is 'added' | 'updated' | 'error'.
    """
    t = (ticker or "").lstrip("#").upper().strip()
    if not t:
        return "error", "Missing ticker"
    list_name = (list_name or "Default")[:100]
    display_name = (name or t)[:200]
    link = (link or "").strip()
    if link.startswith("t.me/"):
        link = "https://" + link

    try:
        existing = await _watchlist_find_pages(
            user.id, t, list_name=list_name
        )
        props: dict = {
            "Name": {
                "rich_text": [{"text": {"content": display_name}}]
            },
            "Telegram User ID": {
                "rich_text": [{"text": {"content": str(user.id)}}]
            },
            "List Name": {
                "rich_text": [{"text": {"content": list_name}}]
            },
        }
        if user.username:
            props["Username"] = {
                "rich_text": [{"text": {"content": user.username[:100]}}]
            }
        if link.startswith("http"):
            props["Group Link"] = {"url": link}

        if existing:
            page_id = existing[0]["id"]
            if notion:
                notion.pages.update(page_id=page_id, properties=props)
            else:
                _notion_http(
                    "PATCH", f"pages/{page_id}", {"properties": props}
                )
            return "updated", t

        create_props = {
            "Ticker": {"title": [{"text": {"content": t}}]},
            **props,
        }
        notion_create_page_in_data_source(
            properties=create_props,
            data_source_id=NOTION_WATCHLIST_DATA_SOURCE_ID,
            database_id=NOTION_WATCHLIST_DB_ID,
        )
        return "added", t
    except Exception as e:
        logger.error("watchlist upsert failed %s: %s", t, e)
        return "error", str(e)[:120]


def _parse_watchlist_add_lines(text: str) -> list[tuple[str, str, str]]:
    """
    Parse one or many tickers from user text.
    Supports:
      #ALRT | Company | https://t.me/+x
      #ALRT
      #A #B #C
      multiline mixes of the above
    Returns list of (ticker, name, link).
    """
    items: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for raw_line in (text or "").splitlines() or [text]:
        line = (raw_line or "").strip()
        if not line:
            continue
        if "|" in line:
            parts = [p.strip() for p in line.split("|")]
            ticks = extract_hashtag_tickers(parts[0])
            # Also accept bare ticker without #
            if not ticks:
                bare = parts[0].lstrip("#").upper().strip()
                if re.fullmatch(r"[A-Z0-9]{1,6}", bare or ""):
                    ticks = [bare]
            if not ticks:
                continue
            name = parts[1] if len(parts) > 1 else ""
            link = parts[2] if len(parts) > 2 else ""
            for t in ticks:
                tu = t.upper()
                if tu in seen:
                    continue
                seen.add(tu)
                items.append((tu, name, link))
        else:
            ticks = extract_hashtag_tickers(line)
            if not ticks:
                # space-separated bare tickers
                for tok in re.split(r"[\s,;]+", line):
                    bare = tok.lstrip("#").upper().strip()
                    if re.fullmatch(r"[A-Z0-9]{1,6}", bare or ""):
                        ticks.append(bare)
            for t in ticks:
                tu = t.upper()
                if tu in seen:
                    continue
                seen.add(tu)
                items.append((tu, "", ""))
    return items


async def handle_watchlist_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, text: str
) -> None:
    user = update.effective_user
    db_id = NOTION_WATCHLIST_DB_ID or (os.getenv("NOTION_WATCHLIST_DB_ID") or "").strip()
    ds_id = NOTION_WATCHLIST_DATA_SOURCE_ID
    if not (notion or NOTION_TOKEN) or (not db_id and not ds_id) or not user:
        await update.message.reply_text("Watchlist is not available right now.")
        return

    text = (text or "").strip()
    tickers = extract_hashtag_tickers(text)
    ticker = tickers[0] if tickers else None
    parts = [p.strip() for p in text.split("|")]
    name = parts[1] if len(parts) > 1 else ""
    link = parts[2] if len(parts) > 2 else ""

    try:
        # ----- Create list -----
        if action == "create_list":
            list_name = text.strip()
            if not list_name or len(list_name) > 80:
                await update.message.reply_text(
                    "Please send a short list name, e.g. UK AIM Growth"
                )
                return
            pages = await _fetch_user_watchlist_pages(user.id)
            names = _list_names_from_pages(pages)
            if len(names) >= MAX_WATCHLISTS:
                await update.message.reply_text(
                    f"You already have {MAX_WATCHLISTS} watchlists."
                )
                return
            _active_watchlist_name[user.id] = list_name
            await _watchlist_finish_action(
                update,
                context,
                f"✅ List *{list_name}* ready — use Edit list to add tickers.",
            )
            return

        # ----- Rename list -----
        if action == "rename_list":
            new_name = text.strip()
            if not new_name or len(new_name) > 80:
                await update.message.reply_text("Send a short new name.")
                return
            old = _active_watchlist_name.get(user.id, "Default")
            pages = await _fetch_user_watchlist_pages(user.id)
            n = 0
            for page in pages:
                props = page.get("properties", {})
                ln = _get_plain_text(props.get("List Name")).strip() or "Default"
                if ln != old:
                    continue
                if notion:
                    notion.pages.update(
                        page_id=page["id"],
                        properties={
                            "List Name": {
                                "rich_text": [
                                    {"text": {"content": new_name[:100]}}
                                ]
                            }
                        },
                    )
                n += 1
            _active_watchlist_name[user.id] = new_name
            await _watchlist_finish_action(
                update,
                context,
                f"✅ Renamed *{old}* → *{new_name}* ({n} items).",
            )
            return

        # ----- Delete list -----
        if action == "delete_list":
            if text.upper() != "YES":
                await update.message.reply_text(
                    "To confirm delete, send: YES"
                )
                return
            active = _active_watchlist_name.get(user.id, "Default")
            pages = await _fetch_user_watchlist_pages(user.id)
            n = 0
            for page in pages:
                props = page.get("properties", {})
                ln = _get_plain_text(props.get("List Name")).strip() or "Default"
                if ln != active:
                    continue
                if notion:
                    notion.pages.update(page_id=page["id"], archived=True)
                n += 1
            _active_watchlist_name.pop(user.id, None)
            await _watchlist_finish_action(
                update,
                context,
                f"✅ Deleted list *{active}* ({n} items).",
            )
            return

        # ----- Set priority 1-10 -----
        if action == "set_priority":
            # Parse: #TICKER 7  or  TICKER 7
            m = re.search(
                r"(?:#)?([A-Za-z0-9]{1,6})\s+([0-9]{1,2})\b",
                text,
            )
            if not m:
                await update.message.reply_text(
                    "Send like: `#ALRT 7` (1–10, or 0 to clear)",
                    parse_mode="Markdown",
                )
                return
            t = m.group(1).upper()
            pri = int(m.group(2))
            if pri < 0 or pri > 10:
                await update.message.reply_text("Priority must be 0–10.")
                return
            results = await _watchlist_find_pages(user.id, t)
            if not results:
                await _watchlist_finish_action(
                    update, context, f"⚠️ #{t} not on your watchlist."
                )
                return
            props = {
                "Priority": {"number": None if pri == 0 else pri}
            }
            for page in results:
                if notion:
                    notion.pages.update(page_id=page["id"], properties=props)
            msg = (
                f"✅ #{t} priority cleared."
                if pri == 0
                else f"✅ #{t} priority set to *{pri}*/10."
            )
            # Prefer priority sort after setting
            _watchlist_sort[user.id] = "priority"
            _watchlist_page[user.id] = 0
            await _watchlist_finish_action(update, context, msg)
            return

        # ----- Delete ticker (supports multiple) -----
        if action == "delete":
            to_delete = [t.upper() for t in tickers]
            if not to_delete:
                bare = text.lstrip("#").upper().strip()
                if re.fullmatch(r"[A-Z0-9]{1,6}", bare or ""):
                    to_delete = [bare]
            if not to_delete:
                await update.message.reply_text(
                    "Send ticker(s) like `#ALRT` or `#AAA #BBB`"
                )
                return
            removed = []
            missing = []
            for t in to_delete:
                results = await _watchlist_find_pages(user.id, t)
                if not results:
                    missing.append(t)
                    continue
                for page in results:
                    if notion:
                        notion.pages.update(page_id=page["id"], archived=True)
                removed.append(t)
            bits = []
            if removed:
                bits.append("Removed: " + ", ".join(f"#{x}" for x in removed))
                try:
                    await log_member_activity(
                        user,
                        REQUEST_TYPE_WATCHLIST,
                        notes=f"Watchlist remove: {', '.join('#'+x for x in removed)}",
                    )
                except Exception as le:
                    logger.warning("watchlist remove history log failed: %s", le)
            if missing:
                bits.append("Not found: " + ", ".join(f"#{x}" for x in missing))
            await _watchlist_finish_action(
                update, context, "\n".join(bits) or "Nothing changed."
            )
            return

        # ----- Add ticker(s) — bulk-safe, multi-source Notion write -----
        if action == "add":
            items = _parse_watchlist_add_lines(text)
            if not items:
                await update.message.reply_text(
                    "Include ticker(s), e.g.\n"
                    "`#ALRT | Company | https://t.me/+…`\n"
                    "or several: `#AAA #BBB #CCC`",
                    parse_mode="Markdown",
                )
                return
            list_name = _active_watchlist_name.get(user.id, "Default")
            added, updated, errors = [], [], []
            for t, n, lnk in items:
                status, info = await _watchlist_upsert_ticker(
                    user,
                    ticker=t,
                    name=n,
                    link=lnk,
                    list_name=list_name,
                )
                if status == "added":
                    added.append(t)
                elif status == "updated":
                    updated.append(t)
                else:
                    errors.append(f"{t}: {info}")

            lines = [f"List: *{list_name}*"]
            if added:
                lines.append(
                    f"✅ Added ({len(added)}): "
                    + ", ".join(f"#{x}" for x in added)
                )
                try:
                    await log_member_activity(
                        user,
                        REQUEST_TYPE_WATCHLIST,
                        notes=f"Watchlist add: {', '.join('#'+x for x in added)}",
                    )
                except Exception as le:
                    logger.warning("watchlist add history log failed: %s", le)
            if updated:
                lines.append(
                    f"♻️ Updated ({len(updated)}): "
                    + ", ".join(f"#{x}" for x in updated)
                )
            if errors:
                lines.append("⚠️ Errors:\n" + "\n".join(errors[:8]))
            lines.append("\nOpen 👀 My Watchlist or tap Refresh RNS to view.")
            await _watchlist_finish_action(
                update, context, "\n".join(lines)
            )
            return

        # ----- Change ticker -----
        if action == "change":
            if not ticker:
                await update.message.reply_text(
                    "Include a ticker, e.g. `#ALRT | New Name | https://t.me/+…`",
                    parse_mode="Markdown",
                )
                return
            list_name = _active_watchlist_name.get(user.id, "Default")
            status, info = await _watchlist_upsert_ticker(
                user,
                ticker=ticker,
                name=name,
                link=link,
                list_name=list_name,
            )
            if status == "error":
                summary = f"⚠️ Could not update #{ticker}: {info}"
            elif status == "updated":
                summary = f"✅ Updated #{ticker}."
            else:
                summary = f"✅ #{ticker} added to *{list_name}*."
            await _watchlist_finish_action(update, context, summary)
            return

        await update.message.reply_text(
            f"Unknown action: {action}. Open My Watchlist and try again."
        )

    except Exception as e:
        logger.error("handle_watchlist_text failed (%s): %s", action, e)
        await update.message.reply_text(
            f"Could not update watchlist.\nError: {str(e)[:300]}"
        )
        
async def create_access_request(
    user, *, is_group_member_flag: bool | None = None
) -> tuple[bool, str]:
    """
    Upsert a Pending access request in Notion Auth DB (multi-source safe).
    Uses data_source_id so Notion API 2025-09-03+ works.
    """
    if not notion:
        return False, "Notion client is not initialised (NOTION_TOKEN missing?)"

    db_id = NOTION_AUTH_DB_ID_ENV or os.getenv("NOTION_AUTH_DB_ID") or os.getenv(
        "NOTION_DATABASE_ID"
    )
    ds_id = NOTION_AUTH_DATA_SOURCE_ID
    if not db_id and not ds_id:
        return False, "NOTION_AUTH_DB_ID / NOTION_AUTH_DATA_SOURCE_ID is missing"

    uid_str = str(user.id)
    try:
        # 1) Look for existing page via data source query
        response = notion_query_data_source(
            data_source_id=ds_id,
            database_id=db_id,
            filter={
                "property": "Telegram User ID",
                "title": {"equals": uid_str},
            },
            page_size=1,
        )
        results = response.get("results", [])

        base_props = {
            "Status": {"select": {"name": "Pending"}},
            "Full Name": {
                "rich_text": [
                    {"text": {"content": (user.full_name or "Unknown")[:100]}}
                ]
            },
        }
        if user.username:
            base_props["Username"] = {
                "rich_text": [{"text": {"content": user.username}}]
            }

        if results:
            page_id = results[0]["id"]
            notion.pages.update(page_id=page_id, properties=base_props)
            logger.info(
                "Updated existing auth row to Pending for user %s (page %s)",
                uid_str,
                page_id,
            )
        else:
            create_props = {
                "Telegram User ID": {
                    "title": [{"text": {"content": uid_str}}]
                },
                **base_props,
                "Date Added": {
                    "date": {
                        "start": datetime.now(timezone.utc).date().isoformat()
                    }
                },
            }
            notion_create_page_in_data_source(
                properties=create_props,
                data_source_id=ds_id,
                database_id=db_id,
            )
            logger.info("Created new Pending auth row for user %s", uid_str)

        # Best-effort Group Member (select: Yes / No)
        if is_group_member_flag is not None:
            try:
                response2 = notion_query_data_source(
                    data_source_id=ds_id,
                    database_id=db_id,
                    filter={
                        "property": "Telegram User ID",
                        "title": {"equals": uid_str},
                    },
                    page_size=1,
                )
                pages = response2.get("results", [])
                if pages:
                    notion.pages.update(
                        page_id=pages[0]["id"],
                        properties={
                            "Group Member": {
                                "select": {
                                    "name": "Yes"
                                    if is_group_member_flag
                                    else "No"
                                }
                            }
                        },
                    )
            except Exception as e:
                logger.warning(
                    "Could not set Group Member (property may be missing): %s", e
                )

        # Standalone history row for this access request
        try:
            await append_request_history(
                user,
                "Access request",
                details="Access request submitted (Status=Pending)",
                status="Pending",
            )
        except Exception as he:
            logger.warning("history row for access request failed: %s", he)

        return True, "OK"

    except Exception as e:
        logger.error(
            "Failed to create/update access request for %s: %s", uid_str, e
        )
        return False, str(e)

async def request_access(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user = update.effective_user
    if not user:
        return

    # Current checks
    in_group, group_detail = await is_group_member(context, user.id)

    auth = await get_authorized_users()
    in_notion = False
    if user.username and user.username.lower() in auth.get("usernames", set()):
        in_notion = True
    if str(user.id) in auth.get("user_ids", set()):
        in_notion = True

    has_valid_id = bool(user.id)

    # Already fully authorised
    if await is_authorized(update, context):
        await update.message.reply_text(
            "Access status\n\n"
            f"Telegram User ID: {user.id}  OK\n"
            f"Group member: Yes\n"
            f"Admin authorised: Yes\n\n"
            "Result: ACCESS GRANTED\n"
            "You already have full access."
        )
        return

    # Group membership is recorded on the request but does NOT block submission.
    # Admin still gets the DM and can approve/deny from Notion.
    if os.getenv("TELEGRAM_GROUP_ID") and not in_group:
        logger.info(
            "Access request from non-member user_id=%s detail=%s – still creating Pending",
            user.id,
            group_detail,
        )

    # Submit Pending request (upsert into Notion)
    success, info = await create_access_request(
        user, is_group_member_flag=in_group
    )

    # Always try to notify admin R (even if Notion write failed)
    try:
        await notify_admins_of_request(context, user, in_group=in_group)
    except Exception as e:
        logger.error("notify_admins_of_request crashed: %s", e)

    if not success:
        await update.message.reply_text(
            "Your request was sent to an admin, but saving to Notion failed.\n\n"
            f"Error: {info}\n\n"
            "An admin has still been notified. Please wait or contact Hive support."
        )
        return

    try:
        await sync_group_member_to_notion(context, user)
    except Exception:
        pass

    await update.message.reply_text(
        "Access request submitted\n\n"
        f"Name: {user.full_name}\n"
        f"Username: @{user.username or 'N/A'}\n"
        f"Telegram User ID: {user.id}  OK\n"
        f"Group member: {'Yes' if in_group else 'No'}\n"
        f"Admin authorised: No (Pending)\n\n"
        "Result: WAITING FOR ADMIN\n\n"
        "You already have:\n"
        "1) Valid Telegram user ID\n"
        f"2) Group membership: {'Yes' if in_group else 'No'}\n\n"
        "Still required:\n"
        "3) Admin approval in Notion (Status = Authorised)\n\n"
        "An admin has been notified and will review your request.\n"
        "Check progress anytime with /status."
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Show access status aligned with Notion Authorised Users + live Telegram check.
    Prefer Notion for Admin status / Group Member when Telegram API fails.
    """
    user = update.effective_user
    if not user:
        return

    msg = update.effective_message
    authorised = await is_authorized(update, context)
    in_group_tg, group_detail = await is_group_member(context, user.id)

    # Live Telegram signal
    tg_error = group_detail.startswith("error:") or group_detail.startswith(
        "Invalid TELEGRAM_GROUP_ID"
    )
    tg_skipped = "not set" in group_detail

    # Notion row (multi-source safe)
    member_since = None
    notion_status = None
    notion_group = None
    db_id = NOTION_AUTH_DB_ID_ENV or os.getenv("NOTION_AUTH_DB_ID") or os.getenv(
        "NOTION_DATABASE_ID"
    )
    ds_id = NOTION_AUTH_DATA_SOURCE_ID

    if NOTION_TOKEN or notion:
        try:
            response = notion_query_data_source(
                data_source_id=ds_id,
                database_id=db_id,
                filter={
                    "property": "Telegram User ID",
                    "title": {"equals": str(user.id)},
                },
                page_size=1,
            )
            results = response.get("results", [])
            if results:
                props = results[0].get("properties", {})
                # Status (select)
                st = props.get("Status") or {}
                if isinstance(st, dict):
                    if st.get("type") == "select" and st.get("select"):
                        notion_status = (st["select"] or {}).get("name")
                    else:
                        notion_status = _get_plain_text(st) or None
                # Group Member (select)
                gm = props.get("Group Member") or {}
                if isinstance(gm, dict):
                    if gm.get("type") == "select" and gm.get("select"):
                        notion_group = (gm["select"] or {}).get("name")
                    else:
                        notion_group = _get_plain_text(gm) or None
                # Date Added
                date_prop = (
                    props.get("Date Added")
                    or props.get("Joined")
                    or props.get("Member Since")
                )
                start = None
                if isinstance(date_prop, dict):
                    if date_prop.get("type") == "date" and date_prop.get("date"):
                        start = (date_prop.get("date") or {}).get("start")
                    elif date_prop.get("date") and isinstance(date_prop["date"], dict):
                        start = date_prop["date"].get("start")
                if start:
                    try:
                        dt = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
                        member_since = dt.strftime("%B %Y")
                    except Exception:
                        member_since = str(start)[:7]
        except Exception as e:
            logger.error("status_cmd Notion lookup failed: %s", e)

    # Resolve group membership display: Telegram first, else Notion, else unknown
    if tg_skipped:
        group_display = (
            f"Yes (Notion)" if (notion_group or "").lower() == "yes"
            else (f"No (Notion)" if (notion_group or "").lower() == "no"
                  else "Unknown (TELEGRAM_GROUP_ID not set)")
        )
        in_group_effective = (notion_group or "").lower() == "yes"
    elif tg_error:
        # Telegram API failed (bot not in group / wrong ID / privacy) — trust Notion
        if (notion_group or "").lower() == "yes":
            group_display = "Yes (from Notion; Telegram check unavailable)"
            in_group_effective = True
        elif (notion_group or "").lower() == "no":
            group_display = "No (from Notion; Telegram check unavailable)"
            in_group_effective = False
        else:
            group_display = "Unknown (Telegram check failed)"
            in_group_effective = False
        logger.warning(
            "status_cmd group check failed for %s: %s – using Notion Group Member=%s",
            user.id,
            group_detail,
            notion_group,
        )
    else:
        group_display = "Yes" if in_group_tg else "No"
        in_group_effective = in_group_tg
        # Best-effort: keep Notion Group Member in sync when Telegram works
        try:
            if notion_group and (
                (in_group_tg and (notion_group or "").lower() != "yes")
                or ((not in_group_tg) and (notion_group or "").lower() != "no")
            ):
                await mark_group_member_in_notion(user, is_member=in_group_tg)
        except Exception:
            pass

    lines = [
        "📋 *Your access status*\n",
        f"• Name: {user.full_name}",
        f"• Username: @{user.username or 'N/A'}",
        f"• Telegram ID: `{user.id}`",
        f"• Group member: *{group_display}*",
    ]

    if member_since:
        lines.append(f"• On file since: *{member_since}*")

    if notion_status:
        lines.append(f"• Admin status (Notion): *{notion_status}*")
    else:
        lines.append("• Admin status (Notion): _no record_")

    if authorised:
        lines.append("\n✅ *Result: ACCESS GRANTED*")
    else:
        lines.append("\n❌ *Result: NOT AUTHORISED*")
        lines.append("Send /request to ask for access.")

    # Helpful note when Telegram and Notion disagree
    if (
        not tg_error
        and not tg_skipped
        and notion_group
        and in_group_tg != ((notion_group or "").lower() == "yes")
    ):
        lines.append(
            f"\n_Note: Notion Group Member is “{notion_group}”; "
            f"live Telegram check says {'Yes' if in_group_tg else 'No'}. "
            "Notion was updated to match Telegram where possible._"
        )

    await msg.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=main_reply_keyboard(),
    )


# ------------------------------------------------------------
# Authorisation (Status = "Authorised" in Hive Bot Authorised Users)
# ------------------------------------------------------------
NOTION_AUTH_DB_ID = os.getenv("NOTION_AUTH_DB_ID") or os.getenv("NOTION_DATABASE_ID")


async def get_authorized_users() -> dict:
    """Load users where Status == Authorised (by Telegram User ID + Username)."""
    empty = {"usernames": set(), "user_ids": set()}
    if not notion:
        return empty

    db_id = NOTION_AUTH_DB_ID_ENV or os.getenv("NOTION_AUTH_DB_ID") or os.getenv(
        "NOTION_DATABASE_ID"
    )
    ds_id = NOTION_AUTH_DATA_SOURCE_ID
    if not db_id and not ds_id:
        return empty

    now = time.time()
    if _authorized_cache.get("expires", 0) > now and (
        _authorized_cache.get("user_ids") is not None
    ):
        return {
            "usernames": _authorized_cache.get("usernames", set()),
            "user_ids": _authorized_cache.get("user_ids", set()),
        }

    try:
        usernames: set[str] = set()
        user_ids: set[str] = set()
        cursor = None

        # Try common Status option spellings
        status_values = ("Authorised", "Authorized", "Approved")
        pages: list = []
        last_err = None
        for status_name in status_values:
            try:
                cursor = None
                pages = []
                while True:
                    kwargs = {
                        "page_size": 100,
                        "filter": {
                            "property": "Status",
                            "select": {"equals": status_name},
                        },
                    }
                    if cursor:
                        kwargs["start_cursor"] = cursor
                    response = notion_query_data_source(
                        data_source_id=ds_id,
                        database_id=db_id,
                        **kwargs,
                    )
                    pages.extend(response.get("results", []))
                    if not response.get("has_more"):
                        break
                    cursor = response.get("next_cursor")
                if pages:
                    break
            except Exception as e:
                last_err = e
                logger.warning("Auth filter Status=%s failed: %s", status_name, e)

        if not pages and last_err:
            logger.error("Failed to load authorised users: %s", last_err)

        for page in pages:
            props = page.get("properties", {})

            # Telegram User ID (title or rich_text)
            uid = (
                _get_plain_text(props.get("Telegram User ID"))
                or _get_plain_text(props.get("Telegram ID"))
                or _get_plain_text(props.get("User ID"))
            )
            if uid:
                user_ids.add(uid.strip())

            uname = (
                _get_plain_text(props.get("Username"))
                or _get_plain_text(props.get("Telegram Username"))
                or _get_plain_text(props.get("TG Username"))
            )
            if uname:
                usernames.add(uname.strip().lstrip("@").lower())

        _authorized_cache["usernames"] = usernames
        _authorized_cache["user_ids"] = user_ids
        _authorized_cache["expires"] = now + AUTH_CACHE_TTL
        logger.info(
            "Loaded authorised users: %d usernames, %d user IDs",
            len(usernames),
            len(user_ids),
        )
        return {"usernames": usernames, "user_ids": user_ids}

    except Exception as e:
        logger.error("Failed to load authorised users: %s", e)
        return {
            "usernames": _authorized_cache.get("usernames", set()),
            "user_ids": _authorized_cache.get("user_ids", set()),
        }

# ------------------------------------------------------------
# Message handling – STRICT
# ------------------------------------------------------------

async def should_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Private: allow (menus / stockpick follow-ups already handled above).

    Group (HARD RULE):
      - user must be authorised
      - message must include @BotUsername
      - message must include at least one #TICKER (e.g. #ALRT)
    Otherwise stay silent.
    """
    msg = update.message
    if not msg or not msg.text:
        return False

    text = msg.text
    lower = text.lower()
    chat_type = msg.chat.type

    # Private chats always allowed past this gate
    if chat_type == "private":
        return True

    # ----- GROUP ONLY FROM HERE -----

    # 1) Authorised members only
    if not await is_authorized(update, context):
        logger.info(
            "Group message ignored – user not authorised (user_id=%s)",
            update.effective_user.id if update.effective_user else None,
        )
        return False

    # 2) Must @mention the bot
    bot_username = (context.bot.username or "").lower()
    has_mention = False
    if bot_username:
        if msg.entities:
            for entity in msg.entities:
                if entity.type == "mention":
                    mention = text[
                        entity.offset : entity.offset + entity.length
                    ].lower()
                    if mention == f"@{bot_username}":
                        has_mention = True
                        break
        if not has_mention and f"@{bot_username}" in lower:
            has_mention = True

    # 3) Must include a #TICKER hashtag
    hashtag_tickers = extract_hashtag_tickers(text)

    if has_mention and hashtag_tickers:
        return True

    # Everything else in the group: stay mute
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


from telegram.ext import ChatMemberHandler
from telegram import ChatMemberUpdated, ChatMember


def _extract_status_change(
    update: ChatMemberUpdated,
) -> tuple[str | None, str | None]:
    """Return (old_status, new_status)."""
    old = update.old_chat_member.status if update.old_chat_member else None
    new = update.new_chat_member.status if update.new_chat_member else None
    return old, new


async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    When someone leaves/is kicked from the Hive group → Group Member = No.
    When they join → Group Member = Yes.
    Uses hardcoded Hive group id if env is unset.
    """
    result = update.chat_member or update.my_chat_member
    if not result:
        return

    hive_id = get_hive_group_chat_id()
    try:
        if int(result.chat.id) != int(hive_id):
            return
    except (TypeError, ValueError):
        return

    old_status, new_status = _extract_status_change(result)
    left_statuses = set(_NON_MEMBER_STATUSES)
    member_statuses = set(_MEMBER_STATUSES)

    user = result.new_chat_member.user if result.new_chat_member else None
    if not user or user.is_bot:
        return

    # Left or kicked → deny future bot use until they rejoin + stay Authorised
    if new_status in left_statuses and old_status in member_statuses | {None}:
        await mark_group_member_in_notion(user, is_member=False)
        try:
            _authorized_cache["expires"] = 0  # force auth list refresh
        except Exception:
            pass
        logger.info(
            "User %s left/kicked Hive group → Group Member=No (bot access gated)",
            user.id,
        )
        return

    # Joined / re-joined
    if new_status in member_statuses and old_status in left_statuses | {None}:
        await mark_group_member_in_notion(user, is_member=True)
        try:
            _authorized_cache["expires"] = 0
        except Exception:
            pass
        logger.info("User %s joined Hive group → Group Member=Yes", user.id)

async def mark_group_member_in_notion(user, *, is_member: bool) -> None:
    """Update Group Member (and optionally Status) in the auth database."""
    if not notion and not NOTION_TOKEN:
        return
    db_id = NOTION_AUTH_DB_ID_ENV or os.getenv("NOTION_AUTH_DB_ID") or os.getenv(
        "NOTION_DATABASE_ID"
    )
    ds_id = NOTION_AUTH_DATA_SOURCE_ID
    if not db_id and not ds_id:
        return

    try:
        response = notion_query_data_source(
            data_source_id=ds_id,
            database_id=db_id,
            filter={
                "property": "Telegram User ID",
                "title": {"equals": str(user.id)},
            },
            page_size=1,
        )
        results = response.get("results", [])
        if not results:
            return

        page_id = results[0]["id"]
        props = {
            "Group Member": {
                "select": {"name": "Yes" if is_member else "No"}
            },
        }

        # Optional: auto-revoke Authorised when they leave
        if not is_member:
            props["Status"] = {"select": {"name": "Pending"}}

        if notion:
            notion.pages.update(page_id=page_id, properties=props)
        else:
            _notion_http("PATCH", f"pages/{page_id}", {"properties": props})
        _authorized_cache["expires"] = 0
    except Exception as e:
        logger.error("mark_group_member_in_notion failed for %s: %s", user.id, e)


REQUEST_TYPE_STOCKPICK = "Stockpick"
REQUEST_TYPE_SNAPSHOT = "Security snapshot"
REQUEST_TYPE_TG_LINK = "Telegram link"
REQUEST_TYPE_WATCHLIST = "My Watchlist"
REQUEST_TYPE_SOTD = "Stock of the Day"  # legacy alias
REQUEST_TYPE_TOP10 = "Top 10"
REQUEST_TYPE_RNS_BRIEF = "RNS Brief"
REQUEST_TYPE_ACCESS = "Access request"
REQUEST_TYPE_FOLLOW = "Follow stockpicker"

# Live Notion Request History select options (must match DB when present).
# Unknown names fall back to "Other" with [Type] prefix in Details.
_HISTORY_SELECT_OPTIONS = {
    "Access request",
    "Telegram link",
    "Stockpick",
    "Security snapshot",
    "My Watchlist",
    "Top 10",
    "RNS Brief",
    "Stock of the Day",
    "Other",
}

_HISTORY_TYPE_PREFERRED = {
    REQUEST_TYPE_STOCKPICK: "Stockpick",
    REQUEST_TYPE_SNAPSHOT: "Security snapshot",
    REQUEST_TYPE_TG_LINK: "Telegram link",
    REQUEST_TYPE_ACCESS: "Access request",
    REQUEST_TYPE_WATCHLIST: "My Watchlist",
    REQUEST_TYPE_SOTD: "Top 10",  # legacy → Top 10
    REQUEST_TYPE_TOP10: "Top 10",
    REQUEST_TYPE_RNS_BRIEF: "RNS Brief",
    REQUEST_TYPE_FOLLOW: "Other",
    "Stockpick": "Stockpick",
    "Security snapshot": "Security snapshot",
    "Telegram link": "Telegram link",
    "Access request": "Access request",
    "My Watchlist": "My Watchlist",
    "Top 10": "Top 10",
    "RNS Brief": "RNS Brief",
    "Stock of the Day": "Top 10",
    "Follow stockpicker": "Other",
    "Other": "Other",
}


async def append_request_history(
    user,
    request_type: str,
    *,
    details: str | None = None,
    status: str = "Logged",
) -> bool:
    """
    Create a standalone row in Hive Bot Request History (one row per request).
    Maps unknown Request Type values to Other; retries with fallbacks.
    Returns True if a row was created.
    """
    if not user:
        return False
    if not NOTION_TOKEN and not notion:
        logger.error("append_request_history: no Notion credentials")
        return False
    ds_id = (NOTION_HISTORY_DATA_SOURCE_ID or "").strip() or None
    db_id = (NOTION_HISTORY_DB_ID or "").strip() or None
    if not ds_id and not db_id:
        logger.error("append_request_history: HISTORY DB id missing")
        return False

    preferred = _HISTORY_TYPE_PREFERRED.get(
        request_type, request_type if request_type else "Other"
    )
    notion_type = preferred if preferred in _HISTORY_SELECT_OPTIONS else "Other"

    detail_text = (details or "").strip()
    if request_type and (
        request_type != notion_type or preferred not in _HISTORY_SELECT_OPTIONS
    ):
        detail_text = f"[{request_type}] {detail_text}".strip()
    elif request_type and not detail_text:
        detail_text = request_type

    now = datetime.now(timezone.utc)
    date_start = now.strftime("%Y-%m-%d")

    def _props(type_label: str, detail: str, *, with_status: bool = True) -> dict:
        title = (
            f"{type_label} · {user.full_name or user.id} · "
            f"{now.strftime('%Y-%m-%d %H:%M')}"
        )
        p = {
            "Request": {"title": [{"text": {"content": title[:100]}}]},
            "Telegram User ID": {
                "rich_text": [{"text": {"content": str(user.id)}}]
            },
            "Full Name": {
                "rich_text": [
                    {"text": {"content": (user.full_name or "Unknown")[:100]}}
                ]
            },
            "Request Type": {"select": {"name": type_label}},
            "Requested At": {"date": {"start": date_start}},
        }
        if with_status:
            p["Status"] = {
                "select": {
                    "name": status
                    if status in {"Logged", "Pending", "Completed", "Denied"}
                    else "Logged"
                }
            }
        if user.username:
            p["Username"] = {
                "rich_text": [{"text": {"content": user.username[:100]}}]
            }
        if detail:
            p["Details"] = {
                "rich_text": [{"text": {"content": detail[:1800]}}]
            }
        return p

    attempts = [
        (notion_type, detail_text, True),
        ("Other", f"[{request_type}] {detail_text}".strip(), True),
        ("Other", f"[{request_type}] {detail_text}".strip(), False),
    ]
    seen = set()
    for attempt_type, attempt_detail, with_status in attempts:
        key = (attempt_type, with_status)
        if key in seen:
            continue
        seen.add(key)
        props = _props(attempt_type, attempt_detail, with_status=with_status)
        try:
            notion_create_page_in_data_source(
                properties=props,
                data_source_id=ds_id,
                database_id=db_id,
            )
            logger.info(
                "Request history OK user=%s type=%s",
                user.id,
                attempt_type,
            )
            return True
        except Exception as e:
            logger.warning(
                "append_request_history fail type=%s user=%s: %s",
                attempt_type,
                user.id,
                e,
            )
            if db_id:
                try:
                    if notion:
                        notion.pages.create(
                            parent={"database_id": db_id},
                            properties=props,
                        )
                    else:
                        _notion_http(
                            "POST",
                            "pages",
                            {
                                "parent": {"database_id": db_id},
                                "properties": props,
                            },
                        )
                    logger.info(
                        "Request history OK via db fallback user=%s type=%s",
                        user.id,
                        attempt_type,
                    )
                    return True
                except Exception as e2:
                    logger.warning(
                        "append_request_history db fallback fail: %s", e2
                    )
    logger.error(
        "append_request_history EXHAUSTED user=%s type=%s",
        user.id,
        request_type,
    )
    return False


async def log_member_activity(
    user, request_type: str, *, notes: str | None = None
) -> None:
    """
    Consistent Notion auto-sync on every request:
      1) Always write Hive Bot Request History (source of truth)
      2) Best-effort update Hive Bot Authorised Users (count / last request)

    History must never depend on finding an Auth row.
    """
    if not user:
        return
    if not notion and not NOTION_TOKEN:
        return

    # --- 1. Request History (always) ---
    try:
        ok = await append_request_history(
            user, request_type, details=notes, status="Logged"
        )
        if not ok:
            logger.error(
                "History write returned False user=%s type=%s notes=%s",
                user.id,
                request_type,
                (notes or "")[:80],
            )
    except Exception as he:
        logger.error(
            "append_request_history failed for %s type=%s: %s",
            user.id, request_type, he,
        )

    # --- 2. Auth row activity counters (best-effort) ---
    db_id = NOTION_AUTH_DB_ID_ENV or os.getenv("NOTION_AUTH_DB_ID") or os.getenv(
        "NOTION_DATABASE_ID"
    )
    ds_id = NOTION_AUTH_DATA_SOURCE_ID
    if not db_id and not ds_id:
        return

    try:
        results = []
        for filt in (
            {"property": "Telegram User ID", "title": {"equals": str(user.id)}},
            {"property": "Telegram User ID", "rich_text": {"equals": str(user.id)}},
        ):
            try:
                response = notion_query_data_source(
                    data_source_id=ds_id,
                    database_id=db_id,
                    filter=filt,
                    page_size=1,
                )
                results = response.get("results", [])
                if results:
                    break
            except Exception as fe:
                logger.warning("log_member_activity filter failed: %s", fe)

        if not results:
            logger.info(
                "log_member_activity: no auth row for user %s (history already written)",
                user.id,
            )
            return

        page = results[0]
        page_id = page["id"]
        props = page.get("properties", {})

        count = 0
        count_prop = props.get("Request Count") or {}
        if isinstance(count_prop, dict):
            if count_prop.get("type") == "number" and count_prop.get("number") is not None:
                count = int(count_prop["number"])
            elif count_prop.get("number") is not None:
                count = int(count_prop["number"])
        elif isinstance(count_prop, (int, float)):
            count = int(count_prop)

        type_for_auth = request_type
        if request_type not in ("Stockpick", "Security snapshot", "Telegram link"):
            type_for_auth = "Security snapshot"

        update_props: dict = {
            "Last Request Date": {
                "date": {"start": datetime.now(timezone.utc).date().isoformat()}
            },
            "Last Request Type": {"select": {"name": type_for_auth}},
            "Request Count": {"number": count + 1},
        }
        if notes:
            update_props["Notes"] = {
                "rich_text": [{"text": {"content": notes[:1800]}}]
            }

        try:
            if notion:
                notion.pages.update(page_id=page_id, properties=update_props)
            else:
                _notion_http("PATCH", f"pages/{page_id}", {"properties": update_props})
            logger.info(
                "Logged activity user=%s type=%s count=%s",
                user.id, request_type, count + 1,
            )
        except Exception as ue:
            logger.warning(
                "Auth update with type failed user=%s: %s – retrying without type",
                user.id, ue,
            )
            update_props.pop("Last Request Type", None)
            if notion:
                notion.pages.update(page_id=page_id, properties=update_props)
            else:
                _notion_http("PATCH", f"pages/{page_id}", {"properties": update_props})
    except Exception as e:
        logger.error("log_member_activity auth update failed for %s: %s", user.id, e)



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

    # Commands — with_command_cleanup removes /status, /mystockpick, etc. after run
    app.add_handler(CommandHandler("start", with_command_cleanup(start)))
    app.add_handler(CommandHandler("menu", with_command_cleanup(menu_cmd)))
    app.add_handler(CommandHandler("faq", with_command_cleanup(faq)))
    app.add_handler(CommandHandler("snap", with_command_cleanup(snap_cmd)))
    app.add_handler(CommandHandler("mystockpick", with_command_cleanup(mystockpick_cmd)))
    app.add_handler(CommandHandler("help", with_command_cleanup(menu_cmd)))
    app.add_handler(CommandHandler("tickers", with_command_cleanup(snap_cmd)))
    app.add_handler(CommandHandler("status", with_command_cleanup(status_cmd)))
    app.add_handler(CommandHandler("request", with_command_cleanup(request_access)))
    app.add_handler(CommandHandler("debug", with_command_cleanup(debug_cmd)))
    app.add_handler(CommandHandler("pending", with_command_cleanup(pending_cmd)))
    app.add_handler(CommandHandler("approve", with_command_cleanup(approve_cmd)))
    app.add_handler(CommandHandler("reject", with_command_cleanup(reject_cmd)))
    app.add_handler(CommandHandler("admin", with_command_cleanup(admin_cmd)))
    app.add_handler(CommandHandler("link", with_command_cleanup(link_cmd)))
    app.add_handler(CommandHandler("links", with_command_cleanup(link_cmd)))
    app.add_handler(CommandHandler("telegram", with_command_cleanup(link_cmd)))

    # Inline buttons (once each — no duplicates)
    app.add_handler(CallbackQueryHandler(stockpick_button, pattern=r"^sp:"))
    app.add_handler(CallbackQueryHandler(hub_button, pattern=r"^hub:"))
    app.add_handler(CallbackQueryHandler(watchlist_button, pattern=r"^wl:"))
    app.add_handler(CallbackQueryHandler(menu_button, pattern=r"^cmd:"))
    app.add_handler(CallbackQueryHandler(snapshot_button, pattern=r"^snap:"))
    app.add_handler(CallbackQueryHandler(sbrief_button, pattern=r"^sbrief:"))
    app.add_handler(CallbackQueryHandler(sotd_button, pattern=r"^sotd:"))
    app.add_handler(CallbackQueryHandler(daily_brief_button, pattern=r"^brief:"))
    app.add_handler(CallbackQueryHandler(msp_button, pattern=r"^msp:"))
    app.add_handler(CallbackQueryHandler(admin_button, pattern=r"^admin:"))
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))

    # Text messages + reply keyboard buttons
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Hive SupportBot starting...")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()


    
