"""Stage 2 backtest harness — replays the team-average rule against
captured history and decides out-of-sample.

Replay: as-of-date averages (stats_snapshots) -> rule -> captured
morning price (kalshi_snapshots) -> actual outcome (settled
rule_evaluations) -> Kalshi fee formula. In-sample fits sigma/HCA per
sport by grid search on fee-adjusted EV; out-of-sample decides.

HONEST GATE: the harness refuses to produce a verdict until there is
enough history. It needs at least MIN_CYCLES cycles with settled picks
(default 10). With only one price cycle (Nov 16, 2026) and no settled
games yet, it reports insufficient_data — it never invents a verdict,
never fits parameters on one day, and never calls the rule validated.

Paper P&L per pick ($0.10 stake, design §2.3):
  win:  floor(10 / ask) - 11 cents   (payout minus stake minus 1c fee)
  lose: -11 cents                     (stake plus 1c fee)
EV = q * (10 / ask) - 11.

Usage:
  python3 backtest.py              # report on the local DB
  python3 backtest.py --min-cycles 10
"""
import argparse
import json
import math
import sys

import config
import rule as rule_mod
import store

DEFAULT_MIN_CYCLES = 10


def paper_pnl_cents(won, ask):
    """Fee-adjusted paper P&L in cents for one $0.10 pick."""
    if won:
        return math.floor(config.STAKE_CENTS / ask) - (config.STAKE_CENTS + 1)
    return -(config.STAKE_CENTS + 1)


def available_cycles(conn):
    """Cycle dates that have both Kalshi prices and settled rule picks."""
    rows = conn.execute(
        "SELECT DISTINCT cycle_date FROM kalshi_snapshots ORDER BY cycle_date"
    ).fetchall()
    cycles = []
    for r in rows:
        cyc = r["cycle_date"]
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM rule_evaluations WHERE cycle_date = ? "
            "AND would_pick = 1 AND outcome IN ('won', 'lost')",
            (cyc,)).fetchone()["n"]
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM rule_evaluations WHERE cycle_date = ? "
            "AND would_pick = 1", (cyc,)).fetchone()["n"]
        cycles.append({"cycle_date": cyc, "settled_picks": n,
                       "total_picks": total})
    return cycles


def _row_match_keys(row):
    """(primary, secondary) match keys derivable from a stored stats row
    (displayName + abbreviation only — no live call in the harness).

    Primary: full display name + abbreviation. Secondary: leading-word
    prefixes of the display name ("Portland Trail Blazers" -> "portland",
    "portland trail") with an initial key from the next word, plus the
    LA -> Los Angeles alias. Secondary keys are only used when
    unclaimed (see build_key_map) — "portland" is shared by Portland
    and Portland State, so a blind prefix would poison the match.
    """
    from stats_client import LOCATION_ALIASES, normalize_name
    display = normalize_name(row.get("team_name"))
    abbr = (row.get("team_abbreviation") or "").upper()
    primary = set()
    secondary = set()
    if display:
        primary.add(display)
        words = display.split(" ")
        for i in range(1, len(words)):
            prefix = " ".join(words[:i])
            secondary.add(prefix)
            secondary.add(f"{prefix} {words[i][0]}")
            alias = LOCATION_ALIASES.get(prefix)
            if alias:
                secondary.add(alias)
                secondary.add(f"{alias} {words[i][0]}")
    if abbr:
        primary.add(abbr.lower())
    return primary, secondary


def replay_cycle(conn, cycle_date, params_by_series):
    """Replay one cycle with the given rule params.

    Returns a list of pick dicts: ticker, series, q, ask, won (True/False
    or None when unsettled). Picks are re-derived from stored stats and
    stored morning prices, so the replay tests the rule, not the log.
    The daily cap is applied exactly as in forward tracking.
    """
    snaps = conn.execute(
        "SELECT ticker, series, market_title, yes_ask, status "
        "FROM kalshi_snapshots WHERE cycle_date = ?", (cycle_date,)).fetchall()
    # Stats keyed by (series, espn_team_id): ESPN team IDs AND
    # abbreviations overlap across leagues, so a bare key would silently
    # hand one sport's stats to another sport's game.
    stats = {(r["sport"], str(r["espn_team_id"])): dict(r) for r in
             store.get_stats(conn, cycle_date)}
    outcomes = {r["ticker"]: r["outcome"] for r in
                store.get_rule_evaluations(conn, cycle_date, would_pick=True)}
    # Pair markets into games (same grouping as scan._pair_markets).
    import stats_client
    games = {}
    for s in snaps:
        game_date, remainder = stats_client.parse_game_slug(s["ticker"])
        games.setdefault((s["series"], game_date, remainder), []).append(dict(s))
    # Team map from stored stats rows (no live calls in the harness),
    # built PER SERIES like the live path, two-pass so a shared prefix
    # (Portland / Portland State) cannot poison an exact key.
    from stats_client import build_key_map
    key_maps = {}
    for series in {s["series"] for s in snaps}:
        key_maps[series] = build_key_map([
            (row,) + _row_match_keys(row)
            for row in stats.values() if row["sport"] == series])
    by_abbr = {(r["sport"], r["team_abbreviation"]): r for r in stats.values()
               if r["team_abbreviation"]}
    picks = []
    for (series, game_date, remainder), sides in games.items():
        params = params_by_series.get(series)
        if params is None:
            continue
        resolved = {}
        for side in sides:
            team, _why = stats_client.match_title(
                side["market_title"] or "", key_maps[series])
            resolved[side["ticker"]] = team
        teams = [t for t in resolved.values() if t]
        if len(teams) != 2:
            continue
        split = stats_client.split_home_away(
            remainder or "",
            teams[0]["team_abbreviation"], teams[1]["team_abbreviation"])
        if not split:
            continue
        away_abbr, home_abbr = split
        home = by_abbr.get((series, home_abbr))
        away = by_abbr.get((series, away_abbr))
        if home is None or away is None:
            continue
        if home.get("pf_per_game") is None or away.get("pf_per_game") is None:
            continue
        for side in sides:
            team_row = resolved.get(side["ticker"])
            if team_row is None:
                continue
            is_home = team_row["espn_team_id"] == home["espn_team_id"]
            margin = rule_mod.expected_margin(
                home["pf_per_game"], home["pa_per_game"],
                away["pf_per_game"], away["pa_per_game"], params["hca"])
            q_home = rule_mod.win_probability(margin, params["sigma"])
            q_side = q_home if is_home else 1.0 - q_home
            games_n = home["games_played"] if is_home else away["games_played"]
            other_n = away["games_played"] if is_home else home["games_played"]
            res = rule_mod.evaluate_side(q_side, side["yes_ask"], games_n,
                                         other_n, params["min_games"],
                                         params.get("floor", True))
            if not res["would_pick"]:
                continue
            outcome = outcomes.get(side["ticker"])
            picks.append({
                "ticker": side["ticker"], "series": series,
                "q": round(q_side, 4), "ask": side["yes_ask"],
                "edge": res["edge"], "threshold": res["threshold"],
                "would_pick": True,
                "won": {"won": True, "lost": False}.get(outcome),
            })
    rule_mod.apply_daily_cap(picks)
    return [p for p in picks if p.get("would_pick")]


def _ev_cents(picks):
    """Total fee-adjusted paper EV (cents) and pick count for settled picks."""
    settled = [p for p in picks if p["won"] is not None]
    total = sum(paper_pnl_cents(p["won"], p["ask"]) for p in settled)
    return total, len(settled)


def fit_params(conn, cycles, params_by_series):
    """Grid-search sigma/HCA per sport on the given cycles.

    Grids are multipliers of the design's starting values. Objective:
    total fee-adjusted EV cents across settled picks. Returns
    {series: {sigma, hca, min_games, ev_cents, picks}}.
    """
    fitted = {}
    for series, base in params_by_series.items():
        best = None
        for s_mult in (0.5, 0.75, 1.0, 1.25, 1.5):
            for h_mult in (0.0, 0.5, 1.0, 1.5, 2.0):
                cand = {"sigma": round(base["sigma"] * s_mult, 4),
                        "hca": round(base["hca"] * h_mult, 4),
                        "min_games": base["min_games"]}
                trial = dict(params_by_series)
                trial[series] = cand
                total_ev = 0
                total_picks = 0
                for cyc in cycles:
                    picks = replay_cycle(conn, cyc, trial)
                    ev, n = _ev_cents([p for p in picks
                                       if p["series"] == series])
                    total_ev += ev
                    total_picks += n
                score = (total_ev, total_picks)
                if best is None or score > best[0]:
                    best = (score, cand, total_ev, total_picks)
        _score, cand, ev, n = best
        fitted[series] = dict(cand, ev_cents=ev, picks=n)
    return fitted


def run_backtest(conn, min_cycles=DEFAULT_MIN_CYCLES):
    """Run the backtest, or report honestly that there is not enough data."""
    cycles = available_cycles(conn)
    settled_cycles = [c for c in cycles if c["settled_picks"] > 0]
    base = {
        "cycles_with_prices": len(cycles),
        "cycles_with_settled_picks": len(settled_cycles),
        "settled_picks_total": sum(c["settled_picks"] for c in cycles),
        "min_cycles_required": min_cycles,
    }
    if len(settled_cycles) < min_cycles:
        return dict(base, verdict="insufficient_data",
                    reason=("Need at least %d cycles with settled picks to "
                            "fit and decide out-of-sample; have %d. "
                            "Keep capturing daily and forward-tracking; "
                            "the durable-history fix (VPS or free database) "
                            "is what keeps this history from resetting."
                            % (min_cycles, len(settled_cycles))))
    dates = [c["cycle_date"] for c in settled_cycles]
    half = len(dates) // 2
    in_sample, out_sample = dates[:half], dates[half:]
    fitted = fit_params(conn, in_sample, config.RULE_PARAMS)
    out_picks = []
    for cyc in out_sample:
        out_picks.extend(replay_cycle(conn, cyc, {
            s: {"sigma": v["sigma"], "hca": v["hca"],
                "min_games": v["min_games"]}
            for s, v in fitted.items()}))
    ev, n = _ev_cents(out_picks)
    wins = sum(1 for p in out_picks if p["won"] is True)
    staked = n * (config.STAKE_CENTS + 1)
    roi = (ev / staked) if staked else None
    verdict = "pass" if (roi is not None and roi > 0) else "kill"
    return dict(
        base, verdict=verdict,
        in_sample_cycles=in_sample, out_sample_cycles=out_sample,
        fitted_params=fitted,
        out_sample_picks=n, out_sample_wins=wins,
        out_sample_ev_cents=ev,
        out_sample_roi=round(roi, 4) if roi is not None else None,
        reason=("Out-of-sample fee-adjusted ROI %s after %d settled picks."
                % ("%.1f%%" % (roi * 100) if roi is not None else "n/a", n)),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fair Line Picks Bot — Stage 2 backtest")
    parser.add_argument("--min-cycles", type=int, default=DEFAULT_MIN_CYCLES)
    args = parser.parse_args(argv)
    conn = store.init_db()
    print(json.dumps(run_backtest(conn, args.min_cycles), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
