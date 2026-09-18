"""Build the verified point-in-time NIFTY membership database.

The builder works backwards from current official constituent CSVs and the
public NSE Indices effective-dated change notices extracted by
``_extract_nifty_membership_sources.py``.  It refuses to mark coverage as
verified when a parsed transition conflicts with the known later snapshot.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import sys
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

from historical_index_membership import (
    HistoricalIndexMembershipStore,
    SUPPORTED_INDICES,
    normalize_index_name,
    normalize_symbol,
)


APP_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
SOURCE_DIR = APP_DIR / "historical_membership_sources"
CURRENT_DIR = SOURCE_DIR / "current"
DATABASE_PATH = APP_DIR / "historical_index_membership.db"
DIAGNOSTICS_PATH = SOURCE_DIR / "build_diagnostics.json"
CURRENT_DATE = date.today()
START_DATE = CURRENT_DATE - timedelta(days=4 * 366)

CURRENT_FILES = {
    "NIFTY 50": "ind_nifty50list.csv",
    "NIFTY NEXT 50": "ind_niftynext50list.csv",
    "NIFTY 100": "ind_nifty100list.csv",
    "NIFTY 200": "ind_nifty200list.csv",
    "NIFTY 500": "ind_nifty500list.csv",
    "NIFTY MIDCAP 150": "ind_niftymidcap150list.csv",
    "NIFTY SMALLCAP 250": "ind_niftysmallcap250list.csv",
    "NIFTY AUTO": "ind_niftyautolist.csv",
    "NIFTY BANK": "ind_niftybanklist.csv",
    "NIFTY FINANCIAL SERVICES": "ind_niftyfinancelist.csv",
    "NIFTY FMCG": "ind_niftyfmcglist.csv",
    "NIFTY IT": "ind_niftyitlist.csv",
    "NIFTY MEDIA": "ind_niftymedialist.csv",
    "NIFTY METAL": "ind_niftymetallist.csv",
    "NIFTY PHARMA": "ind_niftypharmalist.csv",
    "NIFTY PSU BANK": "ind_niftypsubanklist.csv",
    "NIFTY REALTY": "ind_niftyrealtylist.csv",
    "NIFTY ENERGY": "ind_niftyenergylist.csv",
    "NIFTY INFRASTRUCTURE": "ind_niftyinfralist.csv",
    "NIFTY CONSUMPTION": "ind_niftyconsumptionlist.csv",
    "NIFTY COMMODITIES": "ind_niftycommoditieslist.csv",
    "NIFTY HEALTHCARE": "ind_niftyhealthcarelist.csv",
}

# NSE occasionally changes a trading symbol without changing the underlying
# constituent.  Canonicalising those cases lets one entity remain continuous
# across the backward reconstruction and matches the current Yahoo/NSE symbol.
SYMBOL_ALIASES = {
    "ZOMATO": "ETERNAL",
    "LTIM": "LTM",
    "GMRINFRA": "GMRAIRPORT",
    "GET&D": "GVT&D",
    "HBLPOWER": "HBLENGINE",
    "MFL": "EPIGRAL",
    "SWANENERGY": "SWANCORP",
    "AKZOINDIA": "JSWDULUX",
    "TATAMOTORS": "TMPV",
}

MONTHS = {
    name.upper(): number for number, name in enumerate(
        (
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ),
        1,
    )
}


def parse_date_text(value: str) -> date | None:
    value = re.sub(r"(?<=\d)\s+,", ",", value)
    value = re.sub(r"\b(20)\s+(\d{2})\b", r"\1\2", value)
    value = re.sub(r"\b(20\d)\s+(\d)\b", r"\1\2", value)
    match = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
        r"(\d{1,2})(?:st|nd|rd|th)?[,]?\s+(20\d{2})\b",
        value,
        re.IGNORECASE,
    )
    if not match:
        match = re.search(
            r"\b(\d{1,2})(?:st|nd|rd|th)?\s+"
            r"(January|February|March|April|May|June|July|August|September|October|November|December)[,]?\s+"
            r"(20\d{2})\b",
            value,
            re.IGNORECASE,
        )
        if not match:
            return None
        day_value, month_value, year_value = match.groups()
    else:
        month_value, day_value, year_value = match.groups()
    try:
        return date(int(year_value), MONTHS[month_value.upper()], int(day_value))
    except ValueError:
        return None


def effective_date(text: str, title: str) -> date | None:
    title_date = parse_date_text(title)
    if title_date is not None:
        return title_date
    compact = " ".join(text.split())
    patterns = (
        r"changes? shall become effective from\s+(.{0,35}?20\d{2})",
        r"changes? (?:will|would) become effective from\s+(.{0,35}?20\d{2})",
        r"with effect from\s+(.{0,35}?20\d{2})",
        r"w\.e\.f\.?\s+(.{0,35}?20\d{2})",
        r"effective from\s+(.{0,35}?20\d{2})",
    )
    for pattern in patterns:
        match = re.search(pattern, compact, re.IGNORECASE)
        if match:
            parsed = parse_date_text(match.group(1))
            if parsed:
                return parsed
    return parse_date_text(title)


def heading_index(line: str) -> str | None:
    cleaned = " ".join(line.strip().split())
    match = re.match(r"^(?:\d+|[a-z])\s*[\)\.]\s*(Nifty.+)$", cleaned, re.IGNORECASE)
    if not match:
        return None
    candidate = re.sub(r"\s+index$", "", match.group(1), flags=re.IGNORECASE)
    normalized = normalize_index_name(candidate)
    return normalized if normalized in SUPPORTED_INDICES else "OTHER_NIFTY"


def row_symbol(buffer: list[str]) -> str | None:
    value = " ".join(buffer)
    tokens = re.findall(r"(?<![A-Za-z0-9])[A-Z0-9][A-Z0-9&.-]{1,}(?:\*+)?", value)
    if not tokens:
        return None
    symbol = normalize_symbol(tokens[-1].rstrip("*"))
    if (
        not re.search(r"[A-Z]", symbol)
        or symbol in {"SR", "NO", "COMPANY", "NAME", "SYMBOL", "LTD"}
    ):
        return None
    return symbol


def canonical_symbol(value: str) -> str:
    symbol = normalize_symbol(value)
    return SYMBOL_ALIASES.get(symbol, symbol)


def row_looks_complete(line: str) -> bool:
    return bool(
        re.search(r"[A-Z0-9][A-Z0-9&.-]{1,}(?:\*+)?\s*$", line)
        and re.search(r"\b(?:Ltd\.?|Limited|Bank|Corporation)\b", line, re.IGNORECASE)
    )


def parse_notice(
    record: dict[str, str],
    *,
    source_dir: Path = SOURCE_DIR,
    current_date: date = CURRENT_DATE,
    start_date: date = START_DATE,
) -> tuple[list[dict[str, object]], list[str]]:
    text = (source_dir / record["text_file"]).read_text(encoding="utf-8")
    event_date = effective_date(text, record.get("title", ""))
    if event_date is None:
        return [], [f"{record['text_file']}: effective date not found"]
    if event_date > current_date or event_date <= start_date:
        return [], []

    events: dict[str, dict[str, object]] = {}
    current_index: str | None = None
    action: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer
        if current_index in SUPPORTED_INDICES and action and buffer:
            symbol = row_symbol(buffer)
            if symbol:
                cast = events.setdefault(
                    current_index,
                    {
                        "effective_date": event_date.isoformat(),
                        "index_name": current_index,
                        "included": set(),
                        "excluded": set(),
                        "source_url": record["url"],
                        "title": record.get("title", ""),
                    },
                )
                cast[action].add(symbol)  # type: ignore[index, union-attr]
        buffer = []

    for raw_line in text.splitlines():
        line = " ".join(raw_line.strip().split())
        if not line:
            continue
        heading = heading_index(line)
        if heading is not None:
            flush()
            current_index = heading
            action = None
            continue
        lowered = line.lower()
        if "following" in lowered and ("excluded" in lowered or "included" in lowered):
            flush()
            if "revoked" in lowered:
                action = None
            else:
                action = "excluded" if "excluded" in lowered else "included"
            continue
        if action is None or current_index not in SUPPORTED_INDICES:
            continue
        if lowered.startswith("sr. no") or lowered.startswith("sr no"):
            flush()
            continue
        if re.match(r"^\d+(?:\s+|$)", line):
            flush()
            buffer = [line]
            if row_looks_complete(line):
                flush()
        elif buffer:
            if lowered.startswith(("note", "the above", "* on account", "** on account")):
                flush()
                action = None
            else:
                buffer.append(line)
                if row_looks_complete(line):
                    flush()
    flush()

    output: list[dict[str, object]] = []
    warnings: list[str] = []
    for item in events.values():
        included = sorted(item["included"])  # type: ignore[arg-type]
        excluded = sorted(item["excluded"])  # type: ignore[arg-type]
        if included or excluded:
            item["included"] = included
            item["excluded"] = excluded
            output.append(item)
        if len(included) != len(excluded):
            warnings.append(
                f"{record['text_file']} {item['index_name']}: "
                f"{len(included)} included vs {len(excluded)} excluded"
            )
    return output, warnings


def parse_temporary_exclusion_notice(
    record: dict[str, str],
    *,
    source_dir: Path = SOURCE_DIR,
    current_date: date = CURRENT_DATE,
    start_date: date = START_DATE,
) -> list[dict[str, object]]:
    """Recover temporary demerged constituents from their exclusion notice.

    NSE Indices corporate-action notices do not use the normal per-index
    inclusion/exclusion tables.  Once the resulting company is listed, its
    exclusion notice states the original inclusion date, listed trading
    symbol, removal date, and numbered list of affected indices.  That one
    official document is therefore sufficient to reconstruct the interval.
    """

    title = str(record.get("title", ""))
    if "exclusion of" not in title.lower():
        return []
    text = (source_dir / record["text_file"]).read_text(encoding="utf-8")
    if "demerg" not in text.lower():
        return []

    # A notice can contain separate A/B exclusions with different universes.
    section_starts = list(
        re.finditer(
            r"(?im)^\s*[A-Z][\.)]\s+Exclusion of .+?from Nifty indices.*$",
            text,
        )
    )
    if section_starts:
        sections = [
            text[item.start():(
                section_starts[position + 1].start()
                if position + 1 < len(section_starts) else len(text)
            )]
            for position, item in enumerate(section_starts)
        ]
    else:
        sections = [text]

    output: list[dict[str, object]] = []
    for section in sections:
        if "demerg" not in section.lower():
            continue
        inclusion_match = re.search(
            r"(?:various|following)\s+indices\s+effectiv\s*e\s+from\s+(.{0,45}?20\s*\d\s*\d)",
            section,
            re.IGNORECASE | re.DOTALL,
        )
        exclusion_match = re.search(
            r"decided to exclude\s+(.{1,900}?)\s+from\s+various\s+indices.*?"
            r"effectiv\s*e\s+from\s+(.{0,45}?20\s*\d\s*\d)",
            section,
            re.IGNORECASE | re.DOTALL,
        )
        if not inclusion_match or not exclusion_match:
            continue
        included_on = parse_date_text(inclusion_match.group(1))
        excluded_on = parse_date_text(exclusion_match.group(2))
        if not included_on or not excluded_on or excluded_on <= included_on:
            continue
        if excluded_on > current_date or excluded_on <= start_date:
            continue

        decision = exclusion_match.group(1)
        symbols = {
            normalize_symbol(value)
            for value in re.findall(r"\(([A-Z][A-Z0-9&.-]{1,})\)", decision)
        }
        if not symbols:
            listed = re.search(
                r"After (?:the )?demerger,.{0,500}?\(([A-Z][A-Z0-9&.-]{1,})\)\s+was\s+listed",
                section,
                re.IGNORECASE | re.DOTALL,
            )
            if listed:
                symbols = {normalize_symbol(listed.group(1))}
        if not symbols:
            symbols = {
                normalize_symbol(value)
                for value in re.findall(r"\b[A-Z][A-Z0-9&.-]{2,}\b", decision)
                if value not in {"NSE", "NIFTY", "LTD", "LIMITED"}
            }
        symbols = {
            canonical_symbol(value)
            for value in symbols
            if value and not value.startswith("DUMMY")
        }
        indices: set[str] = set()
        for raw_line in section.splitlines():
            match = re.match(r"^\s*\d+\s+(Nifty.+?)\s*$", raw_line, re.IGNORECASE)
            if not match:
                continue
            candidate = re.sub(r"\s+index$", "", match.group(1), flags=re.IGNORECASE)
            normalized = normalize_index_name(candidate)
            if normalized in SUPPORTED_INDICES:
                indices.add(normalized)
        if not symbols or not indices:
            continue
        for index_name in sorted(indices):
            output.extend(
                (
                    {
                        "effective_date": included_on.isoformat(),
                        "index_name": index_name,
                        "included": sorted(symbols),
                        "excluded": [],
                        "source_url": record["url"],
                        "title": title,
                    },
                    {
                        "effective_date": excluded_on.isoformat(),
                        "index_name": index_name,
                        "included": [],
                        "excluded": sorted(symbols),
                        "source_url": record["url"],
                        "title": title,
                    },
                )
            )
    return output


def load_current(current_dir: Path = CURRENT_DIR) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for index_name, filename in CURRENT_FILES.items():
        with (current_dir / filename).open("r", encoding="utf-8-sig", newline="") as handle:
            rows = csv.DictReader(handle)
            result[index_name] = {
                canonical_symbol(row.get("Symbol", "")) for row in rows
                if canonical_symbol(row.get("Symbol", ""))
            }
    return result


def apply_verified_corrections(events: list[dict[str, object]]) -> None:
    """Apply later NSE revocations and non-tabular corporate-action notices."""

    for event in events:
        event["included"] = sorted(
            {canonical_symbol(value) for value in event["included"]}  # type: ignore[arg-type]
        )
        event["excluded"] = sorted(
            {canonical_symbol(value) for value in event["excluded"]}  # type: ignore[arg-type]
        )

    def matching(day: str, index_name: str) -> dict[str, object]:
        for event in events:
            if event["effective_date"] == day and event["index_name"] == index_name:
                return event
        item: dict[str, object] = {
            "effective_date": day,
            "index_name": index_name,
            "included": [],
            "excluded": [],
            "source_url": "",
            "title": "Verified non-tabular NSE Indices adjustment",
        }
        events.append(item)
        return item

    def revise(
        day: str,
        index_name: str,
        *,
        add_included: Iterable[str] = (),
        remove_included: Iterable[str] = (),
        add_excluded: Iterable[str] = (),
        remove_excluded: Iterable[str] = (),
        source_url: str,
    ) -> None:
        item = matching(day, index_name)
        included = set(item["included"])  # type: ignore[arg-type]
        excluded = set(item["excluded"])  # type: ignore[arg-type]
        included.update(map(canonical_symbol, add_included))
        included.difference_update(map(canonical_symbol, remove_included))
        excluded.update(map(canonical_symbol, add_excluded))
        excluded.difference_update(map(canonical_symbol, remove_excluded))
        item["included"] = sorted(included)
        item["excluded"] = sorted(excluded)
        existing = str(item.get("source_url", ""))
        item["source_url"] = " | ".join(
            value for value in dict.fromkeys([existing, source_url]) if value
        )

    march_revision = "https://www.niftyindices.com/Press_Release/ind_prs19032024.pdf"
    revise(
        "2024-03-28", "NIFTY 500",
        remove_included=["IREDA"], remove_excluded=["VGUARD"],
        source_url=march_revision,
    )
    revise(
        "2024-03-28", "NIFTY MIDCAP 150",
        add_included=["BSE"], remove_included=["IREDA"],
        source_url=march_revision,
    )
    revise(
        "2024-03-28", "NIFTY SMALLCAP 250",
        add_excluded=["BSE"], remove_excluded=["VGUARD"],
        source_url=march_revision,
    )
    revise(
        "2024-03-28", "NIFTY 200",
        add_included=["BSE"], remove_included=["IREDA"],
        source_url=march_revision,
    )

    september_revision = "https://www.niftyindices.com/Press_Release/ind_prs25092024.pdf"
    revise(
        "2024-09-30", "NIFTY 500",
        add_excluded=["PRSMJOHNSN"], remove_excluded=["IDEA"],
        source_url=september_revision,
    )
    revise(
        "2024-09-30", "NIFTY MIDCAP 150",
        remove_included=["CENTRALBK"], remove_excluded=["IDEA"],
        source_url=september_revision,
    )
    revise(
        "2024-09-30", "NIFTY SMALLCAP 250",
        add_excluded=["PRSMJOHNSN"], remove_excluded=["CENTRALBK"],
        source_url=september_revision,
    )
    revise(
        "2024-09-30", "NIFTY 200",
        remove_included=["CENTRALBK"], remove_excluded=["IDEA"],
        source_url=september_revision,
    )

    dvr_source = "https://www.niftyindices.com/Press_Release/ind_prs23082024_1.pdf"
    for index_name in ("NIFTY 100", "NIFTY 200", "NIFTY 500", "NIFTY AUTO"):
        revise(
            "2024-08-30", index_name,
            add_excluded=["TATAMTRDVR"], source_url=dvr_source,
        )

    # NSE renamed the continuously listed INE716B01029 security from
    # TIPSINDLTD to TIPSMUSIC.  The official bhavcopy history changes symbol
    # on 2024-09-30 (last TIPSINDLTD row: 2024-09-27), so model this as a
    # same-security ticker transition instead of a constituent replacement.
    tips_rename = (
        "https://nsearchives.nseindia.com/corporate/"
        "TIPSINDLTD_17062024132725_TIPSINDLTDp.pdf"
    )
    revise(
        "2024-09-30", "NIFTY MEDIA",
        add_included=["TIPSMUSIC"], add_excluded=["TIPSINDLTD"],
        source_url=tips_rename,
    )

    # NSE revised Nifty Energy's eligibility and expanded its maximum size
    # from 10 to 40 effective 2024-12-31.  That notice uses a one-sided
    # "changes to" table rather than the standard replacement-table wording,
    # so preserve its complete verified inclusion list explicitly.
    energy_expansion_source = (
        "https://www.niftyindices.com/Press_Release/ind_prs11122024.pdf"
    )
    revise(
        "2024-12-31", "NIFTY ENERGY",
        add_included=[
            "ABB", "ADANIENSOL", "ATGL", "AEGISLOG", "BHEL", "CASTROLIND",
            "CESC", "CGPOWER", "GAIL", "GVT&D", "GUJGASLTD", "GSPL",
            "HINDPETRO", "POWERINDIA", "IGL", "INOXWIND", "JPPOWER",
            "JSWENERGY", "MGL", "NHPC", "NLCINDIA", "OIL", "PETRONET",
            "SCHNEIDER", "SIEMENS", "SJVN", "SUZLON", "THERMAX",
            "TORNTPOWER", "TRITURBINE",
        ],
        source_url=energy_expansion_source,
    )

    tata_add = "https://www.niftyindices.com/Press_Release/ind_prs07102025.pdf"
    tata_remove = "https://www.niftyindices.com/Press_Release/ind_prs13112025.pdf"
    for index_name in ("NIFTY 50", "NIFTY 100", "NIFTY 200", "NIFTY 500"):
        revise(
            "2025-10-14", index_name,
            add_included=["TMCV"], source_url=tata_add,
        )
        revise(
            "2025-11-17", index_name,
            add_excluded=["TMCV"], source_url=tata_remove,
        )

    # Demerged entities are announced in a numbered list of affected indices,
    # rather than the inclusion/exclusion tables used by periodic reviews.
    # Store the listed trading symbol for the entire temporary interval.  It
    # has no price before listing, so it cannot create a false scan match, but
    # becomes usable as soon as historical prices exist under that symbol.
    temporary_intervals = (
        (
            "JIOFIN", "2023-07-20", "2023-09-07",
            ("NIFTY 50", "NIFTY 100", "NIFTY 200", "NIFTY 500"),
            "https://www.niftyindices.com/Press_Release/ind_prs17072023.pdf",
            "https://www.niftyindices.com/Press_Release/ind_prs05092023.pdf",
        ),
        (
            "SANOFICONR", "2024-06-13", "2024-09-23",
            ("NIFTY 500", "NIFTY SMALLCAP 250"),
            "https://www.niftyindices.com/Press_Release/ind_prs07062024_2.pdf",
            "https://www.niftyindices.com/Press_Release/ind_prs19092024.pdf",
        ),
        (
            "RAYMONDLSL", "2024-07-11", "2024-09-17",
            ("NIFTY 500", "NIFTY SMALLCAP 250"),
            "https://www.niftyindices.com/Press_Release/ind_prs05072024.pdf",
            "https://www.niftyindices.com/Press_Release/ind_prs13092024.pdf",
        ),
        (
            "ITCHOTELS", "2025-01-06", "2025-02-10",
            ("NIFTY 50", "NIFTY 100", "NIFTY 200", "NIFTY 500"),
            "https://www.niftyindices.com/Press_Release/ind_prs30122024.pdf",
            "https://www.niftyindices.com/Press_Release/ind_prs06022025_1.pdf",
        ),
        (
            "ENRIN", "2025-04-07", "2025-06-27",
            ("NIFTY NEXT 50", "NIFTY 100", "NIFTY 200", "NIFTY 500"),
            "https://www.niftyindices.com/Press_Release/ind_prs02042025.pdf",
            "https://www.niftyindices.com/Press_Release/ind_prs25062025_2.pdf",
        ),
        (
            "RAYMONDREL", "2025-05-14", "2025-07-10",
            ("NIFTY 500", "NIFTY SMALLCAP 250"),
            "https://www.niftyindices.com/Press_Release/ind_prs08072025.pdf",
            "https://www.niftyindices.com/Press_Release/ind_prs08072025.pdf",
        ),
        (
            "ABLBL", "2025-05-22", "2025-06-27",
            ("NIFTY 200", "NIFTY 500", "NIFTY MIDCAP 150"),
            "https://www.niftyindices.com/Press_Release/ind_prs25062025_2.pdf",
            "https://www.niftyindices.com/Press_Release/ind_prs25062025_2.pdf",
        ),
        (
            "ADVENTHTL", "2025-07-18", "2025-11-28",
            ("NIFTY 500", "NIFTY SMALLCAP 250"),
            "https://www.niftyindices.com/Press_Release/ind_prs26112025.pdf",
            "https://www.niftyindices.com/Press_Release/ind_prs26112025.pdf",
        ),
        (
            "SKFINDUS", "2025-10-15", "2025-12-15",
            ("NIFTY 500", "NIFTY SMALLCAP 250"),
            "https://www.niftyindices.com/Press_Release/ind_prs10102025.pdf",
            "https://www.niftyindices.com/Press_Release/ind_prs11122025_1.pdf",
        ),
    )
    for symbol, added, removed, indices, add_source, remove_source in temporary_intervals:
        for index_name in indices:
            revise(added, index_name, add_included=[symbol], source_url=add_source)
            revise(removed, index_name, add_excluded=[symbol], source_url=remove_source)

    vedanta_indices = ("NIFTY NEXT 50", "NIFTY 100", "NIFTY 200", "NIFTY 500")
    vedanta_add = "https://www.niftyindices.com/Press_Release/ind_prs23042026.pdf"
    vedanta_removals = {
        "VEDPOWER": (
            "2026-06-19",
            "https://www.niftyindices.com/Press_Release/ind_prs17062026.pdf",
        ),
        "VISL": (
            "2026-06-19",
            "https://www.niftyindices.com/Press_Release/ind_prs17062026.pdf",
        ),
        "VAML": (
            "2026-06-23",
            "https://www.niftyindices.com/Press_Release/ind_prs19062026.pdf",
        ),
        "VOGL": (
            "2026-06-24",
            "https://www.niftyindices.com/Press_Release/ind_prs22062026.pdf",
        ),
    }
    for symbol, (removed, remove_source) in vedanta_removals.items():
        for index_name in vedanta_indices:
            revise(
                "2026-04-30", index_name,
                add_included=[symbol], source_url=vedanta_add,
            )
            revise(
                removed, index_name,
                add_excluded=[symbol], source_url=remove_source,
            )


def rebuild(
    app_dir: str | Path | None = None,
    as_of: date | None = None,
) -> dict[str, object]:
    root = Path(app_dir).resolve() if app_dir is not None else APP_DIR
    source_dir = root / "historical_membership_sources"
    current_dir = source_dir / "current"
    database_path = root / "historical_index_membership.db"
    diagnostics_path = source_dir / "build_diagnostics.json"
    current_date = as_of or date.today()
    start_date = current_date - timedelta(days=4 * 366)
    manifest = json.loads((source_dir / "manifest.json").read_text(encoding="utf-8"))
    parsed_events: list[dict[str, object]] = []
    warnings: list[str] = []
    for record in manifest:
        events, notice_warnings = parse_notice(
            record,
            source_dir=source_dir,
            current_date=current_date,
            start_date=start_date,
        )
        parsed_events.extend(events)
        parsed_events.extend(
            parse_temporary_exclusion_notice(
                record,
                source_dir=source_dir,
                current_date=current_date,
                start_date=start_date,
            )
        )
        warnings.extend(notice_warnings)
    apply_verified_corrections(parsed_events)
    parsed_source_urls = {
        url
        for event in parsed_events
        for url in str(event.get("source_url", "")).split(" | ")
        if url
    }
    unparsed_relevant_notices: list[dict[str, str]] = []
    for record in manifest:
        if record["url"] in parsed_source_urls:
            continue
        text = (source_dir / record["text_file"]).read_text(encoding="utf-8")
        notice_date = effective_date(text, record.get("title", ""))
        if (
            notice_date is not None
            and start_date < notice_date <= current_date
            and any(name in text.upper() for name in SUPPORTED_INDICES)
        ):
            unparsed_relevant_notices.append(
                {
                    "effective_date": notice_date.isoformat(),
                    "text_file": record["text_file"],
                    "title": record.get("title", ""),
                    "url": record["url"],
                }
            )

    grouped: dict[str, dict[str, list[dict[str, object]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for event in parsed_events:
        grouped[str(event["index_name"])][str(event["effective_date"])].append(event)

    current = load_current(current_dir)
    snapshots: dict[str, dict[str, set[str]]] = defaultdict(dict)
    errors: list[str] = []
    used_sources: set[str] = set()
    for index_name in SUPPORTED_INDICES:
        state = set(current[index_name])
        snapshots[index_name][current_date.isoformat()] = set(state)
        for event_day in sorted(grouped[index_name], reverse=True):
            notices = grouped[index_name][event_day]
            included = set().union(*(set(item["included"]) for item in notices))
            excluded = set().union(*(set(item["excluded"]) for item in notices))
            used_sources.update(str(item["source_url"]) for item in notices)
            missing_included = included - state
            already_excluded = excluded & state
            if missing_included:
                errors.append(
                    f"{event_day} {index_name}: included symbols absent from later state: "
                    + ", ".join(sorted(missing_included))
                )
            if already_excluded:
                errors.append(
                    f"{event_day} {index_name}: excluded symbols still in later state: "
                    + ", ".join(sorted(already_excluded))
                )
            snapshots[index_name][event_day] = set(state)
            state.difference_update(included)
            state.update(excluded)
        snapshots[index_name][start_date.isoformat()] = set(state)

    diagnostics: dict[str, object] = {
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "start_date": start_date.isoformat(),
        "end_date": current_date.isoformat(),
        "parsed_event_sections": len(parsed_events),
        "parsed_events": parsed_events,
        "unparsed_relevant_notices": unparsed_relevant_notices,
        "warnings": warnings,
        "errors": errors,
        "snapshot_counts": {
            name: {day: len(symbols) for day, symbols in sorted(values.items())}
            for name, values in snapshots.items()
        },
    }
    diagnostics_path.write_text(
        json.dumps(diagnostics, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if errors:
        return diagnostics

    temporary = database_path.with_suffix(".building.db")
    temporary.unlink(missing_ok=True)
    store = HistoricalIndexMembershipStore(temporary)
    for index_name, dated in snapshots.items():
        for snapshot_day, symbols in sorted(dated.items()):
            sources = grouped[index_name].get(snapshot_day, [])
            source_url = " | ".join(sorted({str(item["source_url"]) for item in sources}))
            store.replace_snapshot(index_name, snapshot_day, symbols, source_url)
        store.set_coverage(
            index_name,
            start_date,
            current_date,
            verified=True,
            source_note="Official NSE Indices current CSV baseline reconstructed through effective-dated public notices.",
        )
    with closing(sqlite3.connect(temporary)) as connection:
        for record in manifest:
            if record["url"] not in used_sources:
                continue
            text = (source_dir / record["text_file"]).read_bytes()
            connection.execute(
                """INSERT OR REPLACE INTO sources
                   (source_url, title, sha256) VALUES (?, ?, ?)""",
                (
                    record["url"],
                    record.get("title", ""),
                    hashlib.sha256(text).hexdigest(),
                ),
            )
        connection.commit()
    temporary.replace(database_path)
    return diagnostics


if __name__ == "__main__":
    result = rebuild()
    print(json.dumps({
        "parsed_event_sections": result["parsed_event_sections"],
        "warnings": len(result["warnings"]),
        "errors": len(result["errors"]),
        "database_written": not bool(result["errors"]),
    }, indent=2))
