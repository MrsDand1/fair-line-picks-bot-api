-- Fair Line Picks Bot — Stage 1 schema (SQLite).
-- Every snapshot is stored raw with its fetch timestamp; nothing is overwritten.
-- One snapshot per ticker per cycle day (UNIQUE) so a resumed run can never
-- duplicate a market, and a kill-and-restart loses nothing already captured.

PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS kalshi_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticker TEXT NOT NULL,
  series TEXT NOT NULL,
  cycle_date TEXT NOT NULL,          -- YYYY-MM-DD, America/New_York
  fetched_at TEXT NOT NULL,          -- ISO-8601 UTC
  yes_bid REAL,                      -- dollars 0..1, NULL when book is empty
  yes_ask REAL,                      -- 1 - highest no_dollars price
  yes_levels INTEGER,
  no_levels INTEGER,
  market_title TEXT,
  close_ts TEXT,
  status TEXT NOT NULL DEFAULT 'ok', -- ok | empty_book | malformed
  flags TEXT NOT NULL DEFAULT '[]',   -- JSON list of integrity flags
  raw_json TEXT NOT NULL,
  UNIQUE(ticker, cycle_date)
);

CREATE TABLE IF NOT EXISTS odds_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sport_key TEXT NOT NULL,
  cycle_date TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  credits_used INTEGER,              -- credits this call cost (header-reported)
  requests_remaining INTEGER,        -- x-requests-remaining after the call
  events INTEGER,
  status TEXT NOT NULL DEFAULT 'ok',
  flags TEXT NOT NULL DEFAULT '[]',
  raw_json TEXT,
  UNIQUE(sport_key, cycle_date)
);

CREATE TABLE IF NOT EXISTS odds_outcomes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_id INTEGER NOT NULL REFERENCES odds_snapshots(id),
  event_id TEXT NOT NULL,
  commence_time TEXT,
  home_team TEXT,
  away_team TEXT,
  bookmaker TEXT NOT NULL,
  outcome_name TEXT NOT NULL,
  price_american INTEGER,
  price_decimal REAL,
  UNIQUE(snapshot_id, event_id, bookmaker, outcome_name)
);

-- Ledger scaffold. Opens at the DECIDED $15.00 shared bankroll.
-- No trades are placed by Stage 1; this table is the scaffold Stage 3 uses.
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,                -- open | trade | fee | settle | note
  amount_cents INTEGER NOT NULL,
  balance_cents INTEGER NOT NULL,
  note TEXT
);

CREATE TABLE IF NOT EXISTS capture_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  part TEXT NOT NULL,                -- kalshi | odds
  cycle_date TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL DEFAULT 'running', -- running | complete | failed
  markets_polled INTEGER DEFAULT 0,
  markets_ok INTEGER DEFAULT 0,
  markets_missing INTEGER DEFAULT 0,
  notes TEXT,
  UNIQUE(part, cycle_date, attempt)
);

CREATE INDEX IF NOT EXISTS idx_kalshi_cycle ON kalshi_snapshots(cycle_date, series);
CREATE INDEX IF NOT EXISTS idx_odds_cycle ON odds_snapshots(cycle_date, sport_key);

-- Stage 2 — team-average stats (one row per team per cycle day).
-- PF/PA per game from completed games only, as of the cycle morning
-- (ET date strictly before cycle_date). Source: ESPN public site API.
CREATE TABLE IF NOT EXISTS stats_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  espn_team_id TEXT NOT NULL,
  team_name TEXT,
  team_abbreviation TEXT,
  sport TEXT NOT NULL,               -- Kalshi series, e.g. KXNBAGAME
  cycle_date TEXT NOT NULL,          -- YYYY-MM-DD, America/New_York
  fetched_at TEXT NOT NULL,          -- ISO-8601 UTC
  games_played INTEGER NOT NULL,
  pf_per_game REAL,                  -- NULL when games_played = 0
  pa_per_game REAL,
  UNIQUE(espn_team_id, sport, cycle_date)
);

-- Stage 2 — forward tracking of the rule (PAPER ONLY).
-- One row per market side (ticker) per cycle day. would_pick is the FINAL
-- decision after the daily cap. This table never feeds the ledger and no
-- row here is a trade. outcome: pending | won | lost | unknown.
CREATE TABLE IF NOT EXISTS rule_evaluations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_date TEXT NOT NULL,
  ticker TEXT NOT NULL,
  series TEXT NOT NULL,
  game_slug TEXT,
  game_date TEXT,                    -- YYYY-MM-DD (from ticker)
  home_team TEXT,
  away_team TEXT,
  team TEXT NOT NULL,                -- the side this row evaluates
  q REAL,                            -- stats-implied win probability
  ask REAL,                          -- executable YES ask (NULL one-sided)
  threshold REAL,                    -- 1.1 * ask + 0.03 (NULL when no ask)
  edge REAL,                         -- q - threshold (NULL when no threshold)
  would_pick INTEGER NOT NULL DEFAULT 0,
  reason TEXT NOT NULL,              -- pick | no_ask | below_min_games |
                                     -- below_50pct_floor | below_threshold |
                                     -- daily_cap | no_stats | no_home_away
  outcome TEXT NOT NULL DEFAULT 'pending',
  settled_at TEXT,
  home_score REAL,
  away_score REAL,
  UNIQUE(ticker, cycle_date)
);

-- Stage 2 — parlay SUGGESTIONS (secondary output, never picks).
-- Paper constructions only: Kalshi has no native parlay product, so a
-- suggestion is just "these legs together", never one combined bet.
-- Legs are that day's qualifying singles (post-cap). combined_q is the
-- HONEST probability all legs hit (product of the legs' q values).
-- outcome: pending | won | lost | unknown. Tracked to the same honesty
-- standard as picks: every suggestion's result gets recorded.
CREATE TABLE IF NOT EXISTS parlay_suggestions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_date TEXT NOT NULL,
  legs TEXT NOT NULL,                -- JSON list of tickers
  n_legs INTEGER NOT NULL,           -- 2 or 3 only
  combined_q REAL NOT NULL,          -- product of leg q values
  combined_price REAL NOT NULL,      -- product of leg asks
  combined_payout_cents INTEGER,     -- paper payout if all legs win
  fee_cents INTEGER NOT NULL,        -- 1c per leg (minimum taker fee)
  ev_cents REAL,                     -- fee-adjusted expected value, cents
  note TEXT,
  outcome TEXT NOT NULL DEFAULT 'pending',
  settled_at TEXT,
  UNIQUE(cycle_date, legs)
);

CREATE INDEX IF NOT EXISTS idx_stats_cycle ON stats_snapshots(cycle_date, sport);
CREATE INDEX IF NOT EXISTS idx_rule_cycle ON rule_evaluations(cycle_date, would_pick);
CREATE INDEX IF NOT EXISTS idx_parlay_cycle ON parlay_suggestions(cycle_date);
