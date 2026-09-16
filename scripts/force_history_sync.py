#!/usr/bin/env python3
"""
Re-apply Notion Request History auto-sync on top of latest bot.py.

- Replace append_request_history with hardened multi-attempt writer
- Replace log_member_activity (History first, then Auth)
- Add /logtest admin command to surface live Notion errors
- Watchlist add/remove history logging
"""
from pathlib import Path
import re

path = Path("bot.py")
text = path.read_text()

# ---------------------------------------------------------------------------
# 1) Replace append_request_history entirely
# ---------------------------------------------------------------------------
start = text.find("async def append_request_history(")
if start < 0:
    raise SystemExit("append_request_history not found")
rest = text[start + 10 :]
m = re.search(r"\nasync def ", rest)
if not m:
    raise SystemExit("end of append_request_history not found")
end = start + 10 + m.start()

new_append = r'''async def append_request_history(
    user,
    request_type: str,
    *,
    details: str | None = None,
    status: str = "Logged",
) -> bool:
    """
    Create a standalone row in Hive Bot Request History.
    Returns True on success. Tries data_source then database parent,
    then a minimal property payload. Always logs the full error body.
    """
    if not user:
        return False
    if not NOTION_TOKEN and not notion:
        logger.error("append_request_history: no NOTION_TOKEN")
        return False

    ds_id = (NOTION_HISTORY_DATA_SOURCE_ID or "").strip() or None
    db_id = (NOTION_HISTORY_DB_ID or "").strip() or None
    if db_id:
        raw = db_id.replace("-", "")
        if len(raw) == 32:
            db_id = f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"
    if not ds_id and not db_id:
        logger.error("append_request_history: HISTORY db/ds ids missing")
        return False

    type_map = {
        "Stockpick": "Stockpick",
        "Security snapshot": "Security snapshot",
        "Telegram link": "Telegram link",
        "Access request": "Access request",
        "Other": "Other",
        REQUEST_TYPE_STOCKPICK: "Stockpick",
        REQUEST_TYPE_SNAPSHOT: "Security snapshot",
        REQUEST_TYPE_TG_LINK: "Telegram link",
    }
    if "REQUEST_TYPE_WATCHLIST" in dir() or True:
        try:
            type_map[REQUEST_TYPE_WATCHLIST] = "Other"
        except Exception:
            pass
    notion_type = type_map.get(request_type, "Other")
    if notion_type not in (
        "Stockpick",
        "Security snapshot",
        "Telegram link",
        "Access request",
        "Other",
    ):
        notion_type = "Other"

    st = status if status in ("Logged", "Pending", "Completed", "Denied") else "Logged"
    now = datetime.now(timezone.utc)
    title = f"{notion_type} · {user.full_name or user.id} · {now.strftime('%Y-%m-%d %H:%M')}"

    def _props(minimal: bool = False) -> dict:
        p = {
            "Request": {"title": [{"text": {"content": title[:100]}}]},
            "Telegram User ID": {
                "rich_text": [{"text": {"content": str(user.id)}}]
            },
            "Request Type": {"select": {"name": notion_type}},
            "Status": {"select": {"name": st}},
            "Requested At": {"date": {"start": now.date().isoformat()}},
        }
        if not minimal:
            p["Full Name"] = {
                "rich_text": [
                    {"text": {"content": (user.full_name or "Unknown")[:100]}}
                ]
            }
            if user.username:
                p["Username"] = {
                    "rich_text": [{"text": {"content": user.username[:100]}}]
                }
            if details:
                p["Details"] = {
                    "rich_text": [{"text": {"content": details[:1800]}}]
                }
        return p

    attempts = []
    if ds_id:
        attempts.append(
            ("data_source", {"parent": {"type": "data_source_id", "data_source_id": ds_id}})
        )
    if db_id:
        attempts.append(("database", {"parent": {"database_id": db_id}}))

    last_err = None
    for minimal in (False, True):
        props = _props(minimal=minimal)
        for label, parent in attempts:
            body = {**parent, "properties": props}
            try:
                _notion_http("POST", "pages", body)
                logger.info(
                    "Request history row created user=%s type=%s via=%s minimal=%s",
                    user.id,
                    notion_type,
                    label,
                    minimal,
                )
                return True
            except Exception as e:
                last_err = e
                logger.error(
                    "append_request_history attempt failed user=%s via=%s minimal=%s: %s",
                    user.id,
                    label,
                    minimal,
                    e,
                )

    logger.error(
        "append_request_history ALL attempts failed user=%s type=%s ds=%s db=%s last=%s",
        user.id,
        notion_type,
        ds_id,
        db_id,
        last_err,
    )
    return False


'''

text = text[:start] + new_append + text[end:]

# ---------------------------------------------------------------------------
# 2) Replace log_member_activity
# ---------------------------------------------------------------------------
start = text.find("async def log_member_activity(")
if start < 0:
    raise SystemExit("log_member_activity not found")
rest = text[start + 10 :]
m = re.search(r"\nasync def ", rest)
if not m:
    raise SystemExit("end of log_member_activity not found")
end = start + 10 + m.start()

new_log = r'''async def log_member_activity(
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

    try:
        ok = await append_request_history(
            user, request_type, details=notes, status="Logged"
        )
        if not ok:
            logger.error(
                "log_member_activity: History write returned False user=%s type=%s",
                user.id,
                request_type,
            )
    except Exception as he:
        logger.error(
            "append_request_history failed for %s type=%s: %s",
            user.id,
            request_type,
            he,
        )

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
                "log_member_activity: no auth row for user %s (history already attempted)",
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
                user.id,
                request_type,
                count + 1,
            )
        except Exception as ue:
            logger.warning(
                "Auth update with type failed user=%s: %s – retrying without type",
                user.id,
                ue,
            )
            update_props.pop("Last Request Type", None)
            if notion:
                notion.pages.update(page_id=page_id, properties=update_props)
            else:
                _notion_http("PATCH", f"pages/{page_id}", {"properties": update_props})
    except Exception as e:
        logger.error("log_member_activity auth update failed for %s: %s", user.id, e)


'''

text = text[:start] + new_log + text[end:]

# ---------------------------------------------------------------------------
# 3) REQUEST_TYPE_WATCHLIST
# ---------------------------------------------------------------------------
if "REQUEST_TYPE_WATCHLIST" not in text:
    text = text.replace(
        'REQUEST_TYPE_TG_LINK = "Telegram link"',
        'REQUEST_TYPE_TG_LINK = "Telegram link"\nREQUEST_TYPE_WATCHLIST = "Other"',
        1,
    )

# ---------------------------------------------------------------------------
# 4) Watchlist logging (best-effort patterns)
# ---------------------------------------------------------------------------
if "Watchlist add:" not in text:
    old = '''            if added:
                lines.append(
                    f"✅ Added ({len(added)}): "
                    + ", ".join(f"#{x}" for x in added)
                )
            if updated:'''
    new = '''            if added:
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
            if updated:'''
    if old in text:
        text = text.replace(old, new, 1)

if "Watchlist remove:" not in text:
    old_rm = '''            if removed:
                bits.append("Removed: " + ", ".join(f"#{x}" for x in removed))
            if missing:'''
    new_rm = '''            if removed:
                bits.append("Removed: " + ", ".join(f"#{x}" for x in removed))
                try:
                    await log_member_activity(
                        user,
                        REQUEST_TYPE_WATCHLIST,
                        notes=f"Watchlist remove: {', '.join('#'+x for x in removed)}",
                    )
                except Exception as le:
                    logger.warning("watchlist remove history log failed: %s", le)
            if missing:'''
    if old_rm in text:
        text = text.replace(old_rm, new_rm, 1)

# ---------------------------------------------------------------------------
# 5) Admin /logtest command
# ---------------------------------------------------------------------------
if "async def logtest_cmd" not in text:
    logtest_fn = r'''
async def logtest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: force one Request History write and report success/error."""
    user = update.effective_user
    if not is_admin(user):
        await update.message.reply_text("Admin only.")
        return
    ds = (NOTION_HISTORY_DATA_SOURCE_ID or "").strip()
    db = (NOTION_HISTORY_DB_ID or "").strip()
    tok = "yes" if NOTION_TOKEN else "NO"
    await update.message.reply_text(
        f"History diagnostic…\n"
        f"NOTION_TOKEN set: {tok}\n"
        f"HISTORY_DS: {ds or 'MISSING'}\n"
        f"HISTORY_DB: {db or 'MISSING'}"
    )
    try:
        ok = await append_request_history(
            user,
            "Other",
            details="/logtest forced write from Railway",
            status="Logged",
        )
        if ok:
            await update.message.reply_text(
                "✅ History row created. Check Hive Bot Request History."
            )
        else:
            await update.message.reply_text(
                "❌ History write failed. Check Railway logs for "
                "append_request_history attempt failed…"
            )
    except Exception as e:
        await update.message.reply_text(f"❌ Exception:\n`{e}`", parse_mode="Markdown")


'''
    # Insert before def main
    idx = text.find("\ndef main()")
    if idx < 0:
        idx = text.find("\nasync def main")
    if idx > 0:
        text = text[:idx] + "\n" + logtest_fn + text[idx:]

if 'CommandHandler("logtest"' not in text and "CommandHandler('logtest'" not in text:
    text = text.replace(
        'app.add_handler(CommandHandler("debug", with_command_cleanup(debug_cmd)))',
        'app.add_handler(CommandHandler("debug", with_command_cleanup(debug_cmd)))\n'
        '    app.add_handler(CommandHandler("logtest", with_command_cleanup(logtest_cmd)))',
        1,
    )

path.write_text(text)
assert "History must never depend" in text
assert "async def logtest_cmd" in text
print("OK", path.stat().st_size)
print(" history-first", "History must never depend" in text)
print(" logtest", "logtest_cmd" in text)
print(" multi-attempt", "ALL attempts failed" in text)
