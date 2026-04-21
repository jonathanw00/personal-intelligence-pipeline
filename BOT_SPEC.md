# bot.py — Phase 2 Build Spec
**Project:** Personal Intelligence Pipeline  
**Repo:** https://github.com/jonathanw00/personal-intelligence-pipeline  
**NAS project root:** `/volume1/homes/Jon/intelligence-pipeline/`  
**Date:** April 2026

---

## What we're building

`bot.py` — a Telegram polling script that:
1. Receives a URL or pasted text from the user via Telegram
2. Writes a job file to `jobs/pending/`
3. Immediately triggers `processor.py`
4. Sends a Telegram confirmation back to the user (success or failure)

This replaces the current manual job-file workflow and closes the full loop:
**Send URL in Telegram → Note appears in Obsidian**

This spec covers two Phase 2 items simultaneously:
- ✅ bot.py (Telegram intake)
- ✅ Telegram confirmation message on successful note write

---

## Architecture

### Run model
- **Cron-based polling** — runs every 1 minute via `/etc/crontab`
- Uses a **lock file** to prevent overlapping runs (same pattern as processor.py)
- Maintains a **`.bot_offset`** state file to track last processed Telegram update_id
- No persistent process, no Docker, no external bot framework

### Security
- **Chat ID whitelist** — bot only processes messages from `TELEGRAM_CHAT_ID` in `.env`
- All other senders are silently ignored (no response, no error)

### Dependencies
- **No new pip packages** — uses Python stdlib `urllib.request` and `urllib.parse` to call Telegram HTTP API directly
- Python 3.8.15, venv at `/volume1/homes/Jon/intelligence-pipeline/venv`

---

## File changes

### New files
```
bot.py                    # main polling + intake script (project root)
.bot_offset               # state file — last processed update_id (gitignored)
```

### Modified files
```
.env                      # add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
.gitignore                # add .bot_offset
/etc/crontab              # add bot.py cron entry
```

### Unchanged
Everything in Phase 1 — `processor.py`, `writer.py`, `adapters/`, `config.yaml` — is untouched.

---

## .env additions

Add these two lines to `.env` (never committed):
```
TELEGRAM_BOT_TOKEN=<token from BotFather>
TELEGRAM_CHAT_ID=<your personal numeric chat ID>
```

Load with `python-dotenv` (already a project dependency).

---

## bot.py logic — step by step

### 1. Lock file
```
lock path: /volume1/homes/Jon/intelligence-pipeline/bot.lock
```
- If lock exists and PID inside is still running → exit immediately
- Otherwise write own PID to lock file, proceed
- Always remove lock in a `finally` block

### 2. Read offset
```
offset file: /volume1/homes/Jon/intelligence-pipeline/.bot_offset
```
- Read integer from file, default to `0` if file missing

### 3. Poll Telegram getUpdates
```
GET https://api.telegram.org/bot{TOKEN}/getUpdates?offset={offset}&timeout=5
```
- Parse JSON response
- If no updates → exit cleanly

### 4. Process each update
For each update where `message.chat.id == TELEGRAM_CHAT_ID`:

**Detect input type:**
```python
text = update["message"]["text"].strip()
is_url = text.startswith("http://") or text.startswith("https://")
```

**Write job file:**
```
path: jobs/pending/{timestamp}_{uuid4_short}.json
```
Job file schema (JSON):
```json
{
  "id": "20260421_143022_a3f1",
  "created": "2026-04-21T14:30:22",
  "source": "telegram",
  "type": "url",           // "url" or "text"
  "content": "https://..."  // the URL or the full pasted text
}
```

**Immediately trigger processor:**
```python
subprocess.run(
    ["/volume1/homes/Jon/intelligence-pipeline/venv/bin/python",
     "/volume1/homes/Jon/intelligence-pipeline/processor.py"],
    capture_output=True,
    text=True
)
```

**Send Telegram confirmation** (see section below)

**Update offset:**
```python
new_offset = max(update["update_id"] for update in updates) + 1
```
Write to `.bot_offset`

### 5. Cleanup
Remove lock file in `finally` block.

---

## Telegram confirmation messages

### Success
After processor.py completes (returncode == 0), read the most recently modified `.md` file in the vault's current month folder to get the note title.

```
✅ Saved: [[21-Apr-2026 — Article Title — Article]]
```

### Failure (processor error)
```
❌ Failed to process: <first 80 chars of URL or text>
Check logs: /volume1/homes/Jon/intelligence-pipeline/logs/
```

### Ignored sender (not your chat ID)
Silent — send nothing, log nothing.

### How to send a Telegram message
```python
def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10)
```

---

## Crontab entry

Add to `/etc/crontab`:
```
* * * * * Jon /volume1/homes/Jon/intelligence-pipeline/venv/bin/python /volume1/homes/Jon/intelligence-pipeline/bot.py >> /volume1/homes/Jon/intelligence-pipeline/logs/bot.log 2>&1
```

This runs bot.py every minute as user `Jon`.

---

## Logging

- All stdout/stderr → `logs/bot.log` via crontab redirect
- Log format: match existing processor.py style — timestamp + message
- Log on: startup, message received, job written, processor triggered, confirmation sent, any errors

---

## Job file compatibility note

**IMPORTANT for Claude Code:** Before writing job files, check `processor.py` to confirm the exact schema it reads from `jobs/pending/`. The schema above is the spec intent — if processor.py already reads a different format (e.g., plain text file with just a URL), match that existing format exactly. Do not change processor.py to match bot.py — make bot.py match processor.py.

---

## Error handling

| Error | Behaviour |
|---|---|
| Telegram API unreachable | Log error, exit cleanly, try again next cron |
| processor.py returns non-zero | Send failure message to Telegram |
| Job file write fails | Log error, send failure message, do not run processor |
| `.bot_offset` missing | Default to 0 (reprocess recent messages is acceptable) |
| Unhandled exception | Log full traceback, release lock, exit |

---

## Testing checklist (for Claude Code to include)

1. Send a URL to the bot → confirm job file written to `jobs/pending/`
2. Send pasted text (>100 words) to the bot → confirm job file written
3. Send a message from a different chat ID → confirm silent ignore
4. Confirm `.bot_offset` increments correctly after each run
5. Confirm lock file is cleaned up after normal exit
6. Confirm lock file is cleaned up after an exception
7. Confirm Telegram confirmation message arrives after successful processor run
8. Run two cron ticks simultaneously (manually) → confirm second exits immediately due to lock

---

## What is NOT in scope for this build

- YouTube adapter (Phase 2, next)
- Daily note append (Phase 2, next)
- Weekly digest (Phase 3)
- Error notifications beyond inline Telegram confirmation
- Any changes to processor.py, writer.py, or adapters/