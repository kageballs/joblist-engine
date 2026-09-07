"""Adding one job you found yourself.

Two rules carry this module and both are about not overruling the operator:

  * the funnel ADVISES and does not veto, because a person has already read the
    posting and decided it was worth adding;
  * an absent field means "the page did not say", never "worldwide" or "free" —
    the same tri-state discipline the crawled sources follow.

The fetch path is exercised against fixtures, never the network.
"""

from __future__ import annotations

import urllib.error
from datetime import UTC, datetime

import pytest

import add
import filters
import targeting

PAGE = """<html><head>
<script type="application/ld+json">
{{"@context":"https://schema.org","@type":"JobPosting",{body}}}
</script></head><body>ignored</body></html>"""


def page(**fields) -> str:
    import json
    body = ",".join(f'"{k}":{json.dumps(v)}' for k, v in fields.items())
    return PAGE.format(body=body)


@pytest.fixture
def profile():
    return targeting.load("profile.example.yaml")


# --- finding the posting -----------------------------------------------------

def test_finds_a_plain_jobposting():
    found = add.find_job_posting(page(title="Backend Engineer"))
    assert found["title"] == "Backend Engineer"


def test_finds_a_posting_inside_an_at_graph():
    html = """<script type="application/ld+json">
    {"@context":"x","@graph":[{"@type":"Organization","name":"Acme"},
                              {"@type":"JobPosting","title":"Buried"}]}
    </script>"""
    assert add.find_job_posting(html)["title"] == "Buried"


def test_finds_a_posting_inside_a_top_level_array():
    html = """<script type="application/ld+json">
    [{"@type":"BreadcrumbList"},{"@type":"JobPosting","title":"Second"}]
    </script>"""
    assert add.find_job_posting(html)["title"] == "Second"


def test_a_malformed_block_does_not_stop_a_later_good_one():
    """Boards ship broken ld+json all the time; one bad block is not the page."""
    html = ('<script type="application/ld+json">{not json,,,</script>'
            + page(title="Survivor"))
    assert add.find_job_posting(html)["title"] == "Survivor"


def test_returns_none_when_there_is_no_posting():
    """None on unusable input, never an exception — the rule every parser here follows."""
    assert add.find_job_posting("<html><body>nothing</body></html>") is None
    assert add.find_job_posting("") is None
    assert add.find_job_posting(None) is None


# --- building the job --------------------------------------------------------

def test_maps_the_fields_that_matter():
    job = add.job_from_posting({
        "title": "Automation Engineer",
        "hiringOrganization": {"@type": "Organization", "name": "Acme"},
        "datePosted": "2026-09-01",
        "validThrough": "2026-10-01T00:00:00Z",
        "description": "<p>Build <b>things</b>.</p>",
    }, "https://example.test/job/1")

    assert job.source == "manual"
    assert job.title == "Automation Engineer"
    assert job.company == "Acme"
    assert job.posted.year == 2026 and job.posted.month == 9
    assert job.expires.month == 10
    # Asserting the tags are gone and the words survive, NOT exact whitespace.
    # sources.base.strip_html replaces every tag with a space, so an inline
    # `<b>things</b>.` comes out as `things .`. That is shared behaviour on
    # every source in the repo, not something this module introduced, and it is
    # harmless to a model reading prose — pinning the exact string here would
    # just make this test a tripwire for an unrelated cleanup.
    assert "<" not in job.description
    assert "Build" in job.description and "things" in job.description
    # uid is the URL, so re-adding the same posting updates rather than duplicates
    assert job.key == "manual:https://example.test/job/1"


def test_no_datePosted_means_you_found_it_today():
    """The only honest answer, and it keeps a fresh find out of the stale bucket."""
    before = datetime.now(UTC)
    job = add.job_from_posting({"title": "X", "description": "y"}, "u")
    assert job.posted >= before


def test_salary_maps_period_and_currency():
    low, high, period, currency = add._salary({
        "baseSalary": {"currency": "usd",
                       "value": {"minValue": 25, "maxValue": 32, "unitText": "HOUR"}}})
    assert (low, high, period, currency) == (25, 32, "hourly", "USD")


def test_salary_accepts_numbers_written_as_strings():
    low, high, _, _ = add._salary({
        "baseSalary": {"value": {"minValue": "90,000", "maxValue": "120000",
                                 "unitText": "YEAR"}}})
    assert (low, high) == (90000, 120000)


@pytest.mark.parametrize("unit", ["DAY", "WEEK", "", "FORTNIGHT"])
def test_an_unmappable_period_stays_none_rather_than_being_guessed(unit):
    """annual_usd_max reads None as unknown, which beats inventing "annual".

    Guessing a period here would let the salary floor reject a real job on a
    number the page never meant that way.
    """
    _, _, period, _ = add._salary({"baseSalary": {"value": {"minValue": 5, "unitText": unit}}})
    assert period is None


def test_no_baseSalary_is_all_none():
    assert add._salary({}) == (None, None, None, None)


def test_regions_are_read_when_stated():
    job = add.job_from_posting({
        "title": "X", "description": "y",
        "applicantLocationRequirements": {"@type": "Country", "name": "United States"},
    }, "u")
    assert job.location_restrictions == ("United States",)


def test_no_stated_region_is_empty_and_that_means_unverified(profile):
    """Empty must not read as "worldwide" — Manual.regions_authoritative is False.

    This is the We Work Remotely lesson: a board that does not publish real
    region data has an empty field that proves nothing, so the funnel marks it
    unverified and the scorer is told the silence is not evidence.
    """
    assert add.Manual.regions_authoritative is False
    assert add.Manual.salary_authoritative is False

    job = add.job_from_posting({"title": "X", "description": "y"}, "u")
    assert job.location_restrictions == ()

    verdict = filters.evaluate(job, profile, datetime.now(UTC), add.Manual())
    assert verdict.region_unverified is True


def test_text_mode_takes_the_title_from_the_first_line():
    job = add.job_from_text("https://example.test/j",
                            "Senior Platform Engineer\nAcme\n\nWe want...")
    assert job.title == "Senior Platform Engineer"
    assert job.description.startswith("Senior Platform Engineer")
    assert job.raw == {"pasted": True}


def test_an_explicit_title_and_company_win():
    job = add.job_from_text("u", "whatever", title="Real Title", company="Real Co")
    assert (job.title, job.company) == ("Real Title", "Real Co")


# --- the funnel advises, it does not veto ------------------------------------

def test_a_rejected_verdict_is_let_through_and_the_reason_is_returned():
    """The rule this module exists for.

    Everywhere else a rejection is final, because the tool is choosing among
    thousands nobody has read. Here a person already read it.
    """
    verdict = filters.Verdict(job=add.job_from_text("u", "body"),
                              rejected_by="region", reason="US-only")
    objection = add.advise_not_veto(verdict)

    assert objection == "region: US-only"
    assert verdict.passed is True, "a job you added by hand must survive the funnel"
    assert verdict.rejected_by is None


def test_a_passing_verdict_is_untouched_and_reports_nothing():
    verdict = filters.Verdict(job=add.job_from_text("u", "body"))
    assert add.advise_not_veto(verdict) == ""
    assert verdict.passed is True


# --- the fetch path ----------------------------------------------------------

def test_a_bot_challenge_says_to_paste_instead(monkeypatch):
    """ph.jobstreet.com answers exactly this. The message has to be actionable."""
    challenge = b"<html><head><title>Just a moment...</title></head></html>"

    class FakeResponse:
        def read(self): return challenge
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(add.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    with pytest.raises(add.Unfetchable) as exc:
        add.fetch_page("https://ph.jobstreet.com/job/1")
    assert "--text-file" in str(exc.value)


@pytest.mark.parametrize("code", [401, 403, 429])
def test_a_refusal_says_to_paste_instead(monkeypatch, code):
    def boom(*a, **k):
        raise urllib.error.HTTPError("u", code, "no", {}, None)

    monkeypatch.setattr(add.urllib.request, "urlopen", boom)
    with pytest.raises(add.Unfetchable) as exc:
        add.fetch_page("https://example.test/j")
    assert "--text-file" in str(exc.value)
    assert str(code) in str(exc.value)


def test_a_page_with_no_jobposting_falls_back_to_its_text(monkeypatch):
    """Prose is fine — the model reads it. Refusing would be worse than parsing less."""
    html = (b"<html><head><title>Widget Co - QA Lead</title>"
            b"<script>var x=1</script></head><body><p>We need a QA lead.</p></body></html>")

    class FakeResponse:
        def read(self): return html
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(add.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    job, origin = add.build_job("https://example.test/j", None, "", "")
    assert "no JobPosting" in origin
    assert "QA lead" in job.description
    assert "var x=1" not in job.description, "script bodies must not reach the model"


def test_text_mode_never_touches_the_network(monkeypatch):
    """--text-file must not fetch. That is the whole point of the paste path."""
    def explode(*a, **k):
        raise AssertionError("build_job fetched despite being given a body")

    monkeypatch.setattr(add.urllib.request, "urlopen", explode)
    job, origin = add.build_job("https://ph.jobstreet.com/job/1", "Pasted advert body", "", "")
    assert origin == "pasted text"
    assert job.description == "Pasted advert body"
