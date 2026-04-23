import re
from datetime import datetime
from pathlib import Path
from typing import List, Tuple
from urllib.parse import urlparse


PUBLICATION_MAP = {
    "wsj.com": "Wall Street Journal",
    "nytimes.com": "New York Times",
    "economist.com": "The Economist",
    "nationalreview.com": "National Review",
    "deseretnews.com": "Deseret News",
    "churchofjesuschrist.org": "Church of Jesus Christ",
    "theatlantic.com": "The Atlantic",
    "newyorker.com": "The New Yorker",
}


def _derive_publication(source_url: str) -> str:
    if not source_url:
        return ""
    try:
        netloc = urlparse(source_url).netloc.lower()
    except Exception:
        return ""
    if not netloc:
        return ""
    if netloc.startswith("www."):
        netloc = netloc[4:]
    for domain, name in PUBLICATION_MAP.items():
        if netloc == domain or netloc.endswith("." + domain):
            return name
    if netloc.endswith(".substack.com"):
        subdomain = netloc[: -len(".substack.com")]
        return subdomain.replace("-", " ").title()
    first = netloc.split(".")[0]
    return first.replace("-", " ").title()


# Maps content_type → display suffix used in filename and frontmatter
_TYPE_SUFFIX = {
    "article": "Article",
    "youtube": "YouTube",
    "kindle": "Kindle",
}


def _format_date(dt: datetime) -> str:
    """Return DD-Mon-YYYY, e.g. 19-Apr-2026."""
    return dt.strftime("%d-%b-%Y")


def _sanitise_filename(title: str) -> str:
    """Remove characters that are illegal in filenames across platforms."""
    return re.sub(r'[\\/:*?"<>|]', "", title).strip()


def _quote_markers(quote_style: str) -> Tuple[str, str]:
    """Return (open, close) markers for the configured quote style."""
    if quote_style == "bold":
        return ("**", "**")
    return ("==", "==")


def _apply_highlights(source_text: str, key_points: list, quote_style: str) -> str:
    """Find each key-point quote in source_text and wrap it with the configured markers.

    Tries exact match first, then case-insensitive. Quotes that cannot be
    located are silently skipped so the source text is never corrupted.
    Applies markers right-to-left to preserve character offsets.
    """
    open_m, close_m = _quote_markers(quote_style)
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
        result = result[:start] + open_m + result[start:end] + close_m + result[end:]
    return result


def _build_frontmatter(
    cfg: dict,
    claude_output: dict,
    content_type: str,
    url: str,
    created_str: str,
    note_date_str: str,
) -> str:
    tags_yaml = "\n".join(f"  - {t}" for t in claude_output["tags"])
    summary = claude_output["summary"].replace('"', '\\"')
    source_line = url if url else ""
    publication = _derive_publication(source_line)
    return (
        "---\n"
        f"created: {created_str}\n"
        f"source: {source_line}\n"
        f"publication: {publication}\n"
        f"type: {content_type}\n"
        f"tags:\n{tags_yaml}\n"
        "status: inbox\n"
        "value:\n"
        "character:\n"
        f'summary: "{summary}"\n'
        f'daily-note: "[[{note_date_str}]]"\n'
        "---"
    )


def _build_key_points(key_points: list, quote_style: str) -> str:
    open_m, close_m = _quote_markers(quote_style)
    lines = []
    for kp in key_points:
        point = kp["point"]
        quote = kp.get("quote", "")
        if quote:
            lines.append(f"- {point} — {open_m}{quote}{close_m}")
        else:
            lines.append(f"- {point}")
    return "\n".join(lines)


def _word_count(text: str) -> int:
    return len(text.split())


def _context_windows(highlighted_source: str, window_words: int, quote_style: str) -> str:
    """Return only ~window_words of context around each marked quote, joined by [...]."""
    open_m, close_m = _quote_markers(quote_style)
    marker_pat = re.escape(open_m) + r".+?" + re.escape(close_m)
    highlight_pattern = re.compile(marker_pat, re.DOTALL)
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


def _build_source_body(cfg: dict, highlighted_source: str, quote_style: str) -> str:
    threshold = cfg.get("full_text_threshold_words", 3000)
    window_words = cfg.get("context_window_words", 200)
    if _word_count(highlighted_source) > threshold:
        return _context_windows(highlighted_source, window_words, quote_style)
    return highlighted_source


def write_note(
    cfg: dict,
    claude_output: dict,
    url: str,
    note_dt: datetime,
    source_text: str,
    content_type: str,
) -> Path:
    """Assemble and write a markdown note to the vault. Returns the note path."""
    note_date_str = _format_date(note_dt)
    created_str = _format_date(datetime.now())

    type_suffix = _TYPE_SUFFIX.get(content_type, content_type.capitalize())

    raw_title = _sanitise_filename(claude_output["filename_title"])
    filename = f"{note_date_str} — {raw_title} — {type_suffix}.md"

    quote_style = cfg.get("quote_style", "highlight")
    key_points = claude_output.get("key_points", [])
    highlighted = _apply_highlights(source_text, key_points, quote_style)

    frontmatter = _build_frontmatter(cfg, claude_output, content_type, url, created_str, note_date_str)
    summary_section = f"## Summary\n\n{claude_output['summary']}"
    key_points_section = f"## Key points\n\n{_build_key_points(key_points, quote_style)}"
    source_body = _build_source_body(cfg, highlighted, quote_style)

    note = (
        f"{frontmatter}\n\n"
        f"{summary_section}\n\n"
        f"{key_points_section}\n\n"
        "---\n\n"
        f"{source_body}\n"
    )

    vault_root = Path(cfg["obsidian_vault_path"])
    resources_dir = vault_root / cfg["resources_path"] / note_dt.strftime("%Y") / note_dt.strftime("%B")
    resources_dir.mkdir(parents=True, exist_ok=True)

    note_path = resources_dir / filename
    note_path.write_text(note, encoding="utf-8")
    return note_path


# ---------------------------------------------------------------------------
# File-type note writer (input_type: "file" — Markdownload clippings)
# ---------------------------------------------------------------------------


def _build_file_frontmatter(payload: dict, created_str: str, note_date_str: str) -> str:
    tags = payload.get("tags", [])
    tags_str = ("tags:\n" + "\n".join(f"  - {t}" for t in tags)) if tags else "tags: []"
    summary = payload.get("summary", "").replace('"', '\\"')
    source = payload.get("source", "")
    publication = _derive_publication(source)
    author = payload.get("author", "")
    title = payload.get("title", "").replace('"', '\\"')
    captured = payload.get("captured", note_date_str)
    return (
        "---\n"
        f"created: {created_str}\n"
        f'captured: "[[{captured}]]"\n'
        f"source: {source}\n"
        f"publication: {publication}\n"
        f"author: {author}\n"
        f'title: "{title}"\n'
        "type: article\n"
        f"{tags_str}\n"
        "status: inbox\n"
        "value:\n"
        "character:\n"
        f'summary: "{summary}"\n'
        f'daily-note: "[[{note_date_str}]]"\n'
        "---"
    )


def write_file_note(cfg: dict, payload: dict, note_dt: datetime) -> Path:
    """Assemble and write a file-intake note to the vault. Returns the note path."""
    note_date_str = _format_date(note_dt)
    created_str = _format_date(datetime.now())

    raw_title = _sanitise_filename(payload["title"])
    filename = f"{note_date_str} — {raw_title} — Article.md"

    quote_style = cfg.get("quote_style", "highlight")
    key_points = payload.get("key_points", [])
    source_text = payload.get("body", "")
    highlighted = _apply_highlights(source_text, key_points, quote_style)

    frontmatter = _build_file_frontmatter(payload, created_str, note_date_str)
    summary_section = f"## Summary\n\n{payload.get('summary', '')}"
    key_points_section = f"## Key points\n\n{_build_key_points(key_points, quote_style)}"
    source_body = _build_source_body(cfg, highlighted, quote_style)

    note = (
        f"{frontmatter}\n\n"
        f"{summary_section}\n\n"
        f"{key_points_section}\n\n"
        "---\n\n"
        f"{source_body}\n"
    )

    vault_root = Path(cfg["obsidian_vault_path"])
    resources_dir = vault_root / cfg["resources_path"] / note_dt.strftime("%Y") / note_dt.strftime("%B")
    resources_dir.mkdir(parents=True, exist_ok=True)

    note_path = resources_dir / filename
    note_path.write_text(note, encoding="utf-8")
    return note_path
