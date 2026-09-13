#!/usr/bin/env python3 """ Hive SupportBot – AIM/Small Cap knowledge bot Live data from Notion + #stockpick capture Strict group behaviour: only responds on @mention + #ticker or #ticker + intent keywords (summary, snapshot, thesis, etc.) """
import os import re import asyncio import time import logging from datetime import datetime, timezone
from dotenv import load_dotenv from notion_client import Client
from telegram import ( Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, ) from telegram.ext import ( Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters, )
load_dotenv()
logging.basicConfig( format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO, ) logger = logging.getLogger(name)
------------------------------------------------------------
Environment
------------------------------------------------------------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") if not TOKEN: raise ValueError("TELEGRAM_BOT_TOKEN missing")
NOTION_TOKEN = os.getenv("NOTION_TOKEN") NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")       # for #stockpick captures NOTION_TICKERS_DB_ID = os.getenv("NOTION_TICKERS_DB_ID")   # UK AIM Micro-Cap database
Auth DB container + data source (required for multi-source Notion API 2025-09-03+)
NOTION_AUTH_DB_ID_ENV = ( os.getenv("NOTION_AUTH_DB_ID") or os.getenv("NOTION_DATABASE_ID") or "" ).strip()
Hive Bot Authorised Users data source id (from collection://…)
NOTION_AUTH_DATA_SOURCE_ID = ( os.getenv("NOTION_AUTH_DATA_SOURCE_ID") or "fd1e050c-5396-448d-a2d5-c4749a0cc69e" ).strip()
Standalone request history (one row per request)
NOTION_HISTORY_DB_ID = ( os.getenv("NOTION_HISTORY_DB_ID") or "3dbe81bb-7bfb-4def-ab05-80eac9b0c009" ).strip() NOTION_HISTORY_DATA_SOURCE_ID = ( os.getenv("NOTION_HISTORY_DATA_SOURCE_ID") or "aee62b48-46a5-4cbc-8237-af8912a3f22c" ).strip()
notion = Client(auth=NOTION_TOKEN) if NOTION_TOKEN else None
if not notion: logger.warning("Notion credentials missing – live lookup and #stockpick write disabled")
def _notion_http(method: str, path: str, body: dict | None = None) -> dict: """ Raw Notion REST call with API version that supports multi-source databases. path is relative, e.g. 'data_sources/{id}/query' """ import json as _json import urllib.error import urllib.request
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
    with urllib.request.urlopen(req, timeout=30) as resp:
        return _json.loads(resp.read().decode("utf-8"))
except urllib.error.HTTPError as e:
    err_body = e.read().decode("utf-8", errors="replace")
    raise RuntimeError(f"Notion HTTP {e.code}: {err_body}") from e
def notion_query_data_source( *, data_source_id: str | None = None, database_id: str | None = None, **kwargs, ) -> dict: """ Query a Notion table in a way that works with multi-source databases. Prefer data_sources/{id}/query (API 2025-09-03+); fall back to databases.query. """ if not notion and not NOTION_TOKEN: raise RuntimeError("Notion client is not initialised")
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
if not notion:
    raise RuntimeError("Notion client is not initialised")
return notion.databases.query(database_id=db_id, **kwargs)