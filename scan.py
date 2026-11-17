"""Stage 1 scanner: Kalshi orderbooks + Odds API h2h odds, once-daily cycle.

Usage:
  python3 scan.py kalshi [--cycle YYYY-MM-DD] [--limit N]
  python3 scan.py odds   [--cycle YYYY-MM-DD] [--sport KEY] [--stub]
  python3 scan.py all    [--cycle YYYY-MM-DD]
  python3 scan.py report [--cycle YYYY-MM-DD]

Cycle day defaults to today in America/New_York.

RESTART RECOVERY: every market fetch is committed immediately, and a run
resumes by skipping tickers/sports already captured for the cycle (UNIQUE
constraints make duplicates impossible even if a resume overlaps). A run
killed mid-scan leaves a 'running' row in capture_runs; the next run for
the same cycle starts a new attempt and finishes what is missing.
"""
import argparse
import json
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import config
import integrity
import kalshi_client
import ledger as ledger_mod
import odds_client
import store


def cycle_today():
    return datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%Y-%m-%d")


def scan_kalshi(conn, cycle_date, limit=None):
    attempt = store.start_run(conn, "kalshi", cycle_date)
    polled = ok = 0
    notes = []
    listed = set()
    try:
        done = store.already_captured_tickers(conn, cycle_date)
        for series in config.KALSHI_SERIES:
            markets = kalshi_client.list_open_markets(series)
            listed.update(m["ticker"] for m in markets)
            print(f"[{series}] {len(markets)} open markets listed", flush=True)
            time.sleep(config.KALSHI_DELAY_BETWEEN_SERIES_S)
            fresh = [m for m in markets if m["ticker"] not in done]
            print(f"[{series}] {len(fresh)} to fetch ({len(markets) - len(fresh)} already captured)",
                  flush=True)
            for market in fresh:
                if limit is not None and polled >= limit:
                    break
                ticker = market["ticker"]
                try:
                    book, raw = kalshi_client.fetch_book(ticker, depth=1)
                except Exception as e:
                    notes.append(f"{ticker}: fetch failed ({e})")
                    print(f"FAIL {ticker}: {e}", file=sys.stderr, flush=True)
                    polled += 1
                    time.sleep(config.KALSHI_DELAY_BETWEEN_CALLS_S)
                    continue
                status, flags = integrity.check_kalshi_book(
                    book["yes_levels"], book["no_levels"],
                    book["yes_bid"], book["yes_ask"],
                )
                inserted = store.insert_kalshi_snapshot(conn, {
                    "ticker": ticker, "series": series, "cycle_date": cycle_date,
                    "fetched_at": store.utcnow_iso(),
                    "yes_bid": book["yes_bid"], "yes_ask": book["yes_ask"],
                    "yes_levels": book["yes_levels"], "no_levels": book["no_levels"],
                    "market_title": market.get("title"),
                    "close_ts": market.get("close_time"),
                    "status": status, "flags": flags, "raw": raw,
                })
                polled += 1
                if inserted and status == "ok":
                    ok += 1
                elif status != "ok":
                    notes.append(f"{ticker}: {status} {flags}")
                if polled % 50 == 0:
                    print(f"progress: {polled} polled, {ok} ok", flush=True)
                time.sleep(config.KALSHI_DELAY_BETWEEN_CALLS_S)
            if limit is not None and polled >= limit:
                break
        # Missing = open markets listed but without a snapshot this cycle.
        captured = store.already_captured_tickers(conn, cycle_date)
        missing = len(listed - captured)
        store.finish_run(conn, "kalshi", cycle_date, attempt, "complete",
                         polled, ok, missing,
                         "; ".join(notes) if notes else None)
        return {"attempt": attempt, "polled": polled, "ok": ok,
                "missing": missing, "notes": notes}
    except Exception as e:
        store.finish_run(conn, "kalshi", cycle_date, attempt, "failed",
                         polled, ok, 0, str(e))
        raise


def _credits_for_call(used):
    """Credits a call cost. Header-reported when present; the documented
    cost for this call shape (1 market x 1 region, bookmakers param) is 1."""
    return used if used is not None else 1


def scan_odds(conn, cycle_date, sports=None, stub=False):
    if stub:
        import os
        os.environ["ODDS_API_STUB"] = "1"
    attempt = store.start_run(conn, "odds", cycle_date)
    polled = ok = 0
    notes = []
    total_credits = 0
    try:
        done = store.already_captured_sports(conn, cycle_date)
        for sport_key in (sports or list(config.ODDS_SPORTS)):
            if sport_key in done:
                continue
            try:
                events, used, remaining = odds_client.get_odds(sport_key)
            except Exception as e:
                notes.append(f"{sport_key}: fetch failed ({e})")
                continue
            status, flags = integrity.check_odds_payload(events)
            outcomes = odds_client.parse_outcomes(events)
            credits = _credits_for_call(used)
            snap_id = store.insert_odds_snapshot(conn, {
                "sport_key": sport_key, "cycle_date": cycle_date,
                "fetched_at": store.utcnow_iso(),
                "credits_used": credits, "requests_remaining": remaining,
                "events": len(events), "status": status, "flags": flags,
                "raw": events,
            })
            if snap_id is not None:
                store.insert_odds_outcomes(conn, snap_id, outcomes)
                polled += 1
                total_credits += credits
                if status == "ok":
                    ok += 1
                else:
                    notes.append(f"{sport_key}: {status} {flags}")
        store.finish_run(conn, "odds", cycle_date, attempt, "complete",
                         polled, ok, 0,
                         f"credits_used={total_credits}" +
                         ("; " + "; ".join(notes) if notes else ""))
        return {"attempt": attempt, "polled": polled, "ok": ok,
                "credits_used": total_credits, "notes": notes}
    except Exception as e:
        store.finish_run(conn, "odds", cycle_date, attempt, "failed",
                         polled, ok, 0, str(e))
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fair Line Picks Bot — Stage 1 scanner")
    parser.add_argument("command", choices=["kalshi", "odds", "all", "report"])
    parser.add_argument("--cycle", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--sport", default=None)
    parser.add_argument("--stub", action="store_true")
    args = parser.parse_args(argv)
    cycle_date = args.cycle or cycle_today()

    conn = store.init_db()
    ledger_mod.open_ledger(conn)

    if args.command == "kalshi":
        result = scan_kalshi(conn, cycle_date, limit=args.limit)
        print(json.dumps(result, indent=2))
    elif args.command == "odds":
        sports = [args.sport] if args.sport else None
        result = scan_odds(conn, cycle_date, sports=sports, stub=args.stub)
        print(json.dumps(result, indent=2))
    elif args.command == "all":
        k = scan_kalshi(conn, cycle_date)
        o = scan_odds(conn, cycle_date, stub=args.stub)
        print(json.dumps({"kalshi": k, "odds": o}, indent=2))
    elif args.command == "report":
        print(json.dumps(integrity.cycle_report(conn, cycle_date), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
