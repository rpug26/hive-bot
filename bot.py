#!/usr/bin/env python3
"""Hive SupportBot loader – joins split source files and runs."""
from pathlib import Path
import sys

base = Path(__file__).resolve().parent
part1 = (base / "bot_part1.txt").read_text(encoding="utf-8")
part2 = (base / "bot_part2.txt").read_text(encoding="utf-8")
src = part1 + part2
code = compile(src, str(base / "bot_joined.py"), "exec")
g = {"__name__": "__main__", "__file__": str(base / "bot.py")}
exec(code, g)
