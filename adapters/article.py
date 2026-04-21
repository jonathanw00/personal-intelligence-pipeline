import trafilatura
import requests
from readability import Document


def fetch(url: str) -> dict:
    """Fetch and extract article content from a URL.

    Returns dict with keys: text, title, author, date.
    Raises ValueError if neither extractor yields content.
    """
    downloaded = trafilatura.fetch_url(url)
    if downloaded:
        result = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=False,
            output_format="txt",
        )
        if result and result.strip():
            meta = trafilatura.extract_metadata(downloaded)
            return {
                "text": result.strip(),
                "title": meta.title if meta else "",
                "author": meta.author if meta else "",
                "date": meta.date if meta else "",
            }

    # Fallback: readability-lxml
    response = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    response.raise_for_status()
    doc = Document(response.text)
    text = doc.summary(html_partial=True)
    # Strip HTML tags from readability output
    from html.parser import HTMLParser

    class _Stripper(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []

        def handle_data(self, data):
            self.parts.append(data)

    stripper = _Stripper()
    stripper.feed(text)
    plain = " ".join(stripper.parts).strip()

    if not plain:
        raise ValueError(f"No content extracted from {url}")

    return {
        "text": plain,
        "title": doc.title(),
        "author": "",
        "date": "",
    }
