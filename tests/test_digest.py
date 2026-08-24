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
    assert "No matches above" in text
    assert "wrong stack" in text


def test_local_anchor_is_flagged_in_the_entry(profile, digest_dir):
    verdict = filters.evaluate(a_job(location_restrictions=("Nigeria",)), profile, NOW)
    result = {"i": 0, "score": 80, "verdict": "strong", "why": "good fit",
              "matched_skills": ["Go"], "concerns": [], "cv_variant": "engineering"}

    text = digest.render(filters.Funnel(), [(verdict, result)], profile, "m", NOW)
    assert "Restricted to your own country" in text
