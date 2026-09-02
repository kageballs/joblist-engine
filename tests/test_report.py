"""report.py: per-board sections, never a pooled figure.

Same rule as everywhere else in this rework. A percentage or a ranked score
list blended across boards hides exactly the split that makes it worth
reading -- on this repo's own store OnlineJobs demands work samples in 21% of
its listings against Himalayas' 1%; a single blended number reports neither.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

import config
import report
import store as store_mod
from sources.base import Job

PROFILE = "profile.example.yaml"


def _seed(db_path, entries):
    """Write jobs (and their requirements) straight through Store.record_job,
    the same path main.py uses, so the schema stays honest."""
    st = store_mod.Store(path=str(db_path))
    run_id = st.start_run()
    for e in entries:
        job = Job(
            source=e["source"], source_id=e["source_id"], title=e["title"],
            company=e.get("company", "Acme"), url=e.get("url", "https://example.com/1"),
            posted=e.get("posted", datetime(2026, 9, 1, tzinfo=UTC)),
        )
        st.record_job(
            job, run_id, salary_signal="unknown",
            score=e.get("score", 50), verdict=e.get("verdict", "maybe"), why="",
            cv_variant="engineering",
            score_raw=e.get("score_raw", e.get("score", 50)),
            blockers=e.get("blockers"), requirements=e.get("requirements"),
        )
    st.commit()
    st.close()


def _run(monkeypatch, capsys, db_path, argv):
    monkeypatch.setattr(config, "DB_PATH", str(db_path))
    monkeypatch.setattr(sys, "argv", ["report.py", "--profile", PROFILE, *argv])
    rc = report.main()
    return rc, capsys.readouterr().out


def test_tally_percentage_is_computed_per_board_not_pooled(tmp_path, monkeypatch, capsys):
    """2/2 on one board and 1/10 on another must not blend into one 3/12 figure."""
    db = tmp_path / "db.sqlite3"
    ask = [{"kind": "public_code", "mandatory": True, "detail": "link"}]
    entries = [
        dict(source="himalayas", source_id="h1", title="A", requirements=ask),
        dict(source="himalayas", source_id="h2", title="B", requirements=ask),
    ] + [
        dict(source="onlinejobs", source_id=f"o{i}", title=f"J{i}",
             requirements=ask if i == 0 else [])
        for i in range(10)
    ]
    _seed(db, entries)

    rc, out = _run(monkeypatch, capsys, db, [])
    assert rc == 0
    assert "## himalayas" in out and "## onlinejobs" in out
    himalayas_section = out.split("## himalayas", 1)[1].split("## onlinejobs", 1)[0]
    onlinejobs_section = out.split("## onlinejobs", 1)[1]
    assert "100%" in himalayas_section, "2 of 2 himalayas listings asked for it"
    assert "100%" not in onlinejobs_section
    assert " 10%" in onlinejobs_section, "1 of 10 onlinejobs listings asked for it"


def test_source_filter_still_shows_a_single_board(tmp_path, monkeypatch, capsys):
    db = tmp_path / "db.sqlite3"
    ask = [{"kind": "public_code", "mandatory": True, "detail": "link"}]
    _seed(db, [
        dict(source="himalayas", source_id="h1", title="A", requirements=ask),
        dict(source="onlinejobs", source_id="o1", title="B", requirements=ask),
    ])

    rc, out = _run(monkeypatch, capsys, db, ["--source", "himalayas"])
    assert rc == 0
    assert "## himalayas" in out
    assert "## onlinejobs" not in out


def test_blocked_is_sectioned_by_board_and_ranked_within_it(tmp_path, monkeypatch, capsys):
    """score_raw only ranks against another score_raw from the SAME board."""
    db = tmp_path / "db.sqlite3"
    # work_samples is in profile.example.yaml's cannot_provide.
    ask = [{"kind": "work_samples", "mandatory": True, "detail": "portfolio"}]
    _seed(db, [
        dict(source="himalayas", source_id="h1", title="Low", requirements=ask, score_raw=40),
        dict(source="himalayas", source_id="h2", title="High", requirements=ask, score_raw=90),
        dict(source="onlinejobs", source_id="o1", title="Solo", requirements=ask, score_raw=10),
    ])

    rc, out = _run(monkeypatch, capsys, db, ["--blocked"])
    assert rc == 0
    assert "## himalayas" in out and "## onlinejobs" in out
    himalayas_section = out.split("## himalayas", 1)[1].split("## onlinejobs", 1)[0]
    assert himalayas_section.index("High") < himalayas_section.index("Low"), \
        "ranked within the board, highest score_raw first"


def test_manual_steps_are_surfaced_by_reapply_without_touching_score(tmp_path, monkeypatch, capsys):
    """video_intro is in needs_manual_step -- unpriced, but must be visible."""
    db = tmp_path / "db.sqlite3"
    ask = [{"kind": "video_intro", "mandatory": True, "detail": "record 2 min"}]
    _seed(db, [
        dict(source="himalayas", source_id="h1", title="Needs A Video",
             requirements=ask, score=70, score_raw=70),
    ])

    rc, out = _run(monkeypatch, capsys, db, ["--reapply"])
    assert rc == 0
    assert "1 stored job(s) need a manual step" in out
    assert "Needs A Video" in out
    assert "video_intro" in out

    conn = store_mod.sqlite3.connect(db)
    conn.row_factory = store_mod.sqlite3.Row
    row = conn.execute("SELECT score, score_raw FROM jobs WHERE uid LIKE 'himalayas%'").fetchone()
    conn.close()
    assert row["score"] == 70 and row["score_raw"] == 70, "a manual step must never dock the score"


def test_reapply_manual_steps_respects_source_filter_and_is_sectioned(tmp_path, monkeypatch, capsys):
    """--reapply --source must not list the other board's manual-step rows,
    and the count printed must match the filtered board, not the pool."""
    db = tmp_path / "db.sqlite3"
    ask = [{"kind": "video_intro", "mandatory": True, "detail": "record 2 min"}]
    _seed(db, [
        dict(source="himalayas", source_id="h1", title="Himalayas Video",
             requirements=ask, score=70, score_raw=70),
        dict(source="onlinejobs", source_id="o1", title="OnlineJobs Video",
             requirements=ask, score=70, score_raw=70),
    ])

    rc, out = _run(monkeypatch, capsys, db, ["--reapply", "--source", "himalayas"])
    assert rc == 0
    assert "## himalayas — 1 stored job(s)" in out
    assert "Himalayas Video" in out
    assert "OnlineJobs Video" not in out
    assert "## onlinejobs" not in out


def test_since_survives_the_header_alongside_source(tmp_path, monkeypatch, capsys):
    """--source and --since together must both reach the default-view header;
    the WHERE clause already applies both, so a header missing one lies about
    what the body below it counts."""
    db = tmp_path / "db.sqlite3"
    ask = [{"kind": "public_code", "mandatory": True, "detail": "link"}]
    _seed(db, [dict(source="himalayas", source_id="h1", title="A", requirements=ask)])

    rc, out = _run(monkeypatch, capsys, db, ["--source", "himalayas", "--since", "7d"])
    assert rc == 0
    header = out.splitlines()[1]
    assert "himalayas" in header
    assert "7d" in header


def test_detail_is_sectioned_by_board_and_shows_board_without_company(tmp_path, monkeypatch, capsys):
    """company is NULL on onlinejobs rows in real data -- the board must still
    print, and each board's rows must not spill into the other's section."""
    db = tmp_path / "db.sqlite3"
    ask = [{"kind": "work_samples", "mandatory": True, "detail": "portfolio link"}]
    _seed(db, [
        dict(source="himalayas", source_id="h1", title="Has Company",
             company="Acme", requirements=ask),
        dict(source="onlinejobs", source_id="o1", title="No Company",
             company=None, requirements=ask),
    ])

    rc, out = _run(monkeypatch, capsys, db, ["--detail", "work_samples"])
    assert rc == 0
    assert "## himalayas — 1 listing(s)" in out
    assert "## onlinejobs — 1 listing(s)" in out
    onlinejobs_section = out.split("## onlinejobs", 1)[1]
    assert "onlinejobs" in onlinejobs_section, "board must print even when company is unknown"
    himalayas_section = out.split("## himalayas", 1)[1].split("## onlinejobs", 1)[0]
    assert "No Company" not in himalayas_section


def test_reapply_still_reports_the_global_blocker_penalty(tmp_path, monkeypatch, capsys):
    """The penalty arithmetic itself stays global -- only its listing is sectioned."""
    db = tmp_path / "db.sqlite3"
    ask = [{"kind": "work_samples", "mandatory": True, "detail": "portfolio"}]
    _seed(db, [
        dict(source="himalayas", source_id="h1", title="A", requirements=ask,
             score=70, score_raw=70),
    ])

    rc, out = _run(monkeypatch, capsys, db, ["--reapply"])
    assert rc == 0
    assert "-30/blocker" in out

    conn = store_mod.sqlite3.connect(db)
    conn.row_factory = store_mod.sqlite3.Row
    row = conn.execute("SELECT score FROM jobs WHERE uid LIKE 'himalayas%'").fetchone()
    conn.close()
    assert row["score"] == 40, "70 - blocker_penalty(30), same everywhere -- not a board figure"
