#!/usr/bin/env python3
"""Temporary bootstrap: load bot source from last known good commit.
Replace this with full bot.py (includes /link) from artifacts as soon as possible.
"""
import urllib.request

URL = "https://raw.githubusercontent.com/rpug26/hive-bot/68f2a0c69571fdabad51a6d92acf2bd6999de3fa/bot.py"
print("Loading bot from commit 68f2a0c...", flush=True)
code = urllib.request.urlopen(URL, timeout=60).read().decode("utf-8")
exec(compile(code, "bot.py", "exec"), {"__name__": "__main__", "__file__": "bot.py"})
