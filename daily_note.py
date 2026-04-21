import logging
import re
from datetime import datetime
from pathlib import Path


def _format_date(dt: datetime) -> str:
    return dt.strftime("%d-%b-%Y")


def append_wikilink(cfg: dict, note_stem: str, logger: logging.Logger) -> None:
    """Append a wikilink line to today's daily note. Failures are logged but not re-raised."""
    try:
        _append(cfg, note_stem, logger)
    except Exception as exc:
        logger.error("Daily note append failed (non-fatal): %s", exc, exc_info=True)


def _append(cfg: dict, note_stem: str, logger: logging.Logger) -> None:
    vault_root = Path(cfg["obsidian_vault_path"])
    base_path = cfg.get("daily_note_path", "Journal/2020's")
    heading_name = cfg.get("daily_note_heading", "Articles")
    emoji = cfg.get("daily_note_emoji", "\U0001f4da")  # 📚

    now = datetime.now()
    date_str = _format_date(now)
    note_dir = vault_root / base_path / now.strftime("%Y") / now.strftime("%B")
    note_file = note_dir / f"{date_str}.md"

    wikilink_line = f"{emoji} [[{note_stem}]]"

    # --- Create stub if daily note does not exist ---
    if not note_file.exists():
        note_dir.mkdir(parents=True, exist_ok=True)
        note_file.write_text(f"# {date_str}\n\n{wikilink_line}\n", encoding="utf-8")
        logger.info("Daily note created: %s", note_file.name)
        return

    content = note_file.read_text(encoding="utf-8")
    lines = content.splitlines(keepends=True)

    # --- Duplicate check ---
    for line in lines:
        if line.strip() == wikilink_line:
            logger.warning(
                "Daily note append skipped — wikilink already present: %s", wikilink_line
            )
            return

    # --- Locate ## Articles heading ---
    heading_re = re.compile(
        r"^##\s+" + re.escape(heading_name) + r"\s*$", re.IGNORECASE
    )
    section_stop_re = re.compile(r"^#{1,2}\s")

    heading_idx = None
    for i, line in enumerate(lines):
        if heading_re.match(line.rstrip("\n\r")):
            heading_idx = i
            break

    new_line = wikilink_line + "\n"

    if heading_idx is not None:
        # Find end of section: next # or ## heading, or EOF
        insert_idx = len(lines)
        for i in range(heading_idx + 1, len(lines)):
            if section_stop_re.match(lines[i]):
                insert_idx = i
                break
        lines.insert(insert_idx, new_line)
        note_file.write_text("".join(lines), encoding="utf-8")
    else:
        # No ## Articles heading — append at bottom with one blank line
        raw = content.rstrip("\n") + "\n\n" + new_line
        note_file.write_text(raw, encoding="utf-8")

    logger.info("Daily note updated: %s — %s", note_file.name, wikilink_line)
