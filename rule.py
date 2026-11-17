"""Stage 2 rule module — team-average picks (design §2.2, exactly).

The rule, in one paragraph: each team's points scored and allowed per
game (completed games only, as of the cycle morning) produce an expected
margin; the margin converts to a stats-implied win probability; a pick
happens only when that probability beats the Kalshi ask price by enough
to pay the ~10%-per-leg fee drag plus a safety margin, AND the picked
side is more likely than not to win, AND the day has not already used
its 5-pick cap.

This module EVALUATES and LOGS only. It never trades, never touches the
ledger, and never presents a pick as validated or an edge. No-play is
the default; every failure mode is a named reason, never a silent skip.

Checks, in order (the first failure stops the side):
  1. "no_ask"        — one-sided or empty book: nothing to buy.
  2. "below_min_games" — either team below the minimum games played
                        (>= 5, >= 4 for NFL).
  3. "below_50pct_floor" — q <= 0.50. The picked side must be more likely
                        than not to win (her words, Nov 17, 2026). A long
                        shot that clears the price threshold alone does
                        NOT qualify. THIS CHECK IS PER-SPORT: it applies
                        to team sports only. Tennis singles use the pure
                        value rule with NO floor (her decision, Nov 17,
                        2026): in tennis the mispricing she sees is in
                        the underdogs, and a value underdog (market 30%,
                        stats say 40%) must be allowed to qualify.
                        The floor is a per-sport flag in the rule params
                        ("floor": True/False); evaluate_side takes it as
                        apply_floor.
  4. "below_threshold" — q < 1.1 * ask + 0.03 (fee bar + safety margin).
  5. "daily_cap"     — applied across the day, not per side: at most 5
                        picks per day across all four sports, ranked by
                        edge (q - threshold, largest first). 5 is the
                        ceiling, never a target.
  Pass: "pick".
"""
import math

import config


def norm_cdf(x):
    """Standard normal CDF, via erf (stdlib only)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def expected_margin(home_pf, home_pa, away_pf, away_pa, hca):
    """Expected home margin M = H_exp - A_exp + HCA, where
    H_exp = (H_PF + A_PA) / 2 and A_exp = (A_PF + H_PA) / 2."""
    h_exp = (home_pf + away_pa) / 2.0
    a_exp = (away_pf + home_pa) / 2.0
    return h_exp - a_exp + hca


def win_probability(margin, sigma):
    """q_H = Phi(M / sigma)."""
    return norm_cdf(margin / sigma)


def pick_threshold(ask):
    """q >= 1.1 * ask + 0.03. At a 0.50 ask the threshold is 0.58."""
    return config.FEE_FACTOR * ask + config.SAFETY_MARGIN


def fee_breakeven(ask):
    """Fee-only breakeven q = 1.1 * ask (before the safety margin)."""
    return config.FEE_FACTOR * ask


def evaluate_side(q, ask, games_home, games_away, min_games,
                    apply_floor=True):
    """Evaluate one side of one game. Returns a dict with would_pick,
    edge, threshold and a reason. No cap is applied here — the cap is a
    day-level step in apply_daily_cap. apply_floor=False skips the
    more-likely-than-not check (tennis only)."""
    if ask is None:
        return {"would_pick": False, "q": q, "ask": None, "threshold": None,
                "edge": None, "reason": "no_ask"}
    if games_home < min_games or games_away < min_games:
        return {"would_pick": False, "q": q, "ask": ask, "threshold": None,
                "edge": None, "reason": "below_min_games"}
    if apply_floor and q <= config.MIN_WIN_PROBABILITY:
        return {"would_pick": False, "q": q, "ask": ask,
                "threshold": pick_threshold(ask), "edge": None,
                "reason": "below_50pct_floor"}
    threshold = pick_threshold(ask)
    if q < threshold:
        return {"would_pick": False, "q": q, "ask": ask,
                "threshold": threshold, "edge": round(q - threshold, 4),
                "reason": "below_threshold"}
    return {"would_pick": True, "q": q, "ask": ask, "threshold": threshold,
            "edge": round(q - threshold, 4), "reason": "pick"}


def evaluate_game(home_stats, away_stats, ask_home, ask_away, params):
    """Evaluate both sides of one game.

    home_stats / away_stats: dicts with pf_per_game, pa_per_game,
    games_played. ask_home / ask_away: the executable YES ask for each
    side (None when the book is one-sided). params: the design's
    {sigma, hca, min_games} for the series.
    Returns {"home": <side dict>, "away": <side dict>, "margin": M,
    "q_home": q}.
    """
    margin = expected_margin(
        home_stats["pf_per_game"], home_stats["pa_per_game"],
        away_stats["pf_per_game"], away_stats["pa_per_game"],
        params["hca"],
    )
    q_home = win_probability(margin, params["sigma"])
    min_games = params["min_games"]
    floor = params.get("floor", True)
    return {
        "margin": round(margin, 4),
        "q_home": round(q_home, 4),
        "home": evaluate_side(q_home, ask_home,
                              home_stats["games_played"],
                              away_stats["games_played"], min_games, floor),
        "away": evaluate_side(1.0 - q_home, ask_away,
                              away_stats["games_played"],
                              home_stats["games_played"], min_games, floor),
    }


def apply_daily_cap(evaluations, cap=config.DAILY_PICK_CAP):
    """Hard daily cap, applied AFTER per-side evaluation.

    evaluations: list of dicts, each with would_pick (bool) and edge
    (float, set for candidates). Candidates are ranked by edge, largest
    first (ties: higher q, then ticker alphabetical, so the cut is
    deterministic). The top `cap` stay picks; the rest become
    would_pick=False with reason "daily_cap". Non-candidates pass
    through untouched. Returns the list (mutated in place, also returned).
    """
    candidates = [e for e in evaluations if e.get("would_pick")]
    if len(candidates) <= cap:
        return evaluations
    ranked = sorted(
        candidates,
        key=lambda e: (-(e.get("edge") or 0.0), -(e.get("q") or 0.0),
                       e.get("ticker") or ""),
    )
    keep = {id(e) for e in ranked[:cap]}
    for e in candidates:
        if id(e) not in keep:
            e["would_pick"] = False
            e["reason"] = "daily_cap"
    return evaluations
