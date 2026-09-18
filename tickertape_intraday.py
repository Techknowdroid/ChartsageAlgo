"""Optional Tickertape batch quotes for latest-session scanner candles.

The provider is deliberately isolated from the scanner engines.  It supplies
only the current NSE session's unfinished candle; official Bhavcopy history
remains authoritative for every completed session.  Invalid, stale or missing
quotes are returned as rejections so callers can use their original provider.
"""

from __future__ import annotations

import json
import math
import ssl
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable


ORIGINAL_PRICE_MODE = "original"
TICKERTAPE_PRICE_MODE = "tickertape"
TICKERTAPE_SOURCE = "Tickertape Batch Quote"
# The public quote endpoint accepted the complete NSE ALL universe reliably in
# 500-SID chunks.  This keeps full-market refreshes to roughly six requests
# while retaining the existing per-symbol validation and fallback behavior.
DEFAULT_BATCH_SIZE = 500

_session_price_mode = ORIGINAL_PRICE_MODE
_mapping_lock = threading.RLock()
_ticker_to_sids: dict[str, list[str]] = {}
_bhavcopy_sync_lock = threading.RLock()
_bhavcopy_sync_results: dict[date, "BhavcopySyncResult"] = {}


def set_session_price_mode(mode: str) -> None:
    """Select the latest-price provider for this process only."""
    global _session_price_mode
    selected = str(mode).strip().lower()
    if selected not in {ORIGINAL_PRICE_MODE, TICKERTAPE_PRICE_MODE}:
        raise ValueError(f"Unsupported price mode: {mode!r}")
    _session_price_mode = selected


def get_session_price_mode() -> str:
    return _session_price_mode


def session_price_mode_label() -> str:
    if _session_price_mode == TICKERTAPE_PRICE_MODE:
        return "TICKERTAPE batch prices"
    return "Original price approach"


@dataclass(frozen=True)
class IntradayQuote:
    symbol: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    provider_data_time: str
    sid: str
    source: str = TICKERTAPE_SOURCE


@dataclass
class BatchQuoteResult:
    quotes: dict[str, IntradayQuote] = field(default_factory=dict)
    rejected: dict[str, str] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    request_count: int = 0
    mapping_downloaded: bool = False
    error: str = ""


@dataclass(frozen=True)
class BhavcopySyncResult:
    """Outcome of the small pre-scan official-history catch-up."""

    target_date: date
    start_date: date | None = None
    covered_through: date | None = None
    downloaded: int = 0
    already_present: int = 0
    unavailable: int = 0
    failed: int = 0
    error: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.covered_through and self.covered_through >= self.target_date)


def ensure_recent_bhavcopy(
    target_date: date,
    progress: Callable[[str], None] | None = None,
) -> BhavcopySyncResult:
    """Bring official prices through ``target_date`` before a batch overlay.

    Tickertape supplies only the current unfinished candle.  Missing completed
    sessions must therefore come from the official Bhavcopy archive.  This
    helper performs only the incremental tail update; it never rebuilds the
    historical database.  Any failure is returned to the scanner, which keeps
    its existing safe per-symbol downloader fallback.
    """

    progress = progress or (lambda _message: None)
    with _bhavcopy_sync_lock:
        cached = _bhavcopy_sync_results.get(target_date)
        if cached is not None:
            return cached
        try:
            from ChartSageBhavcopy import (
                BhavcopyDatabase,
                BhavcopyFeeder,
                default_end_date,
            )

            effective_target = min(target_date, default_end_date())
            database = BhavcopyDatabase()
            before = database.stats()
            raw_last = before.get("last_date")
            if not raw_last:
                result = BhavcopySyncResult(
                    target_date=target_date,
                    error=(
                        "Official Bhavcopy history is empty; run Start / Resume "
                        "Backfill before using Tickertape batch prices."
                    ),
                )
                _bhavcopy_sync_results[target_date] = result
                return result
            last_date = date.fromisoformat(str(raw_last))
            if last_date < effective_target:
                start_date = last_date + timedelta(days=1)
                progress(
                    "Updating official Bhavcopy history "
                    f"{start_date.isoformat()} to {effective_target.isoformat()}..."
                )
                feeder = BhavcopyFeeder(database=database, delay_seconds=0.0)
                summary = feeder.run(
                    start_date,
                    effective_target,
                    retry_failed=True,
                    progress=lambda done, total, message: progress(
                        f"Bhavcopy {done}/{total}: {message}"
                    ),
                )
            else:
                start_date = None
                summary = None
            after = database.stats()
            covered_through = (
                date.fromisoformat(str(after["last_date"]))
                if after.get("last_date")
                else None
            )
            target_status = database.status(effective_target)
            if target_status in {"ok", "not_published"}:
                # A verified non-publication (normally a holiday) proves the
                # session boundary just as safely as a downloaded file.
                covered_through = max(
                    filter(None, (covered_through, effective_target)),
                )
            result = BhavcopySyncResult(
                target_date=target_date,
                start_date=start_date,
                covered_through=covered_through,
                downloaded=int(summary.downloaded if summary else 0),
                already_present=int(summary.already_present if summary else 0),
                unavailable=int(summary.unavailable if summary else 0),
                failed=int(summary.failed if summary else 0),
                error=(
                    ""
                    if covered_through and covered_through >= effective_target
                    else "Official Bhavcopy history remains behind the required date."
                ),
            )
        except Exception as exc:
            result = BhavcopySyncResult(
                target_date=target_date,
                error=f"{type(exc).__name__}: {exc}",
            )
        _bhavcopy_sync_results[target_date] = result
        return result


class _TickertapePublicClient:
    """Small dependency-free adapter for the two public endpoints we use.

    Its endpoint shapes and browser-like headers mirror the open-source
    ``tickertape-api-client``.  Keeping this adapter local avoids adding an
    HTTP stack to the portable suite merely to call two public JSON endpoints.
    """

    API_BASE = "https://api.tickertape.in"
    QUOTES_BASE = "https://quotes-api.tickertape.in"
    HEADERS = {
        "Accept": "application/json,text/plain,*/*",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/125 Safari/537.36"
        ),
        "Referer": "https://www.tickertape.in/",
        "Origin": "https://www.tickertape.in",
    }

    def __init__(self) -> None:
        self._ssl_context = ssl.create_default_context()
        # Python 3.13 enables OpenSSL's strict X.509 mode by default. Some
        # otherwise trusted Windows CA chains omit a critical Basic Constraints
        # marker, which strict mode rejects. Clearing only that optional strict
        # flag retains CA-chain and hostname verification.
        self._ssl_context.verify_flags &= ~ssl.VERIFY_X509_STRICT

    @staticmethod
    def _unwrap(payload: Any) -> Any:
        if not isinstance(payload, dict):
            raise RuntimeError("Tickertape returned an unexpected JSON shape.")
        if payload.get("success") is False:
            raise RuntimeError(str(payload.get("message") or "Tickertape API error"))
        return payload.get("data", payload)

    def _request(
        self,
        method: str,
        url: str,
        body: dict[str, Any] | None = None,
    ) -> Any:
        encoded = None
        headers = dict(self.HEADERS)
        if body is not None:
            encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            url,
            data=encoded,
            headers=headers,
            method=method,
        )
        with urllib.request.urlopen(
            request,
            timeout=30,
            context=self._ssl_context,
        ) as response:
            return self._unwrap(json.loads(response.read().decode("utf-8")))

    def screener_query(self, query: dict[str, Any]) -> Any:
        return self._request("POST", f"{self.API_BASE}/screener/query", query)

    def india_quotes(self, sids: list[str]) -> Any:
        query = urllib.parse.urlencode({"sids": ",".join(sids)})
        return self._request("GET", f"{self.QUOTES_BASE}/quotes?{query}")

    def close(self) -> None:
        return None


def _new_client() -> _TickertapePublicClient:
    return _TickertapePublicClient()


def _load_sid_mapping(client: Any) -> tuple[dict[str, list[str]], bool]:
    global _ticker_to_sids
    with _mapping_lock:
        if _ticker_to_sids:
            return _ticker_to_sids, False
        payload = client.screener_query(
            {
                "match": {},
                "sortBy": "mrktCapf",
                "sortOrder": -1,
                "offset": 0,
                "count": 10000,
                "project": ["sector", "subindustry", "mrktCapRank"],
            }
        )
        mapping: dict[str, list[str]] = {}
        for item in payload.get("results", []) if isinstance(payload, dict) else []:
            if not isinstance(item, dict):
                continue
            ticker = str(
                item.get("stock", {}).get("info", {}).get("ticker", "")
            ).strip().upper()
            sid = str(item.get("sid", "")).strip()
            if ticker and sid and sid not in mapping.setdefault(ticker, []):
                mapping[ticker].append(sid)
        if not mapping:
            raise RuntimeError("Tickertape returned no ticker-to-SID mapping.")
        _ticker_to_sids = mapping
        return _ticker_to_sids, True


def _finite_positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _quote_from_row(
    symbol: str,
    sid: str,
    row: dict[str, Any],
    expected_date: date,
) -> tuple[IntradayQuote | None, str]:
    if str(row.get("exchange", "")).strip().upper() != "NSE":
        return None, "quote is not for NSE"
    open_price = _finite_positive(row.get("o"))
    high = _finite_positive(row.get("h"))
    low = _finite_positive(row.get("l"))
    latest = _finite_positive(row.get("price"))
    volume = _finite_positive(row.get("vol"))
    if None in (open_price, high, low, latest, volume):
        return None, "incomplete or non-positive OHLCV"
    if not (low <= open_price <= high and low <= latest <= high):
        return None, "inconsistent OHLC range"
    try:
        stamp = datetime.fromtimestamp(
            int(row.get("ts")) / 1000.0,
            tz=timezone.utc,
        ).astimezone(timezone(timedelta(hours=5, minutes=30)))
    except (TypeError, ValueError, OverflowError):
        return None, "invalid market timestamp"
    if stamp.date() != expected_date:
        return None, f"stale market timestamp ({stamp.date().isoformat()})"
    return (
        IntradayQuote(
            symbol=symbol,
            trade_date=expected_date,
            open=float(open_price),
            high=float(high),
            low=float(low),
            close=float(latest),
            volume=float(volume),
            provider_data_time=stamp.isoformat(),
            sid=sid,
        ),
        "",
    )


def fetch_intraday_quotes(
    symbols: Iterable[str],
    expected_date: date,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> BatchQuoteResult:
    """Fetch validated current-session NSE OHLCV, rejecting unsafe records."""
    started = time.perf_counter()
    clean_symbols = list(
        dict.fromkeys(str(symbol).strip().upper() for symbol in symbols if str(symbol).strip())
    )
    result = BatchQuoteResult()
    if not clean_symbols:
        return result
    client = None
    try:
        client = _new_client()
        mapping, result.mapping_downloaded = _load_sid_mapping(client)
        candidates: dict[str, list[str]] = {
            symbol: list(mapping.get(symbol, [])) for symbol in clean_symbols
        }
        all_sids = list(
            dict.fromkeys(
                sid for symbol in clean_symbols for sid in candidates.get(symbol, [])
            )
        )
        rows_by_sid: dict[str, dict[str, Any]] = {}
        safe_batch_size = max(1, min(500, int(batch_size)))
        for start in range(0, len(all_sids), safe_batch_size):
            rows = client.india_quotes(all_sids[start : start + safe_batch_size])
            result.request_count += 1
            for row in rows if isinstance(rows, list) else []:
                if isinstance(row, dict) and str(row.get("sid", "")).strip():
                    rows_by_sid[str(row["sid"]).strip()] = row

        for symbol in clean_symbols:
            symbol_candidates = candidates.get(symbol, [])
            if not symbol_candidates:
                result.rejected[symbol] = "no Tickertape SID mapping"
                continue
            nse_candidates = [
                sid
                for sid in symbol_candidates
                if sid in rows_by_sid
                and str(rows_by_sid[sid].get("exchange", "")).strip().upper() == "NSE"
            ]
            returned_candidates = [sid for sid in symbol_candidates if sid in rows_by_sid]
            selected = (nse_candidates or returned_candidates)
            if not selected:
                result.rejected[symbol] = "Tickertape returned no quote"
                continue
            quote, reason = _quote_from_row(
                symbol,
                selected[0],
                rows_by_sid[selected[0]],
                expected_date,
            )
            if quote is None:
                result.rejected[symbol] = reason
            else:
                result.quotes[symbol] = quote
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        for symbol in clean_symbols:
            result.rejected.setdefault(symbol, result.error)
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        result.elapsed_seconds = time.perf_counter() - started
    return result
