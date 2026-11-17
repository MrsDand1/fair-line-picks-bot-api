"""Stage 1 scanner: Kalshi orderbooks + Odds API h2h odds, once-daily cycle.
Stage 2 adds: ESPN team-average stats capture, the team-average rule
evaluation (paper tracking only), parlay suggestions, and settlement.

Usage:
  python3 scan.py kalshi [--cycle YYYY-MM-DD] [--limit N]
  python3 scan.py odds   [--cycle YYYY-MM-DD] [--sport KEY] [--stub]
  python3 scan.py all    [--cycle YYYY-MM-DD]
  python3 scan.py report [--cycle YYYY-MM-DD]
  python3 scan.py stats     [--cycle YYYY-MM-DD]   # Stage 2 stats capture
  python3 scan.py evaluate  [--cycle YYYY-MM-DD]   # Stage 2 rule + parlays
  python3 scan.py settle    [--cycle YYYY-MM-DD]   # settle picks + parlays
  python3 scan.py rule-report [--cycle YYYY-MM-DD]

Cycle day defaults to today in America/New_York.

RESTART RECOVERY: every market fetch is committed immediately, and a run
resumes by skipping tickers/sports already captured for the cycle (UNIQUE
constraints make duplicates impossible even if a resume overlaps). A run
killed mid-scan leaves a 'running' row in capture_runs; the next run for
the same cycle starts a new attempt and finishes what is missing.
Stage 2 evaluate is resume-safe the same way: already-logged rows are
kept, missing games are evaluated, and the daily cap is re-applied
across the full day (a row whose cap outcome changes is updated).
"""
import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

import config
import integrity
import kalshi_client
import ledger as ledger_mod
import odds_client
import parlay as parlay_mod
import rule as rule_mod
import stats_client
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
    parser.add_argument("command", choices=["kalshi", "odds", "all", "report",
                                            "stats", "evaluate", "settle",
                                            "rule-report"])
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
        s = scan_stats(conn, cycle_date)
        e = evaluate_rule(conn, cycle_date)
        print(json.dumps({"kalshi": k, "odds": o, "stats": s,
                          "evaluate": e}, indent=2))
    elif args.command == "report":
        print(json.dumps(integrity.cycle_report(conn, cycle_date), indent=2))
    elif args.command == "stats":
        print(json.dumps(scan_stats(conn, cycle_date), indent=2))
    elif args.command == "evaluate":
        print(json.dumps(evaluate_rule(conn, cycle_date), indent=2))
    elif args.command == "settle":
        print(json.dumps(settle_rule(conn, cycle_date), indent=2))
    elif args.command == "rule-report":
        print(json.dumps(rule_report(conn, cycle_date), indent=2))
    return 0


# --- Stage 2: stats capture -------------------------------------------------

def scan_stats(conn, cycle_date):
    """ESPN team-average capture for the cycle's Kalshi markets."""
    attempt = store.start_run(conn, "stats", cycle_date)
    try:
        result = stats_client.capture_stats(conn, cycle_date)
        store.finish_run(conn, "stats", cycle_date, attempt, "complete",
                         result["teams_matched"], result["stored"], 0,
                         "; ".join(result["notes"]) if result["notes"] else None)
        return result
    except Exception as e:
        store.finish_run(conn, "stats", cycle_date, attempt, "failed",
                         0, 0, 0, str(e))
        raise


# --- Stage 2: rule evaluation (paper tracking only) -------------------------

def _pair_markets(conn, cycle_date):
    """Group the cycle's Kalshi snapshots into games.

    Returns a list of games: {series, game_date, remainder, sides} where
    sides maps ticker -> snapshot row. A game normally has two sides;
    a lone side (one market missing) is still returned with one side.
    Tickers that do not parse are returned as single-side games with
    game_date/remainder None (they will log reason no_home_away).
    """
    rows = conn.execute(
        "SELECT ticker, series, market_title, yes_bid, yes_ask, status "
        "FROM kalshi_snapshots WHERE cycle_date = ?",
        (cycle_date,),
    ).fetchall()
    games = {}
    for r in rows:
        game_date, remainder = stats_client.parse_game_slug(r["ticker"])
        key = (r["series"], game_date, remainder)
        games.setdefault(key, {"series": r["series"], "game_date": game_date,
                               "remainder": remainder, "sides": {}})
        games[key]["sides"][r["ticker"]] = dict(r)
    return list(games.values())


def _resolve_game_teams(game, team_map, abbr_map):
    """Resolve both sides of a game to ESPN teams.

    Primary: match each market title against the league team map.
    Fallback: if one side matched, derive the other side's KALSHI
    abbreviation from the ticker slug remainder (strip the known side's
    ticker suffix from the start or end) and look it up by abbreviation.
    The lookup only succeeds where Kalshi and ESPN abbreviations agree
    (Washington WAS/WSH and San Antonio SAS/SA do not); otherwise the
    side stays unresolved and logs no_stats.
    Returns {ticker: team_or_None}.
    """
    resolved = {}
    for ticker, side in game["sides"].items():
        team, _reason = stats_client.match_title(
            side.get("market_title") or "", team_map)
        resolved[ticker] = team
    if game["remainder"]:
        tickers = list(game["sides"])
        known_ticker = next((t for t in tickers if resolved.get(t)), None)
        if known_ticker is not None:
            known_abbr = known_ticker.rsplit("-", 1)[1].upper()
            rem = game["remainder"].upper()
            other_abbr = None
            if known_abbr and rem.startswith(known_abbr):
                other_abbr = rem[len(known_abbr):]
            elif known_abbr and rem.endswith(known_abbr):
                other_abbr = rem[: -len(known_abbr)] if len(known_abbr) else None
            if other_abbr:
                for ticker, team in resolved.items():
                    if team is None:
                        resolved[ticker] = abbr_map.get(other_abbr)
    return resolved


def _home_away(game, resolved, league):
    """(home_team, away_team) for a game, or (None, None).

    Primary: split the ticker slug remainder with the KALSHI
    abbreviations — the ticker suffixes themselves (e.g. WAS, SAS).
    ESPN abbreviations differ (WSH, SA, NY, GS), so ESPN abbreviations
    must never be used to split a Kalshi slug. Away first, home second
    — verified live against ESPN's scoreboard, POR@MIL Nov 19, 2026.
    Fallback: the teams' ESPN schedules.
    """
    tickers = list(game["sides"])
    kalshi_abbr = {t: t.rsplit("-", 1)[1].upper() for t in tickers}
    teams = [t for t in resolved.values() if t]
    if game["remainder"] and len(tickers) == 2 and len(teams) == 2:
        split = stats_client.split_home_away(
            game["remainder"], kalshi_abbr[tickers[0]], kalshi_abbr[tickers[1]])
        if split:
            away_abbr, home_abbr = split
            by_kalshi = {kalshi_abbr[t]: resolved[t] for t in tickers}
            return by_kalshi.get(home_abbr), by_kalshi.get(away_abbr)
    if game["game_date"] and len(teams) == 2:
        try:
            sched = {str(t["id"]): stats_client.fetch_schedule(league, str(t["id"]))
                     for t in teams}
            for home_cand in teams:
                away_cand = teams[1] if teams[0] is home_cand else teams[0]
                ha = stats_client.find_game_in_schedule(
                    sched[str(home_cand["id"])], str(home_cand["id"]),
                    game["game_date"],
                    away_cand.get("abbreviation") or "")
                if ha == "home":
                    return home_cand, away_cand
                if ha == "away":
                    return away_cand, home_cand
        except Exception:
            pass
    if game["remainder"] and len(teams) == 1 and len(tickers) == 1:
        # Lone side: the known Kalshi abbreviation's position in the slug
        # says which side of the game it is.
        team = teams[0]
        ticker = tickers[0]
        abbr = kalshi_abbr[ticker]
        rem = game["remainder"].upper()
        if abbr and rem.startswith(abbr) and len(rem) > len(abbr):
            return None, team  # known team is away; home unknown
        if abbr and rem.endswith(abbr) and len(rem) > len(abbr):
            return team, None  # known team is home; away unknown
    return None, None


def evaluate_rule(conn, cycle_date):
    """Run the team-average rule over the cycle's markets (paper only).

    Logs one row per market side in rule_evaluations, applies the daily
    cap across the full day, and generates parlay suggestions from the
    final picks. Never trades, never writes the ledger.
    """
    attempt = store.start_run(conn, "rule", cycle_date)
    try:
        games = _pair_markets(conn, cycle_date)
        # Stats keyed by (series, espn_team_id): ESPN team IDs overlap
        # across leagues, so a bare id would silently hand one sport's
        # stats to another sport's game.
        stats_rows = {(r["sport"], str(r["espn_team_id"])): dict(r)
                      for r in store.get_stats(conn, cycle_date)}
        existing = {r["ticker"]: dict(r) for r in
                    store.get_rule_evaluations(conn, cycle_date)}

        # Team maps per series (one ESPN teams-list call per series).
        team_maps = {}
        abbr_maps = {}
        for series in {g["series"] for g in games}:
            league = config.ESPN_LEAGUES.get(series)
            if not league:
                continue
            teams = stats_client.list_teams(league)
            team_maps[series] = stats_client.build_team_map(teams)
            abbr_maps[series] = {t.get("abbreviation", "").upper(): t
                                 for t in teams if t.get("abbreviation")}
            time.sleep(config.ESPN_DELAY_BETWEEN_CALLS_S)

        fresh = []  # evaluation dicts for this run (all sides)
        reasons = Counter()
        for game in games:
            series = game["series"]
            params = config.RULE_PARAMS.get(series)
            if params is None:
                continue
            league = config.ESPN_LEAGUES.get(series)
            resolved = _resolve_game_teams(
                game, team_maps.get(series, {}), abbr_maps.get(series, {}))
            home_team, away_team = _home_away(game, resolved, league)

            for ticker, side in game["sides"].items():
                team = resolved.get(ticker)
                base = {
                    "cycle_date": cycle_date, "ticker": ticker,
                    "series": series, "game_slug": game["remainder"],
                    "game_date": game["game_date"],
                    "home_team": (home_team or {}).get("displayName"),
                    "away_team": (away_team or {}).get("displayName"),
                    "team": (team or {}).get("displayName")
                            or side.get("market_title"),
                    "q": None, "ask": side.get("yes_ask"),
                    "threshold": None, "edge": None,
                    "would_pick": False, "reason": None,
                }
                status = side.get("status")
                # Both teams' stats are needed for the margin, so a game
                # with one side unmatched (e.g. a title the stats
                # capture could not match) is no_stats for BOTH sides.
                skey = (series, str(team["id"])) if team else None
                hkey = (series, str(home_team["id"])) if home_team else None
                akey = (series, str(away_team["id"])) if away_team else None
                if status == "malformed":
                    base["reason"] = "bad_book"
                elif team is None:
                    base["reason"] = "no_stats"
                elif home_team is None or away_team is None:
                    base["reason"] = "no_home_away"
                elif (skey not in stats_rows or hkey not in stats_rows
                        or akey not in stats_rows):
                    base["reason"] = "no_stats"
                else:
                    hs = stats_rows[hkey]
                    aws = stats_rows[akey]
                    if (hs.get("pf_per_game") is None
                            or aws.get("pf_per_game") is None):
                        base["reason"] = "below_min_games"
                    else:
                        # Evaluate this side against its own ask:
                        # q for this side, ask for this side.
                        margin = rule_mod.expected_margin(
                            hs["pf_per_game"], hs["pa_per_game"],
                            aws["pf_per_game"], aws["pa_per_game"],
                            params["hca"])
                        q_home = rule_mod.win_probability(
                            margin, params["sigma"])
                        is_home = team == home_team
                        q_side = q_home if is_home else 1.0 - q_home
                        games_n = (hs["games_played"] if is_home
                                   else aws["games_played"])
                        other_n = (aws["games_played"] if is_home
                                   else hs["games_played"])
                        side_result = rule_mod.evaluate_side(
                            q_side, side.get("yes_ask"),
                            games_n, other_n, params["min_games"],
                            params.get("floor", True))
                        base.update({
                            "q": round(q_side, 4),
                            "ask": side.get("yes_ask"),
                            "threshold": side_result.get("threshold"),
                            "edge": side_result.get("edge"),
                            "would_pick": side_result["would_pick"],
                            "reason": side_result["reason"],
                        })
                fresh.append(base)

        # Daily cap across the full day (fresh candidates + picks already
        # logged by an earlier partial run of this cycle).
        cap_input = list(fresh)
        for ticker, row in existing.items():
            if row["would_pick"] and ticker not in {f["ticker"] for f in fresh}:
                cap_input.append({"ticker": ticker, "q": row["q"],
                                  "edge": row["edge"], "would_pick": True,
                                  "reason": row["reason"]})
        rule_mod.apply_daily_cap(cap_input)
        cap_decision = {e["ticker"]: (e["would_pick"], e["reason"])
                        for e in cap_input}

        inserted = updated = already = 0
        for ev in fresh:
            would_pick, reason = cap_decision.get(
                ev["ticker"], (ev["would_pick"], ev["reason"]))
            ev["would_pick"] = would_pick
            ev["reason"] = reason
            reasons[ev["reason"]] += 1  # final reason, after the cap
            old = existing.get(ev["ticker"])
            if old and old["would_pick"] == (1 if would_pick else 0) \
                    and old["reason"] == reason \
                    and old["q"] == ev["q"] and old["ask"] == ev["ask"]:
                already += 1
                continue
            if old:
                store.update_rule_evaluation(conn, cycle_date, ev["ticker"], {
                    "q": ev["q"], "ask": ev["ask"],
                    "threshold": ev["threshold"], "edge": ev["edge"],
                    "would_pick": 1 if would_pick else 0, "reason": reason})
                updated += 1
            else:
                if store.insert_rule_evaluation(conn, ev):
                    inserted += 1

        # Parlay suggestions from the FINAL pick set (post-cap). Settled
        # suggestions are kept; pending ones are regenerated.
        final_picks = [
            {"ticker": r["ticker"], "team": r["team"], "q": r["q"],
             "ask": r["ask"]}
            for r in store.get_rule_evaluations(conn, cycle_date, would_pick=True)
        ]
        store.delete_pending_parlay_suggestions(conn, cycle_date)
        suggestions = parlay_mod.suggest_parlays(final_picks)
        sugg_stored = 0
        for s in suggestions:
            s = dict(s)
            s["cycle_date"] = cycle_date
            if store.insert_parlay_suggestion(conn, s):
                sugg_stored += 1

        picks = [r for r in store.get_rule_evaluations(conn, cycle_date,
                                                       would_pick=True)]
        result = {
            "cycle_date": cycle_date, "games": len(games),
            "sides_evaluated": len(fresh), "inserted": inserted,
            "updated": updated, "already": already,
            "picks": len(picks),
            "pick_tickers": [p["ticker"] for p in picks],
            "parlay_suggestions": sugg_stored,
            "reasons": dict(reasons),
        }
        store.finish_run(conn, "rule", cycle_date, attempt, "complete",
                         len(fresh), len(picks), 0, None)
        return result
    except Exception as e:
        store.finish_run(conn, "rule", cycle_date, attempt, "failed",
                         0, 0, 0, str(e))
        raise


# --- Stage 2: settlement ----------------------------------------------------

def settle_rule(conn, cycle_date=None):
    """Settle would-be picks (and parlay suggestions) against real results.

    Picks: the dated ESPN scoreboard for the game date gives final scores;
    a pick wins when its team scored more. Parlay suggestions settle from
    their legs' outcomes (won only when every leg won). Nothing here
    touches the ledger — settlement is a paper record only.
    """
    if cycle_date is None:
        rows = conn.execute(
            "SELECT DISTINCT cycle_date FROM rule_evaluations "
            "WHERE outcome = 'pending' ORDER BY cycle_date").fetchall()
        cycles = [r["cycle_date"] for r in rows]
    else:
        cycles = [cycle_date]
    settled = {"cycles": cycles, "picks_settled": 0, "picks_won": 0,
               "picks_pending": 0, "parlays_settled": 0, "parlays_won": 0,
               "longshots_settled": 0, "longshots_won": 0}
    sb_cache = {}
    for cyc in cycles:
        # Settle picks AND long-shot watch rows (non-pick evaluations
        # with ask <= 0.10). Everything else stays pending: there is no
        # outcome to measure for an ordinary non-pick.
        pending = [dict(r) for r in conn.execute(
            "SELECT * FROM rule_evaluations WHERE cycle_date = ? "
            "AND outcome = 'pending' AND (would_pick = 1 OR "
            "(ask IS NOT NULL AND ask <= ?))",
            (cyc, config.LONG_SHOT_MAX_ASK)).fetchall()]
        for ev in pending:
            league = config.ESPN_LEAGUES.get(ev["series"])
            game_date = ev["game_date"]
            outcome = None
            home_score = away_score = None
            if league and game_date:
                key = (league, game_date)
                if key not in sb_cache:
                    try:
                        sb_cache[key] = stats_client.fetch_scoreboard(
                            league, game_date.replace("-", ""))
                    except Exception:
                        sb_cache[key] = None
                    time.sleep(config.ESPN_DELAY_BETWEEN_CALLS_S)
                events = sb_cache[key] or []
                for event in events:
                    comps = (event.get("competitions") or [{}])[0]
                    status = (comps.get("status") or {}).get("type") or {}
                    if status.get("state") != "post":
                        continue
                    names = {}
                    for c in comps.get("competitors") or []:
                        team = c.get("team") or {}
                        names[team.get("displayName")] = \
                            stats_client.competitor_score(c)
                    if ev["home_team"] in names and ev["away_team"] in names:
                        home_score = names[ev["home_team"]]
                        away_score = names[ev["away_team"]]
                        if home_score is None or away_score is None:
                            outcome = "unknown"
                        elif home_score == away_score:
                            outcome = "unknown"  # no OT decider recorded
                        else:
                            winner = (ev["home_team"] if home_score > away_score
                                      else ev["away_team"])
                            outcome = "won" if winner == ev["team"] else "lost"
                        break
            if outcome:
                store.update_rule_outcome(conn, ev["id"], outcome,
                                          home_score, away_score)
                if ev["would_pick"]:
                    settled["picks_settled"] += 1
                    if outcome == "won":
                        settled["picks_won"] += 1
                else:
                    settled["longshots_settled"] += 1
                    if outcome == "won":
                        settled["longshots_won"] += 1
        # Parlay suggestions settle from their legs.
        for sugg in store.get_parlay_suggestions(conn, cyc, outcome="pending"):
            legs = json.loads(sugg["legs"])
            leg_outcomes = []
            for leg in legs:
                row = conn.execute(
                    "SELECT outcome FROM rule_evaluations "
                    "WHERE cycle_date = ? AND ticker = ?",
                    (cyc, leg)).fetchone()
                leg_outcomes.append(row["outcome"] if row else "unknown")
            if all(o in ("won", "lost") for o in leg_outcomes):
                outcome = "won" if all(o == "won" for o in leg_outcomes) else "lost"
                store.update_parlay_outcome(conn, sugg["id"], outcome)
                settled["parlays_settled"] += 1
                if outcome == "won":
                    settled["parlays_won"] += 1
    pending_left = conn.execute(
        "SELECT COUNT(*) AS n FROM rule_evaluations "
        "WHERE would_pick = 1 AND outcome = 'pending'").fetchone()["n"]
    settled["picks_pending"] = pending_left
    return settled


def rule_report(conn, cycle_date):
    """Paper-tracking report for one cycle: picks, reasons, settlements,
    parlay suggestions. No money figures, no ledger."""
    evals = [dict(r) for r in store.get_rule_evaluations(conn, cycle_date)]
    picks = [e for e in evals if e["would_pick"]]
    sugg = [dict(r) for r in store.get_parlay_suggestions(conn, cycle_date)]
    # Long-shot watch: every evaluated market at ask <= 0.10, picks or
    # not, with its implied probability (the ask) and settled outcome.
    long_shots = [e for e in evals
                  if e["ask"] is not None
                  and e["ask"] <= config.LONG_SHOT_MAX_ASK]
    ls_settled = [e for e in long_shots if e["outcome"] in ("won", "lost")]
    ls_won = sum(1 for e in ls_settled if e["outcome"] == "won")
    return {
        "cycle_date": cycle_date,
        "sides_evaluated": len(evals),
        "picks": len(picks),
        "pick_tickers": [p["ticker"] for p in picks],
        "reasons": dict(Counter(e["reason"] for e in evals)),
        "pick_outcomes": dict(Counter(p["outcome"] for p in picks)),
        "long_shot_watch": {
            "count": len(long_shots),
            "settled": len(ls_settled),
            "won": ls_won,
            "win_rate": round(ls_won / len(ls_settled), 4)
                        if ls_settled else None,
            "avg_implied": round(
                sum(e["ask"] for e in long_shots) / len(long_shots), 4)
                if long_shots else None,
        },
        "parlay_suggestions": [
            {"legs": json.loads(s["legs"]), "n_legs": s["n_legs"],
             "combined_q": s["combined_q"],
             "combined_price": s["combined_price"],
             "combined_payout_cents": s["combined_payout_cents"],
             "fee_cents": s["fee_cents"], "ev_cents": s["ev_cents"],
             "outcome": s["outcome"]}
            for s in sugg
        ],
    }


if __name__ == "__main__":
    sys.exit(main())
