"""SQLite store: schema init, snapshot writes, run tracking, ledger open.

Every write is its own committed transaction (WAL mode) so a process killed
mid-scan loses nothing already captured.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import config

DB_PATH = Path(__file__).parent / "fairline.db"


def connect(db_path=DB_PATH):
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path=DB_PATH):
    conn = connect(db_path)
    schema = (Path(__file__).parent / "schema.sql").read_text()
    conn.executescript(schema)
    conn.commit()
    return conn


def utcnow_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def start_run(conn, part, cycle_date, notes=None):
    row = conn.execute(
        "SELECT COALESCE(MAX(attempt), 0) AS m FROM capture_runs "
        "WHERE part = ? AND cycle_date = ?",
        (part, cycle_date),
    ).fetchone()
    attempt = row["m"] + 1
    conn.execute(
        "INSERT INTO capture_runs (part, cycle_date, attempt, started_at, notes) "
        "VALUES (?, ?, ?, ?, ?)",
        (part, cycle_date, attempt, utcnow_iso(), notes),
    )
    conn.commit()
    return attempt


def finish_run(conn, part, cycle_date, attempt, status, polled, ok, missing, notes=None):
    conn.execute(
        "UPDATE capture_runs SET finished_at = ?, status = ?, markets_polled = ?, "
        "markets_ok = ?, markets_missing = ?, notes = ? "
        "WHERE part = ? AND cycle_date = ? AND attempt = ?",
        (utcnow_iso(), status, polled, ok, missing, notes, part, cycle_date, attempt),
    )
    conn.commit()


def already_captured_tickers(conn, cycle_date):
    rows = conn.execute(
        "SELECT ticker FROM kalshi_snapshots WHERE cycle_date = ?", (cycle_date,)
    ).fetchall()
    return {r["ticker"] for r in rows}


def already_captured_sports(conn, cycle_date):
    rows = conn.execute(
        "SELECT sport_key FROM odds_snapshots WHERE cycle_date = ?", (cycle_date,)
    ).fetchall()
    return {r["sport_key"] for r in rows}


def insert_kalshi_snapshot(conn, snap):
    conn.execute(
        "INSERT OR IGNORE INTO kalshi_snapshots "
        "(ticker, series, cycle_date, fetched_at, yes_bid, yes_ask, yes_levels, "
        " no_levels, market_title, close_ts, status, flags, raw_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            snap["ticker"], snap["series"], snap["cycle_date"], snap["fetched_at"],
            snap["yes_bid"], snap["yes_ask"], snap["yes_levels"], snap["no_levels"],
            snap.get("market_title"), snap.get("close_ts"), snap["status"],
            json.dumps(snap.get("flags", [])), json.dumps(snap["raw"]),
        ),
    )
    conn.commit()
    return conn.execute("SELECT changes()").fetchone()[0] == 1


def insert_odds_snapshot(conn, snap):
    cur = conn.execute(
        "INSERT OR IGNORE INTO odds_snapshots "
        "(sport_key, cycle_date, fetched_at, credits_used, requests_remaining, "
        " events, status, flags, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            snap["sport_key"], snap["cycle_date"], snap["fetched_at"],
            snap.get("credits_used"), snap.get("requests_remaining"),
            snap.get("events"), snap["status"], json.dumps(snap.get("flags", [])),
            json.dumps(snap["raw"]),
        ),
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    return cur.lastrowid


def insert_odds_outcomes(conn, snapshot_id, outcomes):
    conn.executemany(
        "INSERT OR IGNORE INTO odds_outcomes "
        "(snapshot_id, event_id, commence_time, home_team, away_team, bookmaker, "
        " outcome_name, price_american, price_decimal) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                snapshot_id, o["event_id"], o.get("commence_time"), o.get("home_team"),
                o.get("away_team"), o["bookmaker"], o["outcome_name"],
                o.get("price_american"), o.get("price_decimal"),
            )
            for o in outcomes
        ],
    )
    conn.commit()
