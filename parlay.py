"""Stage 2 parlay SUGGESTIONS — a separate, clearly-labeled output.

PICKS are singles only (see rule.py). From that day's qualifying singles
— after the 5-per-day cap — the system may suggest 2-3 leg combinations,
maximum 3 legs, at most one best 2-leg and one best 3-leg per day, ranked
by combined probability (most likely to hit first).

PAPER CONSTRUCTIONS ONLY: Kalshi has no native parlay product, so a
suggestion is just "these legs together", never one combined bet and
never a pick. Every suggestion shows its HONEST combined probability —
the product of the legs' q values (three 60% legs = 21.6% to hit all
three) — and the fee-adjusted math for the combined slip.

FEE MODEL (stated assumption, not a market fact): one $0.10 stake on the
combined slip. Decimal odds multiply, so the paper payout if every leg
wins is $0.10 / (product of leg asks). Fees are 1c per leg — the minimum
taker fee per order, since a real placement would be one order per leg.
Total cost = $0.10 + $0.01 * n_legs.
EV = combined_q * payout - cost. A suggestion is labelled with its EV
whether it is positive or negative; a negative EV is still shown, never
hidden, because the point of the suggestion is transparency, not sales.

SKEPTIC'S NOTE (shown with every suggestion): parlays multiply the
books' margin — each leg already carries the fee drag and the market's
edge, and combining legs compounds both. A parlay is never more likely
to hit than its combined probability says.

Suggestions are never counted as picks and never touch the daily cap.
They are logged to parlay_suggestions and settled to the same honesty
standard as picks: every suggestion's result gets recorded, wins and
losses.
"""
import itertools
import math

import config

SKEPTIC_NOTE = (
    "Suggestion only, never a pick. Parlays multiply the books' margin: "
    "each leg already carries the fee drag, and combining legs compounds "
    "it. This is a paper construction — Kalshi has no native parlay — "
    "and it is only as likely to hit as its combined probability says."
)


def combined_probability(qs):
    """Honest probability all legs hit: the product of the legs' q."""
    p = 1.0
    for q in qs:
        p *= q
    return round(p, 4)


def combined_price(asks):
    """Combined implied price: the product of the leg asks."""
    p = 1.0
    for a in asks:
        p *= a
    return round(p, 4)


def combined_payout_cents(asks, stake_cents=None):
    """Paper payout in cents if every leg wins: stake / product(asks),
    rounded down to whole cents (never round a payout up)."""
    stake = config.STAKE_CENTS if stake_cents is None else stake_cents
    price = 1.0
    for a in asks:
        price *= a
    if price <= 0:
        return 0
    return math.floor(stake / price)


def parlay_ev_cents(qs, asks, stake_cents=None):
    """Fee-adjusted expected value in cents for the combined slip.

    EV = combined_q * payout - (stake + 1c per leg).
    """
    stake = config.STAKE_CENTS if stake_cents is None else stake_cents
    fee = len(qs)  # 1c minimum taker fee per leg-order
    payout = combined_payout_cents(asks, stake)
    q = 1.0
    for v in qs:
        q *= v
    return round(q * payout - (stake + fee), 2)


def build_suggestion(legs):
    """One suggestion dict from legs: each leg is a dict with ticker,
    team, q and ask. Legs must already be qualifying singles."""
    qs = [leg["q"] for leg in legs]
    asks = [leg["ask"] for leg in legs]
    return {
        "legs": [leg["ticker"] for leg in legs],
        "leg_teams": [leg["team"] for leg in legs],
        "n_legs": len(legs),
        "combined_q": combined_probability(qs),
        "combined_price": combined_price(asks),
        "combined_payout_cents": combined_payout_cents(asks),
        "fee_cents": len(legs),
        "ev_cents": parlay_ev_cents(qs, asks),
        "note": SKEPTIC_NOTE,
    }


def suggest_parlays(picks):
    """Best 2-leg and best 3-leg suggestions from the day's picks.

    picks: the day's FINAL qualifying singles (post-cap), each a dict
    with ticker, team, q, ask. Returns at most two suggestions: the
    highest-combined-q pair and the highest-combined-q triple. Returns
    [] when fewer than 2 picks exist. Suggestions never affect the cap
    and are never picks.
    """
    if len(picks) < config.PARLAY_MIN_LEGS:
        return []
    suggestions = []
    for size in (config.PARLAY_MIN_LEGS, config.PARLAY_MAX_LEGS):
        if len(picks) < size:
            continue
        combos = [build_suggestion(list(c))
                  for c in itertools.combinations(picks, size)]
        # Most likely to hit first; ties broken by higher EV, then legs.
        combos.sort(key=lambda s: (-s["combined_q"], -s["ev_cents"],
                                   s["legs"]))
        suggestions.append(combos[0])
    return suggestions
