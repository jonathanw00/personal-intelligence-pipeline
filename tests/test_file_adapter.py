import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import adapters.file as file_adapter

FIXTURES_DIR = Path(__file__).parent / "fixtures"

MOCK_HAIKU = {
    "filename_title": "Iran Tightens Grip on Strait",
    "summary": "test summary",
    "tags": ["iran", "shipping"],
    "key_points": [
        {
            "point": "Iran restricts shipping through the strait",
            "quote": "There is no freedom of navigation",
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
}


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def make_job(fixture_name: str) -> dict:
    return {
        "input_type": "file",
        "filename": fixture_name,
        "content": load_fixture(fixture_name),
        "type": "article",
        "received_at": "2026-04-22T18:33:20",
        "telegram_date": 0,
        "chat_id": "test",
    }


def test_nyt_full_pipeline():
    with patch.object(file_adapter, "_call_haiku", return_value=MOCK_HAIKU):
        payload = file_adapter.process(make_job("nyt-with-frontmatter.md"), MOCK_CFG)

    body = payload["body"]

    # Bug 1 — multi-line H1
    assert payload["title"] == "Iran Again Tightens Its Grip on the Strait of Hormuz", (
        f"Got: {payload['title']!r}"
    )

    # Bug 2 — NYT boilerplate stripped
    assert "Advertisement" not in body
    assert "SKIP ADVERTISEMENT" not in body
    assert "\nListen\n" not in body
    assert "· 6:45 min" not in body
    assert not ("Updated" in body and "p.m. ET" in body)

    # Bug 3 — excerpt block stripped
    assert "## Excerpt" not in body
    assert "> Traffic in the strait" not in body

    # Bug 4 — dek paragraph appears at most once
    dek_fragment = "Traffic in the strait has all but halted"
    assert body.count(dek_fragment) <= 1, (
        f"Dek fragment appears {body.count(dek_fragment)} times"
    )

    # Bug 5 — tags are atomic (checked against live Haiku output; mock skips this)
    for tag in payload["tags"]:
        assert tag.count("-") < 2 or tag in KNOWN_MULTIWORD_NAMES, (
            f"Tag '{tag}' looks compound"
        )


if __name__ == "__main__":
    test_nyt_full_pipeline()
    print("All assertions passed.")
