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


# --- manual steps cost time, never points --------------------------------


def _scored(score=70, requirements=()):
    return {"i": 0, "score": score, "verdict": "maybe", "why": "reason",
            "matched_skills": [], "concerns": [], "cv_variant": "engineering",
            "requirements": list(requirements)}


def test_a_manual_step_never_changes_the_score(profile):
    """The whole point of the third category. Costs an evening, not points."""
    import blockers

    out = blockers.evaluate(
        _scored(70, [{"kind": "video_intro", "mandatory": True, "detail": "record 2 min"}]),
        profile,
    )
    assert out["manual_steps"] == ["video_intro"]
    assert out["score"] == out["score_raw"] == 70, "a winnable job must not be buried"
    assert out["blockers"] == []


def test_a_manual_step_and_a_real_blocker_coexist(profile):
    """Only the impossible one is priced."""
    import blockers

    out = blockers.evaluate(
        _scored(70, [
            {"kind": "video_intro", "mandatory": True, "detail": "record 2 min"},
            {"kind": "work_samples", "mandatory": True, "detail": "send 5"},
        ]),
        profile,
    )
    assert out["manual_steps"] == ["video_intro"]
    assert out["blockers"] == ["work_samples"]
    assert out["score"] == 70 - profile.blocker_penalty, "only the blocker is priced"


def test_a_non_mandatory_manual_step_is_still_worth_flagging(profile):
    """Unlike a blocker, it is flagged whether or not it is a hard condition."""
    import blockers

    out = blockers.evaluate(
        _scored(70, [{"kind": "test_task", "mandatory": False, "detail": "optional"}]),
        profile,
    )
    assert out["manual_steps"] == ["test_task"]
    assert out["score"] == 70


def test_the_digest_puts_the_checklist_where_it_will_be_read(profile):
    import digest

    now = datetime(2026, 9, 2, tzinfo=UTC)
    verdict, result = _pair(profile, "himalayas", 70)
    result["manual_steps"] = ["video_intro"]
    text = digest.render(filters.Funnel(), [(verdict, result)], profile, "m", now)
    assert "BEFORE APPLYING" in text
    assert "video" in text.lower()


def test_the_draft_file_carries_the_checklist(profile):
    """The draft has to stand alone: the letter is useless if the video is not made."""
    import cover

    row = {"title": "T", "company": "C", "url": "u", "source": "himalayas", "score": 70}
    out = cover.render(row, "Letter body.", [], ["video_intro"])
    assert "Before applying" in out
    assert "- [ ]" in out, "a checklist, not prose"
    assert "did not cost the job any points" in out


def test_manual_steps_are_re_derived_from_the_live_profile(profile):
    """Never stored, so editing the list re-flags history with no API calls."""
    import cover

    requirements = [{"kind": "video_intro"}, {"kind": "degree"}]
    assert cover.live_manual_steps(requirements, profile) == ["video_intro"]


# --- auto-drafting: per-board selection, globally capped -----------------


class _FakeClient:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on
        outer = self

        class _M:
            def create(self, **kw):
                outer.calls.append(kw)
                if outer.fail_on and len(outer.calls) == outer.fail_on:
                    raise RuntimeError("rate limited")
                block = type("B", (), {"type": "text", "text": "Letter body."})()
                return type("R", (), {"content": [block]})()

        self.messages = _M()


def _wire_cover(monkeypatch, tmp_path, client):
    import cover
    monkeypatch.setattr(cover.config, "COVER_DIR", str(tmp_path / "covers"))
    monkeypatch.setitem(
        __import__("sys").modules, "anthropic",
        __import__("types").SimpleNamespace(Anthropic=lambda **kw: client, APIError=Exception),
    )


def test_selection_uses_each_boards_own_draft_threshold(profile, tmp_path, monkeypatch):
    """70 qualifies on a board whose bar is 70, not on one whose bar is 90."""
    import cover

    path = _write_profile(tmp_path, lambda raw: raw["boards"].update(
        {"himalayas": {"draft_at": 70}, "onlinejobs": {"draft_at": 90}}))
    profile = targeting.load(path)
    client = _FakeClient()
    _wire_cover(monkeypatch, tmp_path, client)

    pairs = [_pair(profile, "himalayas", 70), _pair(profile, "onlinejobs", 70)]
    written = cover.auto_draft(pairs, profile, "sk-test", log=lambda m: None)

    assert written == 1
    assert len(client.calls) == 1, "the 90-bar board must not have been drafted"


def test_the_global_cap_bounds_a_good_day(profile, tmp_path, monkeypatch):
    import cover

    client = _FakeClient()
    _wire_cover(monkeypatch, tmp_path, client)
    monkeypatch.setattr(cover.config, "COVER_MAX_PER_RUN", 2)

    pairs = [_pair(profile, "himalayas", s) for s in (72, 95, 80, 71, 88)]
    written = cover.auto_draft(pairs, profile, "sk-test", log=lambda m: None)

    assert written == 2
    drafted_titles = [c["messages"][0]["content"] for c in client.calls]
    assert len(drafted_titles) == 2, "must stop at the cap, not draft all five"


def test_the_cap_gives_the_slots_to_the_highest_scorers(profile, tmp_path, monkeypatch):
    import cover

    client = _FakeClient()
    _wire_cover(monkeypatch, tmp_path, client)
    monkeypatch.setattr(cover.config, "COVER_MAX_PER_RUN", 1)

    pairs = [_pair(profile, "himalayas", 71), _pair(profile, "himalayas", 99)]
    # Distinguish them by uid so the written file can be identified.
    pairs[1][0].job = pairs[1][0].job.__class__(
        **{**pairs[1][0].job.__dict__, "source_id": "top"})
    cover.auto_draft(pairs, profile, "sk-test", log=lambda m: None)

    written = list((tmp_path / "covers").glob("*.md"))
    assert len(written) == 1
    assert "top" in written[0].name, "the 99 should have taken the only slot"


def test_one_failure_does_not_lose_the_other_drafts(profile, tmp_path, monkeypatch):
    import cover

    client = _FakeClient(fail_on=2)
    _wire_cover(monkeypatch, tmp_path, client)

    pairs = [_pair(profile, "himalayas", 90), _pair(profile, "himalayas", 80)]
    pairs[1][0].job = pairs[1][0].job.__class__(
        **{**pairs[1][0].job.__dict__, "source_id": "second"})
    written = cover.auto_draft(pairs, profile, "sk-test", log=lambda m: None)

    assert written == 1, "the surviving draft must still be written"
    assert len(list((tmp_path / "covers").glob("*.md"))) == 1


def test_an_unscored_job_is_never_drafted(profile, tmp_path, monkeypatch):
    import cover

    client = _FakeClient()
    _wire_cover(monkeypatch, tmp_path, client)
    verdict, result = _pair(profile, "himalayas", 90)
    result["score"] = None

    assert cover.auto_draft([(verdict, result)], profile, "sk-test", log=lambda m: None) == 0
    assert client.calls == []


def test_an_already_drafted_job_is_not_redrafted(profile, tmp_path, monkeypatch):
    import cover

    client = _FakeClient()
    _wire_cover(monkeypatch, tmp_path, client)
    pairs = [_pair(profile, "himalayas", 90)]

    assert cover.auto_draft(pairs, profile, "sk-test", log=lambda m: None) == 1
    assert cover.auto_draft(pairs, profile, "sk-test", log=lambda m: None) == 0
    assert len(client.calls) == 1, "a second run must not pay for the same letter twice"


def test_an_unverified_region_reaches_the_scorer_as_a_warning(profile):
    """The flag has to change the prompt, or computing it is theatre.

    filters.py promises the scorer is told not to read silence as "worldwide".
    That promise went unkept once already -- the field was set and no consumer
    ever read it -- so this pins the wording to the flag rather than trusting
    a comment to stay true.
    """
    import scorer

    now = datetime(2026, 9, 2, tzinfo=UTC)
    trusted = filters.evaluate(_job(), profile, now, source=_Board(True))
    untrusted = filters.evaluate(_job(), profile, now, source=_Board(False))

    assert "none stated (worldwide)" in scorer.render_batch([trusted])
    warned = scorer.render_batch([untrusted])
    assert "do NOT assume worldwide" in warned
    assert "(worldwide)" not in warned, "the reassuring phrasing must not survive"


def test_a_stated_region_renders_the_same_on_either_board(profile):
    """The caveat is about silence. A populated field is a fact on any board."""
    import scorer

    now = datetime(2026, 9, 2, tzinfo=UTC)
    job = _job(location_restrictions=("Philippines",))
    on_trusted = scorer.render_batch([filters.evaluate(job, profile, now, source=_Board(True))])
    on_untrusted = scorer.render_batch([filters.evaluate(job, profile, now, source=_Board(False))])

    assert "hiring regions: Philippines" in on_trusted
    assert on_trusted == on_untrusted


def test_the_cap_is_shared_between_boards_not_won_by_the_generous_one(
    profile, tmp_path, monkeypatch
):
    """The cap must not be allocated by a cross-board score ranking.

    This is the isolation rule at its sharpest. On real history himalayas
    averages 20.3 and onlinejobs 5.9, so sorting every candidate into one list
    hands each slot to himalayas and the onlinejobs job that cleared its OWN
    board's bar -- the harder achievement of the two -- never gets written.
    """
    import cover

    path = _write_profile(tmp_path, lambda raw: raw["boards"].update(
        {"himalayas": {"draft_at": 70}, "onlinejobs": {"draft_at": 70}}))
    profile = targeting.load(path)
    client = _FakeClient()
    _wire_cover(monkeypatch, tmp_path, client)
    monkeypatch.setattr(cover.config, "COVER_MAX_PER_RUN", 2)

    # Both himalayas jobs outscore the onlinejobs one, which is exactly the
    # situation a global sort gets wrong.
    pairs = [_pair(profile, "himalayas", 95), _pair(profile, "himalayas", 88),
             _pair(profile, "onlinejobs", 71)]
    for i, pair in enumerate(pairs):
        pair[0].job = pair[0].job.__class__(
            **{**pair[0].job.__dict__, "source_id": f"job{i}"})

    written = cover.auto_draft(pairs, profile, "sk-test", log=lambda m: None)
    names = " ".join(p.name for p in (tmp_path / "covers").glob("*.md"))

    assert written == 2
    assert "job2" in names, "the onlinejobs job cleared its own bar and must get a slot"
    assert "job0" in names, "himalayas' best still takes the first slot"
    assert "job1" not in names, "himalayas' second must yield to the other board"


def test_within_one_board_the_best_still_goes_first(profile, tmp_path, monkeypatch):
    """Sharing between boards must not turn into ignoring rank inside one."""
    import cover

    client = _FakeClient()
    _wire_cover(monkeypatch, tmp_path, client)
    monkeypatch.setattr(cover.config, "COVER_MAX_PER_RUN", 1)

    pairs = [_pair(profile, "onlinejobs", 72), _pair(profile, "onlinejobs", 97)]
    for i, pair in enumerate(pairs):
        pair[0].job = pair[0].job.__class__(
            **{**pair[0].job.__dict__, "source_id": f"ol{i}"})

    cover.auto_draft(pairs, profile, "sk-test", log=lambda m: None)
    names = " ".join(p.name for p in (tmp_path / "covers").glob("*.md"))
    assert "ol1" in names and "ol0" not in names
