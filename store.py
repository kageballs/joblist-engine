"""Local SQLite state: what has been seen, when the last run succeeded, and
every scored job.

Two decisions worth keeping:

* Jobs are marked seen only AFTER they have been filtered and scored. Marking
  at fetch time makes `seen` a one-way ratchet -- widen a rule later and
  everything it wrongly rejected last week is already suppressed forever.
* Rejections are recorded with the stage that killed them, so a filter change
  can be replayed against real history instead of guessed at.

Schema mirrors what the phase-2 D1 dashboard will need, so that lands as a
migration rather than a redesign.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    ok           INTEGER NOT NULL DEFAULT 0,
    fetched      INTEGER NOT NULL DEFAULT 0,
    scored       INTEGER NOT NULL DEFAULT 0,
    funnel       TEXT,
    model        TEXT,
    error        TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    uid          TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    title        TEXT NOT NULL,
    company      TEXT,
    url          TEXT NOT NULL,
    posted_at    TEXT NOT NULL,
    regions      TEXT,
    salary_signal TEXT,
    score        INTEGER,
    verdict      TEXT,
    why          TEXT,
    cv_variant   TEXT,
    description  TEXT,
    run_id       INTEGER REFERENCES runs(id),
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_posted ON jobs(posted_at DESC);
CREATE INDEX IF NOT EXISTS jobs_score  ON jobs(score DESC);

-- What each posting asked the applicant to PRODUCE, one row per requirement.
--
-- A table rather than a JSON column on `jobs`, because the whole reason this
-- is stored is `GROUP BY kind` across months of listings -- "what am I asked
-- for most often". That query is trivial here and awkward against JSON.
--
-- `blocking` is denormalised from profile.yaml at write time for convenience,
-- but it is NOT the source of truth: report.py re-derives it from the live
-- profile, so editing cannot_provide re-flags history without a rewrite.
CREATE TABLE IF NOT EXISTS job_requirements (
    uid          TEXT NOT NULL,
    kind         TEXT NOT NULL,
    mandatory    INTEGER NOT NULL DEFAULT 0,
    detail       TEXT,
    blocking     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (uid, kind)
);
CREATE INDEX IF NOT EXISTS jobreq_kind ON job_requirements(kind);

-- Local only. Never pushed anywhere: a reject row carrying
-- "blocklisted employer" would make the blocklist inferable.
CREATE TABLE IF NOT EXISTS rejects (
    uid          TEXT NOT NULL,
    stage        TEXT NOT NULL,
    reason       TEXT,
    title        TEXT,
    posted_at    TEXT,
    run_id       INTEGER REFERENCES runs(id),
    PRIMARY KEY (uid, run_id)
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: str | None = None):
        self.path = path or config.DB_PATH
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- watermark ---------------------------------------------------------

    def since(self) -> datetime:
        """How far back to look: the last successful run, clamped.

        A fixed lookback window is the bug that made v1 useless -- a 1-hour
        window on a daily schedule sees 1/24th of the board. Measuring from
        the last success means a missed day is caught up automatically.
        """
        row = self.conn.execute(
            "SELECT started_at FROM runs WHERE ok = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        now = datetime.now(UTC)
        if not row:
            return now - timedelta(hours=config.FIRST_RUN_LOOKBACK_HOURS)
        last = datetime.fromisoformat(row["started_at"])
        floor = now - timedelta(hours=config.MAX_LOOKBACK_HOURS)
        ceiling = now - timedelta(hours=config.MIN_LOOKBACK_HOURS)
        return max(floor, min(last, ceiling))

    # -- runs --------------------------------------------------------------

    def start_run(self) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at, ok) VALUES (?, 0)", (_now(),)
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id, ok, fetched=0, scored=0, funnel="", model="", error=None):
        self.conn.execute(
            "UPDATE runs SET finished_at=?, ok=?, fetched=?, scored=?, funnel=?, model=?, error=?"
            " WHERE id=?",
            (_now(), 1 if ok else 0, fetched, scored, funnel, model, error, run_id),
        )
        self.conn.commit()

    def last_run(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()

    # -- seen --------------------------------------------------------------

    def seen(self, uid: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM jobs WHERE uid=?", (uid,)).fetchone()
        if row:
            return True
        return bool(self.conn.execute("SELECT 1 FROM rejects WHERE uid=?", (uid,)).fetchone())

    # -- writes ------------------------------------------------------------

    def _migrate(self) -> None:
        """Add columns to a `jobs` table that predates them.

        `CREATE TABLE IF NOT EXISTS` is a no-op on an existing table, so new
        columns never appear without this. Idempotent and additive: it reads
        the live column list rather than tracking a version number, and it
        never drops or rewrites, so running an older build against a migrated
        database still works.
        """
        have = {row[1] for row in self.conn.execute("PRAGMA table_info(jobs)")}
        for column, ddl in (
            # What the model said, before any blocker penalty. Kept because the
            # penalty is applied locally and must stay re-derivable.
            ("score_raw", "ALTER TABLE jobs ADD COLUMN score_raw INTEGER"),
            # JSON array of requirement kinds this candidate cannot meet.
            ("blockers", "ALTER TABLE jobs ADD COLUMN blockers TEXT"),
            # The full advert as fetched. Kept so cover.py can draft against
            # the real posting without re-fetching a page that may already be
            # gone, and so a redraft costs nothing but the one call. Local
            # only: push.FIELDS is an allowlist and does not name it, so an
            # employer's advert text is never copied to the D1 dashboard.
            ("description", "ALTER TABLE jobs ADD COLUMN description TEXT"),
        ):
            if column not in have:
                self.conn.execute(ddl)
        self.conn.commit()

    def record_job(self, job, run_id, salary_signal, score=None, verdict=None,
                   why=None, cv_variant=None, score_raw=None, blockers=None,
                   requirements=None) -> None:
        """Store every scored survivor, above threshold or not.

        The display threshold must stay a rendering concern: absolute model
        scores drift with prompt and model version, so filtering at write time
        would make history incomparable and silently rewrite what was kept.
        """
        self.conn.execute(
            "INSERT OR REPLACE INTO jobs (uid, source, title, company, url, posted_at,"
            " regions, salary_signal, score, verdict, why, cv_variant, run_id, created_at,"
            " score_raw, blockers, description)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job.key, job.source, job.title, job.company, job.url,
                job.posted.isoformat(), json.dumps(list(job.location_restrictions)),
                salary_signal, score, verdict, why, cv_variant, run_id, _now(),
                score_raw, json.dumps(sorted(blockers or [])), job.description,
            ),
        )
        # Replace rather than accumulate: a re-scored job must not keep the
        # requirements of its previous pass alongside the new ones.
        self.conn.execute("DELETE FROM job_requirements WHERE uid = ?", (job.key,))
        for row in requirements or []:
            self.conn.execute(
                "INSERT OR REPLACE INTO job_requirements (uid, kind, mandatory, detail, blocking)"
                " VALUES (?,?,?,?,?)",
                (
                    job.key, row["kind"], int(bool(row.get("mandatory"))),
                    row.get("detail") or "", int(row["kind"] in (blockers or [])),
                ),
            )

    def record_reject(self, verdict, run_id: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO rejects (uid, stage, reason, title, posted_at, run_id)"
            " VALUES (?,?,?,?,?,?)",
            (
                verdict.job.key, verdict.rejected_by, verdict.reason,
                verdict.job.title, verdict.job.posted.isoformat(), run_id,
            ),
        )

    def commit(self) -> None:
        self.conn.commit()

    # -- reads -------------------------------------------------------------

    def recent(self, limit: int = 50, min_score: int = 0):
        return self.conn.execute(
            "SELECT * FROM jobs WHERE COALESCE(score, 0) >= ?"
            " ORDER BY score DESC, posted_at DESC LIMIT ?",
            (min_score, limit),
        ).fetchall()

    def get_job(self, uid: str):
        """One stored job by uid, or None.

        Accepts a unique prefix as well as the full uid: the uids in a digest
        are long, and the realistic caller is a human retyping one from a
        rendered list. Ambiguity raises rather than guessing, because drafting
        a letter against the wrong advert is worse than being asked again.
        """
        row = self.conn.execute("SELECT * FROM jobs WHERE uid = ?", (uid,)).fetchone()
        if row is not None:
            return row
        # `%` and `_` are LIKE wildcards. Concatenating a raw prefix would let
        # `himalayas:%` match any single job and draft against an advert the
        # caller never asked for, which is the exact outcome this method exists
        # to prevent.
        escaped = uid.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE uid LIKE ? ESCAPE '!' LIMIT 2", (escaped + "%",)
        ).fetchall()
        if len(rows) > 1:
            raise LookupError(f"{uid!r} matches more than one job; use the full uid")
        return rows[0] if rows else None

    def requirements_for(self, uid: str):
        """What this posting asked the applicant to produce."""
        return self.conn.execute(
            "SELECT kind, mandatory, detail FROM job_requirements"
            " WHERE uid = ? ORDER BY mandatory DESC, kind",
            (uid,),
        ).fetchall()

    def reject_stats(self) -> dict:
        rows = self.conn.execute(
            "SELECT stage, COUNT(*) n FROM rejects GROUP BY stage ORDER BY n DESC"
        ).fetchall()
        return {r["stage"]: r["n"] for r in rows}
