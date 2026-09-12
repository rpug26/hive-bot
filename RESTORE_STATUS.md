# Bot restore status

`bot.py` on main was accidentally overwritten during a large-file push.

Current `bot.py` is a **temporary bootstrap** that loads the last known good source from commit `68f2a0c`.

## To finish restore + add /link feature

1. Download the full updated `bot.py` (with /link) provided in the project artifacts, **or**
2. On your machine:
   ```bash
   git clone https://github.com/rpug26/hive-bot.git
   cd hive-bot
   git checkout 68f2a0c -- bot.py   # if you only want the pre-link version
   # OR replace bot.py with the full file that includes /link
   git add bot.py
   git commit -m "Restore full bot.py with /link feature"
   git push origin main
   ```
3. Redeploy on Railway.

The full file with private-only `/link` + keyboard button is ready; upload it as `bot.py` to replace the bootstrap.
