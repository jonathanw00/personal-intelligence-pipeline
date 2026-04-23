# Backlog

## File adapter

- **Title truncation when source H1 contains `<br>` tags.** Symptom: NYT
  articles (and likely others) render headlines with `<br>` line breaks in
  the H1, which MarkDownload truncates at the break. The truncated H1 lands
  in both the input filename and the body H1, so no adapter-side regex can
  recover the missing words. Two viable fixes when this becomes painful:
  (a) replace current MarkDownload extension with the canonical
  deepfocus/MarkDownload which exposes a configurable title template, or
  (b) add URL-fetch fallback to the file adapter — fetch the source URL,
  extract `<title>` or `og:title`, strip publication suffixes, use as
  canonical title. First seen: 22-Apr-2026, NYT "Iran Again Tightens Its
  Grip on the Strait of Hormuz".

- **NYT audio player UI on a single line.** Cosmetic. `Listen · 6:45 min`
  appears as one line in some MarkDownload outputs, bypassing the current
  two-line stripping pattern. Fix: extend NYT_BOILERPLATE_PATTERNS to
  match `^Listen\s*·\s*\d+:\d+\s*min\s*$` in addition to the two-line
  variant. Trivial when next touched.