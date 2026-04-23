# Change Spec — April 23, 2026

## Context

The intelligence pipeline has two input paths: URL submissions and .md file submissions (via Telegram document attachment). Both should produce identical output notes, but currently the file adapter produces `## Key Quotes` with blockquotes instead of `## Key points` with analytical bullets + `==quoted evidence==`. This spec fixes that and adds three new frontmatter fields.

**Read before starting:** `FILE_ADAPTER_SPEC.md`, `PIPELINE_SPEC.md`, `writer.py`, `processor.py`, `adapters/file.py`

**Git first:**

```
git pull
```

---

## Change 1 — Unify file adapter output with URL adapter

### Problem

URL-path notes produce:

```markdown
## Summary

{summary}

## Key points

- {analytical observation} — =={supporting quote}==

---

{source text with ==highlights==}
```

File-path notes incorrectly produce:

```markdown
## Summary

{summary}

## Key Quotes

> {quote 1}

> {quote 2}

## Article

{body}
```

### Fix

Make file-path notes produce identical output to URL-path notes. Specifically:

1. **Haiku prompt for file-type jobs** must request the same JSON schema as URL-type jobs: `filename_title`, `tags`, `summary`, and `key_points` (each with `point` and `quote`). No `key_quotes` array — use `key_points` with analytical bullets.
2. **`writer.py`** must use the same note template for both input types: `## Key points` with `- {point} — =={quote}==` format, then `---`, then source body with highlights applied by `_apply_highlights`.
3. Remove any `if/else` branching in `writer.py` that produces different templates based on `input_type`.
4. The `body` field from file-type jobs should be treated as `source_text` — same as the extracted text from URL jobs — and passed through `_apply_highlights` and `_build_source_body` identically.

### Test

Reprocess one of the existing file-type test fixtures. Output should have `## Key points` (not `## Key Quotes`), analytical bullets with `==highlighted==` quotes, and source text below `---` with highlights applied.

---

## Change 2 — Add `publication` field to frontmatter

### What

Derive the publication name from the `source` URL and add it to frontmatter.

### Implementation

Add a domain-to-name lookup in `writer.py` (or a shared utility):

```python
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
```

For Substack URLs (`*.substack.com`), extract the subdomain as the publication name (e.g., `chrisbray.substack.com` → `Chris Bray`). Capitalize each word.

For unknown domains, use the domain name cleaned up (strip `www.`, strip TLD, title-case). E.g., `example.org` → `Example`.

If `source` is empty or missing, set `publication: ""`.

### Frontmatter placement

```yaml
source: <url>
publication: Wall Street Journal
```

Place `publication` immediately after `source` in the frontmatter.

---

## Change 3 — Add `value` and `character` fields to frontmatter

### What

Two new empty fields on every note, filled in manually by the user after reading.

### Implementation

In `writer.py`, add these two lines to the frontmatter template for all note types (both URL and file):

```yaml
value:
character:
```

Place them after `status` and before `summary` in the field order.

These are always written empty. The pipeline never populates them.

### Full frontmatter field order (all note types)

```yaml
---
created: 22-Apr-2026
captured: "[[22-Apr-2026]]"
source: <url>
publication: <derived name>
author: <name>
type: article
tags:
  - tag-one
  - tag-two
status: inbox
value:
character:
summary: "<summary>"
daily-note: "[[22-Apr-2026]]"
---
```

Note: not all fields will be present on every note (e.g., `captured` and `author` may only appear on file-type notes). That's fine — preserve existing behavior for which fields appear. Just ensure `value`, `character`, and `publication` appear on ALL notes going forward.

---

## Change 4 — Update publication map for existing file-type notes

For file-type jobs where `source` is present in the incoming Markdownload frontmatter, parse it the same way. If `source` is absent (pre-Markdownload-config files), `publication` stays empty.

---

## Build order

1. Change 2 (publication) — smallest, isolated
2. Change 3 (value + character) — two lines, trivial
3. Change 1 (unify output) — largest, test after
4. Verify by reprocessing a test fixture through both paths

## After changes verified

```
git add .
```

```
git commit -m "Unify file/URL output format, add publication/value/character fields"
```

```
git push
```

Then on NAS:

```
cd /volume1/homes/Jon/intelligence-pipeline && git pull
```
