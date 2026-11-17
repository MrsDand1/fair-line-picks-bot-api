"""Fair Line Picks Bot — Stage 1 configuration.

All values decided by Cedrina on Nov 16, 2026, or verified in Stage 0.
Nothing here is a secret. The Odds API key is NEVER in this file:
it comes only from the ODDS_API_KEY environment variable at runtime.
"""

# Kalshi public API (keyless, free)
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# The four decided starting series (Stage 0: KXNCAAMBGAME is the live
# men's college basketball game-winner series; KXNCAABGAME is dead).
KALSHI_SERIES = ["KXNFLGAME", "KXNBAGAME", "KXNCAAMBGAME", "KXNHLGAME"]

# The Odds API (free tier) — one h2h call per sport per day.
# regions=us + bookmakers=fanduel,draftkings,betmgm,pinnacle costs
# 1 credit per sport per day: 4/day, ~120/month of the 500 free credits.
ODDS_BASE = "https://api.the-odds-api.com/v4"
ODDS_SPORTS = {
    "americanfootball_nfl": "KXNFLGAME",
    "basketball_nba": "KXNBAGAME",
    "basketball_ncaab": "KXNCAAMBGAME",
    "icehockey_nhl": "KXNHLGAME",
}
ODDS_BOOKMAKERS = "fanduel,draftkings,betmgm,pinnacle"
ODDS_REGIONS = "us"
ODDS_MARKETS = "h2h"

# Money — DECIDED Nov 16, 2026 (her words: live from start, $0.10/trade,
# bankroll = available Kalshi cash, $15, ONE shared pool with the BTC bot).
# The plan's $100 paper bankroll / $1 trade is superseded; see README.
BANKROLL_CENTS = 1500
STAKE_CENTS = 10

# Kalshi taker fee: round up (M x 0.07 x C x P x (1-P)), M=1 for these series
# (BTC Stage 0, verified against Kalshi's fee schedule PDF, effective Jul 7 2026).
FEE_RATE = 0.07

# Politeness: Kalshi's keyless rate limit is undocumented and this VM got
# HTTP 429 from a burst on Nov 16, 2026. Space calls seconds apart, never burst.
KALSHI_DELAY_BETWEEN_CALLS_S = 2.0
KALSHI_DELAY_BETWEEN_SERIES_S = 5.0
MAX_RETRIES = 3
RETRY_BACKOFF_S = [15, 30, 60]

# Cycle day is America/New_York (Cedrina's timezone).
TIMEZONE = "America/New_York"

# Staleness: a snapshot older than this is flagged stale by integrity checks.
STALE_AFTER_MINUTES = 60

# ---------------------------------------------------------------------------
# Stage 2 — team-average picks (design: stage-2-team-averages-design.md).
# ---------------------------------------------------------------------------

# ESPN public site API (free, no key, no payment). Verified live Nov 17, 2026
# for all four sports (team stats + schedule + dated scoreboard).
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
# Kalshi series -> ESPN league path.
ESPN_LEAGUES = {
    "KXNFLGAME": "football/nfl",
    "KXNBAGAME": "basketball/nba",
    "KXNCAAMBGAME": "basketball/mens-college-basketball",
    "KXNHLGAME": "hockey/nhl",
}
# NCAAB teams list is paginated (default 50); 365 D1 teams need a big limit.
ESPN_TEAMS_LIMIT = 500
# Politeness: public no-key API; space calls, never burst.
ESPN_DELAY_BETWEEN_CALLS_S = 0.3

# Rule parameters (design §2.2 — STARTING values to be fitted by the
# backtest, not measured facts; fitted values win only with her approval).
# sigma = typical spread of a final margin; HCA = home advantage, same units.
RULE_PARAMS = {
    # "floor": the more-likely-than-not floor (q > 0.50) applies to team
    # sports. Tennis singles do NOT use the floor (see TENNIS_SERIES
    # below): in tennis the mispricing she sees is in the underdogs, and
    # a value underdog (market 30%, stats say 40%) must be allowed to
    # qualify on the pure value rule alone.
    "KXNBAGAME": {"sigma": 12.0, "hca": 2.5, "min_games": 5, "floor": True},
    "KXNFLGAME": {"sigma": 13.5, "hca": 2.0, "min_games": 4, "floor": True},
    "KXNCAAMBGAME": {"sigma": 11.0, "hca": 3.0, "min_games": 5, "floor": True},
    "KXNHLGAME": {"sigma": 1.7, "hca": 0.2, "min_games": 5, "floor": True},
}

# TENNIS — first-class track, model NOT built in this pass (next build).
# Series are already in the plan (KXATPGAME / KXWTAGAME, captured when in
# season; season ends mid-Nov, resumes late Dec/Jan). Tennis singles use
# the pure value rule (q >= 1.1 * ask + 0.03) with NO 50% floor, singles
# only, sharing the 5-per-day cap. The model is Elo-based implied
# probability vs Kalshi ask; the existing Fair Line tennis Elo trails
# the market (Brier 0.2166 vs 0.1999), so its data source needs its own
# verification before it can drive picks.
TENNIS_SERIES = ["KXATPGAME", "KXWTAGAME"]
TENNIS_FLOOR = False
# Pick threshold: q >= FEE_FACTOR * ask + SAFETY_MARGIN (design §2.2).
# At a 0.50 ask the threshold is q >= 0.58.
FEE_FACTOR = 1.1
SAFETY_MARGIN = 0.03
# MORE-LIKELY-THAN-NOT FLOOR (her words, Nov 17, 2026): a pick must have
# q > 0.50 — the picked side has to be more likely than not to win, in
# addition to clearing the price threshold.
MIN_WIN_PROBABILITY = 0.50
# DAILY CAP (her words: "no more than 4-5 on a slip"): hard maximum of 5
# picks per day across all four sports, ranked by edge (q - threshold,
# largest first). 5 is the ceiling, never a target; normally the threshold
# yields 4 or fewer. The daily slip holds up to 5 SINGLES (her decision,
# Nov 17, 2026); parlay suggestions are a separate output (see parlay.py)
# and are never counted in this cap.
DAILY_PICK_CAP = 5

# LONG-SHOT WATCH (her observation, Nov 17, 2026): tennis underdogs
# priced at 4-10% win often enough daily to be worth measuring. Every
# evaluated market with yes_ask <= 0.10 is logged in rule_evaluations
# with its implied probability (the ask) and settled like a pick, so the
# actual win rate of 4-10% shots can be measured against the price.
# Measurement only: a long shot still has to clear the normal value
# threshold to become a pick. Note the threshold is demanding at low
# prices by design (at a 0.05 ask the rule needs q >= 0.085, nearly
# double the implied probability), and the fee is 1c per order even on
# a 4c contract.
LONG_SHOT_MAX_ASK = 0.10

# Parlay SUGGESTIONS (her decision, Nov 17, 2026): from that day's
# qualifying singles, the system may suggest 2-3 leg combinations,
# maximum 3 legs. Paper constructions only — Kalshi has no native parlay
# product. At most one best 2-leg and one best 3-leg suggestion per day,
# ranked by combined probability (most likely to hit first).
PARLAY_MIN_LEGS = 2
PARLAY_MAX_LEGS = 3
