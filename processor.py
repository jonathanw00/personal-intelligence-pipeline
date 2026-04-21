import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import anthropic
import yaml
from dotenv import load_dotenv

from adapters import article as article_adapter
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
  - quote: The single sharpest line from the source that best supports this point. Clean it up — remove filler, false starts, verbal tics. Preserve authentic voice. Make it scannable and memorable on first read. If no strong quote exists for a point, omit this field.

highlighted_source: The full primary article text, with each selected quote wrapped in ==double equals signs== at its exact location in the body. Highlights should correspond to the quotes used in key_points. Do not highlight anything that isn't a key_points quote.

content_type: Either "article" or "youtube"

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

    # For long content, honour full_text_threshold — pass full text or windowed text.
    # Context windowing is applied by the writer after we receive highlighted_source,
    # so we always send the full text to Haiku here (Haiku handles its own context).
    user_message = build_user_message(content_type, url or "", iso_date, text)

    response = client.messages.create(
        model=cfg["claude_model"],
        max_tokens=cfg["claude_max_tokens"],
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    raw = response.content[0].text.strip()
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Job processing
# ---------------------------------------------------------------------------


def process_job(job_path: Path, cfg: dict, logger: logging.Logger, dry_run: bool):
    job_name = job_path.name
    proc_path = PROCESSING_DIR / job_name

    job_path.rename(proc_path)
    logger.info("Processing %s", job_name)

    try:
        job = json.loads(proc_path.read_text())
        content_type = job.get("type", "article")

        if content_type != "article":
            raise ValueError(f"Unsupported content type: {content_type}")

        # --- Extract text ---
        if job["input_type"] == "url":
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
            note_path = writer.write_note(cfg, claude_output, url, job.get("received_at"))
            logger.info("Note written: %s", note_path)

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

TEST_URL = "https://paulgraham.com/greatwork.html"


def run_test(cfg: dict, logger: logging.Logger, dry_run: bool):
    logger.info("TEST MODE — URL: %s", TEST_URL)
    fake_job = {
        "input_type": "url",
        "url": TEST_URL,
        "type": "article",
        "received_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
        "chat_id": "test",
    }
    job_path = INBOX_DIR / "test_run.job"
    job_path.write_text(json.dumps(fake_job))
    process_job(job_path, cfg, logger, dry_run)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Personal Intelligence Pipeline processor")
    parser.add_argument("--test", action="store_true", help="Run a test job against a hardcoded URL")
    parser.add_argument("--dry-run", action="store_true", help="Process jobs but do not write to vault")
    args = parser.parse_args()

    cfg = load_config()
    dry_run = args.dry_run or cfg.get("dry_run", False)
    logger = setup_logging(cfg["log_max_bytes"])

    if args.test:
        run_test(cfg, logger, dry_run)
        return

    if not acquire_lock(logger):
        sys.exit(1)
    try:
        run_loop(cfg, logger, dry_run)
    finally:
        release_lock()


if __name__ == "__main__":
    main()
