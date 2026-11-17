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
