#!/usr/bin/env python3
from pathlib import Path
import sys
p = Path(__file__).resolve().parent
code = (p / "bot_part1.py").read_text() + (p / "bot_part2.py").read_text()
exec(compile(code, str(p / "bot.py"), "exec"), {"__name__": "__main__", "__file__": str(p / "bot.py")})
