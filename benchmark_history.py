"""Validated local cache of official NIFTY Total Return Index history."""

from __future__ import annotations

import json
import math
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import requests


IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_INDEX = "NIFTY 500 TRI"
SOURCE_PAGE = "https://www.niftyindices.com/reports/historical-data"
SOURCE_ENDPOINT = "https://www.niftyindices.com/BackPage/getTotalReturnIndexString"

# Universe name -> (official display name returned by NSE Indices, trading
# index name required by its historical-data endpoint). Keeping this explicit
# makes benchmark selection auditable and prevents a similarly named index
# from being cached accidentally.
SUPPORTED_BENCHMARKS = {
    "NIFTY 50": ("NIFTY 50", "NIFTY 50"),
    "NIFTY NEXT 50": ("NIFTY NEXT 50", "NIFTY NEXT 50"),
    "NIFTY 100": ("NIFTY 100", "NIFTY 100"),
    "NIFTY 200": ("NIFTY 200", "NIFTY 200"),
    "NIFTY 500": ("NIFTY 500", "NIFTY 500"),
    "NIFTY MIDCAP 150": ("NIFTY MIDCAP 150", "NIFTY MIDCAP 150"),
    "NIFTY SMALLCAP 250": ("NIFTY SMALLCAP 250", "NIFTY SMLCAP 250"),
    "NIFTY AUTO": ("NIFTY AUTO", "NIFTY AUTO"),
    "NIFTY BANK": ("NIFTY BANK", "NIFTY BANK"),
    "NIFTY FINANCIAL SERVICES": ("NIFTY FINANCIAL SERVICES", "NIFTY FIN SERVICE"),
    "NIFTY FMCG": ("NIFTY FMCG", "NIFTY FMCG"),
    "NIFTY IT": ("NIFTY IT", "NIFTY IT"),
    "NIFTY MEDIA": ("NIFTY MEDIA", "NIFTY MEDIA"),
    "NIFTY METAL": ("NIFTY METAL", "NIFTY METAL"),
    "NIFTY PHARMA": ("NIFTY PHARMA", "NIFTY PHARMA"),
    "NIFTY PSU BANK": ("NIFTY PSU BANK", "NIFTY PSU BANK"),
    "NIFTY REALTY": ("NIFTY REALTY", "NIFTY REALTY"),
    "NIFTY ENERGY": ("NIFTY ENERGY", "NIFTY ENERGY"),
    "NIFTY INFRASTRUCTURE": ("NIFTY INFRASTRUCTURE", "NIFTY INFRA"),
    "NIFTY CONSUMPTION": ("NIFTY INDIA CONSUMPTION", "NIFTY CONSUMPTION"),
    "NIFTY COMMODITIES": ("NIFTY COMMODITIES", "NIFTY COMMODITIES"),
    "NIFTY HEALTHCARE": ("NIFTY HEALTHCARE", "NIFTY HEALTHCARE"),
}


def benchmark_label(universe_name: str) -> str:
    name = " ".join(str(universe_name or "").upper().split())
    if name not in SUPPORTED_BENCHMARKS:
        raise BenchmarkHistoryError(f"Unsupported TRI benchmark universe: {name!r}")
    return f"{name} TRI"


def benchmark_universe(label: str) -> str:
    name = " ".join(str(label or "").upper().split())
    if name.endswith(" TRI"):
        name = name[:-4].strip()
    if name not in SUPPORTED_BENCHMARKS:
        raise BenchmarkHistoryError(f"Unsupported TRI benchmark: {label!r}")
    return name


class BenchmarkHistoryError(RuntimeError):
    """Raised when official benchmark data cannot be safely cached."""


def _parse_day(value: object) -> str:
    text = str(value or "").strip()
    for pattern in ("%d %b %Y", "%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            pass
    raise BenchmarkHistoryError(f"Unrecognised benchmark date: {text!r}")


def _year_chunks(start: date, end: date):
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=364))
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


class BenchmarkHistoryStore:
    """SQLite store with atomic, provenance-bearing official TRI updates."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS benchmark_values (
                    benchmark TEXT NOT NULL,
                    date TEXT NOT NULL,
                    value REAL NOT NULL CHECK(value > 0),
                    source_url TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    tls_verified INTEGER NOT NULL,
                    PRIMARY KEY (benchmark, date)
                );
                CREATE TABLE IF NOT EXISTS benchmark_updates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    benchmark TEXT NOT NULL,
                    requested_start TEXT NOT NULL,
                    requested_end TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    source_url TEXT NOT NULL,
                    tls_verified INTEGER NOT NULL
                );
                """
            )

    def load(self, start: str, end: str, benchmark: str = DEFAULT_INDEX):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT date, value FROM benchmark_values "
                "WHERE benchmark=? AND date BETWEEN ? AND ? ORDER BY date",
                (benchmark, start, end),
            ).fetchall()
        return {str(row["date"]): float(row["value"]) for row in rows}

    def coverage(self, benchmark: str = DEFAULT_INDEX):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT MIN(date) first_date, MAX(date) last_date, COUNT(*) rows "
                "FROM benchmark_values WHERE benchmark=?", (benchmark,)
            ).fetchone()
        return dict(
            first_date=row["first_date"] or "",
            last_date=row["last_date"] or "",
            rows=int(row["rows"] or 0),
        )

    def update_official(
        self,
        start: str,
        end: str,
        *,
        progress: Callable[[str], None] | None = None,
        verify_tls: bool = True,
        benchmark: str = DEFAULT_INDEX,
    ):
        start_day, end_day = date.fromisoformat(start), date.fromisoformat(end)
        if start_day > end_day:
            raise BenchmarkHistoryError("Benchmark start date is after end date.")
        universe = benchmark_universe(benchmark)
        benchmark = benchmark_label(universe)
        official_name, trading_name = SUPPORTED_BENCHMARKS[universe]
        session = requests.Session()
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124 Safari/537.36"
            ),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": SOURCE_PAGE,
            "Origin": "https://www.niftyindices.com",
            "X-Requested-With": "XMLHttpRequest",
        }
        try:
            session.get(SOURCE_PAGE, headers=headers, timeout=(10, 40), verify=verify_tls).raise_for_status()
        except requests.RequestException as exc:
            raise BenchmarkHistoryError(f"Could not open the official NSE Indices page: {exc}") from exc

        fetched_at = datetime.now(IST).isoformat(timespec="seconds")
        collected: dict[str, float] = {}
        for chunk_start, chunk_end in _year_chunks(start_day, end_day):
            if progress:
                progress(f"Downloading {benchmark}: {chunk_start} to {chunk_end}")
            payload = {"cinfo": (
                f"{{'name':'{trading_name}','startDate':'"
                f"{chunk_start:%d-%b-%Y}','endDate':'{chunk_end:%d-%b-%Y}',"
                f"'indexName':'{official_name}'}}"
            )}
            last_error = None
            for attempt in range(3):
                try:
                    response = session.post(
                        SOURCE_ENDPOINT, headers=headers, json=payload,
                        timeout=(10, 60), verify=verify_tls,
                    )
                    response.raise_for_status()
                    raw = response.json()
                    if isinstance(raw, dict) and "d" in raw:
                        raw = json.loads(raw["d"]) if isinstance(raw["d"], str) else raw["d"]
                    if not isinstance(raw, list):
                        raise BenchmarkHistoryError("Official endpoint returned an unexpected format.")
                    chunk_rows = 0
                    for item in raw:
                        if not isinstance(item, dict):
                            continue
                        returned_name = " ".join(
                            str(item.get("Index Name") or "").upper().split()
                        )
                        if returned_name != official_name:
                            raise BenchmarkHistoryError(
                                f"Official endpoint returned the wrong index: {returned_name!r}."
                            )
                        row_day = _parse_day(item.get("Date") or item.get("HistoricalDate"))
                        value = float(item.get("TotalReturnsIndex"))
                        if not math.isfinite(value) or value <= 0:
                            raise BenchmarkHistoryError(f"Invalid TRI value on {row_day}.")
                        if chunk_start.isoformat() <= row_day <= chunk_end.isoformat():
                            if row_day in collected and not math.isclose(
                                collected[row_day], value, rel_tol=1e-12
                            ):
                                raise BenchmarkHistoryError(
                                    f"Conflicting TRI values were returned for {row_day}."
                                )
                            collected[row_day] = value
                            chunk_rows += 1
                    if chunk_rows == 0:
                        raise BenchmarkHistoryError(
                            "Official endpoint returned no observations for this date chunk."
                        )
                    last_error = None
                    break
                except (requests.RequestException, ValueError, json.JSONDecodeError, BenchmarkHistoryError) as exc:
                    last_error = exc
                    if attempt < 2:
                        time.sleep(attempt + 1)
            if last_error is not None:
                raise BenchmarkHistoryError(
                    f"Official NIFTY 500 TRI download failed for {chunk_start} to {chunk_end}: {last_error}"
                ) from last_error

        if not collected:
            raise BenchmarkHistoryError("The official source returned no NIFTY 500 TRI observations.")
        ordered = sorted(collected.items())
        if date.fromisoformat(ordered[0][0]) > start_day + timedelta(days=10):
            raise BenchmarkHistoryError("Benchmark history does not cover the requested start.")
        if date.fromisoformat(ordered[-1][0]) < end_day - timedelta(days=10):
            raise BenchmarkHistoryError("Benchmark history does not cover the requested end.")
        for (earlier_day, earlier), (later_day, later) in zip(ordered, ordered[1:]):
            if (
                date.fromisoformat(later_day) - date.fromisoformat(earlier_day)
            ).days > 14:
                raise BenchmarkHistoryError(
                    f"Benchmark validation found an unexplained gap after {earlier_day}."
                )
            ratio = later / earlier
            if not 0.5 < ratio < 2.0:
                raise BenchmarkHistoryError("Benchmark validation found an implausible one-session change.")
        with self._connect() as connection:
            connection.executemany(
                "INSERT INTO benchmark_values "
                "(benchmark,date,value,source_url,fetched_at,tls_verified) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(benchmark,date) DO UPDATE SET value=excluded.value, "
                "source_url=excluded.source_url,fetched_at=excluded.fetched_at,tls_verified=excluded.tls_verified",
                [(benchmark, day, value, SOURCE_PAGE, fetched_at, int(verify_tls)) for day, value in ordered],
            )
            connection.execute(
                "INSERT INTO benchmark_updates "
                "(benchmark,requested_start,requested_end,fetched_at,row_count,source_url,tls_verified) "
                "VALUES (?,?,?,?,?,?,?)",
                (benchmark, start, end, fetched_at, len(ordered), SOURCE_PAGE, int(verify_tls)),
            )
        result = self.coverage(benchmark)
        result.update(downloaded_rows=len(ordered), fetched_at=fetched_at)
        return result
