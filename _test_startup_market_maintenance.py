from __future__ import annotations

import sys
import tempfile
import types
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import startup_market_maintenance as maintenance


class _Summary:
    downloaded = 2
    failed = 0


class StartupMarketMaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.old_values = (
            maintenance.APP_DIR,
            maintenance.STATE_PATH,
            maintenance.LOCK_PATH,
        )
        maintenance.APP_DIR = self.root
        maintenance.STATE_PATH = self.root / "market_data_maintenance_state.json"
        maintenance.LOCK_PATH = self.root / "market_data_maintenance.lock"

    def tearDown(self) -> None:
        maintenance.APP_DIR, maintenance.STATE_PATH, maintenance.LOCK_PATH = self.old_values
        self.temporary.cleanup()

    def _modules(self, *, current: bool):
        target = date(2026, 9, 17)
        calls = {"prices": 0, "actions": 0, "membership": 0}

        class Database:
            target_status = "ok" if current else None

            def stats(self):
                return {"last_date": "2026-09-17" if current else "2026-09-15"}

            def status(self, _day):
                return type(self).target_status

        class Feeder:
            def __init__(self, **_kwargs):
                pass

            def run(self, _start, _end, **kwargs):
                calls["prices"] += 1
                kwargs["progress"](1, 1, "prices complete")
                Database.target_status = "ok"
                return _Summary()

            def update_corporate_actions(self, _start, _end, progress):
                calls["actions"] += 1
                progress(1, 1, "actions complete")
                return {"supported": 12, "ambiguous": 1}

        bhavcopy = types.ModuleType("ChartSageBhavcopy")
        bhavcopy.EARLIEST_SUPPORTED_DATE = date(2014, 1, 1)
        bhavcopy.BhavcopyDatabase = Database
        bhavcopy.BhavcopyFeeder = Feeder
        bhavcopy.default_end_date = lambda: target

        membership = types.ModuleType("membership_update")
        membership.membership_coverage_summary = lambda _root: {
            "supported_indices": 22,
            "verified_indices": 22 if current else 21,
            "end_date": "2999-12-31" if current else "2026-09-16",
        }

        def update(_root, progress):
            calls["membership"] += 1
            progress(6, 6, "membership complete")
            return {
                "verified_indices": 22,
                "end_date": "2999-12-31",
            }

        membership.update_historical_membership = update
        return calls, {"ChartSageBhavcopy": bhavcopy, "membership_update": membership}

    def test_all_three_updates_run_when_stale(self):
        calls, modules = self._modules(current=False)
        with mock.patch.dict(sys.modules, modules):
            report = maintenance.run_startup_market_maintenance()
        self.assertEqual(calls, {"prices": 1, "actions": 1, "membership": 1})
        self.assertEqual([step.status for step in report.steps], ["updated"] * 3)
        self.assertFalse(maintenance.LOCK_PATH.exists())
        self.assertTrue(maintenance.STATE_PATH.is_file())

    def test_current_steps_are_skipped_without_network_work(self):
        calls, modules = self._modules(current=True)
        maintenance.STATE_PATH.write_text(
            '{"corporate_actions":{"through":"2026-09-17"}}',
            encoding="utf-8",
        )
        with mock.patch.dict(sys.modules, modules):
            report = maintenance.run_startup_market_maintenance()
        self.assertEqual(calls, {"prices": 0, "actions": 0, "membership": 0})
        self.assertEqual([step.status for step in report.steps], ["current"] * 3)

    def test_existing_lock_prevents_duplicate_update(self):
        maintenance.LOCK_PATH.write_text("busy", encoding="utf-8")
        report = maintenance.run_startup_market_maintenance()
        self.assertTrue(report.already_running)
        self.assertEqual([step.status for step in report.steps], ["skipped"] * 3)


if __name__ == "__main__":
    unittest.main()
