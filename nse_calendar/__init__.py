"""NSE trading calendar, implemented locally for ChartSageAlgoOPENcode.py.

The PyPI "nse-trading-calendar" package is a different, incompatible
library (its `nse_calendar.calendar` exposes only `get_trading_day_info`),
so this module ships next to the app and shadows it on sys.path because the
script directory comes first.

Design notes:
- A session is a weekday that is not an official NSE holiday, plus any
  officially announced special trading session (e.g. Diwali muhurat).
- Holidays are stored per year in a JSON cache next to the app. Fixed-date
  holidays (Republic Day, Independence Day, etc.) seed every year so that
  historical scans always have coverage; movable holidays (Holi, Diwali,
  Dussehra, ...) arrive from the official NSE payload on refresh.
- Special sessions with unannounced hours fail closed: their close time is
  None and they are never treated as safely open/complete.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Optional

import requests

__version__ = "1.0.0"

_FORMAT = "chartsage-nse-calendar"
_VERSION = 1

_NSE_BASE = "https://www.nseindia.com"
_HOLIDAY_ENDPOINT = _NSE_BASE + "/api/holiday-master?type=trading"
_STATUS_ENDPOINT = _NSE_BASE + "/api/marketStatus"

_FIXED_HOLIDAYS = ((1, 26), (4, 14), (5, 1), (8, 15), (10, 2), (12, 25))

_MONTHS = {name: num for num, name in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"),
    1,
)}

_TIMEFRAME_VALUES = ("D", "W", "M")


def _to_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if hasattr(value, "date") and not isinstance(value, date):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    raise TypeError(f"cannot convert {type(value).__name__} to date")


def _parse_nse_date(raw: Any) -> Optional[date]:
    if raw is None:
        return None
    text = str(raw).strip()
    match = re.match(
        r"^(\d{1,2})[-/\s]([A-Za-z]{3})[-/\s](\d{4})(?:\s+\d{1,2}:\d{2})?$", text
    )
    if match:
        day = int(match.group(1))
        month = _MONTHS.get(match.group(2).upper())
        if month:
            try:
                return date(int(match.group(3)), month, day)
            except ValueError:
                return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _parse_hhmm(raw: Any) -> Optional[time]:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    match = re.match(r"^(\d{1,2}):(\d{2})$", text)
    if match:
        try:
            return time(int(match.group(1)), int(match.group(2)))
        except ValueError:
            return None
    match = re.match(r"^(\d{1,2})(\d{2})$", text)
    if match:
        try:
            return time(int(match.group(1)), int(match.group(2)))
        except ValueError:
            return None
    return None


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _blank_data() -> dict[str, Any]:
    return {
        "format": _FORMAT,
        "version": _VERSION,
        "bootstrap": True,
        "fetched_at": None,
        "holiday_fetched_at": None,
        "status_fetched_at": None,
        "years": {},
        "last_status": {},
    }


def _extract_time(value: Any, keys: tuple[str, ...]) -> Optional[time]:
    if isinstance(value, dict):
        for key in keys:
            if key in value and value[key] not in (None, ""):
                return _parse_hhmm(value[key])
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            found = _extract_time(item, keys)
            if found is not None:
                return found
        return None
    return _parse_hhmm(value)


def _handle_holiday_item(
    item: Any,
    holidays: list[date],
    specials: list[dict[str, Any]],
) -> None:
    if not isinstance(item, dict):
        return
    raw = item.get("tradingDate") or item.get("tradingDate1") or item.get("date")
    stamp = _parse_nse_date(raw)
    if stamp is None:
        return
    morning = item.get("morning_session")
    evening = item.get("evening_session")
    description = str(item.get("description") or item.get("event") or "")
    if morning is None and evening is None:
        purpose = str(item.get("purpose") or "").lower()
        start = _parse_hhmm(item.get("startTime"))
        end = _parse_hhmm(item.get("endTime"))
        if ("trading" in purpose and "holiday" not in purpose) or start is not None or end is not None:
            specials.append({
                "date": stamp,
                "open": start.strftime("%H:%M") if start else None,
                "close": end.strftime("%H:%M") if end else None,
                "description": description,
            })
        else:
            holidays.append(stamp)
        return
    starts = [
        _extract_time(part, ("start", "start_time", "startTime", "starttime", "open"))
        for part in (morning, evening)
    ]
    ends = [
        _extract_time(part, ("end", "end_time", "endTime", "endtime", "close"))
        for part in (morning, evening)
    ]
    valid_starts = [t for t in starts if t is not None]
    valid_ends = [t for t in ends if t is not None]
    specials.append({
        "date": stamp,
        "open": min(valid_starts).strftime("%H:%M") if valid_starts else None,
        "close": max(valid_ends).strftime("%H:%M") if valid_ends else None,
        "description": description,
    })


def _parse_holiday_master(payload: Any) -> tuple[list[date], list[dict[str, Any]]]:
    holidays: list[date] = []
    specials: list[dict[str, Any]] = []

    if isinstance(payload, list):
        for item in payload:
            _handle_holiday_item(item, holidays, specials)
        return holidays, specials

    if not isinstance(payload, dict):
        return holidays, specials

    # Current NSE form: dict keyed by segment, e.g. {"CM": [...], "FO": [...]}.
    # "CM" is the capital-market (equity cash) holiday list used here.
    for key in ("CM",):
        segment = payload.get(key)
        if isinstance(segment, list):
            for item in segment:
                _handle_holiday_item(item, holidays, specials)
            return holidays, specials
    for value in payload.values():
        if isinstance(value, list):
            for item in value:
                _handle_holiday_item(item, holidays, specials)
            return holidays, specials

    # Legacy form: {"trading": {...}, "holidays": [...]}.
    trading = payload.get("trading")
    if isinstance(trading, dict):
        for raw_date in trading.get("dates") or []:
            stamp = _parse_nse_date(raw_date)
            if stamp is not None:
                holidays.append(stamp)
    for item in payload.get("holidays") or []:
        _handle_holiday_item(item, holidays, specials)
    return holidays, specials


def _parse_market_status(payload: Any) -> dict[str, Any]:
    state: dict[str, Any] = {}
    if not isinstance(payload, dict):
        return state
    entries = payload.get("marketState")
    if not isinstance(entries, list):
        return state
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        market = str(entry.get("market") or "").lower()
        if market != "nse" and "capital" not in market:
            continue
        trade_stamp = _parse_nse_date(entry.get("tradeDate"))
        state = {
            "market_status": str(entry.get("marketStatus") or ""),
            "trade_date": trade_stamp.isoformat() if trade_stamp else None,
            "market_type": str(entry.get("marketType") or ""),
            "status_message": str(
                entry.get("marketStatusMessage")
                or entry.get("statusMessage")
                or ""
            ),
            "session_description": str(
                entry.get("sessionDescription")
                or entry.get("marketStatusMessage")
                or ""
            ),
        }
        break
    return state


def _get_json(session: requests.Session, url: str, timeout: int) -> Any:
    response = session.get(url, timeout=timeout)
    if response.status_code != 200:
        raise RuntimeError(
            f"NSE calendar endpoint returned HTTP {response.status_code} for {url}"
        )
    content_type = response.headers.get("Content-Type", "")
    if "json" not in content_type.lower():
        raise RuntimeError(
            f"NSE calendar endpoint returned non-JSON content for {url}"
        )
    return response.json()


def fetch_official_nse_payloads(
    include_holidays: bool = True,
    timeout: int = 15,
) -> tuple[Optional[Any], Any]:
    """Fetch official NSE data. Returns ``(holiday_payload, status_payload)``.

    ``holiday_payload`` is None when ``include_holidays`` is False; the market
    status payload is always fetched so intraday open/close checks stay fresh.
    Raises RuntimeError when NSE is unreachable or blocks the request; the app
    keeps using its validated disk cache in that case.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": _NSE_BASE,
    }
    session = requests.Session()
    session.headers.update(headers)
    session.get(_NSE_BASE, timeout=timeout)

    holiday_payload: Optional[Any] = None
    if include_holidays:
        try:
            holiday_payload = _get_json(session, _HOLIDAY_ENDPOINT, timeout)
        except Exception:
            session.get(_NSE_BASE, timeout=timeout)
            holiday_payload = _get_json(session, _HOLIDAY_ENDPOINT, timeout)
    try:
        status_payload = _get_json(session, _STATUS_ENDPOINT, timeout)
    except Exception:
        session.get(_NSE_BASE, timeout=timeout)
        status_payload = _get_json(session, _STATUS_ENDPOINT, timeout)
    return holiday_payload, status_payload


class NSETradingCalendar:
    """NSE trading calendar backed by a JSON cache.

    ``path`` points at the cache file (``nse_trading_calendar.json`` next to
    the app). A missing or unreadable cache is rebuilt from bootstrap data and
    rewritten so the app can always start; the official NSE payload fills in
    real holiday data on the first refresh.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._data = self._load()

    # ------------------------------------------------------------------ io

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict) and data.get("format") == _FORMAT:
                    data.setdefault("version", _VERSION)
                    data.setdefault("bootstrap", True)
                    data.setdefault("fetched_at", None)
                    data.setdefault("holiday_fetched_at", None)
                    data.setdefault("status_fetched_at", None)
                    data.setdefault("years", {})
                    data.setdefault("last_status", {})
                    return data
            except Exception as exc:
                print(
                    f"[nse_calendar] cache {self.path} unreadable ({exc}); rebuilding",
                    file=sys.stderr,
                )
        data = _blank_data()
        self._seed_all(data)
        self._save(data)
        return data

    def _save(self, data: Optional[dict[str, Any]] = None) -> None:
        data = data or self._data
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(self.path.parent),
            prefix=self.path.name + ".",
            suffix=".tmp",
            delete=False,
        )
        try:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(handle.name, self.path)

    # ------------------------------------------------------------ seeding

    def _seed_all(self, data: Optional[dict[str, Any]] = None) -> None:
        data = data or self._data
        today = date.today()
        for year in range(today.year - 12, today.year + 3):
            self._ensure_year(data, year)

    def _ensure_year(self, data: Optional[dict[str, Any]], year: int) -> dict[str, Any]:
        data = data or self._data
        key = str(year)
        existing = data["years"].get(key)
        if existing is not None:
            existing.setdefault("holidays", [])
            existing.setdefault("special", {})
            return existing
        entry: dict[str, Any] = {"holidays": [], "special": {}}
        for month, day in _FIXED_HOLIDAYS:
            try:
                stamp = date(year, month, day)
            except ValueError:
                continue
            if stamp.weekday() < 5:
                entry["holidays"].append(stamp.isoformat())
        data["years"][key] = entry
        return entry

    # ------------------------------------------------------------ queries

    def snapshot(self) -> dict[str, Any]:
        years = self._data.get("years", {})
        return {
            "format": self._data.get("format"),
            "version": self._data.get("version"),
            "bootstrap": self._data.get("bootstrap", True),
            "fetched_at": self._data.get("fetched_at"),
            "holiday_fetched_at": self._data.get("holiday_fetched_at"),
            "status_fetched_at": self._data.get("status_fetched_at"),
            "years": {
                year: {
                    "holiday_count": len(entry.get("holidays", [])),
                    "special_count": len(entry.get("special", {})),
                }
                for year, entry in sorted(years.items())
            },
            "last_status": self._data.get("last_status", {}),
        }

    def is_session_date(self, value: Any) -> bool:
        stamp = _to_date(value)
        entry = self._ensure_year(self._data, stamp.year)
        iso = stamp.isoformat()
        if iso in entry.get("special", {}):
            return True
        if stamp.weekday() >= 5:
            return False
        return iso not in entry.get("holidays", [])

    def _special_entry(self, stamp: date) -> Optional[dict[str, Any]]:
        entry = self._ensure_year(self._data, stamp.year)
        return entry.get("special", {}).get(stamp.isoformat())

    def session_open_time(self, value: Any, default_open: time) -> time:
        stamp = _to_date(value)
        special = self._special_entry(stamp)
        if special:
            parsed = _parse_hhmm(special.get("open"))
            if parsed is not None:
                return parsed
        return default_open

    def session_close_time(self, value: Any, default_close: time) -> Optional[time]:
        stamp = _to_date(value)
        if not self.is_session_date(stamp):
            return None
        special = self._special_entry(stamp)
        if special:
            parsed = _parse_hhmm(special.get("close"))
            if parsed is None:
                return None
            return parsed
        return default_close

    def is_open_at(
        self,
        value: Any,
        regular_open: time,
        regular_close: time,
    ) -> bool:
        stamp = _to_date(value)
        if not self.is_session_date(stamp):
            return False
        close = self.session_close_time(stamp, regular_close)
        if close is None:
            return False
        opened = self.session_open_time(stamp, regular_open)
        current = value.time() if isinstance(value, datetime) else datetime.combine(stamp, value.time()).time()
        return opened <= current < close

    def previous_session(self, value: Any) -> date:
        stamp = _to_date(value)
        cursor = stamp
        for _ in range(400):
            if self.is_session_date(cursor):
                return cursor
            cursor = cursor - timedelta(days=1)
        raise RuntimeError("unable to locate a previous NSE trading session")

    def next_session(self, value: Any) -> date:
        stamp = _to_date(value)
        cursor = stamp
        for _ in range(400):
            if self.is_session_date(cursor):
                return cursor
            cursor = cursor + timedelta(days=1)
        raise RuntimeError("unable to locate a next NSE trading session")

    def period_final_session(self, value: Any, timeframe: str) -> date:
        stamp = _to_date(value)
        if timeframe == "D":
            return self.previous_session(stamp)
        if timeframe == "W":
            period_end = stamp + timedelta(days=6 - stamp.weekday())
            return self.previous_session(period_end)
        if timeframe == "M":
            first_next = (stamp.replace(day=1) + timedelta(days=32)).replace(day=1)
            period_end = first_next - timedelta(days=1)
            return self.previous_session(period_end)
        raise ValueError(f"unsupported timeframe {timeframe!r}; expected one of {_TIMEFRAME_VALUES}")

    def require_coverage(self, start: Any, end: Any) -> None:
        first = _to_date(start)
        last = _to_date(end)
        missing = [
            str(year) for year in range(first.year, last.year + 1)
            if str(year) not in self._data.get("years", {})
        ]
        if missing:
            raise RuntimeError(
                "NSE trading calendar lacks data for year(s): "
                + ", ".join(sorted(missing))
                + ". Refresh the calendar and try again."
            )

    # ------------------------------------------------------------- update

    def update_from_official_payloads(
        self,
        holiday_payload: Any,
        status_payload: Any,
        now: Optional[datetime] = None,
    ) -> str:
        now = now or datetime.now()
        notes: list[str] = []
        changed = False

        if holiday_payload:
            holidays, specials = _parse_holiday_master(holiday_payload)
            for stamp in holidays:
                entry = self._ensure_year(self._data, stamp.year)
                iso = stamp.isoformat()
                if iso not in entry["holidays"]:
                    entry["holidays"].append(iso)
                    changed = True
            for special in specials:
                stamp = special["date"]
                entry = self._ensure_year(self._data, stamp.year)
                key = stamp.isoformat()
                existing = entry["special"].get(key)
                serialized = {
                    "open": special.get("open"),
                    "close": special.get("close"),
                    "description": special.get("description"),
                }
                if existing != serialized:
                    entry["special"][key] = serialized
                    changed = True
            self._data["holiday_fetched_at"] = _iso(now)
            self._data["bootstrap"] = False
            notes.append(f"{len(holidays)} holidays, {len(specials)} special session(s)")

        if status_payload:
            state = _parse_market_status(status_payload)
            if state:
                self._data["last_status"] = state
                changed = True
            self._data["status_fetched_at"] = _iso(now)
            notes.append("market status")

        if changed or holiday_payload or status_payload:
            self._data["fetched_at"] = _iso(now)
            self._save()

        return "Updated calendar from NSE: " + "; ".join(notes) if notes else "Calendar unchanged"
