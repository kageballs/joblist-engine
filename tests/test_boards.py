"""Per-board policy: the isolation rule, enforced.

A score, a rate floor and an age window are only meaningful within the board
they came from. These tests exist to stop a future change quietly reintroducing
a global setting.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import yaml

import filters
import targeting
from sources.base import Job


@pytest.fixture
def profile():
    return targeting.load("profile.example.yaml")


def _write_profile(tmp_path, mutate):
    """Copy the example profile, mutate it, write it out, return the path."""
    raw = yaml.safe_load(open("profile.example.yaml", encoding="utf-8"))
    mutate(raw)
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return str(path)


def _job(source="himalayas", **kw):
    defaults = dict(
        source=source,
        source_id="1",
        title="Senior Backend Engineer",
        company="Acme",
        url="https://example.com/j/1",
        posted=datetime(2026, 9, 1, tzinfo=UTC),
    )
    return Job(**{**defaults, **kw})


# --- the rule itself -----------------------------------------------------


def test_an_undeclared_board_raises_rather_than_inheriting(profile):
    """The whole point. A new board must be declared, never defaulted."""
    with pytest.raises(targeting.ProfileError) as exc:
        profile.board("indeed")
    message = str(exc.value)
    assert "indeed" in message
    assert "boards.indeed" in message, "must say exactly what to add"
    assert "himalayas" in message, "must list what IS configured"


def test_require_boards_fails_before_a_run_starts(profile):
    with pytest.raises(targeting.ProfileError):
        profile.require_boards(["himalayas", "indeed"])


def test_require_boards_passes_when_all_are_declared(profile):
    profile.require_boards(["himalayas", "onlinejobs"])


def test_the_same_salary_is_rejected_on_one_board_and_kept_on_another(profile):
    """The isolation rule, demonstrated end to end.

    20 USD/hour is under the international board's floor and over the domestic
    board's. One global number cannot express that, which is the entire reason
    this structure exists.
    """
    paid = dict(salary_max=20, salary_period="hourly", currency="USD")
    now = datetime(2026, 9, 2, tzinfo=UTC)

    himalayas = filters.stage_salary(_job("himalayas", **paid), profile, now)
    onlinejobs = filters.stage_salary(_job("onlinejobs", **paid), profile, now)

    assert himalayas is not None, "20/hr is below the 30/hr international floor"
    assert onlinejobs is None, "20/hr clears the 15/hr domestic floor"


# --- parsing -------------------------------------------------------------


def test_defaults_fill_in_only_what_a_board_omits(tmp_path):
    path = _write_profile(tmp_path, lambda raw: raw["boards"].update(
        {"defaults": {"display_threshold": 42, "draft_at": 88,
                      "absolute_floor_hourly_usd": 30, "max_age_days": 21},
         "himalayas": {"draft_at": 55}}))
    board = targeting.load(path).board("himalayas")
    assert board.draft_at == 55, "explicit value wins"
    assert board.display_threshold == 42, "omitted key comes from defaults"
    assert board.max_age_days == 21


def test_draft_at_defaults_to_the_display_threshold_not_to_zero(tmp_path):
    """A board that never says when to draft must not draft everything."""
    path = _write_profile(tmp_path, lambda raw: raw["boards"].update(
        {"defaults": {"display_threshold": 65}, "himalayas": {}}))
    assert targeting.load(path).board("himalayas").draft_at == 65


def test_a_board_block_may_be_empty(tmp_path):
    path = _write_profile(tmp_path, lambda raw: raw["boards"].update({"himalayas": None}))
    assert targeting.load(path).board("himalayas").name == "himalayas"


# --- manual steps --------------------------------------------------------


def test_a_typo_in_needs_manual_step_fails_loudly(tmp_path):
    path = _write_profile(tmp_path, lambda raw: raw["deliverables"].update(
        {"needs_manual_step": ["video_intro", "vidoe_intro"]}))
    with pytest.raises(targeting.ProfileError, match="vidoe_intro"):
        targeting.load(path)


def test_a_key_cannot_be_both_impossible_and_merely_manual(tmp_path):
    """They mean opposite things: one docks the score, the other never does."""
    path = _write_profile(tmp_path, lambda raw: raw["deliverables"].update(
        {"cannot_provide": ["work_samples"], "needs_manual_step": ["work_samples"]}))
    with pytest.raises(targeting.ProfileError, match="work_samples"):
        targeting.load(path)


def test_needs_manual_step_loads(profile):
    assert "video_intro" in profile.needs_manual_step
    assert "video_intro" not in profile.cannot_provide


# --- staleness -----------------------------------------------------------


def test_is_stale_uses_the_boards_own_window(profile):
    now = datetime(2026, 9, 2, tzinfo=UTC)
    ten_days = now - timedelta(days=10)
    assert profile.board("himalayas").is_stale(ten_days, now) is False  # 21-day window
    assert profile.board("onlinejobs").is_stale(ten_days, now) is True  # 7-day window


def test_a_board_with_no_age_window_never_goes_stale(tmp_path):
    path = _write_profile(tmp_path, lambda raw: raw["boards"].update(
        {"defaults": {"max_age_days": None}, "himalayas": {"max_age_days": None}}))
    board = targeting.load(path).board("himalayas")
    now = datetime(2026, 9, 2, tzinfo=UTC)
    assert board.max_age_days is None
    assert board.is_stale(now - timedelta(days=3650), now) is False


def test_is_stale_tolerates_a_missing_posted_date(profile):
    assert profile.board("onlinejobs").is_stale(None, datetime(2026, 9, 2, tzinfo=UTC)) is False


# --- capability flags ----------------------------------------------------


class _Board:
    def __init__(self, authoritative):
        self.name = "himalayas"
        self.regions_authoritative = authoritative


def test_silence_is_evidence_only_on_a_board_that_publishes_regions(profile):
    """An empty restriction list means 'worldwide' only where the field is real.

    Elsewhere the restriction is routinely in the prose instead, so the listing
    still passes the funnel but is marked for the scorer to read properly.
    """
    now = datetime(2026, 9, 2, tzinfo=UTC)
    trusted = filters.evaluate(_job(), profile, now, source=_Board(True))
    untrusted = filters.evaluate(_job(), profile, now, source=_Board(False))

    assert trusted.passed and untrusted.passed, "neither may be rejected on absent data"
    assert trusted.region_unverified is False
    assert untrusted.region_unverified is True


def test_a_stated_restriction_is_never_marked_unverified(profile):
    job = _job(location_restrictions=("Nigeria",))
    verdict = filters.evaluate(job, profile, datetime(2026, 9, 2, tzinfo=UTC),
                               source=_Board(False))
    assert verdict.region_unverified is False, "the field is populated; nothing to doubt"


def test_evaluate_without_a_source_assumes_the_protocol_default(profile):
    verdict = filters.evaluate(_job(), profile, datetime(2026, 9, 2, tzinfo=UTC))
    assert verdict.region_unverified is False


# --- digest: sectioned by board, trimmed by each board's own window -------


def _pair(profile, source, score, days_old=0, now=None):
    now = now or datetime(2026, 9, 2, tzinfo=UTC)
    job = _job(source, posted=now - timedelta(days=days_old))
    verdict = filters.evaluate(job, profile, now)
    result = {"i": 0, "score": score, "verdict": "maybe", "why": "reason",
              "matched_skills": [], "concerns": [], "cv_variant": "engineering"}
    return verdict, result


def test_the_digest_is_sectioned_by_board_never_pooled(profile):
    """Scores are not comparable across boards, so one ranked list ranks nothing."""
    import digest

    now = datetime(2026, 9, 2, tzinfo=UTC)
    pairs = [_pair(profile, "himalayas", 70), _pair(profile, "onlinejobs", 65)]
    text = digest.render(filters.Funnel(), pairs, profile, "m", now)

    assert "## himalayas" in text
    assert "## onlinejobs" in text


def test_a_listing_stale_for_its_own_board_is_hidden_and_counted(profile):
    """10 days is fine on a 21-day board and stale on a 7-day one."""
    import digest

    now = datetime(2026, 9, 2, tzinfo=UTC)
    pairs = [_pair(profile, "himalayas", 70, days_old=10),
             _pair(profile, "onlinejobs", 70, days_old=10)]
    text = digest.render(filters.Funnel(), pairs, profile, "m", now)

    assert "## himalayas" in text, "21-day window keeps it"
    assert "## onlinejobs" not in text, "7-day window trims it"
    assert "1 listing(s) hidden as stale" in text


def test_each_board_applies_its_own_display_threshold(tmp_path):
    import digest

    path = _write_profile(tmp_path, lambda raw: raw["boards"].update(
        {"himalayas": {"display_threshold": 50},
         "onlinejobs": {"display_threshold": 90}}))
    profile = targeting.load(path)
    now = datetime(2026, 9, 2, tzinfo=UTC)
    pairs = [_pair(profile, "himalayas", 60), _pair(profile, "onlinejobs", 60)]
    text = digest.render(filters.Funnel(), pairs, profile, "m", now)

    assert "## himalayas" in text, "60 clears its board's 50"
    assert "## onlinejobs" not in text, "the same 60 does not clear its board's 90"
