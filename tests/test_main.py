"""main.py's closing summary line: per board, never pooled.

Same rule as everywhere else in this rework — an "above threshold" count
pooled across boards would blend a domestic board's zero threshold into an
international board's non-zero one, which ranks nothing. See CLAUDE.md.
"""

from __future__ import annotations

from datetime import UTC, datetime

import main
import targeting
from sources.base import Job


def _pair(source, score):
    job = Job(source=source, source_id=str(score), title="T", company="C",
              url="https://example.com/1", posted=datetime(2026, 9, 1, tzinfo=UTC))
    verdict = type("V", (), {"job": job})()
    return verdict, {"score": score}


def _profile_with(thresholds: dict) -> targeting.Profile:
    base = targeting.load("profile.example.yaml")
    boards = {
        name: targeting.BoardPolicy(
            name=name, display_threshold=threshold, draft_at=threshold,
            absolute_floor_hourly_usd=base.board(name).absolute_floor_hourly_usd,
        )
        for name, threshold in thresholds.items()
    }
    return targeting.Profile(**{**base.__dict__, "boards": boards})


def test_each_board_is_counted_against_its_own_threshold():
    """30/40 clears one board's bar and misses another's, in the same line."""
    profile = _profile_with({"himalayas": 30, "onlinejobs": 11})
    pairs = [_pair("himalayas", 10), _pair("himalayas", 40),
             _pair("onlinejobs", 5), _pair("onlinejobs", 20)]

    line = main._run_summary(pairs, profile, blocked=0)

    assert "himalayas 1/2 above 30" in line
    assert "onlinejobs 1/2 above 11" in line
    assert "4 scored" in line


def test_a_zero_threshold_still_formats_sensibly():
    """The owner's real profile sets display_threshold to 0 on every board."""
    profile = _profile_with({"himalayas": 0})
    pairs = [_pair("himalayas", 0), _pair("himalayas", 5)]

    line = main._run_summary(pairs, profile, blocked=0)

    assert "himalayas 2/2 above 0" in line


def test_no_scored_pairs_omits_the_empty_breakdown():
    profile = _profile_with({"himalayas": 30})
    line = main._run_summary([], profile, blocked=0)
    assert line == "[run] done — 0 scored"


def test_blocked_note_is_still_appended():
    profile = _profile_with({"himalayas": 30})
    pairs = [_pair("himalayas", 40)]
    line = main._run_summary(pairs, profile, blocked=2)
    assert line.endswith("2 blocked")
