import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Tuple
from urllib.parse import urlparse

import anthropic
import yaml

logger = logging.getLogger("pipeline")

FRONTMATTER_PATTERN = re.compile(r"^---\n(.*?)\n---\n?(.*)", re.DOTALL)
IMAGE_RE = re.compile(r"^!\[[^\]]*\]\([^)]+\)\s*$")
CAPTION_KEEP_RE = re.compile(r"^(?:[#\-\*\+>]|\d+[.)])")

PUBLICATION_NAMES = {
    "wsj": "Wall Street Journal",
    "nyt": "New York Times",
    "economist": "The Economist",
    "generic": "",
}

HAIKU_PROMPT = (
    'You are processing a clipped web article for a personal knowledge base.\n\n'
    'The article body is provided below. Existing metadata: title="{title}", '
    'source="{source}", author="{author}".\n\n'
    "Generate the following in valid JSON only (no preamble, no code fences):\n\n"
    "{{\n"
    '  "summary": "1-3 sentence summary of the article\'s core argument or finding",\n'
    '  "tags": ["4-6 kebab-case tags", "..."],\n'
    '  "key_quotes": ["1-3 verbatim pull quotes from the article that capture key ideas'
    ' — strings only, no attribution"]\n'
    "}}\n\n"
    "Article body:\n---\n{body}\n---"
)


# ---------------------------------------------------------------------------
# H1 extraction (handles multi-line titles)
# ---------------------------------------------------------------------------


def extract_h1_multiline(body: str) -> Tuple[Optional[str], str]:
    """Extract H1 title (handling multi-line H1s) and return (title, body_without_h1).

    Returns (None, body) if no H1 found.
    """
    lines = body.splitlines(keepends=True)
    h1_start = None
    for i, line in enumerate(lines):
        if line.startswith("# ") and not line.startswith("## "):
            h1_start = i
            break
    if h1_start is None:
        return None, body

    h1_lines = [lines[h1_start][2:]]  # strip "# "
    consumed = h1_start + 1
    while consumed < len(lines):
        nxt = lines[consumed]
        if not nxt.strip():
            break
        if nxt.lstrip().startswith("#"):
            break
        h1_lines.append(nxt)
        consumed += 1

    title = " ".join(part.strip() for part in h1_lines)
    title = re.sub(r"\s+", " ", title).strip()

    new_body = "".join(lines[:h1_start] + lines[consumed:])
    return title, new_body


# ---------------------------------------------------------------------------
# Publication detection
# ---------------------------------------------------------------------------


def detect_publication(source_url: str) -> str:
    if not source_url:
        return "generic"
    domain = urlparse(source_url).netloc.lower()
    if "wsj.com" in domain:
        return "wsj"
    if "nytimes.com" in domain:
        return "nyt"
    if "economist.com" in domain:
        return "economist"
    return "generic"


# ---------------------------------------------------------------------------
# Author normalisation (handles YAML lists and Obsidian wikilinks)
# ---------------------------------------------------------------------------


def _normalize_author(raw) -> str:
    if not raw:
        return ""
    if isinstance(raw, list):
        parts = [_normalize_author(a) for a in raw]
        return ", ".join(p for p in parts if p)
    s = str(raw).strip()
    m = re.match(r"\[\[([^|\]]+)(?:\|[^\]]+)?\]\]", s)
    if m:
        return m.group(1).strip()
    return s


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------


def parse_markdownload_created(created_val) -> Optional[datetime]:
    """Parse Markdownload's created field to a datetime.

    Handles:
    - datetime objects (YAML auto-parsed)
    - date objects (YAML auto-parsed plain date)
    - strings like '2026-04-22T12:16:02 (UTC -06:00)'
    Returns None if unparseable.
    """
    if created_val is None:
        return None
    if isinstance(created_val, datetime):
        return created_val
    if isinstance(created_val, date):
        return datetime.combine(created_val, datetime.min.time())
    try:
        s = str(created_val).strip()
        iso_part, _, tz_part = s.partition(" (")
        if tz_part:
            tz_part = tz_part.rstrip(")")
            sign = 1 if "+" in tz_part else -1
            offset_str = tz_part.split()[-1].lstrip("+-")
            hours, minutes = map(int, offset_str.split(":"))
            offset = timezone(sign * timedelta(hours=hours, minutes=minutes))
            return datetime.fromisoformat(iso_part).replace(tzinfo=offset)
        return datetime.fromisoformat(iso_part)
    except (ValueError, AttributeError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Cleanup functions
# ---------------------------------------------------------------------------


def strip_image_blocks(body: str) -> str:
    """Remove image lines and their immediately-following caption line."""
    lines = body.split("\n")
    result = []
    skip_next_content = False
    for line in lines:
        if skip_next_content:
            if line.strip():
                skip_next_content = False
                stripped = line.strip()
                if not CAPTION_KEEP_RE.match(stripped):
                    continue  # caption — drop it
            result.append(line)
            continue
        if IMAGE_RE.match(line.strip()):
            skip_next_content = True
            continue
        result.append(line)
    return "\n".join(result)


def strip_excerpt_separator(body: str) -> str:
    """Remove standalone --- lines (Markdownload excerpt separator)."""
    return re.sub(r"^---\s*$", "", body, flags=re.MULTILINE)


def strip_html_small_tags(body: str) -> str:
    """Replace <small>X</small> with X (Economist quirk)."""
    return re.sub(r"<small>(.*?)</small>", r"\1", body, flags=re.DOTALL)


def strip_inline_links(body: str) -> str:
    """Replace [text](url) with text, leaving plain prose."""
    return re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", body)


def strip_wsj_boilerplate(body: str) -> str:
    """Remove WSJ copyright lines, fingerprint hashes, print edition notices, and ad markers."""
    lines = body.split("\n")
    cleaned = []
    for line in lines:
        if re.search(r"Copyright\s*©\d{4}", line):
            continue
        if re.match(r"^[a-f0-9]{32}\s*$", line):
            continue
        if re.search(r"Appeared in the.+print edition", line, re.IGNORECASE):
            continue
        if line.strip() == "Advertisement":
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


def strip_economist_newsletter(body: str) -> str:
    """Truncate at the Economist newsletter signup heading."""
    match = re.search(r"\n## Sign up to our", body, re.IGNORECASE)
    if match:
        return body[: match.start()]
    return body


NYT_BOILERPLATE_PATTERNS = [
    re.compile(r"^Advertisement\s*$", re.MULTILINE),
    re.compile(r"^SKIP ADVERTISEMENT\s*$", re.MULTILINE),
    re.compile(r"^Listen\s*$", re.MULTILINE),
    re.compile(r"^·\s*\d+:\d+\s*min\s*$", re.MULTILINE),
    re.compile(
        r"^[A-Z][a-z]+ \d{1,2}, \d{4}Updated \d{1,2}:\d{2}\s*[ap]\.m\.\s*ET\s*$",
        re.MULTILINE,
    ),
]


def strip_nyt_boilerplate(body: str) -> str:
    for pat in NYT_BOILERPLATE_PATTERNS:
        body = pat.sub("", body)
    return body


def collapse_blank_lines(body: str) -> str:
    """Collapse 3+ consecutive blank lines to 2."""
    return re.sub(r"\n{3,}", "\n\n", body)


def clean_body(body: str, publication: str) -> str:
    body = strip_image_blocks(body)
    body = strip_excerpt_separator(body)
    body = strip_html_small_tags(body)
    body = strip_inline_links(body)
    if publication == "wsj":
        body = strip_wsj_boilerplate(body)
    elif publication == "economist":
        body = strip_economist_newsletter(body)
    elif publication == "nyt":
        body = strip_nyt_boilerplate(body)
    body = collapse_blank_lines(body)
    return body.strip()


# ---------------------------------------------------------------------------
# Haiku enrichment
# ---------------------------------------------------------------------------


def _call_haiku(cfg: dict, title: str, source: str, author: str, body: str) -> dict:
    prompt = HAIKU_PROMPT.format(title=title, source=source, author=author, body=body)
    logger.info("Calling Haiku for enrichment (%d chars)", len(body))
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=cfg["claude_model"],
        max_tokens=cfg["claude_max_tokens"],
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    # Strip markdown fences if present
    cleaned = raw
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Haiku JSON parse failed — using empty defaults. Raw: %r", raw[:200])
        return {"summary": "", "tags": [], "key_quotes": []}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def process(job: dict, cfg: dict) -> dict:
    """Process a file-type job. Returns a payload dict writer.write_file_note expects."""
    filename = job.get("filename", "unnamed.md")
    content = job.get("content", "")
    logger.info("Processing file job: %s", filename)

    # Step 1 — Parse frontmatter
    match = FRONTMATTER_PATTERN.match(content)
    if match:
        try:
            metadata = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            logger.warning("Frontmatter YAML parse failed for %s — treating as body-only", filename)
            metadata = {}
        body = match.group(2)
    else:
        logger.warning("No frontmatter found in %s — treating entire content as body", filename)
        metadata = {}
        body = content

    # Step 2 — Extract H1 title, remove from body
    title, body = extract_h1_multiline(body)
    if title is None:
        stem = os.path.splitext(filename)[0]
        title = stem.replace("-", " ").replace("_", " ")
        if metadata.get("title"):
            title = str(metadata["title"]).strip()

    # Step 3 — Resolve capture date
    captured_dt = parse_markdownload_created(metadata.get("created"))
    if captured_dt:
        captured_str = captured_dt.strftime("%d-%b-%Y")
    else:
        received_at = job.get("received_at", "")
        try:
            captured_str = datetime.fromisoformat(received_at).strftime("%d-%b-%Y")
        except (ValueError, TypeError):
            captured_str = datetime.now().strftime("%d-%b-%Y")

    # Step 4 — Detect publication and clean body
    source = str(metadata.get("source", "")).strip()
    publication = detect_publication(source)
    logger.info("Publication: %s", publication)
    cleaned_body = clean_body(body, publication)

    # Step 5 — Normalise author
    raw_author = metadata.get("author", "")
    author = _normalize_author(raw_author)
    if not author:
        author = PUBLICATION_NAMES.get(publication, "")

    # Step 6 — Call Haiku
    haiku = _call_haiku(cfg, title, source, author, cleaned_body)

    return {
        "type": "article",
        "title": title,
        "source": source,
        "author": author,
        "captured": captured_str,
        "summary": haiku.get("summary", ""),
        "tags": haiku.get("tags", []),
        "key_quotes": haiku.get("key_quotes", []),
        "body": cleaned_body,
        "status": "inbox",
    }
