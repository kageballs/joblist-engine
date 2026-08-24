"""Parsing tests against a committed fixture, plus a live contract test.

The split matters: fixtures make assertions deterministic and offline, while
the live test asserts only the response *shape* the parser depends on. Nothing
here asserts on counts or specific jobs, because those change hourly and a
test that fails for that reason gets ignored, then deleted.

Himalayas deprecated `offset` on 2026-08-21 with no notice, so treat drift as
expected. The live test is how you find out before the 7am run does.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from sources.base import strip_html
from sources.himalayas import Himalayas, to_job

FIXTURE = "fixtures/himalayas.json"


@pytest.fixture
def items():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)["jobs"]


def test_every_fixture_item_parses(items):
    parsed = [to_job(i) for i in items]
    assert parsed and all(job is not None for job in parsed)


def test_required_fields_present(items):
    job = to_job(items[0])
    assert job.title and job.url and job.source == "himalayas"
    assert job.posted.tzinfo is not None, "posted must be timezone-aware"
    assert job.key.startswith("himalayas:")


def test_timezone_restrictions_are_ints(items):
    for item in items:
        job = to_job(item)
        assert all(isinstance(o, int) for o in job.timezone_restrictions)


def test_location_restrictions_are_stripped_strings(items):
    for item in items:
        job = to_job(item)
        assert all(isinstance(r, str) and r == r.strip() for r in job.location_restrictions)


def test_missing_required_field_yields_none():
    """Field drift must degrade to a skipped row, never a KeyError mid-run."""
    assert to_job({}) is None
    assert to_job({"title": "x"}) is None
    assert to_job({"guid": "g", "title": ""}) is None


def test_unparseable_dates_do_not_raise():
    assert to_job({"guid": "g", "title": "t", "pubDate": "not-a-date"}) is None


def test_zero_salary_becomes_none():
    """0 in the feed means "not stated" — it must not survive as a real figure."""
    job = to_job({"guid": "g", "title": "t", "pubDate": 1787558368,
                  "minSalary": 0, "maxSalary": 0})
    assert job.salary_max is None
    assert job.annual_usd_max() is None


def test_strip_html_handles_entities_and_breaks():
    out = strip_html("<p>Ben &amp; Jerry&#39;s</p><li>remote</li>")
    assert "&amp;" not in out and "&#39;" not in out
    assert "Ben & Jerry's" in out


# -- live contract --------------------------------------------------------

@pytest.mark.live
def test_live_response_shape():
    """Asserts the contract the parser relies on, not the content."""
    source = Himalayas()
    payload = source._get({"limit": 5})
    assert "jobs" in payload, "feed no longer returns a `jobs` array"
    assert "nextCursor" in payload, "cursor pagination gone; offset is deprecated"
    assert payload["jobs"], "feed returned no jobs at all"

    item = payload["jobs"][0]
    for key in ("guid", "title", "pubDate", "locationRestrictions"):
        assert key in item, f"field `{key}` disappeared from the feed"
    assert to_job(item) is not None


@pytest.mark.live
def test_live_fetch_respects_watermark():
    source = Himalayas()
    since = datetime.now(UTC) - timedelta(hours=3)
    jobs = list(source.fetch(since))
    assert jobs, "no jobs in the last 3 hours — unexpected for this feed"
    assert all(job.posted >= since for job in jobs)
