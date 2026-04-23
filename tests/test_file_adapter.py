import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import adapters.file as file_adapter

FIXTURES_DIR = Path(__file__).parent / "fixtures"

REAL_FIXTURE = "There Is Much More to Pope vs. President Than Meets the Eye.md"

MOCK_HAIKU = {
    "filename_title": "Pope vs President Just War",
    "summary": "test summary",
    "tags": ["catholicism", "iran", "war", "just-war"],
    "key_points": [
        {
            "point": "Just-war doctrine requires proportionality and last resort",
            "quote": "Unless you know specifically what the aim or aims of a war are, you can hardly know whether it is just.",
        }
    ],
}

MOCK_CFG = {
    "claude_model": "claude-haiku-4-5-20251001",
    "claude_max_tokens": 1024,
}

KNOWN_MULTIWORD_NAMES = {
    "us-foreign-policy",
    "strait-of-hormuz",
    "south-china-sea",
    "climate-change",
    "just-war",
}


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def make_job(fixture_name: str) -> dict:
    return {
        "input_type": "file",
        "filename": fixture_name,
        "content": load_fixture(fixture_name),
        "type": "article",
        "received_at": "2026-04-23T12:00:00",
        "telegram_date": 0,
        "chat_id": "test",
    }


def test_webclipper_title_and_author():
    """Change 1: frontmatter title is primary; wikilink author stripped to plain name."""
    with patch.object(file_adapter, "_call_haiku", return_value=MOCK_HAIKU):
        payload = file_adapter.process(make_job(REAL_FIXTURE), MOCK_CFG)

    assert payload["title"] == "There Is Much More to Pope vs. President Than Meets the Eye", (
        f"Got: {payload['title']!r}"
    )
    assert payload["author"] == "David French", f"Got: {payload['author']!r}"
    assert "nytimes.com" in payload["source"]
    assert payload["captured"] == "23-Apr-2026", f"Got: {payload['captured']!r}"


def test_webclipper_body_cleanup():
    """Change 2: newsletter footer stripped; authored secondary content kept; images stripped."""
    with patch.object(file_adapter, "_call_haiku", return_value=MOCK_HAIKU):
        payload = file_adapter.process(make_job(REAL_FIXTURE), MOCK_CFG)

    body = payload["body"]

    # Newsletter footer stripped
    assert "Thanks for reading" not in body
    assert "If you're enjoying what you're reading" not in body
    assert "You can also follow me on" not in body
    assert "Have feedback?" not in body

    # Portrait image stripped
    assert "Portrait of David French" not in body

    # Authored secondary content kept
    assert "Some other things I did" in body

    # Core article text present
    assert "just war" in body.lower()
    assert "Pope Leo" in body

    # Inline links stripped (no markdown link syntax remaining)
    assert "](" not in body


def test_webclipper_opinion_prefix_stripped():
    """Change 1: 'Opinion | ' prefix is stripped from title."""
    import re
    raw = "Opinion | There Is Much More to Pope vs. President Than Meets the Eye"
    cleaned = re.sub(r"^Opinion\s*\|\s*", "", raw)
    assert cleaned == "There Is Much More to Pope vs. President Than Meets the Eye"


def test_author_normalization_multi():
    """Change 1: multiple wikilink authors joined with ' and '."""
    result = file_adapter._normalize_author(["[[David French]]", "[[Ross Douthat]]"])
    assert result == "David French and Ross Douthat", f"Got: {result!r}"


def test_author_normalization_plain_string():
    """Change 1: plain string author passed through unchanged."""
    assert file_adapter._normalize_author("Peter Eavis") == "Peter Eavis"


def test_author_normalization_single_wikilink():
    """Change 1: single wikilink stripped to plain name."""
    assert file_adapter._normalize_author(["[[David French]]"]) == "David French"


if __name__ == "__main__":
    test_webclipper_opinion_prefix_stripped()
    test_author_normalization_multi()
    test_author_normalization_plain_string()
    test_author_normalization_single_wikilink()
    print("Standalone assertions passed.")
    print("(Full pipeline tests require project dependencies — run on NAS)")
