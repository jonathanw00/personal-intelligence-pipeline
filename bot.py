import json
import logging
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
import urllib.request

import yaml
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
INBOX_DIR = BASE_DIR / "inbox"
LOCK_FILE = BASE_DIR / "bot.lock"
OFFSET_FILE = BASE_DIR / ".bot_offset"
CONFIG_FILE = BASE_DIR / "config.yaml"
ENV_FILE = BASE_DIR / ".env"
LOGS_DIR = BASE_DIR / "logs"

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

load_dotenv(ENV_FILE)
LOGS_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Logging (stdout only — crontab redirects to logs/bot.log)
# ---------------------------------------------------------------------------


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("bot")
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Lock file (PID-aware — stale locks from crashed runs are ignored)
# ---------------------------------------------------------------------------


def acquire_lock(logger: logging.Logger) -> bool:
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            os.kill(pid, 0)  # raises OSError if process is gone
            logger.info("Lock held by PID %s — exiting.", pid)
            return False
        except (ValueError, OSError):
            logger.info("Stale lock file found — proceeding.")
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock():
    LOCK_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Offset (tracks last processed Telegram update_id)
# ---------------------------------------------------------------------------


def read_offset() -> int:
    try:
        return int(OFFSET_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def write_offset(offset: int):
    OFFSET_FILE.write_text(str(offset))


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------


def telegram_get(token: str, method: str, params: dict) -> dict:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"https://api.telegram.org/bot{token}/{method}?{query}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode())


def send_telegram(token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    urllib.request.urlopen(req, timeout=10)


# ---------------------------------------------------------------------------
# Job file writer
# ---------------------------------------------------------------------------


def write_job(input_type: str, content: str, chat_id: str, telegram_date: int) -> Path:
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    uid = uuid.uuid4().hex[:8]
    filename = f"{ts}_{uid}.job"

    now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

    if input_type == "url":
        job = {
            "input_type": "url",
            "url": content,
            "type": "article",
            "received_at": now_iso,
            "telegram_date": telegram_date,
            "chat_id": chat_id,
        }
    else:
        job = {
            "input_type": "text",
            "url": None,
            "type": "article",
            "text": content,
            "received_at": now_iso,
            "telegram_date": telegram_date,
            "chat_id": chat_id,
        }

    job_path = INBOX_DIR / filename
    job_path.write_text(json.dumps(job, indent=2))
    return job_path


# ---------------------------------------------------------------------------
# Processor trigger
# ---------------------------------------------------------------------------


def run_processor() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(BASE_DIR / "processor.py")],
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Success confirmation — find newest note written this month
# ---------------------------------------------------------------------------


def find_latest_note(cfg: dict, before: float) -> str:
    now = datetime.now()
    month_dir = (
        Path(cfg["obsidian_vault_path"])
        / cfg["resources_path"]
        / now.strftime("%Y")
        / now.strftime("%B")
    )
    try:
        md_files = sorted(month_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        if md_files and md_files[0].stat().st_mtime > before:
            return md_files[0].stem
    except OSError:
        pass
    return ""


# ---------------------------------------------------------------------------
# Document handler (file attachments)
# ---------------------------------------------------------------------------


def handle_document(message: dict, cfg: dict, token: str, allowed_chat_id: str, logger: logging.Logger):
    chat_id = str(message.get("chat", {}).get("id", ""))
    if chat_id != allowed_chat_id:
        return  # silently ignore unknown senders

    doc = message["document"]
    file_name = doc.get("file_name", "file.md")
    file_size = doc.get("file_size", 0)
    file_id = doc["file_id"]
    tg_date = message.get("date", 0)

    logger.info("Document received: %s (%d bytes)", file_name, file_size)

    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    if ext not in ("md", "txt"):
        send_telegram(token, chat_id, "❌ Unsupported file type. Send .md or .txt files only.")
        return

    if file_size > 5 * 1024 * 1024:
        send_telegram(token, chat_id, "❌ File too large (max 5MB).")
        return

    try:
        # Get file path from Telegram
        file_info = telegram_get(token, "getFile", {"file_id": file_id})
        tg_file_path = file_info["result"]["file_path"]

        # Download file bytes
        download_url = f"https://api.telegram.org/file/bot{token}/{tg_file_path}"
        with urllib.request.urlopen(download_url, timeout=30) as resp:
            file_bytes = resp.read()
        file_content = file_bytes.decode("utf-8", errors="replace")
    except Exception as exc:
        logger.error("Failed to download document %s: %s", file_name, exc)
        send_telegram(token, chat_id, f"❌ Failed to download file: {file_name}")
        return

    # Write job to inbox/
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    uid = uuid.uuid4().hex[:8]
    job_filename = f"{ts}_{uid}.job"
    now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

    job = {
        "input_type": "file",
        "filename": file_name,
        "content": file_content,
        "type": "article",
        "received_at": now_iso,
        "telegram_date": tg_date,
        "chat_id": chat_id,
    }
    job_path = INBOX_DIR / job_filename
    try:
        job_path.write_text(json.dumps(job, indent=2), encoding="utf-8")
        logger.info("Job written: %s", job_path.name)
    except Exception as exc:
        logger.error("Failed to write file job: %s", exc)
        send_telegram(token, chat_id, f"❌ Failed to write job for: {file_name}")
        return

    logger.info("Triggering processor")
    triggered_at = datetime.now().timestamp()
    result = run_processor()

    if result.returncode == 0:
        note_title = find_latest_note(cfg, triggered_at)
        if note_title:
            msg = f"✅ Saved: [[{note_title}]]"
        else:
            msg = "❌ Processor ran but no new note found. Check logs."
        logger.info("Processor succeeded. %s", msg)
    else:
        msg = "❌ Job failed — check failed/ folder"
        logger.error("Processor failed (rc=%s): %s", result.returncode, result.stderr[:200])

    send_telegram(token, chat_id, msg)


# ---------------------------------------------------------------------------
# Message handler
# ---------------------------------------------------------------------------


def handle_message(update: dict, cfg: dict, token: str, allowed_chat_id: str, logger: logging.Logger):
    message = update.get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))

    if chat_id != allowed_chat_id:
        return  # silently ignore unknown senders

    # Document attachment — handle before text routing
    if "document" in message:
        handle_document(message, cfg, token, allowed_chat_id, logger)
        return

    text = message.get("text", "").strip()

    if not text:
        return  # ignore non-text messages (photos, stickers, etc.)

    logger.info("Message from %s: %s", chat_id, text[:80])

    tg_date = message.get("date", 0)
    is_url = text.startswith("http://") or text.startswith("https://")
    word_count = len(text.split())
    min_paste_words = cfg.get("min_paste_words", 100)

    if is_url:
        input_type = "url"
        content = text
    elif word_count >= min_paste_words:
        input_type = "text"
        content = text
    else:
        send_telegram(
            token, chat_id,
            "Send a URL or paste the full article text (100+ words) to create a note."
        )
        return

    try:
        job_path = write_job(input_type, content, chat_id, tg_date)
        logger.info("Job written: %s", job_path.name)
    except Exception as exc:
        logger.error("Failed to write job: %s", exc)
        preview = text[:80]
        send_telegram(token, chat_id, f"❌ Failed to write job: {preview}\nCheck logs: {LOGS_DIR}/")
        return

    logger.info("Triggering processor")
    triggered_at = datetime.now().timestamp()
    result = run_processor()

    if result.returncode == 0:
        note_title = find_latest_note(cfg, triggered_at)
        if note_title:
            msg = f"✅ Saved: [[{note_title}]]"
        else:
            msg = "❌ Processor ran but no new note found. Check logs."
        logger.info("Processor succeeded. %s", msg)
    else:
        msg = "❌ Job failed — check failed/ folder"
        logger.error("Processor failed (rc=%s): %s", result.returncode, result.stderr[:200])

    send_telegram(token, chat_id, msg)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    logger = setup_logging()
    logger.info("bot.py starting")

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    allowed_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not token or not allowed_chat_id:
        logger.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing from .env")
        sys.exit(1)

    cfg = load_config()

    if not acquire_lock(logger):
        sys.exit(0)

    try:
        offset = read_offset()
        logger.info("Polling from offset %s", offset)

        response = telegram_get(token, "getUpdates", {"offset": offset, "timeout": 5})
        updates = response.get("result", [])

        if not updates:
            logger.info("No updates.")
            return

        for update in updates:
            handle_message(update, cfg, token, allowed_chat_id, logger)

        new_offset = max(u["update_id"] for u in updates) + 1
        write_offset(new_offset)
        logger.info("Offset updated to %s", new_offset)

    except Exception as exc:
        logger.error("Unhandled exception: %s", exc, exc_info=True)
    finally:
        release_lock()


if __name__ == "__main__":
    main()
