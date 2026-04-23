# Change Spec — Web Clipper Switch (April 23, 2026)

## Context

The file adapter (`adapters/file.py`) currently expects Markdownload frontmatter. We're switching to the Obsidian Web Clipper as the sole browser clipper. The Web Clipper produces cleaner output with a different frontmatter schema. This spec updates the adapter to handle the new format.

**Read before starting:** `adapters/file.py`, `writer.py`, the previous `CHANGE_SPEC_2026-04-23.md`

**Git first:**

```
git pull
```

---

## Web Clipper default frontmatter (what the adapter now receives)

```yaml
---
title: "There Is Much More to Pope vs. President Than Meets the Eye"
source: "https://www.nytimes.com/2026/04/23/opinion/trump-iran-unjust-war-catholicism.html"
author:
  - "[[David French]]"
published: 2026-04-23
created: 2026-04-23
description: "What fighting an unjust war really means."
tags:
  - "clippings"
---
```

Key differences from Markdownload:
- `title` is a proper frontmatter field (Markdownload put it in an H1 that often truncated)
- `author` is a YAML list of wikilink strings (e.g., `["[[David French]]"]`)
- `published` field exists (ISO date)
- `description` field exists (article excerpt/subtitle)
- `tags` defaults to `["clippings"]` (ignore this — Haiku generates real tags)
- No timezone offset in `created` (just ISO date)
- Body is cleaner: no ad blocks, no "SKIP ADVERTISEMENT" text

---

## Change 1 — Update frontmatter parsing in `adapters/file.py`

### Title extraction

**Old behavior:** Fall back to H1 heading in body if no `title` in frontmatter.

**New behavior:** Use `title` from frontmatter (primary). If absent, fall back to first H1 in body (safety net). Remove any leading `Opinion | ` prefix from the title — NYT often prepends this.

### Author extraction

**Old behavior:** `author` is a plain string.

**New behavior:** `author` may be:
- A YAML list of wikilink strings: `["[[David French]]"]`
- A YAML list of plain strings: `["David French"]`
- A plain string: `"David French"`

Normalize to a single plain string:
1. If it's a list, join with ` and ` (e.g., `["[[David French]]", "[[Ross Douthat]]"]` → `"David French and Ross Douthat"`)
2. Strip wikilink brackets: `[[David French]]` → `David French`
3. If it's already a plain string, use as-is

### Published date

**New field.** If `published` exists in frontmatter, use it to derive the `created` date for the output note (formatted as DD-Mmm-YYYY). This is the article's publication date, which is more meaningful than the clip date for the note's filename and frontmatter.

If `published` is absent, fall back to existing behavior (use `created` or current date).

### Description

**New field.** If `description` exists, pass it to Haiku as additional context alongside the body. It often contains the article subtitle or excerpt, which helps Haiku write a better summary. Do NOT use it as the summary directly — Haiku still generates the summary.

### Tags

Ignore any incoming `tags` from the Web Clipper frontmatter (discard `["clippings"]`). Haiku generates all tags.

---

## Change 2 — Update body cleanup in `adapters/file.py`

### Remove Markdownload-specific cleanup

Remove any logic that strips:
- `Advertisement` / `SKIP ADVERTISEMENT` blocks
- Markdownload-specific boilerplate patterns

The Web Clipper doesn't produce these.

### Keep or add these cleanup rules

- Strip all `![...]()` image markdown (already exists — keep)
- Strip inline links: convert `[text](url)` to `text` (already exists — keep)
- Strip newsletter signup boilerplate: remove everything after patterns like `Thanks for reading` or `If you're enjoying what you're reading` at the end of the article
- Strip author portrait images and footer content
- Strip horizontal rules (`---` or `___`) that separate article body from newsletter footer content — but only when followed by boilerplate patterns, not mid-article horizontal rules

### New cleanup: newsletter footer detection

Many NYT opinion pieces (like David French's) have a main article followed by `---` then a "Some other things I did" or "Breviary" or similar section, then another `---` and signup boilerplate. The adapter should:

1. Keep the main article body
2. Keep secondary content sections (like "Some other things I did") — these are still authored content
3. Strip everything after the final signup/footer pattern (e.g., "Thanks for reading", "If you're enjoying what you're reading, please consider recommending", "You can also follow me on")

---

## Change 3 — Update test fixtures

Replace the existing Markdownload test fixtures with Web Clipper format fixtures. Use the David French article as the primary test case since we have the actual file.

### Test assertions

1. Title extracted correctly: "There Is Much More to Pope vs. President Than Meets the Eye" (not truncated)
2. Author normalized to plain string: "David French" (no wikilinks)
3. `publication` derived correctly: "New York Times"
4. Body contains article text, no images, no signup boilerplate
5. Key points use analytical bullets with ==highlighted quotes==

---

## Build order

1. Change 1 (frontmatter parsing) — most important, fixes the title problem
2. Change 2 (body cleanup) — adjust to Web Clipper output
3. Change 3 (test fixtures) — verify everything works

## After changes verified

```
git add .
```

```
git commit -m "Switch file adapter from Markdownload to Obsidian Web Clipper"
```

```
git push
```

Then on NAS:

```
cd /volume1/homes/Jon/intelligence-pipeline && git pull
```
