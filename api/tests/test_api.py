"""Tests for the Stage 1 deploy service (api/app.py).

Covers:
- the scheduler's once-a-day logic with a mocked clock,
- the /health endpoint (TestClient),
- the /status endpoint on a seeded temp DB,
- a missing ODDS_API_KEY making the odds leg refuse cleanly instead of
  crashing,
- cycle_is_complete from the DB.

No live calls, no keys. The scheduler thread is disabled on import
(PICKS_API_NO_SCHEDULER=1) so tests never start background work.
Importing app touches the real fairline.db only idempotently
(schema init + ledger open check); every test below uses a temp DB.
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

os.environ["PICKS_API_NO_SCHEDULER"] = "1"

API_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = API_DIR.parent
sys.path.insert(0, str(API_DIR))
sys.path.insert(0, str(PROJECT_DIR))

import app as picks_app  # noqa: E402
import ledger as ledger_mod  # noqa: E402
import store  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ET = ZoneInfo("America/New_York")


def _et(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET)


class SchedulerLogicTest(unittest.TestCase):
    def test_before_8am_never_runs(self):
        self.assertFalse(
            picks_app.should_run(_et(2026, 11, 17, 7, 59), None, None, False)
        )

    def test_at_8am_first_time_runs(self):
        self.assertTrue(
            picks_app.should_run(_et(2026, 11, 17, 8, 0), None, None, False)
        )

    def test_after_8am_first_time_runs(self):
        self.assertTrue(
            picks_app.should_run(_et(2026, 11, 17, 12, 30), None, None, False)
        )

    def test_completed_today_does_not_rerun(self):
        now = _et(2026, 11, 17, 8, 30)
        self.assertFalse(
            picks_app.should_run(now, "2026-11-17", now.timestamp() - 600, True)
        )

    def test_incomplete_today_retries_only_after_an_hour(self):
        now = _et(2026, 11, 17, 9, 0)
        self.assertFalse(
            picks_app.should_run(now, "2026-11-17", now.timestamp() - 1800, False)
        )
        now2 = _et(2026, 11, 17, 10, 30)
        self.assertTrue(
            picks_app.should_run(now2, "2026-11-17", now2.timestamp() - 7200, False)
        )

    def test_incomplete_today_stops_retrying_after_20_00(self):
        now = _et(2026, 11, 17, 21, 0)
        self.assertFalse(
            picks_app.should_run(now, "2026-11-17", now.timestamp() - 7200, False)
        )

    def test_next_day_runs_again(self):
        now = _et(2026, 11, 18, 8, 0)
        self.assertFalse(
            picks_app.should_run(_et(2026, 11, 18, 7, 0), "2026-11-17",
                                 now.timestamp() - 86400, True)
        )
        self.assertTrue(
            picks_app.should_run(now, "2026-11-17", now.timestamp() - 86400, True)
        )


class CycleCompleteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        self.conn = store.init_db(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _seed_odds(self, sports):
        for sport in sports:
            store.insert_odds_snapshot(self.conn, {
                "sport_key": sport, "cycle_date": "2026-11-16",
                "fetched_at": store.utcnow_iso(), "credits_used": 1,
                "requests_remaining": 499, "events": 12,
                "status": "ok", "flags": [], "raw": [],
            })

    def test_complete_when_kalshi_and_all_four_odds_present(self):
        store.insert_kalshi_snapshot(self.conn, {
            "ticker": "KXNFLGAME-TEST", "series": "KXNFLGAME",
            "cycle_date": "2026-11-16", "fetched_at": store.utcnow_iso(),
            "yes_bid": 0.5, "yes_ask": 0.51, "yes_levels": 1, "no_levels": 1,
            "market_title": "test", "close_ts": None,
            "status": "ok", "flags": [], "raw": {},
        })
        self._seed_odds([
            "americanfootball_nfl", "basketball_nba",
            "basketball_ncaab", "icehockey_nhl",
        ])
        self.assertTrue(picks_app.cycle_is_complete(self.conn, "2026-11-16"))

    def test_incomplete_when_an_odds_sport_is_missing(self):
        store.insert_kalshi_snapshot(self.conn, {
            "ticker": "KXNFLGAME-TEST", "series": "KXNFLGAME",
            "cycle_date": "2026-11-16", "fetched_at": store.utcnow_iso(),
            "yes_bid": 0.5, "yes_ask": 0.51, "yes_levels": 1, "no_levels": 1,
            "market_title": "test", "close_ts": None,
            "status": "ok", "flags": [], "raw": {},
        })
        self._seed_odds(["americanfootball_nfl", "basketball_nba"])
        self.assertFalse(picks_app.cycle_is_complete(self.conn, "2026-11-16"))

    def test_incomplete_when_no_kalshi_snapshots(self):
        self._seed_odds([
            "americanfootball_nfl", "basketball_nba",
            "basketball_ncaab", "icehockey_nhl",
        ])
        self.assertFalse(picks_app.cycle_is_complete(self.conn, "2026-11-16"))


class MissingKeyTest(unittest.TestCase):
    """A missing ODDS_API_KEY makes the odds leg refuse cleanly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        self.conn = store.init_db(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_odds_leg_refuses_without_key_and_without_stub(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("ODDS_API_KEY", "ODDS_API_STUB")}
        with mock.patch.dict(os.environ, env, clear=True):
            result = picks_app.run_odds_leg(self.conn, "2026-11-16")
        # No exception, no HTTP call: polled 0, reason in the notes.
        self.assertEqual(result["polled"], 0)
        self.assertEqual(result["ok"], 0)
        self.assertTrue(
            any("ODDS_API_KEY" in n for n in result["notes"]),
            f"notes should name the missing key: {result['notes']}",
        )
        # The refusal is recorded as a run row, so /status can show it.
        rows = self.conn.execute(
            "SELECT status FROM capture_runs WHERE part = 'odds' "
            "AND cycle_date = '2026-11-16'"
        ).fetchall()
        self.assertEqual(len(rows), 1)

    def test_run_daily_cycle_reports_partial_not_crash_on_missing_key(self):
        # run_daily_cycle calls the live Kalshi leg, so it is NOT run here.
        # The contract it relies on is the odds leg above: it returns a
        # summary dict instead of raising. Assert that directly.
        env = {k: v for k, v in os.environ.items()
               if k not in ("ODDS_API_KEY", "ODDS_API_STUB")}
        with mock.patch.dict(os.environ, env, clear=True):
            odds = picks_app.run_odds_leg(self.conn, "2026-11-16")
        self.assertIn("polled", odds)
        self.assertFalse(picks_app.cycle_is_complete(self.conn, "2026-11-16"))


class EndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        self.conn = store.init_db(self.db)
        ledger_mod.open_ledger(self.conn)
        self._orig_db_path = picks_app.DB_PATH
        picks_app.DB_PATH = self.db
        self.client = TestClient(picks_app.app)

    def tearDown(self):
        picks_app.DB_PATH = self._orig_db_path
        self.conn.close()
        self.tmp.cleanup()

    def test_health(self):
        env = {k: v for k, v in os.environ.items() if k != "ODDS_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["service"], "fair-line-picks-bot-stage1")
        # Scheduler is disabled in tests; the endpoint says so honestly.
        self.assertIs(body["scheduler_alive"], False)
        # The key is never reported — only whether one is set.
        self.assertIs(body["odds_api_key_set"], False)
        self.assertNotIn("apiKey", resp.text)
        self.assertIsNone(body["last_scan_time"])
        self.assertIsNone(body["last_run_status"])
        self.assertEqual(body["recent_errors"], [])

    def test_status_reports_credits_and_ledger(self):
        today = datetime.now(ET).strftime("%Y-%m-%d")
        store.insert_odds_snapshot(self.conn, {
            "sport_key": "americanfootball_nfl", "cycle_date": today,
            "fetched_at": store.utcnow_iso(), "credits_used": 1,
            "requests_remaining": 499, "events": 14,
            "status": "ok", "flags": [], "raw": [],
        })
        store.start_run(self.conn, "odds", today)
        resp = self.client.get("/status")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["credits_used_today"], 1)
        self.assertEqual(body["ledger_balance_cents"], 1500)
        self.assertEqual(body["ledger_balance_dollars"], 15.0)
        self.assertEqual(body["cycle_date"], today)
        self.assertIs(body["odds_api_key_set"], False)
        self.assertIn("next_scheduled_run_et", body)

    def test_root_describes_service(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("fair-line-picks-bot-stage1", resp.json()["service"])


if __name__ == "__main__":
    unittest.main()
