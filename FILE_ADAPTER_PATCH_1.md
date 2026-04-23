# FILE_ADAPTER_PATCH_1.md

**Read this alongside `FILE_ADAPTER_SPEC.md`.** This patch modifies `adapters/file.py` only. No changes to `bot.py`, `processor.py`, or `writer.py`.

**Scope:** Five bug fixes from the 22-Apr-2026 NYT end-to-end test (commit 28d01cc baseline). All bugs were discovered against a real-world Markdownload export that the existing fixtures did not represent.

---

## 1. Files changed

- `adapters/file.py` — five logic changes (see §3)
- `tests/fixtures/nyt-with-frontmatter.md` — replace with real-world fixture
- `tests/test_file_adapter.py` (or equivalent) — add assertions per §4

No changes to spec §3 (Markdownload template), §4 (frontmatter schema), §5.3 step ordering except where noted.

---

## 2. Reference: source-of-truth file

The actual buggy output that drove this patch:
`Resources/2026/April/22-Apr-2026 — Iran Again Tightens Its Grip on — Article.md`

Copy this file into `tests/fixtures/nyt-with-frontmatter.md` (overwriting the existing fixture) so future regressions are caught.

---

## 3. Logic changes in `adapters/file.py`

The cleanup pipeline (spec §5.3) currently has 7 steps. After this patch it has 8, with one step modified and three added. New ordering:

| Step | Function | Status |
|------|----------|--------|
| 1 | Parse frontmatter | unchanged |
| 2 | Extract H1 → title | **MODIFIED (Bug 1)** |
| 3 | Strip H1 from body | unchanged |
| 4 | Extract excerpt text + strip excerpt block | **NEW (Bug 3)** |
| 5 | Strip publication-specific boilerplate | **MODIFIED (Bug 2)** |
| 6 | Dedupe excerpt paragraph from body | **NEW (Bug 4)** |
| 7 | Strip inline links / generic cleanup | unchanged |
| 8 | Call Haiku for summary / quotes / tags | **MODIFIED (Bug 5)** |

Note: step 4 must run *before* step 5 so the captured excerpt text survives boilerplate stripping. Step 6 must run *after* step 5 so the excerpt-duplicate paragraph isn't surrounded by `Advertisement`/`Listen` lines that would prevent clean paragraph-boundary detection.

---

### 3.1 Bug 1 — Multi-line H1 extraction (step 2)

**Current behavior:** Regex `^# (.+?)\n` captures only the first line of the H1, truncating titles that span line breaks. The NYT title rendered as:

```
# Iran Again Tightens Its Grip on
the Strait of Hormuz
```

was captured as `"Iran Again Tightens Its Grip on"`.

**New behavior:** Capture the H1 marker plus all immediately following non-empty, non-heading lines. Stop at first blank line or new heading marker. Collapse internal whitespace.

**Replacement function:**

```python
import re

def extract_h1_multiline(body: str) -> tuple[str | None, str]:
    """
    Extract H1 title (handling multi-line H1s) and return (title, body_without_h1).
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

    # Collect H1 line + continuation lines (no blank, no new heading)
    h1_lines = [lines[h1_start][2:]]  # strip "# "
    consumed = h1_start + 1
    while consumed < len(lines):
        nxt = lines[consumed]
        if not nxt.strip():            # blank line ends heading
            break
        if nxt.lstrip().startswith("#"):  # new heading ends previous
            break
        h1_lines.append(nxt)
        consumed += 1

    title = " ".join(part.strip() for part in h1_lines)
    title = re.sub(r"\s+", " ", title).strip()

    new_body = "".join(lines[:h1_start] + lines[consumed:])
    return title, new_body
```

**Wire-in:** Replace the existing single-line H1 extraction call in `process()` with `title, body = extract_h1_multiline(body)`.

---

### 3.2 Bug 3 — Extract and strip excerpt block (new step 4)

**Decision (Jon, this chat):** Strip the excerpt block entirely. Do **not** promote to its own `## Excerpt` section. Haiku's `## Summary` covers this purpose.

**Why we still capture the text:** Bug 4 needs it for paragraph-dedup downstream.

**New function:**

```python
def extract_and_strip_excerpt(body: str) -> tuple[str | None, str]:
    """
    Find a Markdownload excerpt block of the form:
        > ## Excerpt
        > <text...>
        > <text continued...>
    Capture the text content (without `> ` prefixes), strip the entire block
    from body, and return (excerpt_text, body_without_excerpt).
    Returns (None, body) if no excerpt block present.
    """
    pattern = re.compile(
        r"^> ## Excerpt\s*\n((?:^>.*\n?)+)",
        re.MULTILINE,
    )
    match = pattern.search(body)
    if not match:
        return None, body

    quoted_block = match.group(1)
    # Strip "> " prefix from each line and rejoin
    excerpt_lines = [
        re.sub(r"^>\s?", "", line).strip()
        for line in quoted_block.splitlines()
    ]
    excerpt_text = " ".join(line for line in excerpt_lines if line).strip()

    # Remove the entire matched block (including the "> ## Excerpt" header)
    new_body = body[:match.start()] + body[match.end():]
    return excerpt_text, new_body
```

**Wire-in:** After step 3 (H1 strip), before step 5 (boilerplate). Capture `excerpt_text` in a local variable for use in step 6.

---

### 3.3 Bug 2 — NYT boilerplate stripping (modified step 5)

**Current behavior:** `clean_body` dispatch handles `wsj` and `economist` only. NYT was assumed clean.

**Lines to strip from NYT bodies:**

| Pattern | Example | Notes |
|---------|---------|-------|
| `^Advertisement\s*$` | `Advertisement` | Appears 2× (top + bottom of article) |
| `^SKIP ADVERTISEMENT\s*$` | `SKIP ADVERTISEMENT` | Same — 2× |
| `^Listen\s*$` | `Listen` | Audio player UI label |
| `^·\s*\d+:\d+\s*min\s*$` | `· 6:45 min` | Audio duration. The `·` is U+00B7 (middle dot), not a regular bullet |
| `^[A-Z][a-z]+ \d{1,2}, \d{4}Updated \d{1,2}:\d{2}\s*[ap]\.m\.\s*ET\s*$` | `April 22, 2026Updated 4:42 p.m. ET` | Date + update timestamp mashed together with no separator |

**New function:**

```python
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
    # Collapse runs of 3+ blank lines down to 2
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body
```

**Wire-in:** Add `nyt` case to the existing `clean_body` dispatch (or whatever mechanism routes by `publication`). Apply `strip_nyt_boilerplate(body)` when `publication == "nyt"`.

---

### 3.4 Bug 4 — Dedupe excerpt paragraph (new step 6)

**Symptom:** NYT places the dek as both a quoted excerpt block and as a plain paragraph in the body. After step 4 strips the excerpt block and step 5 strips the surrounding boilerplate, the duplicate plain-text paragraph remains and is the first thing the reader sees in `## Article`.

**New function:**

```python
def dedupe_excerpt_paragraph(body: str, excerpt_text: str | None) -> str:
    """
    If the captured excerpt text appears as a standalone paragraph in body,
    remove it. Match is on normalized whitespace, exact text.
    """
    if not excerpt_text:
        return body

    # Normalize for comparison
    target = re.sub(r"\s+", " ", excerpt_text).strip()
    if not target:
        return body

    # Walk paragraphs (separated by blank lines), drop any whose normalized
    # text matches target exactly.
    paragraphs = re.split(r"\n\s*\n", body)
    kept = []
    removed = False
    for p in paragraphs:
        normalized = re.sub(r"\s+", " ", p).strip()
        if not removed and normalized == target:
            removed = True
            continue
        kept.append(p)

    return "\n\n".join(kept)
```

**Wire-in:** Call after `strip_nyt_boilerplate`, passing in the `excerpt_text` captured in step 4. Pass `None` if no excerpt was found — the function no-ops.

**Defensive note:** Only removes the *first* exact match. If the dek text legitimately appears later in the article (rare but possible), it's preserved.

---

### 3.5 Bug 5 — Atomic tags in Haiku prompt (modified step 8)

**Current behavior:** Haiku produced compound tags like `iran-strait-of-hormuz`, `geopolitics-energy-crisis`, `us-iran-conflict`, `global-economy-impact`. These don't cross-reference well — `iran-strait-of-hormuz` won't link to `iran` or to `strait-of-hormuz`.

**Prompt addition** (insert into the existing tag-generation section of the Haiku prompt in `adapters/file.py`, spec §5.3 step 5):

```
Tags must be ATOMIC single concepts — one idea per tag. Each tag should be
something that could plausibly tag dozens of unrelated articles over time.

Good (atomic):
  iran, shipping, oil, us-foreign-policy, strait-of-hormuz, sanctions, war

Bad (compound, multi-concept):
  iran-strait-of-hormuz, us-iran-conflict, geopolitics-energy-crisis,
  global-economy-impact, shipping-blockade, maritime-security

If a concept feels like it needs two ideas joined by a hyphen, split it
into two separate tags instead. Hyphens are only for multi-word names of
single concepts (e.g. "us-foreign-policy", "climate-change", "south-china-sea").

Aim for 4-7 tags total.
```

**No code changes needed** beyond the prompt string edit.

---

## 4. Test verification

### 4.1 Replace fixture

Overwrite `tests/fixtures/nyt-with-frontmatter.md` with the actual Markdownload output from the 22-Apr-2026 NYT test. (Source file noted in §2.)

### 4.2 Assertions to add

For the NYT fixture, the processed payload should satisfy:

```python
def test_nyt_full_pipeline():
    payload = process(load_fixture("nyt-with-frontmatter.md"))

    # Bug 1
    assert payload.title == "Iran Again Tightens Its Grip on the Strait of Hormuz"

    # Bug 2 — boilerplate gone
    assert "Advertisement" not in payload.body
    assert "SKIP ADVERTISEMENT" not in payload.body
    assert "\nListen\n" not in payload.body
    assert "· 6:45 min" not in payload.body
    assert "Updated" not in payload.body or "p.m. ET" not in payload.body

    # Bug 3 — excerpt block gone
    assert "## Excerpt" not in payload.body
    assert "> Traffic in the strait" not in payload.body

    # Bug 4 — dek paragraph appears at most once in body
    dek_fragment = "Traffic in the strait has all but halted"
    assert payload.body.count(dek_fragment) <= 1

    # Bug 5 — tags are atomic
    for tag in payload.tags:
        # No tag should contain three or more hyphenated segments
        # (proxy for "compound multi-concept")
        assert tag.count("-") < 2 or tag in KNOWN_MULTIWORD_NAMES, \
            f"Tag '{tag}' looks compound"
```

`KNOWN_MULTIWORD_NAMES` is an allow-list for legitimate multi-word single concepts (`us-foreign-policy`, `south-china-sea`, etc.) — start it empty and add as real cases come up.

### 4.3 Manual end-to-end re-test

After Claude Code completes the patch and unit tests pass, re-send the same NYT article through Telegram. Expected vault note:

- Title in frontmatter: `Iran Again Tightens Its Grip on the Strait of Hormuz`
- `## Article` section starts with: `The number of ships passing through the Strait of Hormuz has become a barometer...`
- No `Advertisement`, `SKIP ADVERTISEMENT`, `Listen`, audio duration, or mashed date line anywhere in body
- No `> ## Excerpt` block
- Tags look like `iran`, `shipping`, `oil`, etc. — not `iran-strait-of-hormuz`

---

## 5. Git commit boundaries

Four commits, each independently verifiable:

| # | Commit message | Scope |
|---|----------------|-------|
| 1 | `fix(file-adapter): handle multi-line H1 titles` | Bug 1 — `extract_h1_multiline()` + wire-in |
| 2 | `fix(file-adapter): strip NYT boilerplate (ads, audio, date)` | Bug 2 — `strip_nyt_boilerplate()` + dispatch |
| 3 | `fix(file-adapter): strip excerpt block and dedupe leaked paragraph` | Bugs 3 + 4 — `extract_and_strip_excerpt()` + `dedupe_excerpt_paragraph()` (must ship together; dedup depends on captured excerpt) |
| 4 | `fix(file-adapter): require atomic single-concept tags from Haiku` | Bug 5 — prompt edit only |

Run unit tests after each commit. Run end-to-end Telegram test after commit 4.

**Git reminder for Jon:** Before opening Claude Code, in the project directory:

```
git pull
```

After all four commits land and the end-to-end test passes:

```
git push
```

---

## 6. Out of scope (do not relitigate)

From the original spec and locked decisions:

- Inline link stripping — already working, leave alone
- Author = plain string (not wikilink)
- `captured` = local time
- Author fallback = publication name when missing
- Body images stripped entirely
- WSJ author bio block at end = kept
- Timezone handling (commit febfc2e) — settled

If any of the above appears broken during testing, file a separate patch spec — do not bundle into this one.

---

**End of patch spec.**
