"""Safe forward updater for ChartSage point-in-time NIFTY membership.

The live membership database is rebuilt only after all newly published source
notices and current constituent CSVs have been downloaded and validated.  The
builder writes a separate ``.building.db`` and replaces the live SQLite file
only when every backward transition is internally consistent.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

from historical_index_membership import HistoricalIndexMembershipStore


IST = timezone(timedelta(hours=5, minutes=30))
ARCHIVE_URL = "https://www.niftyindices.com/press-release"
PRESS_ROOT = "https://www.niftyindices.com/"
CURRENT_ROOT = "https://archives.nseindia.com/content/indices/"
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/124 Safari/537.36"
    ),
    "Accept": "text/html,application/pdf,text/csv,*/*",
    "Referer": ARCHIVE_URL,
}


class MembershipUpdateError(RuntimeError):
    """Raised when an update cannot be completed without weakening coverage."""


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href = ""
        self._text: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
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


def _candidate(title: str, href: str) -> bool:
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


def _notice_date(url: str) -> date | None:
    match = re.search(r"ind_prs(\d{2})(\d{2})(20\d{2})", Path(url).name, re.I)
    if not match:
        return None
    day_value, month_value, year_value = map(int, match.groups())
    try:
        return date(year_value, month_value, day_value)
    except ValueError:
        return None


def _ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    strict_flag = getattr(ssl, "VERIFY_X509_STRICT", 0)
    if strict_flag:
        context.verify_flags &= ~strict_flag
    return context


def _download(url: str, *, attempts: int = 3, timeout: int = 40) -> bytes:
    if not (
        url.startswith("https://www.niftyindices.com/")
        or url.startswith("https://archives.nseindia.com/")
    ):
        raise MembershipUpdateError(f"Refusing unexpected source URL: {url}")
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers=HTTP_HEADERS)
            with urllib.request.urlopen(
                request, timeout=timeout, context=_ssl_context()
            ) as response:
                payload = response.read()
            if not payload:
                raise MembershipUpdateError(f"Empty response from {url}")
            return payload
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(float(attempt))
    raise MembershipUpdateError(f"Download failed for {url}: {last_error}")


def _pdf_text(payload: bytes, app_dir: Path) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        tools_dir = app_dir / "_membership_tools"
        if tools_dir.is_dir() and str(tools_dir) not in sys.path:
            sys.path.insert(0, str(tools_dir))
        try:
            from pypdf import PdfReader  # type: ignore
        except ImportError as exc:
            raise MembershipUpdateError(
                "The bundled PDF reader required for official index notices is missing."
            ) from exc
    try:
        return "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(payload)).pages)
    except Exception as exc:
        raise MembershipUpdateError(f"Could not read an official index notice: {exc}") from exc


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _load_manifest(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MembershipUpdateError(f"Historical membership manifest is invalid: {exc}") from exc
    if not isinstance(raw, list):
        raise MembershipUpdateError("Historical membership manifest must contain a list.")
    return [dict(item) for item in raw if isinstance(item, dict)]


def _validate_constituent_csv(payload: bytes, index_name: str, expected: int) -> None:
    try:
        rows = list(csv.DictReader(io.StringIO(payload.decode("utf-8-sig"))))
    except Exception as exc:
        raise MembershipUpdateError(f"Invalid {index_name} constituent CSV: {exc}") from exc
    symbols = {
        str(row.get("Symbol", "")).strip().upper()
        for row in rows
        if str(row.get("Symbol", "")).strip()
    }
    # Demerger methodology can temporarily retain one or more resulting
    # entities in addition to the index's normal target count.
    if not expected <= len(symbols) <= expected + 10:
        raise MembershipUpdateError(
            f"{index_name} constituent CSV contains {len(symbols)} symbols; "
            f"expected {expected} (or a small temporary demerger excess)."
        )


def membership_coverage_summary(app_dir: str | Path) -> dict[str, object]:
    root = Path(app_dir)
    store = HistoricalIndexMembershipStore(root / "historical_index_membership.db")
    from historical_index_membership import SUPPORTED_INDICES
    names = SUPPORTED_INDICES
    coverage = [store.coverage(name) for name in names]
    installed = [item for item in coverage if item is not None and item.get("verified")]
    return {
        "verified_indices": len(installed),
        "supported_indices": len(names),
        "start_date": max(str(item["start_date"]) for item in installed) if installed else "",
        "end_date": min(str(item["end_date"]) for item in installed) if installed else "",
    }


def update_historical_membership(
    app_dir: str | Path,
    progress: Callable[[int, int, str], None] | None = None,
    *,
    download: Callable[[str], bytes] | None = None,
) -> dict[str, object]:
    """Refresh official sources and atomically rebuild the live database."""

    root = Path(app_dir).resolve()
    source_dir = root / "historical_membership_sources"
    current_dir = source_dir / "current"
    downloads_dir = source_dir / "downloads"
    manifest_path = source_dir / "manifest.json"
    updater_state = root / "historical_membership_update_state.json"
    report = progress or (lambda _done, _total, _message: None)
    fetch = download or _download

    old_manifest = _load_manifest(manifest_path)
    old_by_url = {
        str(item.get("url", "")): item for item in old_manifest if item.get("url")
    }
    coverage_before = membership_coverage_summary(root)
    covered_text = str(coverage_before.get("end_date") or "")
    # A normal distribution includes the verified membership database, but a
    # first-run repair must also work when that database is absent or empty.
    # Existing archived source notices still provide the older reconstruction;
    # the rolling window below discovers recent additions from NSE Indices.
    covered_through = (
        date.fromisoformat(covered_text)
        if covered_text
        else datetime.now(IST).date() - timedelta(days=120)
    )
    lookback = covered_through - timedelta(days=120)

    report(1, 6, "Historical membership: checking NSE Indices notices")
    archive_payload = fetch(ARCHIVE_URL)
    parser = _AnchorParser()
    parser.feed(archive_payload.decode("utf-8", errors="replace"))
    selected: list[tuple[str, str]] = []
    for href, title in parser.links:
        url = urljoin(PRESS_ROOT, href)
        published = _notice_date(url)
        if _candidate(title, url) and (published is None or published >= lookback):
            selected.append((url, title))

    staged_notices: list[tuple[Path, bytes, Path, bytes]] = []
    manifest_by_url = dict(old_by_url)
    new_notices = 0
    for position, (url, title) in enumerate(selected, 1):
        old = old_by_url.get(url)
        if old and (source_dir / str(old.get("text_file", ""))).is_file():
            continue
        report(
            1,
            6,
            f"Historical membership: reading new notice {position}/{len(selected)}",
        )
        pdf = fetch(url)
        text = _pdf_text(pdf, root)
        stem = Path(url).stem
        pdf_path = downloads_dir / f"{stem}.pdf"
        text_path = source_dir / f"{stem}.txt"
        staged_notices.append((pdf_path, pdf, text_path, text.encode("utf-8")))
        manifest_by_url[url] = {
            "title": title,
            "url": url,
            "text_file": text_path.name,
            "sha256": hashlib.sha256(pdf).hexdigest(),
        }
        new_notices += 1

    report(2, 6, "Historical membership: downloading current constituent lists")
    current_files = {
        "NIFTY 50": ("ind_nifty50list.csv", 50),
        "NIFTY NEXT 50": ("ind_niftynext50list.csv", 50),
        "NIFTY 100": ("ind_nifty100list.csv", 100),
        "NIFTY 200": ("ind_nifty200list.csv", 200),
        "NIFTY 500": ("ind_nifty500list.csv", 500),
        "NIFTY MIDCAP 150": ("ind_niftymidcap150list.csv", 150),
        "NIFTY SMALLCAP 250": ("ind_niftysmallcap250list.csv", 250),
        "NIFTY AUTO": ("ind_niftyautolist.csv", 15),
        "NIFTY BANK": ("ind_niftybanklist.csv", 14),
        "NIFTY FINANCIAL SERVICES": ("ind_niftyfinancelist.csv", 20),
        "NIFTY FMCG": ("ind_niftyfmcglist.csv", 15),
        "NIFTY IT": ("ind_niftyitlist.csv", 10),
        "NIFTY MEDIA": ("ind_niftymedialist.csv", 10),
        "NIFTY METAL": ("ind_niftymetallist.csv", 15),
        "NIFTY PHARMA": ("ind_niftypharmalist.csv", 20),
        "NIFTY PSU BANK": ("ind_niftypsubanklist.csv", 12),
        "NIFTY REALTY": ("ind_niftyrealtylist.csv", 10),
        "NIFTY ENERGY": ("ind_niftyenergylist.csv", 40),
        "NIFTY INFRASTRUCTURE": ("ind_niftyinfralist.csv", 30),
        "NIFTY CONSUMPTION": ("ind_niftyconsumptionlist.csv", 30),
        "NIFTY COMMODITIES": ("ind_niftycommoditieslist.csv", 30),
        "NIFTY HEALTHCARE": ("ind_niftyhealthcarelist.csv", 20),
    }
    staged_csvs: list[tuple[Path, bytes]] = []
    for index_name, (filename, expected) in current_files.items():
        payload = fetch(CURRENT_ROOT + filename)
        _validate_constituent_csv(payload, index_name, expected)
        staged_csvs.append((current_dir / filename, payload))

    report(3, 6, "Historical membership: committing verified source downloads")
    for pdf_path, pdf, text_path, text in staged_notices:
        _atomic_write(pdf_path, pdf)
        _atomic_write(text_path, text)
    for csv_path, payload in staged_csvs:
        _atomic_write(csv_path, payload)
    ordered_manifest = sorted(
        manifest_by_url.values(),
        key=lambda item: (str(item.get("url", "")), str(item.get("title", ""))),
    )
    _atomic_write(
        manifest_path,
        (json.dumps(ordered_manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
    )
    _atomic_write(source_dir / "press-release.html", archive_payload)

    report(4, 6, "Historical membership: reconstructing point-in-time snapshots")
    import build_historical_index_membership as builder

    diagnostics = builder.rebuild(root, datetime.now(IST).date())
    errors = [str(value) for value in diagnostics.get("errors", [])]
    if errors:
        raise MembershipUpdateError(
            "The new membership database failed validation; the previous database "
            "was preserved.\n" + "\n".join(errors[:12])
        )

    report(5, 6, "Historical membership: verifying installed coverage")
    coverage_after = membership_coverage_summary(root)
    from historical_index_membership import SUPPORTED_INDICES
    if int(coverage_after["verified_indices"]) != len(SUPPORTED_INDICES):
        raise MembershipUpdateError(
            "The rebuilt database does not verify every supported NIFTY universe."
        )
    if str(coverage_after["end_date"]) < datetime.now(IST).date().isoformat():
        raise MembershipUpdateError("The rebuilt membership coverage did not reach today.")

    result: dict[str, object] = {
        **coverage_after,
        "updated_at": datetime.now(IST).isoformat(timespec="seconds"),
        "new_notices": new_notices,
        "source_notices": len(ordered_manifest),
    }
    _atomic_write(
        updater_state,
        (json.dumps(result, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
    )
    report(6, 6, "Historical membership update complete")
    return result
