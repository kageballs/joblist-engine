"""Digest rendering and, mainly, the fact that it does not eat itself."""

from datetime import UTC, datetime

import pytest

import config
import digest
import filters
import targeting
from sources.base import Job

NOW = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)


@pytest.fixture
def profile():
    return targeting.load("profile.example.yaml")


@pytest.fixture
def digest_dir(tmp_path, monkeypatch):
    path = tmp_path / "digest"
    monkeypatch.setattr(config, "DIGEST_DIR", str(path))
    return path


def a_job(**kw):
    base = dict(
        source="himalayas", source_id="1", title="Senior Backend Engineer",
        company="Acme", url="https://example.com/1", posted=NOW,
    )
    base.update(kw)
    return Job(**base)


def test_second_run_appends_rather_than_replacing(digest_dir):
    """A later run that finds nothing must not delete the morning's matches.

    Runs are per-day files and the second run of a day usually finds nothing
    new, so overwriting is silent data loss — the one outcome worse than
    finding nothing at all.
    """
    first = digest.write("# Job digest — morning\n\nfound something", NOW)
    second = digest.write("# Job digest — evening\n\nnothing new", NOW)

    assert first == second
    body = open(first, encoding="utf-8").read()
    assert "found something" in body
    assert "nothing new" in body
    assert body.count("# Job digest") == 2


def test_funnel_line_is_always_rendered(profile, digest_dir):
    """Zero results is the expected case; it must be explicable, not blank."""
    funnel = filters.Funnel()
    funnel.fetched = 900
    funnel.add(filters.evaluate(a_job(location_restrictions=("Japan",)), profile, NOW))

    text = digest.render(funnel, [], profile, "none", NOW, no_llm=True)
    assert "900 fetched" in text
    assert "Nothing survived the filters" in text
    assert "Near misses" in text


def test_below_threshold_jobs_are_still_shown_with_reasons(profile, digest_dir):
    """Storing everything but rendering a threshold means near-misses stay visible."""
    verdict = filters.evaluate(a_job(), profile, NOW)
    result = {"i": 0, "score": 30, "verdict": "no", "why": "wrong stack",
              "matched_skills": [], "concerns": [], "cv_variant": "engineering"}

    text = digest.render(filters.Funnel(), [(verdict, result)], profile, "m", NOW)
    assert "No matches cleared their board's threshold" in text
    assert "wrong stack" in text


def a_result(**kw):
    base = {"i": 0, "score": 80, "verdict": "strong", "why": "good fit",
            "matched_skills": ["Go"], "concerns": [], "cv_variant": "engineering"}
    base.update(kw)
    return base


def test_own_country_is_noted_but_not_warned_about(profile, digest_dir):
    """Neutral since 2026-08-24: PH-eligible is often the fastest yes."""
    verdict = filters.evaluate(a_job(location_restrictions=("Nigeria",)), profile, NOW)
    text = digest.render(filters.Funnel(), [(verdict, a_result())], profile, "m", NOW)
    assert "Nigeria-eligible" in text or "eligible" in text
    assert "often local-rate pay" not in text


def test_flagged_employer_is_shown_not_hidden(profile, digest_dir):
    """A flag explains the tradeoff; a blocklist would have deleted the job.

    The company here is invented, deliberately. Real names from a flag list
    are the thing profile.yaml exists to keep out of a public repo, and a
    fixture asserting "outsourcing intermediary" against a real employer is
    a published opinion about them. Test files are still the repo.
    """
    flagged = targeting.Profile(**{**profile.__dict__, "employer_flags": ("Havershill",)})
    verdict = filters.evaluate(a_job(company="Havershill Staffing Ltd"), flagged, NOW)

    assert verdict.passed, "a flagged employer must not be rejected"
    assert verdict.flagged_employer == "Havershill"
    text = digest.render(filters.Funnel(), [(verdict, a_result())], flagged, "m", NOW)
    assert "Havershill" in text
    assert "outsourcing intermediary" in text


def test_digest_splits_on_the_inbound_line(profile, digest_dir):
    """Both groups are worth applying to; they differ on pushing the rate."""
    low = filters.evaluate(
        a_job(source_id="low", title="Backend A", salary_max=40000,
              currency="USD", salary_period="annual"), profile, NOW)
    high = filters.evaluate(
        a_job(source_id="high", title="Backend B", salary_max=150000,
              currency="USD", salary_period="annual"), profile, NOW)

    assert low.clears_inbound_floor is False
    assert high.clears_inbound_floor is True

    text = digest.render(
        filters.Funnel(),
        [(low, a_result(i=0)), (high, a_result(i=1))],
        profile, "m", NOW,
    )
    assert "Apply now" in text
    assert "Worth the ask" in text
    assert text.index("Apply now") < text.index("Worth the ask")


def test_unstated_salary_lands_in_worth_the_ask(profile, digest_dir):
    """Nothing stated means nothing to push back on yet — it is still an ask."""
    verdict = filters.evaluate(a_job(salary_max=None), profile, NOW)
    assert verdict.clears_inbound_floor is True
    text = digest.render(filters.Funnel(), [(verdict, a_result())], profile, "m", NOW)
    assert "Worth the ask" in text
    assert "Apply now" not in text
