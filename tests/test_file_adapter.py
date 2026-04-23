import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import adapters.file as file_adapter

FIXTURES_DIR = Path(__file__).parent / "fixtures"

MOCK_HAIKU = {
    "summary": "test summary",
    "tags": ["iran", "shipping"],
    "key_quotes": ["test quote"],
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

    # Bug 1 — multi-line H1
    assert payload["title"] == "Iran Again Tightens Its Grip on the Strait of Hormuz", (
        f"Got: {payload['title']!r}"
    )


if __name__ == "__main__":
    test_nyt_full_pipeline()
    print("All assertions passed.")
