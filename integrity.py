"""Integrity checks for captured snapshots.

A snapshot is rejected (status 'malformed', never stored as 'ok') when it is
structurally wrong. An empty book is stored but flagged 'empty_book'.
Staleness is judged at report time from fetched_at.
"""
import json
from datetime import datetime, timezone

import config


def check_kalshi_book(yes_levels, no_levels, yes_bid, yes_ask):
    """Return (status, flags) for a parsed orderbook.

    One-sided books are legitimate market states (e.g. a huge favorite
    whose underdog side has bids on one side only) — they get status
    'one_sided', not 'malformed'. The Hunter simply cannot use a leg
    with no ask (nothing to buy) or no bid.
    """
    flags = []
    if not yes_levels and not no_levels:
        return "empty_book", ["empty_book"]
    if yes_bid is None or yes_ask is None:
        return "one_sided", ["one_sided_book"]
    if not (0.0 <= yes_bid <= 1.0) or not (0.0 <= yes_ask <= 1.0):
        flags.append("price_out_of_range")
    if yes_bid > yes_ask:
        flags.append("bid_above_ask")
    if flags:
        return "malformed", flags
    return "ok", []


def is_stale(fetched_at_iso, now=None, minutes=config.STALE_AFTER_MINUTES):
    now = now or datetime.now(timezone.utc)
    fetched = datetime.fromisoformat(fetched_at_iso)
    return (now - fetched).total_seconds() > minutes * 60


def check_odds_payload(events):
    """Return (status, flags) for a parsed Odds API payload."""
    flags = []
    if not isinstance(events, list):
        return "malformed", ["payload_not_a_list"]
    for ev in events:
        if not isinstance(ev, dict) or "id" not in ev or "bookmakers" not in ev:
            flags.append("event_missing_fields")
            break
    return ("ok", flags) if not flags else ("malformed", flags)


def cycle_report(conn, cycle_date):
    """Daily capture report: counts, flags, and what is missing."""
    kalshi = conn.execute(
        "SELECT series, status, COUNT(*) AS n FROM kalshi_snapshots "
        "WHERE cycle_date = ? GROUP BY series, status",
        (cycle_date,),
    ).fetchall()
    odds = conn.execute(
        "SELECT sport_key, status, credits_used, events FROM odds_snapshots "
        "WHERE cycle_date = ?",
        (cycle_date,),
    ).fetchall()
    runs = conn.execute(
        "SELECT part, attempt, status, markets_polled, markets_ok, markets_missing, notes "
        "FROM capture_runs WHERE cycle_date = ? ORDER BY part, attempt",
        (cycle_date,),
    ).fetchall()
    flagged = conn.execute(
        "SELECT ticker, status, flags FROM kalshi_snapshots "
        "WHERE cycle_date = ? AND status != 'ok'",
        (cycle_date,),
    ).fetchall()
    return {
        "cycle_date": cycle_date,
        "kalshi_by_series_status": [
            {"series": r["series"], "status": r["status"], "n": r["n"]} for r in kalshi
        ],
        "kalshi_total": sum(r["n"] for r in kalshi),
        "odds": [
            {
                "sport_key": r["sport_key"], "status": r["status"],
                "credits_used": r["credits_used"], "events": r["events"],
            }
            for r in odds
        ],
        "odds_credits_total": sum((r["credits_used"] or 0) for r in odds),
        "runs": [dict(r) for r in runs],
        "flagged_kalshi": [
            {"ticker": r["ticker"], "status": r["status"],
             "flags": json.loads(r["flags"])} for r in flagged
        ],
    }
