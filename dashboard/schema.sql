-- joblist dashboard — D1 schema
--
-- PRIVACY CONTRACT (see ../CLAUDE.md "Privacy split"):
-- The `rejects` table is NEVER mirrored here. Reject rows carry reasons like
-- "flagged employer (X)", so publishing them would make the employer list
-- inferable from the dashboard. Only scored survivors are pushed.
-- Do not add a rejects table to this file.

CREATE TABLE IF NOT EXISTS jobs (
  uid              TEXT PRIMARY KEY,
  source           TEXT NOT NULL,
  title            TEXT NOT NULL,
  company          TEXT,
  url              TEXT,
  posted_at        TEXT,
  regions          TEXT,            -- JSON array as stored locally
  salary_signal    TEXT,
  score            INTEGER,
  verdict          TEXT,
  why              TEXT,
  cv_variant       TEXT,
  first_seen       TEXT NOT NULL,
  -- triage state is owned by the dashboard, never by ingest
  state            TEXT NOT NULL DEFAULT 'new',   -- new | interested | applied | passed
  state_updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_state_score ON jobs(state, score DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(score DESC);

-- login throttling: one row per client IP, reset on success or window expiry
CREATE TABLE IF NOT EXISTS login_attempts (
  ip           TEXT PRIMARY KEY,
  fails        INTEGER NOT NULL DEFAULT 0,
  window_start TEXT NOT NULL
);
