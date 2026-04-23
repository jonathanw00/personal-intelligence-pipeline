import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import anthropic
import yaml
from dotenv import load_dotenv

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from adapters import article as article_adapter
import daily_note
import writer

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
INBOX_DIR = BASE_DIR / "inbox"
PROCESSING_DIR = BASE_DIR / "processing"
DONE_DIR = BASE_DIR / "done"
FAILED_DIR = BASE_DIR / "failed"
LOCK_FILE = BASE_DIR / "pipeline.lock"
LOG_FILE = BASE_DIR / "pipeline.log"
CONFIG_FILE = BASE_DIR / "config.yaml"
ENV_FILE = BASE_DIR / ".env"

# ---------------------------------------------------------------------------
# Config + env
# ---------------------------------------------------------------------------

load_dotenv(ENV_FILE)


def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging(max_bytes: int) -> logging.Logger:
    logger = logging.getLogger("pipeline")
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(LOG_FILE, maxBytes=max_bytes, backupCount=1)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(stream)
    return logger


# ---------------------------------------------------------------------------
# Lock file
# ---------------------------------------------------------------------------


def acquire_lock(logger: logging.Logger) -> bool:
    if LOCK_FILE.exists():
        logger.warning("Lock file exists — another run is active. Exiting.")
        return False
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock():
    LOCK_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Haiku system prompt (spec §7.1)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an intelligent reading assistant that processes articles and video transcripts into structured, high-signal Obsidian notes. Your job is not to summarize passively — it is to distill the most important ideas, surface the sharpest quotes, and produce a note that teaches someone who wasn't there.

If the content contains multiple unrelated sections (newsletter sidebars, comments, related articles, navigation, footer text), process only the primary article or video body. Ignore page furniture.

Produce output as a single JSON object with these exact keys:

filename_title: A clean, readable title for the filename. No special characters except spaces and hyphens. 3–8 words. This is the article or video title, not a description of it.

tags: Array of 4–6 kebab-case tags. Pick naturally based on themes, domain, and concepts. Do not create tag variations of the same concept (e.g. use "ai" not both "ai" and "artificial-intelligence").

summary: 1–3 sentences. What is this piece about, and why does it matter? Write this as if briefing someone before they read. Be specific — avoid vague generalities.

key_points: Array of objects. Scale the number to content depth — 3–5 for focused pieces, 5–8 for substantial ones, up to 10 for dense long-form. Each object has:
  - point: A distilled insight in your own words. One tight sentence. The kind of thing someone would underline.
  - quote: The single sharpest line from the source that best supports this point. Copy it verbatim from the source text — do not paraphrase or reword. Preserve authentic voice. Make it scannable and memorable on first read. If no strong quote exists for a point, omit this field.

Quote discipline:
— One quote per key point maximum
— Remove 30–50% of filler without losing meaning or voice
— Prefer the surprising, specific, or counterintuitive over the obvious
— If no strong quotable lines exist, omit quotes entirely — do not fabricate

Return only valid JSON. No preamble, no markdown fences."""


def build_user_message(content_type: str, url: str, iso_date: str, text: str) -> str:
    return f"Content type: {content_type}\nSource URL: {url}\nRetrieved: {iso_date}\n\n{text}"


# ---------------------------------------------------------------------------
# Claude call
# ---------------------------------------------------------------------------


def call_claude(cfg: dict, content_type: str, url: str, text: str) -> dict:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    iso_date = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

    user_message = build_user_message(content_type, url or "", iso_date, text)

    max_tokens = cfg["claude_max_tokens"]
    logging.getLogger("pipeline").info(
        "Calling Claude model=%s max_tokens=%s", cfg["claude_model"], max_tokens
    )
    response = client.messages.create(
        model=cfg["claude_model"],
        max_tokens=max_tokens,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    raw = response.content[0].text.strip()
    logger_inst = logging.getLogger("pipeline")
    logger_inst.info("Claude raw response: %s", raw)

    # Strip markdown code fences that Claude sometimes wraps around JSON
    cleaned = raw
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip()

    if not cleaned:
        raise ValueError(f"Claude returned an empty response after stripping fences. Raw: {raw!r}")

    return json.loads(cleaned)


# ---------------------------------------------------------------------------
# Job processing
# ---------------------------------------------------------------------------


def _resolve_note_dt(job: dict, cfg: dict, logger: logging.Logger) -> datetime:
    """Return a naive local datetime for the note date, derived from telegram_date."""
    tz_name = cfg.get("timezone", "UTC")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        logger.warning("Unknown timezone %r — falling back to UTC", tz_name)
        tz = ZoneInfo("UTC")

    raw_ts = job.get("telegram_date")
    if raw_ts:
        return datetime.fromtimestamp(int(raw_ts), tz=tz).replace(tzinfo=None)

    logger.warning("Job missing telegram_date — falling back to received_at for note date")
    received_at = job.get("received_at", "")
    if received_at:
        try:
            utc_dt = datetime.fromisoformat(received_at).replace(tzinfo=timezone.utc)
            return utc_dt.astimezone(tz).replace(tzinfo=None)
        except (ValueError, AttributeError):
            pass
    return datetime.now()


def process_job(job_path: Path, cfg: dict, logger: logging.Logger, dry_run: bool):
    job_name = job_path.name
    proc_path = PROCESSING_DIR / job_name

    job_path.rename(proc_path)
    logger.info("Processing %s", job_name)

    try:
        job = json.loads(proc_path.read_text())
        content_type = job.get("type", "article")
        note_dt = _resolve_note_dt(job, cfg, logger)

        if content_type != "article":
            raise ValueError(f"Unsupported content type: {content_type}")

        input_type = job["input_type"]

        if input_type == "file":
            # --- File adapter path ---
            from adapters.file import process as process_file
            payload = process_file(job, cfg)

            if dry_run:
                safe = {k: v for k, v in payload.items() if k != "body"}
                logger.info("Dry run — skipping vault write. File payload:\n%s",
                            json.dumps(safe, indent=2))
            else:
                note_path = writer.write_file_note(cfg, payload, note_dt)
                logger.info("Note written: %s", note_path)
                if cfg.get("daily_note_append", True):
                    daily_note.append_wikilink(cfg, note_path.stem, note_dt, logger)

        else:
            # --- URL / text path ---
            if input_type == "url":
                fetched = article_adapter.fetch(job["url"])
                text = fetched["text"]
                url = job["url"]
            else:
                text = job["text"]
                url = job.get("url") or ""

            # --- Claude ---
            logger.info("Calling Claude (%s)", cfg["claude_model"])
            claude_output = call_claude(cfg, content_type, url, text)

            # --- Write note ---
            if dry_run:
                logger.info("Dry run — skipping vault write. Claude output:\n%s",
                            json.dumps(claude_output, indent=2))
            else:
                note_path = writer.write_note(
                    cfg, claude_output, url, note_dt, text, content_type
                )
                logger.info("Note written: %s", note_path)
                if cfg.get("daily_note_append", True):
                    daily_note.append_wikilink(cfg, note_path.stem, note_dt, logger)

        proc_path.rename(DONE_DIR / job_name)
        logger.info("Done: %s", job_name)

    except Exception as exc:
        logger.error("Failed %s: %s", job_name, exc, exc_info=True)
        proc_path.rename(FAILED_DIR / job_name)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def run_loop(cfg: dict, logger: logging.Logger, dry_run: bool):
    jobs = sorted(INBOX_DIR.glob("*.job"))
    if not jobs:
        logger.info("No jobs in inbox.")
        return
    for job_path in jobs:
        process_job(job_path, cfg, logger, dry_run)


# ---------------------------------------------------------------------------
# --test mode
# ---------------------------------------------------------------------------

TEST_STEM = None  # computed at runtime from date


def run_test(cfg: dict, logger: logging.Logger, dry_run: bool):
    tz_name = cfg.get("timezone", "UTC")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    note_dt = datetime.now(tz=tz).replace(tzinfo=None)
    date_str = note_dt.strftime("%d-%b-%Y")
    test_stem = f"{date_str} — Test Fixture — Article"

    logger.info("TEST MODE — no network, no Haiku API calls")
    logger.info("TEST STEM: %s", test_stem)

    fake_body = (
        f"---\n"
        f"source: test\n"
        f"type: article\n"
        f"url: https://example.com/test\n"
        f"created: {datetime.now().isoformat()}\n"
        f"tags: [test, fixture]\n"
        f"status: inbox\n"
        f"summary: \"Hardcoded test fixture — no Haiku call made.\"\n"
        f"daily-note: \"[[{date_str}]]\"\n"
        f"---\n\n"
        f"# {test_stem}\n\n"
        f"This is a hardcoded test fixture. No article was fetched. "
        f"No API calls were made. Used to verify writer + daily_note append logic.\n"
    )

    vault_root = Path(cfg["obsidian_vault_path"])
    resources_path = cfg["resources_path"]
    target_dir = vault_root / resources_path / note_dt.strftime("%Y") / note_dt.strftime("%B")

    if dry_run:
        logger.info("TEST DRY-RUN — would write to: %s", target_dir / f"{test_stem}.md")
        return

    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{test_stem}.md"
    target_path.write_text(fake_body, encoding="utf-8")
    logger.info("TEST: wrote fake note: %s", target_path)

    daily_note.append_wikilink(cfg, test_stem, note_dt, logger)
    logger.info("TEST MODE complete")


def run_test_tz(cfg: dict, logger: logging.Logger):
    """Verify cross-midnight UTC→local conversion using a timestamp at 03:00 UTC today.

    03:00 UTC is always the previous calendar day in America/Denver (UTC-6/UTC-7), so
    utc_date and local_date should always differ for that timezone.
    """
    from datetime import date, timedelta
    from datetime import timezone as stdlib_tz

    tz_name = cfg.get("timezone", "UTC")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        logger.error("TEST-TZ FAILED — timezone %r not resolvable", tz_name)
        return

    today_utc_midnight = datetime.combine(date.today(), datetime.min.time()).replace(
        tzinfo=stdlib_tz.utc
    )
    ts = int((today_utc_midnight + timedelta(hours=3)).timestamp())

    fake_job = {"telegram_date": ts}
    note_dt = _resolve_note_dt(fake_job, cfg, logger)

    utc_date = datetime.fromtimestamp(ts, tz=stdlib_tz.utc).strftime("%d-%b-%Y")
    local_date = note_dt.strftime("%d-%b-%Y")

    logger.info("TEST-TZ — UTC date: %s, local date (%s): %s", utc_date, tz_name, local_date)
    if utc_date != local_date:
        logger.info("TEST-TZ PASSED — cross-midnight conversion correct (UTC %s → local %s)",
                    utc_date, local_date)
    else:
        logger.warning("TEST-TZ INCONCLUSIVE — UTC and local dates are the same (%s); "
                       "configured timezone may be east of UTC or have small offset", local_date)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Personal Intelligence Pipeline processor")
    parser.add_argument("--test", action="store_true", help="Offline fixture: write fake note + daily note append")
    parser.add_argument("--test-tz", action="store_true", help="Verify cross-midnight UTC→local date conversion")
    parser.add_argument("--dry-run", action="store_true", help="Process jobs but do not write to vault")
    args = parser.parse_args()

    cfg = load_config()
    dry_run = args.dry_run or cfg.get("dry_run", False)
    logger = setup_logging(cfg["log_max_bytes"])

    if not acquire_lock(logger):
        sys.exit(1)
    try:
        if args.test:
            run_test(cfg, logger, dry_run)
        elif args.test_tz:
            run_test_tz(cfg, logger)
        else:
            run_loop(cfg, logger, dry_run)
    finally:
        release_lock()


if __name__ == "__main__":
    main()
