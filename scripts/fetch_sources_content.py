#!/usr/bin/env python3
"""Fetch full content for sources_externes entries."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.db.models import Base, SourceExterne


def get_session():
    """Create database session."""
    engine = create_engine("sqlite:///data/legislateur.db")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def extract_text_from_html(html: str, max_length: int = 50000) -> str:
    """Extract clean text from HTML."""
    soup = BeautifulSoup(html, "html.parser")

    # Remove scripts and styles
    for element in soup(["script", "style", "nav", "header", "footer"]):
        element.decompose()

    # Get text
    text = soup.get_text(separator="\n", strip=True)

    # Clean up multiple newlines
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    text = "\n".join(lines)

    return text[:max_length]


def fetch_content(url: str) -> str:
    """Fetch and extract content from URL."""
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            }
            response = client.get(url, headers=headers)
            response.raise_for_status()

            content_type = response.headers.get("content-type", "")

            if "text/html" in content_type:
                return extract_text_from_html(response.text)
            elif "application/pdf" in content_type:
                return "[PDF - content not extracted]"
            else:
                return response.text[:50000]

    except httpx.HTTPStatusError as e:
        return f"[HTTP Error: {e.response.status_code}]"
    except Exception as e:
        return f"[Error: {str(e)}]"


def fetch_sources_content():
    """Fetch content for all sources_externes with NULL contenu."""

    session = get_session()

    # Get sources without content
    sources = session.query(SourceExterne).filter(SourceExterne.contenu.is_(None)).all()

    print(f"Found {len(sources)} sources without content.\n")

    updated = 0
    for source in sources:
        print(f"Fetching: {source.titre[:60]}...")

        content = fetch_content(source.url)

        if content and not content.startswith("["):
            source.contenu = content
            updated += 1
            print(f"  OK ({len(content)} chars)")
        else:
            print(f"  {content}")

        # Be polite to servers
        time.sleep(1)

    session.commit()
    print(f"\nUpdated {updated}/{len(sources)} sources with content.")

    session.close()


if __name__ == "__main__":
    print("Fetching content for sources_externes...\n")
    fetch_sources_content()
