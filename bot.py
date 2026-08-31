#!/usr/bin/env python3

"""

Hive SupportBot – AIM/Small Cap knowledge bot

"""

from notion_client import client 

from datetime import datetime, timezone

import os

import re

import logging

from datetime import datetime

from dotenv import load_dotenv

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



TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not TOKEN:

    raise ValueError("TELEGRAM_BOT_TOKEN missing")

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID)

notion = None 
if NOTION_TOKEN and NOTION_DATABASE_ID:
    notion = Client(auth=NOTION_TOKEN)
else logger.warning("Notion credentials missing - "Stockpick will only be acknowledged")

# ============================================================

# Knowledge snapshot (UK AIM Micro-Cap)

# ============================================================

KNOWLEDGE = {

    "KEFI": {

        "company": "KEFI Gold and Copper plc",

        "status": "Action",

        "mcap": "£147m",

        "summary": "Fully-funded Tulu Kapi gold project Ethiopia. US$400m mining contract signed. Construction on schedule. Debt drawdown Q4 2026 target. Main Market listing path with Stifel.",

        "red_flags": "Very large share count. Dilution history.",

        "next": "Q4 2026 project debt drawdown + Jibal Qutman FID",

    },

    "AXL": {

        "company": "Arrow Exploration Corp.",

        "status": "Action",

        "mcap": "£65m",

        "summary": "Colombian oil producer. >5,300 boe/d. Strong cash generation, no debt. Excellent drilling record.",

        "red_flags": "Oil & gas sector. Colombia risk.",

        "next": "Further 2026 drilling results",

    },

    "ATOM": {

        "company": "ATOME plc",

        "status": "Watchlist",

        "mcap": "£32m",

        "summary": "Green fertiliser project Villeta Paraguay. Fully financed US$665m. PPA under government review.",

        "red_flags": "Paraguay PPA uncertainty. Single project risk.",

        "next": "PPA clarity (early Sep 2026 target)",

    },

    "ALRT": {

        "company": "Defence Holdings plc",

        "status": "Watchlist",

        "mcap": "£27m",

        "summary": "UK defence technology group. First MoD contract. Meridian accelerator. £4m placing. Moving toward multi-pillar defence model.",

        "red_flags": "Early commercial stage. Dilution from placing. Main Market transition.",

        "next": "Further MoD / defence contracts + Meridian progress",

    },

    "GSCU": {

        "company": "Great Southern Copper Plc",

        "status": "Watchlist",

        "mcap": "£25m",

        "summary": "Chile copper-gold-silver explorer. High-grade hits at Mostaza. System open ~2 km.",

        "red_flags": "Chile jurisdiction. Early stage.",

        "next": "Further drilling / resource definition",

    },

    "HE1": {

        "company": "Helium One Global Ltd",

        "status": "Watchlist",

        "mcap": "£40m",

        "summary": "Helium explorer Tanzania (Rukwa) + 50% Colorado project with first offtake sales.",

        "red_flags": "Tanzania risk. Exploration stage.",

        "next": "Rukwa appraisal or Colorado production ramp",

    },

    "GROC": {

        "company": "GreenRoc Strategic Materials Plc",

        "status": "Watchlist",

        "mcap": "£12m",

        "summary": "Amitsoq graphite (Greenland) – high grade. Exploitation Licence granted. Anode pilot plant progressing. EU Strategic Project.",

        "red_flags": "Greenland jurisdiction. Financing still needed.",

        "next": "Pilot plant + financing / offtake",

    },

    "88E": {

        "company": "88 Energy Ltd",

        "status": "Researching",

        "mcap": "£15m",

        "summary": "Alaska North Slope explorer. Resources upgraded. Augusta-1 well targeted Q1 2027.",

        "red_flags": "Pre-revenue. 2027 spud. Funding needs.",

        "next": "Farm-out progress + Q1 2027 spud",

    },

    "AAU": {

        "company": "Ariana Resources plc",

        "status": "Watchlist",

        "mcap": "£43m",

        "summary": "Dokwe Gold Project Zimbabwe (~1.1Moz). Non-dilutive cash from Turkish asset sales. Debt-free.",

        "red_flags": "Zimbabwe / Turkey jurisdiction.",

        "next": "Dokwe feasibility & resource updates",

    },

    "AEG": {

        "company": "Active Energy Group plc",

        "status": "Watchlist",

        "mcap": "£6.8m",

        "summary": "UAE AI/crypto hosting + UK solar/BESS. Hosting revenue started. Pipeline growing.",

        "red_flags": "Multi-pillar complexity. Early revenue. Dilution history.",

        "next": "UAE revenue ramp + further energisations",

    },

    "AEX": {

        "company": "Aminex plc",

        "status": "Watchlist",

        "mcap": "£110m",

        "summary": "Tanzania gas. Debt-free. Ntorya development carried but currently in operator budget dispute.",

        "red_flags": "Operator dispute could delay first gas.",

        "next": "Resolution of ARA Petroleum budget dispute",

    },

    "AGI": {

        "company": "Potentially AI plc",

        "status": "Watchlist",

        "mcap": "£30m",

        "summary": "AI platform (model routing). RTO completed Jul 2026. Products targeted H2 2026.",

        "red_flags": "Pre-revenue. Competitive AI space.",

        "next": "H2 2026 product launches",

    },

    "APTA": {

        "company": "Aptamer Group plc",

        "status": "Watchlist",

        "mcap": "£20m",

        "summary": "Optimer binders. Ebola diagnostic programme + pharma contracts + radiopharma.",

        "red_flags": "Small revenue. Cash runway. Binary development risk.",

        "next": "Ebola programme progress + further contracts",

    },

    "ARCM": {

        "company": "Arc Minerals Ltd",

        "status": "Watchlist",

        "mcap": "£10m",

        "summary": "Kalahari Copper Belt Botswana + Zambia. Major diamond drill programme starting.",

        "red_flags": "Early stage. Funding for drilling.",

        "next": "First assays from Virgo programme",

    },

    "BHL": {

        "company": "Bradda Head Lithium Limited",

        "status": "Watchlist",

        "mcap": "£6m",

        "summary": "Arizona lithium. Technical committee with Rio Tinto subsidiary. Drilling mobilising at Whistlejacket.",

        "red_flags": "Still far below 2022 highs.",

        "next": "First assay results",

    },

    "BLOE": {

        "company": "Block Energy plc",

        "status": "Watchlist",

        "mcap": "£14m",

        "summary": "Georgia producer + Gabon growth. Partner-funded model. 3D seismic underway.",

        "red_flags": "Country risk. Historical dilution.",

        "next": "Seismic results + Gabon progress",

    },

    "BRES": {

        "company": "Blencowe Resources plc",

        "status": "Watchlist",

        "mcap": "£34m",

        "summary": "Orom-Cross graphite Uganda. Big resource upgrade. Non-China purification LOI. Defence applications testing.",

        "red_flags": "Still pre-production / pre-funding Phase 1.",

        "next": "Tender results Q3 2026 + orbital testing",

    },

    "ALL": {

        "company": "Atlantic Lithium Ltd",

        "status": "Watchlist",

        "mcap": "£125m",

        "summary": "Ewoyaa lithium Ghana. Moving toward construction decision.",

        "red_flags": "Ghana risk. Financing still required.",

        "next": "FID / funding / offtake updates",

    },

    "AVCT": {

        "company": "Avacta Group plc",

        "status": "Watchlist",

        "mcap": "£319m",

        "summary": "Oncology biopharma. pre|CISION platform. AVA6103 Phase 1 data expected late H2 2026.",

        "red_flags": "Convertible bond overhang. Ongoing cash needs.",

        "next": "AVA6103 Phase 1 data H2 2026",

    },

    "SYME": {

        "company": "Supply@ME Capital plc",

        "status": "Researching",

        "mcap": "£15.4m",

        "summary": "Inventory monetisation fintech. Expanding into Italian credit intermediation.",

        "red_flags": "Funding dependence. Delayed accounts.",

        "next": "SFE deal documentation + Italian transactions",

    },

    "FRG": {

        "company": "Firering Strategic Minerals plc",

        "status": "Researching",

        "mcap": "£5m",

        "summary": "Lithium / critical minerals West Africa (Côte d'Ivoire).",

        "red_flags": "Early exploration. Jurisdiction risk.",

        "next": "Exploration results",

    },

    "CMRS": {

        "company": "Critical Mineral Resources PLC",

        "status": "Researching",

        "mcap": "£2m",

        "summary": "Copper-silver Morocco.",

        "red_flags": "Very small. Limited track record.",

        "next": "Exploration updates",

    },

    "CPX": {

        "company": "CAP-XX Limited",

        "status": "Researching",

        "mcap": "£9m",

        "summary": "Supercapacitors for IoT / electronics / automotive.",

        "red_flags": "Heavy dilution. Loss-making.",

        "next": "Design wins / volume orders",

    },

    "80M": {

        "company": "80 Mile Plc",

        "status": "Researching",

        "mcap": "£28m",

        "summary": "Greenland critical minerals + Italy biofuels (Ferrandina). Disko drilling under JV funding.",

        "red_flags": "Greenland early stage.",

        "next": "Disko assays + Ferrandina progress",

    },

    "TERN": {

        "company": "Tern plc",

        "status": "Passed",

        "mcap": "£11m",

        "summary": "IoT/AI venture investor. Continuous discounted raises. Shrinking NAV.",

        "red_flags": "Chronic dilution. Falling NAV.",

        "next": "Any portfolio liquidity event",

    },

}





def extract_tickers(text: str) -> list:

    """Extract tickers from plain text or hashtags (#ALRT → ALRT)."""

    if not text:

        return []

    pattern = r"(?:^|[\s$#])([A-Za-z0-9]{2,5})(?=[\s.,!?;:\)]|$)"

    candidates = re.findall(pattern, text)

    found = []

    for c in candidates:

        t = c.upper()

        if t in KNOWLEDGE and t not in found:

            found.append(t)

    return found





def format_reply(ticker: str, data: dict) -> str:

    return (

        f"📊 {ticker} – {data['company']}\n"

        f"Status: {data['status']}  |  Mkt Cap: {data['mcap']}\n\n"

        f"Summary:\n{data['summary']}\n\n"

        f"Red Flags:\n{data['red_flags']}\n\n"

        f"Next catalyst:\n{data['next']}\n\n"

        f"_Hive knowledge snapshot. Not financial advice. DYOR._"

    )





async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    name = update.effective_user.first_name or "there"

    await update.message.reply_text(

        f"Hi {name}! 👋\n\n"

        "I'm SupportBot for The Hive 🐝\n\n"

        "Send a ticker (KEFI, AXL, #ALRT, etc.) and I’ll pull the notes.\n"

        "Commands: /help  /tickers  /faq"

    )





async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    await update.message.reply_text(

        "What I can do:\n"

        "• Look up AIM micro-caps from the Hive knowledge base\n"

        "• Accept tickers with or without # (e.g. ALRT or #ALRT)\n\n"

        "/start – Welcome\n"

        "/help – This message\n"

        "/tickers – List loaded tickers\n"

        "/faq – Common questions\n\n"

        "In groups, tag me or just post the ticker / #ticker."

    )





async def faq(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    await update.message.reply_text(

        "1. How do I look up a stock?\n"

        "   → Send the ticker (KEFI or #KEFI)\n\n"

        "2. Is this advice?\n"

        "   → No. Discussion points only. Always DYOR.\n\n"

        "3. Admin support?\n"

        "   → Contact human admin in The Hive group."

    )





async def tickers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    tickers = ", ".join(sorted(KNOWLEDGE.keys()))

    await update.message.reply_text(

        f"Loaded tickers ({len(KNOWLEDGE)}):\n\n{tickers}\n\n"

        "Just send any of them (with or without #)."

    )





def should_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:

    if update.effective_chat.type == "private":

        return True



    msg = update.message

    if not msg or not msg.text:

        return False



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


async def save_stockpick_to_notion(text: str, user_name: str, ticker: str | None = None) -> bool:
    """Write a #stockpick message into the Notion database."""
    if not notion:
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
        logger.error("Failed to write to Notion: %s", e)
        return False
        
    if "#stockpick" in lower:
    user = update.effective_user
    user_name = user.full_name if user else "Unknown"
    tickers = extract_tickers(text)
    ticker = tickers[0] if tickers else None

    success = await save_stockpick_to_notion(text, user_name, ticker)

    if success:
        await update.message.reply_text(
            f"Got it ✅  Captured your #stockpick"
            + (f" ({ticker})" if ticker else "")
            + " and saved to Notion."
        )
    else:
        await update.message.reply_text(
            "Got it ✅  Captured your #stockpick.\n"
            "(Could not write to Notion – check the bot logs.)"
        )
    return


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    if not should_reply(update, context):

        return



    text = (update.message.text or "").strip()

    lower = text.lower()



    if any(w in lower for w in ("hi", "hello", "hey", "good morning", "good evening")) and len(text) < 25:

        await update.message.reply_text(

            "Hi! 👋 Send a ticker (e.g. KEFI or #ALRT) and I’ll look it up."

        )

        return



    if any(w in lower for w in ("thank", "thanks", "cheers")):

        await update.message.reply_text("You're welcome! 🐝")

        return



    tickers = extract_tickers(text)

    if tickers:

        for t in tickers:

            await update.message.reply_text(format_reply(t, KNOWLEDGE[t]))

        return



    if "#stockpick" in lower:

        await update.message.reply_text(

            "Got it ✅  Captured your #stockpick.\n"

            "(Notion live write will be enabled once the integration token is provided.)"

        )

        return



    await update.message.reply_text(

        "I don't have that ticker in the current snapshot.\n"

        "Try /tickers to see what’s loaded, or contact human admin support in The Hive group."

    )





async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:

    logger.error("Error: %s", context.error)





def main() -> None:

    app = Application.builder().token(TOKEN).build()



    app.add_handler(CommandHandler("start", start))

    app.add_handler(CommandHandler("help", help_cmd))

    app.add_handler(CommandHandler("faq", faq))

    app.add_handler(CommandHandler("tickers", tickers_cmd))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.add_error_handler(error_handler)



    logger.info("Hive SupportBot starting...")

    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)





if __name__ == "__main__":

    main()
