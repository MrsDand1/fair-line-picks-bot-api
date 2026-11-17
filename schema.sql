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
