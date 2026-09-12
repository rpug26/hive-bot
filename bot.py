#!/usr/bin/env python3
"""Bootstrap: assemble bot from base64 chunk files and run."""
from pathlib import Path
import base64
import sys

p = Path(__file__).resolve().parent
parts = sorted(p.glob("bot_b64_*.txt"), key=lambda x: x.name)
if not parts:
    raise SystemExit("Missing bot_b64_*.txt chunk files")
b64 = "".join(x.read_text().strip() for x in parts)
code = base64.b64decode(b64).decode("utf-8")
exec(compile(code, str(p / "bot.py"), "exec"), {"__name__": "__main__", "__file__": str(p / "bot.py")})
