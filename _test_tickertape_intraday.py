from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import sys
import types

import tickertape_intraday as provider


class _FakeClient:
    def screener_query(self, _query):
        return {
            "results": [
                {"sid": "GOOD", "stock": {"info": {"ticker": "GOOD"}}},
                {"sid": "DUPB", "stock": {"info": {"ticker": "DUP"}}},
                {"sid": "DUPN", "stock": {"info": {"ticker": "DUP"}}},
                {"sid": "BAD", "stock": {"info": {"ticker": "BAD"}}},
                {"sid": "OLD", "stock": {"info": {"ticker": "OLD"}}},
            ]
        }

    def india_quotes(self, sids):
        expected = date(2026, 9, 17)
        current = int(
            datetime.combine(
                expected,
                datetime.min.time(),
                timezone(timedelta(hours=5, minutes=30)),
            ).timestamp()
            * 1000
        )
        stale = current - 86_400_000
        rows = {
            "GOOD": dict(sid="GOOD", exchange="NSE", price=105, o=100, h=110, l=95, c=99, vol=50, ts=current),
            "DUPB": dict(sid="DUPB", exchange="BSE", price=2, o=2, h=2, l=2, c=2, vol=2, ts=current),
            "DUPN": dict(sid="DUPN", exchange="NSE", price=205, o=200, h=210, l=195, c=199, vol=60, ts=current),
            "BAD": dict(sid="BAD", exchange="NSE", price=10, o=0, h=11, l=9, c=9, vol=10, ts=current),
            "OLD": dict(sid="OLD", exchange="NSE", price=10, o=10, h=11, l=9, c=9, vol=10, ts=stale),
        }
        return [rows[sid] for sid in sids if sid in rows]

    def close(self):
        return None


def main() -> None:
    original_factory = provider._new_client
    original_mapping = provider._ticker_to_sids
    try:
        provider._ticker_to_sids = {}
        provider._new_client = lambda: _FakeClient()
        result = provider.fetch_intraday_quotes(
            ["GOOD", "DUP", "BAD", "OLD", "MISSING"],
            date(2026, 9, 17),
            batch_size=2,
        )
        assert set(result.quotes) == {"GOOD", "DUP"}
        assert result.quotes["DUP"].sid == "DUPN"
        assert result.quotes["GOOD"].close == 105
        assert "non-positive" in result.rejected["BAD"]
        assert "stale" in result.rejected["OLD"]
        assert "no Tickertape SID" in result.rejected["MISSING"]
        assert result.request_count == 3
        assert not result.error

        provider.set_session_price_mode(provider.TICKERTAPE_PRICE_MODE)
        assert provider.get_session_price_mode() == provider.TICKERTAPE_PRICE_MODE
        provider.set_session_price_mode(provider.ORIGINAL_PRICE_MODE)
        assert provider.get_session_price_mode() == provider.ORIGINAL_PRICE_MODE

        # The pre-scan catch-up must update only the missing tail and cache a
        # successful result so several rules in one process do not redownload it.
        fake_module = types.ModuleType("ChartSageBhavcopy")
        state = {"last": "2026-09-15", "runs": 0}

        class FakeDatabase:
            def stats(self):
                return {"last_date": state["last"]}

            def status(self, target):
                return "ok" if target.isoformat() <= state["last"] else None

        class FakeSummary:
            downloaded = 2
            already_present = 0
            unavailable = 0
            failed = 0

        class FakeFeeder:
            def __init__(self, database, delay_seconds):
                assert isinstance(database, FakeDatabase)
                assert delay_seconds == 0.0

            def run(self, start, end, **kwargs):
                assert start == date(2026, 9, 16)
                assert end == date(2026, 9, 17)
                state["runs"] += 1
                state["last"] = end.isoformat()
                return FakeSummary()

        fake_module.BhavcopyDatabase = FakeDatabase
        fake_module.BhavcopyFeeder = FakeFeeder
        fake_module.default_end_date = lambda: date(2026, 9, 17)
        previous_module = sys.modules.get("ChartSageBhavcopy")
        sys.modules["ChartSageBhavcopy"] = fake_module
        try:
            provider._bhavcopy_sync_results = {}
            sync = provider.ensure_recent_bhavcopy(date(2026, 9, 17))
            assert sync.ready and sync.downloaded == 2 and not sync.error
            cached_sync = provider.ensure_recent_bhavcopy(date(2026, 9, 17))
            assert cached_sync == sync and state["runs"] == 1
        finally:
            provider._bhavcopy_sync_results = {}
            if previous_module is None:
                sys.modules.pop("ChartSageBhavcopy", None)
            else:
                sys.modules["ChartSageBhavcopy"] = previous_module
    finally:
        provider._new_client = original_factory
        provider._ticker_to_sids = original_mapping
    print("tickertape intraday tests passed")


if __name__ == "__main__":
    main()
