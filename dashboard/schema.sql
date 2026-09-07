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
  -- What the model scored before the local blocker penalty. Kept so a card can
  -- show "40 (was 70)" rather than a number that reads as a bad review of the
  -- work, when the work is fine and the application is simply not possible.
  score_raw        INTEGER,
  blockers         TEXT,            -- JSON array of requirement kinds
  first_seen       TEXT NOT NULL,
  -- triage state is owned by the dashboard, never by ingest
  state            TEXT NOT NULL DEFAULT 'new',   -- new | interested | applied | passed
  state_updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_state_score ON jobs(state, score DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(score DESC);

-- What each posting asked the applicant to PRODUCE. Mirrors the local table.
--
-- Safe to publish, unlike `rejects`: these are the employers' demands, not a
-- record of who was filtered out and why. `blocking` is the one personal bit --
-- it says a requirement is one the candidate cannot meet -- and that is already
-- implied by jobs.blockers, on a dashboard only they can sign into.
--
-- (uid, kind) is the primary key, so a listing can never contribute two rows
-- for the same requirement and the tally cannot double-count.
CREATE TABLE IF NOT EXISTS job_requirements (
  uid        TEXT NOT NULL,
  kind       TEXT NOT NULL,
  mandatory  INTEGER NOT NULL DEFAULT 0,
  detail     TEXT,
  blocking   INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (uid, kind)
);
CREATE INDEX IF NOT EXISTS idx_jobreq_kind ON job_requirements(kind);

-- login throttling: one row per client IP, reset on success or window expiry
CREATE TABLE IF NOT EXISTS login_attempts (
  ip           TEXT PRIMARY KEY,
  fails        INTEGER NOT NULL DEFAULT 0,
  window_start TEXT NOT NULL
);

-- Drafted cover letters. LOCAL DASHBOARD ONLY.
--
-- A letter is written from the resume and speaks in the candidate's own voice
-- about their own history, which makes it the most personal thing the pipeline
-- produces -- more so than the advert text that push.py already keeps off the
-- wire. So ../push.py refuses --with-covers against any host that is not
-- localhost, and on a deployed copy this table simply stays empty.
--
-- It lives in this shared file rather than a separate local-only schema so the
-- two databases cannot drift apart. An empty table is not a leak; two schemas
-- that disagree are a bug waiting for the day someone runs the wrong one.
CREATE TABLE IF NOT EXISTS covers (
  uid        TEXT PRIMARY KEY,
  body       TEXT NOT NULL,
  drafted_at TEXT
);

-- Each board's age window, so this dashboard can retire a listing on the same
-- day the digest does.
--
-- The window is a rendering concern in both places and is never applied at
-- fetch or write time, so nothing is ever deleted for being stale and changing
-- a number here re-renders the whole history for free. Same discipline as
-- digest.py's _partition() and report.py --reapply.
--
-- It has to travel as data because the source of truth is boards.<name>.
-- max_age_days in profile.yaml, which is local and never pushed. Without this
-- table the Worker had no way to know a window existed at all, so the two
-- surfaces disagreed the moment the first row aged out -- and the dashboard,
-- being the one you actually look at, became the one showing dead listings.
--
-- A board with no row here is NOT trimmed. That is the safe default: a new
-- source appears on the dashboard in full rather than silently empty.
CREATE TABLE IF NOT EXISTS board_policy (
  source       TEXT PRIMARY KEY,
  max_age_days INTEGER
);
