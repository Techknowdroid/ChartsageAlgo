"""Developer utility: download public NSE Indices equity change notices.

This is intentionally not used by the GUI.  It creates an auditable local
source set from which the bundled point-in-time membership database is built.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin


APP_DIR = Path(__file__).resolve().parent
TOOLS_DIR = APP_DIR / "_membership_tools"
sys.path.insert(0, str(TOOLS_DIR))
from pypdf import PdfReader  # type: ignore  # noqa: E402


ARCHIVE_URL = "https://www.niftyindices.com/press-release"
OUTPUT_DIR = APP_DIR / "historical_membership_sources"
EARLIEST_YEAR = 2022


class AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            self._href = dict(attrs).get("href") or ""
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            self.links.append((self._href, " ".join(self._text).strip()))
            self._href = ""
            self._text = []


def fetch(url: str) -> bytes:
    archive = OUTPUT_DIR / "press-release.html"
    if url == ARCHIVE_URL and archive.exists():
        return archive.read_bytes()
    if not url.lower().startswith("https://www.niftyindices.com/"):
        raise ValueError(f"Refusing unexpected source URL: {url}")
    download_dir = OUTPUT_DIR / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)
    target = download_dir / Path(url).name
    if not target.exists():
        safe_url = url.replace("'", "''")
        safe_target = str(target).replace("'", "''")
        subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                f"Invoke-WebRequest -Uri '{safe_url}' -UseBasicParsing "
                f"-OutFile '{safe_target}'",
            ],
            check=True,
        )
    return target.read_bytes()


def candidate(title: str, href: str) -> bool:
    value = " ".join(title.lower().split())
    if not href.lower().endswith(".pdf"):
        return False
    if any(word in value for word in ("fixed income", "sme emerge", "nifty ipo")):
        return False
    return any(
        word in value
        for word in (
            "replacement",
            "replacements",
            "exclusion",
            "corporate adjustment",
            "corporate action adjustment",
            "change in",
            "changes in",
            "reconstitution",
        )
    )


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = OUTPUT_DIR / "manifest.json"
    previous_records: dict[str, dict[str, str]] = {}
    if manifest_path.exists():
        previous_records = {
            str(item.get("url", "")): item
            for item in json.loads(manifest_path.read_text(encoding="utf-8"))
            if str(item.get("url", ""))
        }
    parser = AnchorParser()
    parser.feed(fetch(ARCHIVE_URL).decode("utf-8", errors="replace"))
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for href, title in parser.links:
        url = urljoin(ARCHIVE_URL, href)
        if url in seen or not candidate(title, url):
            continue
        date_match = re.search(r"(20\d{2})", url)
        if date_match and int(date_match.group(1)) < EARLIEST_YEAR:
            continue
        seen.add(url)
        try:
            payload = fetch(url)
        except (OSError, subprocess.SubprocessError) as exc:
            previous = previous_records.get(url)
            if previous and (OUTPUT_DIR / previous.get("text_file", "")).is_file():
                records.append(previous)
                print(f"Reused cached notice after download failure: {title} ({exc})")
            else:
                print(f"Skipped unavailable notice: {title} ({exc})")
            continue
        text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(payload)).pages)
        if not any(name in text.upper() for name in ("NIFTY 50", "NIFTY 500", "NIFTY NEXT 50")):
            continue
        stem = Path(url).stem
        text_path = OUTPUT_DIR / f"{stem}.txt"
        text_path.write_text(text, encoding="utf-8")
        records.append(
            {
                "title": title,
                "url": url,
                "text_file": text_path.name,
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
        print(f"{stem}: {title}")
    manifest_path.write_text(
        json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Saved {len(records)} relevant notices to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
