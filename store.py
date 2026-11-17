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
    _migrate_stats_unique(conn)
    conn.commit()
    return conn


def _migrate_stats_unique(conn):
    """One-off migration (Stage 2, Nov 17 2026): the original
    stats_snapshots UNIQUE was (espn_team_id, cycle_date), but ESPN team
    IDs overlap across leagues (NBA id 1 is Atlanta, NFL id 1 is someone
    else), so 66 teams were silently dropped as duplicates in the first
    Nov 16 capture. The fixed constraint is
    (espn_team_id, sport, cycle_date). Existing rows are preserved (they
    are correct for their sport); the missing teams are fetched on the
    next capture run."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' "
        "AND name = 'stats_snapshots'").fetchone()
    if row is None or "UNIQUE(espn_team_id, cycle_date)" not in (row["sql"] or ""):
        return
    conn.execute("ALTER TABLE stats_snapshots RENAME TO stats_snapshots_old")
    schema = (Path(__file__).parent / "schema.sql").read_text()
    conn.executescript(schema)
    conn.execute(
        "INSERT INTO stats_snapshots (espn_team_id, team_name, "
        "team_abbreviation, sport, cycle_date, fetched_at, games_played, "
        "pf_per_game, pa_per_game) SELECT espn_team_id, team_name, "
        "team_abbreviation, sport, cycle_date, fetched_at, games_played, "
        "pf_per_game, pa_per_game FROM stats_snapshots_old")
    conn.execute("DROP TABLE stats_snapshots_old")
    conn.commit()


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


# --- Stage 2: stats snapshots -----------------------------------------------

def already_captured_stats(conn, cycle_date):
    # (espn_team_id, sport) pairs: ESPN team IDs overlap across leagues.
    rows = conn.execute(
        "SELECT espn_team_id, sport FROM stats_snapshots WHERE cycle_date = ?",
        (cycle_date,),
    ).fetchall()
    return {(r["espn_team_id"], r["sport"]) for r in rows}


def insert_stats_snapshot(conn, snap):
    conn.execute(
        "INSERT OR IGNORE INTO stats_snapshots "
        "(espn_team_id, team_name, team_abbreviation, sport, cycle_date, "
        " fetched_at, games_played, pf_per_game, pa_per_game) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            snap["espn_team_id"], snap.get("team_name"),
            snap.get("team_abbreviation"), snap["sport"], snap["cycle_date"],
            snap["fetched_at"], snap["games_played"], snap.get("pf_per_game"),
            snap.get("pa_per_game"),
        ),
    )
    conn.commit()
    return conn.execute("SELECT changes()").fetchone()[0] == 1


def get_stats(conn, cycle_date, sport=None, espn_team_id=None):
    sql = "SELECT * FROM stats_snapshots WHERE cycle_date = ?"
    args = [cycle_date]
    if sport is not None:
        sql += " AND sport = ?"
        args.append(sport)
    if espn_team_id is not None:
        sql += " AND espn_team_id = ?"
        args.append(str(espn_team_id))
    return conn.execute(sql, args).fetchall()


# --- Stage 2: rule evaluations (paper tracking only) ------------------------

def insert_rule_evaluation(conn, ev):
    conn.execute(
        "INSERT OR IGNORE INTO rule_evaluations "
        "(cycle_date, ticker, series, game_slug, game_date, home_team, "
        " away_team, team, q, ask, threshold, edge, would_pick, reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ev["cycle_date"], ev["ticker"], ev["series"], ev.get("game_slug"),
            ev.get("game_date"), ev.get("home_team"), ev.get("away_team"),
            ev["team"], ev.get("q"), ev.get("ask"), ev.get("threshold"),
            ev.get("edge"), 1 if ev.get("would_pick") else 0, ev["reason"],
        ),
    )
    conn.commit()
    return conn.execute("SELECT changes()").fetchone()[0] == 1


def get_rule_evaluations(conn, cycle_date, would_pick=None):
    sql = "SELECT * FROM rule_evaluations WHERE cycle_date = ?"
    args = [cycle_date]
    if would_pick is not None:
        sql += " AND would_pick = ?"
        args.append(1 if would_pick else 0)
    return conn.execute(sql, args).fetchall()


def update_rule_evaluation(conn, cycle_date, ticker, fields):
    """Update a logged evaluation (used when a resumed evaluate run
    changes the daily-cap outcome for an already-logged row)."""
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE rule_evaluations SET {sets} WHERE cycle_date = ? AND ticker = ?",
        (*fields.values(), cycle_date, ticker),
    )
    conn.commit()


def update_rule_outcome(conn, eval_id, outcome, home_score=None, away_score=None):
    conn.execute(
        "UPDATE rule_evaluations SET outcome = ?, settled_at = ?, "
        "home_score = ?, away_score = ? WHERE id = ?",
        (outcome, utcnow_iso(), home_score, away_score, eval_id),
    )
    conn.commit()


# --- Stage 2: parlay suggestions (separate from picks) ---------------------

def insert_parlay_suggestion(conn, sugg):
    conn.execute(
        "INSERT OR IGNORE INTO parlay_suggestions "
        "(cycle_date, legs, n_legs, combined_q, combined_price, "
        " combined_payout_cents, fee_cents, ev_cents, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sugg["cycle_date"], json.dumps(sugg["legs"]), sugg["n_legs"],
            sugg["combined_q"], sugg["combined_price"],
            sugg["combined_payout_cents"], sugg["fee_cents"],
            sugg["ev_cents"], sugg.get("note"),
        ),
    )
    conn.commit()
    return conn.execute("SELECT changes()").fetchone()[0] == 1


def get_parlay_suggestions(conn, cycle_date, outcome=None):
    sql = "SELECT * FROM parlay_suggestions WHERE cycle_date = ?"
    args = [cycle_date]
    if outcome is not None:
        sql += " AND outcome = ?"
        args.append(outcome)
    return conn.execute(sql, args).fetchall()


def delete_pending_parlay_suggestions(conn, cycle_date):
    """Drop not-yet-settled suggestions for a cycle (used when a resumed
    evaluate run regenerates them from a corrected pick set). Settled
    suggestions are never deleted."""
    conn.execute(
        "DELETE FROM parlay_suggestions WHERE cycle_date = ? AND outcome = 'pending'",
        (cycle_date,),
    )
    conn.commit()


def update_parlay_outcome(conn, sugg_id, outcome):
    conn.execute(
        "UPDATE parlay_suggestions SET outcome = ?, settled_at = ? WHERE id = ?",
        (outcome, utcnow_iso(), sugg_id),
    )
    conn.commit()
