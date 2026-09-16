# Request History auto-sync fix

## Root cause
`log_member_activity()` only wrote to **Hive Bot Request History** *after* a successful Auth-row update.
If the Auth lookup failed (wrong Telegram User ID property type: title vs rich_text), or the select update failed, the function returned early and **no history row was created**.

Last live rows in Notion stopped at **2026-09-12** (backfill only).

## Fix (in bot.py)
1. **Always** call `append_request_history()` first for every request.
2. Auth-row update is best-effort (title + rich_text filters).
3. Fallback `pages.create` if data_source create fails.

Redeploy Railway after pulling this bot.py change.

## Verify
1. Send a Security snapshot or Telegram link request in Hive bot.
2. Check Hive Bot Request History – newest row within seconds.
3. Railway logs: `Request history row created user=...`
