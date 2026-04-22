# FILE_ADAPTER_SPEC.md

**Project:** Personal Intelligence Pipeline — File Intake Subsystem
**Status:** Design locked; ready for Claude Code build
**Phase:** Extends Phase 2 of `BOT_SPEC.md`
**Build target:** Windows machine (Claude Code), then `git pull` on NAS for testing

---

## 1. Purpose

Add a third intake channel to the pipeline: **`.md` and `.txt` files sent as Telegram document attachments**. This solves the paywall problem (subscribed-content articles that can't be scraped from URLs) and provides reusable plumbing for future bulk imports (Readwise backlog, Kindle highlights).

The current `bot.py` ignores `message.document` entirely — files arrive, the bot consumes the update, and nothing happens (no job written, no reply sent). This spec fixes that and adds a new adapter that produces enriched Obsidian notes from clipped article content.

---

## 2. Architecture overview

```
Browser (Markdownload extension, configured per §3)
   ↓ saves .md to disk
   ↓
Telegram desktop/mobile (drag/share .md to bot)
   ↓ message.document arrives
   ↓
bot.py (extended per §4)
   ↓ getFile → download bytes → write inbox/*.job with input_type: "file"
   ↓
processor.py (routing change per §6)
   ↓ input_type == "file" → adapters/file.py
   ↓
adapters/file.py (new — per §5)
   ↓ parse frontmatter → clean body → reformat dates → call Haiku → return enriched payload
   ↓
writer.py (extended per §7)
   ↓ merge Markdownload frontmatter + Haiku enrichment + schema additions
   ↓ write Resources/YYYY/Month/<title>.md (full body preserved)
   ↓
Daily note append (📚 wikilink) — unchanged from existing pattern
```

---

## 3. Markdownload configuration (one-time manual setup)

**This is a user task, not a code task.** Document it clearly in the spec but Jon will perform it in his browser.

In Markdownload extension options (Chrome/Firefox/Safari), set the **Front Matter Template** to:

```
---
created: {date:YYYY-MM-DDTHH:mm:ss} ({date:Z})
tags: []
source: {baseURI}
author: {byline}
---

# {pageTitle}

> ## Excerpt
> {excerpt}

---
```

**Back Matter Template:** leave empty.

**Why this template:**
- `created` with timezone offset → adapter parses, applies offset, derives `captured` wikilink in Jon's local time
- `source` → canonical URL (this was missing in the pre-config samples)
- `author` → resolves co-bylines correctly when site provides schema.org markup
- `tags: []` → empty placeholder; adapter overwrites with Haiku-generated tags
- H1 title and excerpt blockquote → preserved in body for visual skimming in Obsidian

---

## 4. Changes to `bot.py`

### 4.1 New code path: handle `message.document`

In the message handler, **before** the existing text/URL routing logic, check for `message.document`:

```python
if "document" in message:
    handle_document(message, config)
    return
```

### 4.2 New function: `handle_document(message, config)`

**Responsibilities:**

1. Extract `file_id`, `file_name`, `file_size`, `mime_type` from `message["document"]`
2. **Validate extension:** filename must end in `.md` or `.txt` (case-insensitive). Otherwise reply: `❌ Unsupported file type. Send .md or .txt files only.` and return.
3. **Validate size:** reject if `file_size > 5 * 1024 * 1024` (5MB). Reply: `❌ File too large (max 5MB).` and return.
4. **Fetch file path** via Telegram's `getFile` endpoint:
   - `GET https://api.telegram.org/bot<TOKEN>/getFile?file_id=<file_id>`
   - Parse response → `result.file_path`
5. **Download file bytes:**
   - `GET https://api.telegram.org/file/bot<TOKEN>/<file_path>`
   - Decode as UTF-8
6. **Write job to `inbox/`:**

```json
{
  "input_type": "file",
  "filename": "<original filename>",
  "content": "<decoded UTF-8 file contents>",
  "type": "article",
  "received_at": "<ISO timestamp local time>",
  "telegram_date": <unix timestamp from message>,
  "chat_id": "<chat_id>"
}
```

   - Job filename pattern: match the existing pattern from URL/text jobs (use the same naming convention `processor.py` already expects)
7. **Trigger processor** the same way URL/text jobs do (existing pattern in `bot.py`)
8. **Confirmation reply** flow: identical to existing — find newest `.md` written after `before` timestamp, reply `✅ Saved: [[filename]]`

### 4.3 Telegram API calls

Use `urllib.request` only (no new dependencies — matches existing bot.py pattern).

### 4.4 Logging

Log at INFO level: `Document received: <filename> (<size> bytes)` on entry; `Job written: <job_path>` on success; appropriate ERROR-level logs on validation failures.

---

## 5. New file: `adapters/file.py`

### 5.1 Module purpose

Process a job with `input_type: "file"` — parse Markdownload frontmatter, clean the body, reformat dates, call Haiku for enrichment, and return a payload `writer.py` can persist.

### 5.2 Public function

```python
def process(job: dict, config: dict) -> dict:
    """
    Process a file-type job.

    Returns a dict with the structure writer.py expects (matches existing
    article adapter return shape, plus 'body' field for option-(b) preservation).
    """
```

### 5.3 Processing pipeline (in order)

**Step 1 — Parse frontmatter**

Use `PyYAML` (`pyyaml` — add to `requirements.txt` if not already present).

```python
import yaml
import re

FRONTMATTER_PATTERN = re.compile(r'^---\n(.*?)\n---\n(.*)$', re.DOTALL)

match = FRONTMATTER_PATTERN.match(content)
if not match:
    # Fallback: treat entire content as body, no frontmatter
    metadata = {}
    body = content
else:
    metadata = yaml.safe_load(match.group(1)) or {}
    body = match.group(2)
```

**Step 2 — Extract H1 as title, remove from body**

```python
H1_PATTERN = re.compile(r'^# (.+?)\n', re.MULTILINE)
h1_match = H1_PATTERN.search(body)
if h1_match:
    title = h1_match.group(1).strip()
    body = body[:h1_match.start()] + body[h1_match.end():]
else:
    # Fallback: derive title from filename (strip extension, replace dashes with spaces)
    title = os.path.splitext(job["filename"])[0].replace("-", " ").replace("_", " ")
```

**Step 3 — Reformat dates**

Parse Markdownload's `created` field (ISO timestamp + timezone offset like `2026-04-22T12:16:02 (UTC -06:00)`):

```python
from datetime import datetime, timezone, timedelta

def parse_markdownload_created(created_str: str) -> datetime:
    """Parse '2026-04-22T12:16:02 (UTC -06:00)' → timezone-aware datetime in local time."""
    # Extract timestamp and offset
    iso_part, _, tz_part = created_str.partition(" (")
    tz_part = tz_part.rstrip(")")
    # tz_part now looks like "UTC -06:00" or "UTC +05:30"
    sign = 1 if "+" in tz_part else -1
    hours, minutes = map(int, tz_part.split()[1].lstrip("+-").split(":"))
    offset = timezone(sign * timedelta(hours=hours, minutes=minutes))
    dt = datetime.fromisoformat(iso_part).replace(tzinfo=offset)
    return dt

dt = parse_markdownload_created(metadata.get("created", ""))
captured_str = dt.strftime("%d-%b-%Y")  # "22-Apr-2026"
```

If `created` field is missing or unparseable, fall back to `job["received_at"]` (the bot's local timestamp).

**Step 4 — Detect publication and apply cleanup**

```python
def detect_publication(source_url: str) -> str:
    """Return 'wsj', 'nyt', 'economist', or 'generic'."""
    if not source_url:
        return "generic"
    domain = urlparse(source_url).netloc.lower()
    if "wsj.com" in domain: return "wsj"
    if "nytimes.com" in domain: return "nyt"
    if "economist.com" in domain: return "economist"
    return "generic"
```

**Cleanup functions (apply in order):**

```python
def clean_body(body: str, publication: str) -> str:
    body = strip_image_blocks(body)           # all sources
    body = strip_excerpt_separator(body)      # the standalone "---" after excerpt block
    body = strip_html_small_tags(body)        # economist
    body = strip_inline_links(body)           # all sources — per Jon's decision
    if publication == "wsj":
        body = strip_wsj_boilerplate(body)
    elif publication == "economist":
        body = strip_economist_newsletter(body)
    body = collapse_blank_lines(body)
    return body.strip()
```

**Cleanup function specs:**

- **`strip_image_blocks`**: Remove `![...](...)` markdown image lines. Also remove the immediately-following caption line (heuristic: next non-empty line if it's not a heading and doesn't start with a markdown list marker).

- **`strip_excerpt_separator`**: Remove standalone `---` lines that appear after the excerpt blockquote and before the body proper.

- **`strip_html_small_tags`**: Replace `<small>X</small>` with `X` (Economist quirk).

- **`strip_inline_links`**: Replace `[text](url)` with `text`. Per Jon's locked decision: body is clean prose only.

- **`strip_wsj_boilerplate`**: Remove lines matching:
  - `Copyright ©\d{4}.*All Rights Reserved\.?`
  - `^[a-f0-9]{32}$` (article fingerprint hash)
  - `Appeared in the.*print edition.*`
  - Author bio block at end: per Jon's locked decision, **keep** the author bio.

- **`strip_economist_newsletter`**: Remove the newsletter signup block at the end. Heuristic: find the last `![bartleby]` or similar newsletter ident image and truncate everything from that point onward, including the `## Sign up to our...` heading.

- **`collapse_blank_lines`**: Collapse 3+ consecutive blank lines to 2.

**Step 5 — Call Haiku for enrichment**

Use the same Haiku client setup as `adapters/article.py`. Prompt:

```
You are processing a clipped web article for a personal knowledge base.

The article body is provided below. Existing metadata: title="{title}", source="{source}", author="{author}".

Generate the following in valid JSON only (no preamble, no code fences):

{
  "summary": "1-3 sentence summary of the article's core argument or finding",
  "tags": ["4-6 kebab-case tags", "..."],
  "key_quotes": ["1-3 verbatim pull quotes from the article that capture key ideas — strings only, no attribution"]
}

Article body:
---
{cleaned_body}
---
```

Use `claude_model` and `claude_max_tokens` from `config.yaml`.

Parse Haiku response; if JSON parse fails, log error and use empty defaults (`summary: ""`, `tags: []`, `key_quotes: []`) — fail soft, never crash the pipeline.

**Step 6 — Build return payload**

```python
return {
    "type": "article",
    "title": title,
    "source": metadata.get("source", ""),
    "author": metadata.get("author", "") or detect_publication(metadata.get("source", "")).upper(),
    "captured": captured_str,         # "22-Apr-2026"
    "summary": haiku_result["summary"],
    "tags": haiku_result["tags"],
    "key_quotes": haiku_result["key_quotes"],
    "body": cleaned_body,             # full article preserved per option-(b)
    "status": "inbox",
}
```

### 5.4 Author fallback

If `author` field is empty or missing in Markdownload's frontmatter, fall back to publication name from URL (e.g. `New York Times`, `Wall Street Journal`, `The Economist`). Map:

```python
PUBLICATION_NAMES = {
    "wsj": "Wall Street Journal",
    "nyt": "New York Times",
    "economist": "The Economist",
    "generic": "",   # leave blank rather than guess
}
```

### 5.5 Logging

- INFO on entry: `Processing file job: {filename}`
- INFO on publication detection: `Publication: {publication}`
- INFO on Haiku call: `Calling Haiku for enrichment ({len(body)} chars)`
- WARNING if frontmatter parse fails (fallback path taken)
- WARNING if Haiku JSON parse fails (defaults used)
- ERROR + raise on truly unrecoverable issues

---

## 6. Changes to `processor.py`

### 6.1 Routing

In the existing job-routing logic (where `input_type` is dispatched), add:

```python
elif job["input_type"] == "file":
    from adapters.file import process as process_file
    payload = process_file(job, config)
```

The payload returned matches the existing article adapter's structure with one addition: a `body` field carrying the full cleaned article text. Pass the payload to `writer.py` exactly as the existing path does.

### 6.2 No other changes required

The lock file, log rotation, daily note append, and Telegram confirmation flow all work unchanged.

---

## 7. Changes to `writer.py`

### 7.1 Handle the `body` field

The existing writer takes a payload and produces frontmatter + standardized body sections (summary, key quotes). For `input_type: "file"` jobs, the payload now contains a `body` field with the full cleaned article.

**Required change:** if `payload.get("body")` is present, append it as a new section after the existing summary/quotes sections:

```markdown
---
{frontmatter}
---

## Summary

{summary}

## Key Quotes

> {quote 1}

> {quote 2}

## Article

{body}
```

If `body` is absent (URL-path jobs), behave exactly as before — no `## Article` section.

### 7.2 Frontmatter additions

The frontmatter for file-type notes should include:

```yaml
---
created: 22-Apr-2026
captured: "[[22-Apr-2026]]"
source: <url>
author: <name or publication>
type: article
status: inbox
title: "<title>"
tags:
  - tag-one
  - tag-two
summary: "<one-sentence summary>"
daily-note: "[[22-Apr-2026]]"
---
```

Note: `created` matches the existing schema convention (DD-Mmm-YYYY plain string). `captured` is new — wikilink pointing to the daily note for the day of capture. `daily-note` is preserved for compatibility with the existing daily-note-append logic.

### 7.3 Filename convention

Use the existing filename convention from `writer.py` for URL-path articles. Do not alter — keep one rule for all article-type notes.

---

## 8. Changes to `requirements.txt`

Add (if not already present):

```
pyyaml>=6.0
```

(Markdownload may already pull this in transitively for other adapters; check before adding.)

---

## 9. Changes to spec docs

Update `BOT_SPEC.md` (or `PIPELINE_SPEC.md`, whichever is the canonical handoff doc):

- Add `input_type: "file"` to the documented job schema
- Add the `handle_document` flow to the bot.py section
- Add a reference to `adapters/file.py` and this spec document

---

## 10. Test cases

### 10.1 Test fixtures

Place these in `tests/fixtures/` (create the directory if it doesn't exist):

1. **`wsj_apple_ternus.md`** — pre-Markdownload-config WSJ sample (no frontmatter, tests fallback paths)
2. **`nyt_mythos_no_frontmatter.md`** — pre-config NYT sample (no frontmatter, no author, no date)
3. **`nyt_mythos_with_frontmatter.md`** — post-config NYT sample (full frontmatter, co-byline, embedded images)
4. **`economist_neoprimes.md`** — pre-config Economist sample (no frontmatter, has `<small>` tags, newsletter footer)

(Jon has the source files for all four — copy them into the fixtures folder before running tests.)

### 10.2 Required test assertions

For each fixture, the adapter should:

1. Return a dict with all required keys (`type`, `title`, `source`, `author`, `captured`, `summary`, `tags`, `key_quotes`, `body`, `status`)
2. Produce a `captured` string in `DD-Mmm-YYYY` format
3. Produce a `body` string with no `![...]` image markdown
4. Produce a `body` string with no `<small>` tags
5. Produce a `body` string with no `[text](url)` inline links
6. Produce 4-6 tags
7. Produce a non-empty summary

### 10.3 Integration test

After unit tests pass, do a manual end-to-end test on the NAS:

1. Send the post-config NYT sample as a `.md` document attachment to the Telegram bot
2. Verify within ~60 seconds:
   - Confirmation message appears in Telegram with `✅ Saved: [[...]]`
   - Note appears in `Resources/YYYY/Month/`
   - Daily note for today has 📚 entry linking to the new note
   - Frontmatter has all required fields
   - Body has full article text, no images, no `<small>` tags, no inline links

---

## 11. Build order recommendation

Build in this order to minimize blast radius if something breaks:

1. **`adapters/file.py`** first — most complex, fully testable in isolation with fixtures
2. **`writer.py`** second — extend to handle `body` field; test by feeding it a known-good payload
3. **`processor.py`** third — add routing; test by manually placing a file-type job in `inbox/`
4. **`bot.py`** last — once everything downstream works, wire up document handling

---

## 12. Git workflow

**Before starting (on Windows where Claude Code runs):**

`git pull`

**After each logical chunk completes and tests pass:**

`git add .`

`git commit -m "<descriptive message>"`

`git push`

**Before testing on NAS:**

`git pull`

**Suggested commit boundaries:**
- Commit 1: `adapters/file.py` + fixtures + adapter unit tests
- Commit 2: `writer.py` extension + writer tests
- Commit 3: `processor.py` routing change
- Commit 4: `bot.py` document handling
- Commit 5: spec doc updates

---

## 13. Out of scope (do not build)

- Generic image preservation (decision: strip all images)
- Auto-stub-page creation for authors (decision: plain string author)
- Bulk import workflows for Readwise/Kindle (future phase, will reuse this adapter)
- Web Clipper integration (Markdownload-only for now)
- Print-to-PDF intake (separate spec if pursued)

---

## 14. Open questions for Claude Code

If any of the following arise during build, **stop and ask before proceeding**:

1. Does the existing `writer.py` filename convention conflict with files where the title contains characters that need sanitization (e.g. `:`, `/`, `?`)? If so, surface for a sanitization-rule decision.
2. Is `pyyaml` already in `requirements.txt`? If yes, no action. If no, add it.
3. Does the existing `bot.py` use a job filename pattern that should be matched exactly for file-type jobs, or is there flexibility?

---

**End of spec.**
