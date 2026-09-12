#!/usr/bin/env python3
from pathlib import Path
p = Path(__file__).resolve().parent
code = "".join((p / f"bot_chunk_{i}.py").read_text() for i in range(5))
exec(compile(code, str(p / "bot.py"), "exec"), {"__name__": "__main__", "__file__": str(p / "bot.py")})
