"""The dashboard's age cut must mean the same thing as the digest's.

`targeting.BoardPolicy.is_stale` decides what the digest renders; a SQL
predicate in `dashboard/src/index.js` decides what the dashboard renders. Two
implementations of one rule is exactly how the two surfaces started disagreeing
in the first place, so these tests do not restate the SQL — they **read the
shipped JS**, pull the predicate out of it, and run it against SQLite next to
the Python. A change to either side that moves the boundary fails here.

`docs/quality.md` names "the dashboard has no tests" as a hole. This closes the
part of it that can be closed without a Worker runtime: the logic, not the HTTP.
"""

from __future__ import annotations

import pathlib
import re
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

import targeting

INDEX_JS = pathlib.Path(__file__).resolve().parents[1] / "dashboard" / "src" / "index.js"


def js_const(name: str) -> str:
    """Pull a `const <name> = "a" + "b";` string concatenation out of the Worker.

    Deliberately not an import or a copy. If someone edits the predicate in the
    Worker, this test picks up the edited one and the comparison below is still
    meaningful; a copied constant would keep passing while the surfaces drifted.
    """
    source = INDEX_JS.read_text(encoding="utf-8")
    match = re.search(rf"const {name} =\s*(.+?);\s*\n", source, re.S)
    assert match, f"{name} not found in {INDEX_JS.name} — did the constant get renamed?"
    parts = re.findall(r'"([^"]*)"', match.group(1))
    assert parts, f"{name} did not parse as a string concatenation"
    return "".join(parts)


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE jobs (source TEXT, posted_at TEXT)")
    conn.execute("CREATE TABLE board_policy (source TEXT PRIMARY KEY, max_age_days INTEGER)")
    yield conn
    conn.close()


def dashboard_keeps(db, posted_at, max_age_days) -> bool:
    """Would the dashboard render this row, per the Worker's own SQL?"""
    db.execute("DELETE FROM jobs")
    db.execute("DELETE FROM board_policy")
    db.execute("INSERT INTO jobs VALUES ('b', ?)", (posted_at,))
    if max_age_days is not ...:
        db.execute("INSERT INTO board_policy VALUES ('b', ?)", (max_age_days,))
    sql = "SELECT COUNT(*)" + js_const("WITH_POLICY") + " WHERE " + js_const("FRESH")
    return db.execute(sql).fetchone()[0] == 1


def digest_keeps(posted, max_age_days) -> bool:
    """Would the digest render it? The same question, asked of the Python."""
    policy = targeting.BoardPolicy(name="b", display_threshold=0, draft_at=70,
                                   absolute_floor_hourly_usd=15,
                                   max_age_days=max_age_days)
    return not policy.is_stale(posted, datetime.now(UTC))


# Ages are offset by an hour so no case sits exactly on an integer boundary,
# where Python's clock and SQLite's `now` could disagree by microseconds and
# make this suite flaky for a reason that has nothing to do with the rule.
AGES = [0, 1, 6, 7, 8, 13, 14, 20, 21, 22, 40]
WINDOWS = [7, 14, 21]


@pytest.mark.parametrize("window", WINDOWS)
@pytest.mark.parametrize("age", AGES)
def test_the_two_surfaces_agree(db, age, window):
    """The whole point. One rule, two implementations, same answer."""
    posted = datetime.now(UTC) - timedelta(days=age, hours=1)
    assert dashboard_keeps(db, posted.isoformat(), window) == digest_keeps(posted, window), (
        f"dashboard and digest disagree at age={age}d window={window}d"
    )


@pytest.mark.parametrize("window", WINDOWS)
def test_the_boundary_is_inclusive_on_both_sides(db, window):
    """`(now - posted).days > max_age_days` is stale, so exactly N days is KEPT.

    Off-by-one here is the whole bug: drop the `+ 1` in the SQL and every board
    retires a day early while the digest does not.
    """
    just_inside = datetime.now(UTC) - timedelta(days=window, hours=1)
    just_outside = datetime.now(UTC) - timedelta(days=window + 1, hours=1)

    assert digest_keeps(just_inside, window) is True
    assert dashboard_keeps(db, just_inside.isoformat(), window) is True
    assert digest_keeps(just_outside, window) is False
    assert dashboard_keeps(db, just_outside.isoformat(), window) is False


def test_a_board_with_no_policy_row_is_never_trimmed(db):
    """Silence is not evidence of staleness — a new source shows in full."""
    ancient = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    assert dashboard_keeps(db, ancient, ...) is True


def test_a_null_window_never_trims(db):
    """`max_age_days: null` in profile.yaml means never trim, on both surfaces."""
    ancient = datetime.now(UTC) - timedelta(days=400)
    assert dashboard_keeps(db, ancient.isoformat(), None) is True
    assert digest_keeps(ancient, None) is True


def test_a_row_with_no_posted_at_is_kept(db):
    """is_stale returns False when posted is None; the SQL must not drop it."""
    assert dashboard_keeps(db, None, 7) is True
    assert digest_keeps(None, 7) is True


@pytest.mark.parametrize("stamp", [
    "2026-09-03T17:36:35+00:00",   # what store.py actually writes
    "2026-09-03T17:36:35Z",
    "2026-09-03",
])
def test_the_timestamp_formats_the_store_can_produce_all_parse(db, stamp):
    """julianday() must understand the offset form, or every row reads as stale."""
    assert dashboard_keeps(db, stamp, None) is True
