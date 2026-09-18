"""Point-in-time NIFTY index membership storage.

The database stores complete constituent snapshots at effective dates.  Reads
are deliberately strict: a date is usable only when an explicit verified
coverage interval exists, so callers never silently substitute today's index
members in a historical backtest.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Iterable


SUPPORTED_INDICES = (
    "NIFTY 50",
    "NIFTY NEXT 50",
    "NIFTY 100",
    "NIFTY 200",
    "NIFTY 500",
    "NIFTY MIDCAP 150",
    "NIFTY SMALLCAP 250",
    "NIFTY AUTO",
    "NIFTY BANK",
    "NIFTY FINANCIAL SERVICES",
    "NIFTY FMCG",
    "NIFTY IT",
    "NIFTY MEDIA",
    "NIFTY METAL",
    "NIFTY PHARMA",
    "NIFTY PSU BANK",
    "NIFTY REALTY",
    "NIFTY ENERGY",
    "NIFTY INFRASTRUCTURE",
    "NIFTY CONSUMPTION",
    "NIFTY COMMODITIES",
    "NIFTY HEALTHCARE",
)


class HistoricalMembershipError(RuntimeError):
    """Base error for point-in-time membership operations."""


class HistoricalMembershipCoverageError(HistoricalMembershipError):
    """Raised when a requested index/date is outside verified coverage."""


def normalize_index_name(value: str) -> str:
    name = " ".join(str(value or "").strip().upper().split())
    aliases = {
        "NIFTY50": "NIFTY 50",
        "NIFTY NEXT50": "NIFTY NEXT 50",
        "NIFTY NEXT50 50": "NIFTY NEXT 50",
        "NIFTY100": "NIFTY 100",
        "NIFTY200": "NIFTY 200",
        "NIFTY500": "NIFTY 500",
        "NIFTY MIDCAP150": "NIFTY MIDCAP 150",
        "NIFTY SMALLCAP250": "NIFTY SMALLCAP 250",
        "NIFTY FIN SERVICE": "NIFTY FINANCIAL SERVICES",
        "NIFTY FIN SERVICES": "NIFTY FINANCIAL SERVICES",
        "NIFTY INFRA": "NIFTY INFRASTRUCTURE",
        "NIFTY INDIA CONSUMPTION": "NIFTY CONSUMPTION",
    }
    return aliases.get(name, name)


def normalize_symbol(value: str) -> str:
    symbol = str(value or "").strip().upper()
    return symbol[:-3] if symbol.endswith(".NS") else symbol


def is_placeholder_symbol(value: str) -> bool:
    """Return True for non-tradable placeholders found in source documents.

    Some historical index PDFs contain internal dummy labels (for example
    ``DUMMYHEG``) in the constituent column.  They are not NSE securities and
    must never enter a price-download universe.
    """
    return normalize_symbol(value).startswith("DUMMY")


def iso_date(value: str | date) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(str(value).strip()).isoformat()


class HistoricalIndexMembershipStore:
    """SQLite-backed, effective-dated constituent snapshot store."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _migrate(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    index_name TEXT NOT NULL,
                    effective_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    source_url TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (index_name, effective_date, symbol)
                );
                CREATE INDEX IF NOT EXISTS snapshots_lookup
                    ON snapshots(index_name, effective_date);

                CREATE TABLE IF NOT EXISTS coverage (
                    index_name TEXT PRIMARY KEY,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    verified INTEGER NOT NULL DEFAULT 0,
                    source_note TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS sources (
                    source_url TEXT PRIMARY KEY,
                    published_date TEXT NOT NULL DEFAULT '',
                    effective_date TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    sha256 TEXT NOT NULL DEFAULT ''
                );
                """
            )
            # Also repair databases created by older versions.  Keeping this
            # idempotent makes every future app/update run self-healing.
            connection.execute(
                "DELETE FROM snapshots WHERE UPPER(TRIM(symbol)) LIKE 'DUMMY%'"
            )
            connection.commit()

    def replace_snapshot(
        self,
        index_name: str,
        effective_date: str | date,
        symbols: Iterable[str],
        source_url: str = "",
    ) -> None:
        name = normalize_index_name(index_name)
        if name not in SUPPORTED_INDICES:
            raise HistoricalMembershipError(f"Unsupported historical index: {name}")
        effective = iso_date(effective_date)
        clean = sorted(
            {
                normalized
                for symbol in symbols
                if (normalized := normalize_symbol(symbol))
                and not is_placeholder_symbol(normalized)
            }
        )
        if not clean:
            raise HistoricalMembershipError(
                f"Refusing to store an empty {name} snapshot for {effective}."
            )
        with closing(self._connect()) as connection:
            connection.execute(
                "DELETE FROM snapshots WHERE index_name=? AND effective_date=?",
                (name, effective),
            )
            connection.executemany(
                """INSERT INTO snapshots
                   (index_name, effective_date, symbol, source_url)
                   VALUES (?, ?, ?, ?)""",
                [(name, effective, symbol, str(source_url or "")) for symbol in clean],
            )
            connection.commit()

    def set_coverage(
        self,
        index_name: str,
        start_date: str | date,
        end_date: str | date,
        *,
        verified: bool,
        source_note: str = "",
    ) -> None:
        name = normalize_index_name(index_name)
        start, end = iso_date(start_date), iso_date(end_date)
        if name not in SUPPORTED_INDICES:
            raise HistoricalMembershipError(f"Unsupported historical index: {name}")
        if start > end:
            raise HistoricalMembershipError("Coverage start date is after its end date.")
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO coverage
                   (index_name, start_date, end_date, verified, source_note)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(index_name) DO UPDATE SET
                       start_date=excluded.start_date,
                       end_date=excluded.end_date,
                       verified=excluded.verified,
                       source_note=excluded.source_note""",
                (name, start, end, int(bool(verified)), str(source_note or "")),
            )
            connection.commit()

    def coverage(self, index_name: str) -> dict[str, object] | None:
        name = normalize_index_name(index_name)
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT start_date, end_date, verified, source_note
                   FROM coverage WHERE index_name=?""",
                (name,),
            ).fetchone()
        if row is None:
            return None
        return {
            "index_name": name,
            "start_date": str(row[0]),
            "end_date": str(row[1]),
            "verified": bool(row[2]),
            "source_note": str(row[3] or ""),
        }

    def require_coverage(
        self, index_names: Iterable[str], start_date: str | date, end_date: str | date
    ) -> None:
        start, end = iso_date(start_date), iso_date(end_date)
        missing: list[str] = []
        for raw_name in sorted(set(index_names)):
            name = normalize_index_name(raw_name)
            if name not in SUPPORTED_INDICES:
                continue
            item = self.coverage(name)
            if (
                item is None
                or not item["verified"]
                or str(item["start_date"]) > start
                or str(item["end_date"]) < end
            ):
                available = (
                    f"{item['start_date']} to {item['end_date']}"
                    if item is not None else "not installed"
                )
                missing.append(f"{name} (available: {available})")
        if missing:
            raise HistoricalMembershipCoverageError(
                "Verified point-in-time index membership does not cover the "
                f"requested period {start} to {end}:\n" + "\n".join(missing)
            )

    def members_on(self, index_name: str, as_of: str | date) -> set[str]:
        name, day = normalize_index_name(index_name), iso_date(as_of)
        self.require_coverage([name], day, day)
        with closing(self._connect()) as connection:
            snapshot = connection.execute(
                """SELECT MAX(effective_date) FROM snapshots
                   WHERE index_name=? AND effective_date<=?""",
                (name, day),
            ).fetchone()[0]
            if snapshot is None:
                raise HistoricalMembershipCoverageError(
                    f"No {name} constituent snapshot exists on or before {day}."
                )
            rows = connection.execute(
                """SELECT symbol FROM snapshots
                   WHERE index_name=? AND effective_date=? ORDER BY symbol""",
                (name, snapshot),
            ).fetchall()
        return {str(row[0]) for row in rows}

    def members_during(
        self, index_name: str, start_date: str | date, end_date: str | date
    ) -> set[str]:
        name, start, end = (
            normalize_index_name(index_name),
            iso_date(start_date),
            iso_date(end_date),
        )
        self.require_coverage([name], start, end)
        symbols = self.members_on(name, start)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT DISTINCT symbol FROM snapshots
                   WHERE index_name=? AND effective_date>? AND effective_date<=?""",
                (name, start, end),
            ).fetchall()
        symbols.update(str(row[0]) for row in rows)
        return symbols

    def snapshot_dates(self, index_name: str) -> list[str]:
        name = normalize_index_name(index_name)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT DISTINCT effective_date FROM snapshots
                   WHERE index_name=? ORDER BY effective_date""",
                (name,),
            ).fetchall()
        return [str(row[0]) for row in rows]
