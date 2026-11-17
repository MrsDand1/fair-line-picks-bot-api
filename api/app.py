"""Fair Line Picks Bot — Stage 1 deploy service (Render, free tier).

Small FastAPI app that runs the Stage 1 daily capture cycle once a day,
pre-game, in the morning US/Eastern (target ~08:00 ET), then idles.
Style follows the BTC Oracle Pro tracker (supervised scheduler thread +
watchdog, /health endpoint); no trading or signal logic is reused here —
this service only captures data, exactly like `python3 scan.py all`.

FREE-TIER HONESTY: Render's free web services sleep after ~15 min idle
and the disk is ephemeral, so history resets on restart (same caveat as
the BTC API). The scheduler catches up: if the service starts (or wakes)
after 08:00 ET with today's cycle incomplete, it runs the cycle at once.
A keep-alive ping to /health (the same pattern as the BTC API's hook) is
what keeps the service awake so the scheduled run fires on time.

The Odds API key comes ONLY from the ODDS_API_KEY environment variable at
runtime. It is never in a file, in code, in a log, or in a test. Without
it (and without ODDS_API_STUB=1) the odds leg refuses cleanly and the run
is recorded as incomplete — the scheduler retries hourly until 20:00 ET.

No signups, no accounts, no money, no bets, no trades, no order-placement
code anywhere in this service.
"""
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# The pipeline modules live in the project root (one level up from api/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI

import config
import integrity
import ledger as ledger_mod
import scan
import store

app = FastAPI(title="Fair Line Picks Bot — Stage 1 capture service")

# --- schedule ---------------------------------------------------------------
SCHEDULE_HOUR_ET = 8          # ~08:00 America/New_York, pre-game
SCHEDULE_MINUTE_ET = 0
TICK_SECONDS = 60             # scheduler checks the clock once a minute
RETRY_INTERVAL_S = 3600       # retry an incomplete day once an hour
RETRY_UNTIL_HOUR_ET = 20      # stop retrying an incomplete day after 20:00 ET

DB_PATH = store.DB_PATH        # project root fairline.db (WAL mode)

_state_lock = threading.Lock()
_state = {
    "started_at": time.time(),
    "last_attempt_cycle": None,    # YYYY-MM-DD of the last attempted run
    "last_attempt_ts": None,       # epoch seconds of the last attempt
    "last_attempt_complete": False,
    "last_run_started_at": None,   # ISO UTC
    "last_run_finished_at": None,  # ISO UTC
    "last_run_status": None,       # complete | partial | None
    "last_run_summary": None,
    "errors": [],
}


def _record_error(msg):
    with _state_lock:
        _state["errors"].append(f"{store.utcnow_iso()} {msg}")
        _state["errors"] = _state["errors"][-10:]


def _today_et(now=None):
    now = now or datetime.now(ZoneInfo(config.TIMEZONE))
    return now


def should_run(now, last_attempt_cycle, last_attempt_ts, last_attempt_complete):
    """Pure clock logic: should a capture run start at `now`?

    `now` is an aware datetime in America/New_York. A run starts once a
    day at/after 08:00 ET. If the day's attempt already completed, no
    re-run. If the day's attempt was incomplete (e.g. the odds leg refused
    for a missing key), retry once an hour until 20:00 ET.
    """
    today = now.strftime("%Y-%m-%d")
    run_time = now.replace(hour=SCHEDULE_HOUR_ET, minute=SCHEDULE_MINUTE_ET,
                           second=0, microsecond=0)
    if now < run_time:
        return False
    if last_attempt_cycle != today:
        return True
    if last_attempt_complete:
        return False
    if now.hour >= RETRY_UNTIL_HOUR_ET:
        return False
    if last_attempt_ts is None:
        return True
    return (now.timestamp() - last_attempt_ts) >= RETRY_INTERVAL_S


def cycle_is_complete(conn, cycle_date):
    """True when today's cycle already has Kalshi snapshots and all four
    odds sports captured. Used after restarts (state is fresh, DB may not
    be) so a completed day is never re-run."""
    k = conn.execute(
        "SELECT COUNT(*) AS n FROM kalshi_snapshots WHERE cycle_date = ?",
        (cycle_date,),
    ).fetchone()["n"]
    o = conn.execute(
        "SELECT COUNT(*) AS n FROM odds_snapshots WHERE cycle_date = ?",
        (cycle_date,),
    ).fetchone()["n"]
    return k > 0 and o >= len(config.ODDS_SPORTS)


def run_odds_leg(conn, cycle_date):
    """Odds leg of the daily cycle. Refuses cleanly (no raise, no HTTP
    call) when ODDS_API_KEY is absent and stub mode is off: scan_odds
    records the refusal per sport and returns polled=0 with the reason
    in notes. Never raises for a missing key."""
    return scan.scan_odds(conn, cycle_date)


def run_daily_cycle(conn, cycle_date):
    """Run both legs of the daily cycle. Never raises: a failing leg is
    recorded in the summary and the run is marked partial."""
    started = store.utcnow_iso()
    summary = {
        "cycle_date": cycle_date,
        "started_at": started,
        "kalshi": None,
        "odds": None,
        "status": "complete",
        "complete": False,
    }
    try:
        summary["kalshi"] = scan.scan_kalshi(conn, cycle_date)
    except Exception as exc:  # noqa: BLE001 - one bad leg must not kill the run
        summary["kalshi"] = {"error": str(exc)}
        summary["status"] = "partial"
    try:
        summary["odds"] = run_odds_leg(conn, cycle_date)
    except Exception as exc:  # noqa: BLE001
        summary["odds"] = {"error": str(exc)}
        summary["status"] = "partial"
    summary["complete"] = cycle_is_complete(conn, cycle_date)
    if not summary["complete"]:
        summary["status"] = "partial"
    summary["finished_at"] = store.utcnow_iso()
    return summary


def _set_attempt(cycle_date, complete, summary=None):
    with _state_lock:
        _state["last_attempt_cycle"] = cycle_date
        _state["last_attempt_ts"] = time.time()
        _state["last_attempt_complete"] = bool(complete)
        _state["last_run_started_at"] = (summary or {}).get("started_at")
        _state["last_run_finished_at"] = (summary or {}).get("finished_at")
        _state["last_run_status"] = (summary or {}).get("status")
        _state["last_run_summary"] = summary


def scheduler_tick(now=None):
    """One scheduler tick. Returns not_due | already_done | ran."""
    now = _today_et(now)
    today = now.strftime("%Y-%m-%d")
    with _state_lock:
        last_cycle = _state["last_attempt_cycle"]
        last_ts = _state["last_attempt_ts"]
        last_complete = _state["last_attempt_complete"]
    if not should_run(now, last_cycle, last_ts, last_complete):
        return "not_due"
    conn = store.init_db(DB_PATH)
    try:
        if cycle_is_complete(conn, today):
            _set_attempt(today, complete=True)
            return "already_done"
        summary = run_daily_cycle(conn, today)
        _set_attempt(today, complete=summary["complete"], summary=summary)
        return "ran"
    finally:
        conn.close()


def _scheduler_forever():
    while True:
        try:
            scheduler_tick()
        except Exception as exc:  # noqa: BLE001 - a bad tick must not kill the loop
            _record_error(f"scheduler tick: {exc}")
        time.sleep(TICK_SECONDS)


def _scheduler_supervised():
    """Run the scheduler forever: if one pass raises, log it and restart
    the loop instead of letting the thread die (cf. BTC API freeze,
    2026-11-16)."""
    while True:
        try:
            _scheduler_forever()
        except Exception as exc:  # noqa: BLE001
            _record_error(f"scheduler: {exc}")
        time.sleep(5)


_scheduler_thread = None
_watchdog_thread = None


def _start_scheduler():
    global _scheduler_thread, _watchdog_thread
    _scheduler_thread = threading.Thread(target=_scheduler_supervised, daemon=True)
    _scheduler_thread.start()

    def _watchdog():
        global _scheduler_thread
        while True:
            time.sleep(30)
            # Restart ONLY a dead thread.
            if _scheduler_thread is not None and not _scheduler_thread.is_alive():
                _record_error("scheduler thread restarted by watchdog")
                _scheduler_thread = threading.Thread(
                    target=_scheduler_supervised, daemon=True
                )
                _scheduler_thread.start()

    _watchdog_thread = threading.Thread(target=_watchdog, daemon=True)
    _watchdog_thread.start()


# --- startup: init DB + ledger (idempotent), start the scheduler -----------
_conn = store.init_db(DB_PATH)
ledger_mod.open_ledger(_conn)
_conn.close()

if os.environ.get("PICKS_API_NO_SCHEDULER") != "1":
    _start_scheduler()


# --- endpoints ---------------------------------------------------------------
@app.get("/")
def root():
    return {
        "service": "fair-line-picks-bot-stage1",
        "description": "Stage 1 daily capture: Kalshi orderbooks + Odds API h2h odds, once a day ~08:00 ET.",
        "schedule": "daily ~08:00 America/New_York (pre-game); catch-up run on start/wake after 08:00 if the day is incomplete",
        "endpoints": ["/health", "/status"],
        "notes": (
            "Free-tier Render service: sleeps after ~15 min idle and the disk is "
            "ephemeral — history resets on restart. Keep-alive pings to /health "
            "keep it awake. No picks, no trades, no validated edge here."
        ),
    }


@app.get("/health")
def health():
    with _state_lock:
        state = dict(_state)
        errors = list(state["errors"])
    return {
        "status": "ok",
        "service": "fair-line-picks-bot-stage1",
        "uptime_seconds": round(time.time() - state["started_at"]),
        "scheduler_alive": (
            _scheduler_thread is not None and _scheduler_thread.is_alive()
        ),
        "last_scan_time": state["last_run_finished_at"],
        "last_run_status": state["last_run_status"],
        "odds_api_key_set": bool(os.environ.get("ODDS_API_KEY")),
        "recent_errors": errors[-3:],
    }


def _latest_cycle(conn):
    row = conn.execute(
        "SELECT MAX(cycle_date) AS m FROM capture_runs"
    ).fetchone()
    return row["m"] if row else None


@app.get("/status")
def status():
    conn = store.init_db(DB_PATH)
    try:
        today = _today_et().strftime("%Y-%m-%d")
        latest = _latest_cycle(conn) or today
        report = integrity.cycle_report(conn, latest)
        credits_today = conn.execute(
            "SELECT COALESCE(SUM(credits_used), 0) AS n FROM odds_snapshots "
            "WHERE cycle_date = ?",
            (today,),
        ).fetchone()["n"]
        balance = ledger_mod.balance_cents(conn)
        with _state_lock:
            last_summary = _state["last_run_summary"]
            last_status = _state["last_run_status"]
        if cycle_is_complete(conn, today):
            next_run = (
                _today_et() + timedelta(days=1)
            ).replace(hour=SCHEDULE_HOUR_ET, minute=SCHEDULE_MINUTE_ET,
                      second=0, microsecond=0)
        else:
            next_run = _today_et().replace(
                hour=SCHEDULE_HOUR_ET, minute=SCHEDULE_MINUTE_ET,
                second=0, microsecond=0,
            )
        return {
            "service": "fair-line-picks-bot-stage1",
            "cycle_date": latest,
            "cycle_report": report,
            "credits_used_today": credits_today,
            "credits_free_tier_per_month": 500,
            "ledger_balance_dollars": (
                round(balance / 100, 2) if balance is not None else None
            ),
            "ledger_balance_cents": balance,
            "odds_api_key_set": bool(os.environ.get("ODDS_API_KEY")),
            "last_run_status": last_status,
            "last_run_summary": last_summary,
            "next_scheduled_run_et": next_run.isoformat(),
        }
    finally:
        conn.close()
