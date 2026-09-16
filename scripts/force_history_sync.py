#!/usr/bin/env python3
"""Rewrite log_member_activity so Request History is always written first."""
from pathlib import Path
import re

path = Path("bot.py")
text = path.read_text()

start = text.find("async def log_member_activity(")
if start < 0:
    raise SystemExit("log_member_activity not found")

rest = text[start + 10 :]
m = re.search(r"\nasync def ", rest)
if not m:
    raise SystemExit("could not find end of log_member_activity")
end = start + 10 + m.start()

new_fn = '''async def log_member_activity(
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
        await append_request_history(
            user, request_type, details=notes, status="Logged"
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


'''

text = text[:start] + new_fn + text[end:]

old_err = (
    '    except Exception as e:\n'
    '        logger.error("append_request_history failed for %s: %s", user.id, e)'
)
new_err = (
    '    except Exception as e:\n'
    '        logger.error(\n'
    '            "append_request_history failed for %s type=%s ds=%s db=%s: %s",\n'
    '            user.id, notion_type, ds_id, db_id, e,\n'
    '        )\n'
    '        try:\n'
    '            if db_id and notion:\n'
    '                notion.pages.create(parent={"database_id": db_id}, properties=props)\n'
    '                logger.info(\n'
    '                    "Request history row created via fallback user=%s type=%s",\n'
    '                    user.id, notion_type,\n'
    '                )\n'
    '        except Exception as e2:\n'
    '            logger.error("append_request_history fallback failed: %s", e2)'
)
if old_err in text:
    text = text.replace(old_err, new_err, 1)

path.write_text(text)
assert "History must never depend" in text
print("OK rewritten", path.stat().st_size)
