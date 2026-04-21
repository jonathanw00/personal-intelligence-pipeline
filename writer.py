import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple


# Maps content_type → display suffix used in filename and frontmatter
_TYPE_SUFFIX = {
    "article": "Article",
    "youtube": "YouTube",
    "kindle": "Kindle",
}


def _format_date(dt: datetime) -> str:
    """Return DD-Mon-YYYY, e.g. 19-Apr-2026."""
    return f"{dt.day}-{dt.strftime('%b')}-{dt.strftime('%Y')}"


def _parse_received_at(received_at: Optional[str]) -> datetime:
    if received_at:
        try:
            return datetime.fromisoformat(received_at)
        except ValueError:
            pass
    return datetime.utcnow()


def _sanitise_filename(title: str) -> str:
    """Remove characters that are illegal in filenames across platforms."""
    return re.sub(r'[\\/:*?"<>|]', "", title).strip()


def _apply_highlights(source_text: str, key_points: list) -> str:
    """Find each key-point quote in source_text and wrap it in ==...==.

    Tries exact match first, then case-insensitive. Quotes that cannot be
    located are silently skipped so the source text is never corrupted.
    Applies highlights right-to-left to preserve character offsets.
    """
    positions: List[Tuple[int, int]] = []
    for kp in key_points:
        quote = kp.get("quote", "")
        if not quote:
            continue
        # Exact match
        idx = source_text.find(quote)
        if idx == -1:
            # Case-insensitive fallback
            m = re.search(re.escape(quote), source_text, re.IGNORECASE)
            idx = m.start() if m else -1
            if idx != -1:
                quote = source_text[idx: idx + len(quote)]
        if idx == -1:
            continue
        positions.append((idx, idx + len(quote)))

    # Remove overlapping spans (keep first occurrence)
    positions.sort()
    deduped: List[Tuple[int, int]] = []
    for start, end in positions:
        if deduped and start < deduped[-1][1]:
            continue
        deduped.append((start, end))

    # Insert markers right-to-left so earlier offsets stay valid
    result = source_text
    for start, end in reversed(deduped):
        result = result[:start] + "==" + result[start:end] + "==" + result[end:]
    return result


def _build_frontmatter(cfg: dict, claude_output: dict, content_type: str, url: str, date_str: str) -> str:
    tags_yaml = "\n".join(f"  - {t}" for t in claude_output["tags"])
    summary = claude_output["summary"].replace('"', '\\"')
    source_line = url if url else ""
    return (
        "---\n"
        f"created: {date_str}\n"
        f"source: {source_line}\n"
        f"type: {content_type}\n"
        f"tags:\n{tags_yaml}\n"
        "status: inbox\n"
        f'summary: "{summary}"\n'
        f'daily-note: "[[{date_str}]]"\n'
        "---"
    )


def _build_key_points(key_points: list) -> str:
    lines = []
    for kp in key_points:
        point = kp["point"]
        quote = kp.get("quote", "")
        if quote:
            lines.append(f'- {point} — "{quote}"')
        else:
            lines.append(f"- {point}")
    return "\n".join(lines)


def _word_count(text: str) -> int:
    return len(text.split())


def _context_windows(highlighted_source: str, window_words: int) -> str:
    """Return only ~window_words of context around each ==highlight==, joined by [...]."""
    highlight_pattern = re.compile(r"==.+?==", re.DOTALL)
    matches = list(highlight_pattern.finditer(highlighted_source))
    if not matches:
        return highlighted_source

    # Tokenise to word spans (preserving whitespace positions)
    word_spans = list(re.finditer(r"\S+", highlighted_source))
    if not word_spans:
        return highlighted_source

    half = window_words // 2

    # For each highlight find its word-index range, then expand by half on each side
    windows: List[Tuple[int, int]] = []
    for m in matches:
        hit = [
            i for i, w in enumerate(word_spans)
            if w.start() < m.end() and w.end() > m.start()
        ]
        if not hit:
            continue
        win_start = max(0, hit[0] - half)
        win_end = min(len(word_spans) - 1, hit[-1] + half)
        windows.append((win_start, win_end))

    if not windows:
        return highlighted_source

    # Merge overlapping / adjacent windows
    windows.sort()
    merged: List[List[int]] = [list(windows[0])]
    for start, end in windows[1:]:
        if start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    # Extract character slices and join
    segments = []
    for win_start, win_end in merged:
        char_start = word_spans[win_start].start()
        char_end = word_spans[win_end].end()
        segments.append(highlighted_source[char_start:char_end].strip())

    return "\n\n[...]\n\n".join(segments)


def _build_source_body(cfg: dict, highlighted_source: str) -> str:
    threshold = cfg.get("full_text_threshold_words", 3000)
    window_words = cfg.get("context_window_words", 200)
    if _word_count(highlighted_source) > threshold:
        return _context_windows(highlighted_source, window_words)
    return highlighted_source


def write_note(
    cfg: dict,
    claude_output: dict,
    url: str,
    received_at: Optional[str],
    source_text: str,
    content_type: str,
) -> Path:
    """Assemble and write a markdown note to the vault. Returns the note path."""
    dt = _parse_received_at(received_at)
    date_str = _format_date(dt)

    type_suffix = _TYPE_SUFFIX.get(content_type, content_type.capitalize())

    raw_title = _sanitise_filename(claude_output["filename_title"])
    filename = f"{date_str} — {raw_title} — {type_suffix}.md"

    highlighted = _apply_highlights(source_text, claude_output.get("key_points", []))

    frontmatter = _build_frontmatter(cfg, claude_output, content_type, url, date_str)
    summary_section = f"## Summary\n\n{claude_output['summary']}"
    key_points_section = f"## Key points\n\n{_build_key_points(claude_output['key_points'])}"
    source_body = _build_source_body(cfg, highlighted)

    note = (
        f"{frontmatter}\n\n"
        f"{summary_section}\n\n"
        f"{key_points_section}\n\n"
        "---\n\n"
        f"{source_body}\n"
    )

    vault_root = Path(cfg["obsidian_vault_path"])
    resources_dir = vault_root / cfg["resources_path"] / dt.strftime("%Y") / dt.strftime("%B")
    resources_dir.mkdir(parents=True, exist_ok=True)

    note_path = resources_dir / filename
    note_path.write_text(note, encoding="utf-8")
    return note_path
