"""The funnel is where the logic lives, so this is where the tests live.

The assertions that matter most are the ones guarding against silent
over-rejection: a filter that quietly drops everything looks identical to a
quiet job market, and the tool would just report zero forever.
"""

import re
from datetime import UTC, datetime, timedelta

import pytest

import filters
import targeting
from sources.base import Job

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


@pytest.fixture
def profile():
    return targeting.load("profile.example.yaml")


def make_job(**kwargs) -> Job:
    defaults = dict(
        source="himalayas",
        source_id="1",
        title="Senior Backend Engineer",
        company="Acme",
        url="https://example.com/1",
        posted=NOW - timedelta(hours=1),
    )
    defaults.update(kwargs)
    return Job(**defaults)


# -- region ---------------------------------------------------------------

def test_no_restrictions_passes(profile):
    """The most important pass case: no restriction means worldwide."""
    verdict = filters.evaluate(make_job(location_restrictions=()), profile, NOW)
    assert verdict.passed


def test_foreign_only_is_rejected(profile):
    verdict = filters.evaluate(
        make_job(location_restrictions=("United States",)), profile, NOW
    )
    assert verdict.rejected_by == "region"


def test_anywhere_in_the_world_passes(profile):
    verdict = filters.evaluate(
        make_job(location_restrictions=("Anywhere in the World",)), profile, NOW
    )
    assert verdict.passed


def test_mixed_restrictions_pass_if_any_admits_us(profile):
    verdict = filters.evaluate(
        make_job(location_restrictions=("United States", "Nigeria")), profile, NOW
    )
    assert verdict.passed


def test_own_country_only_passes_but_is_flagged(profile):
    """Restricted to the user's own country passes, and is recorded.

    It was marked down until 2026-08-24, on the basis that the only such
    listing in a 300-job Himalayas sample was a BPO. Now neutral: while the
    goal is work soon, home-country eligibility is often the fastest yes.
    The flag is still set so the digest can note it.
    """
    verdict = filters.evaluate(
        make_job(location_restrictions=("Nigeria",)), profile, NOW
    )
    assert verdict.passed
    assert verdict.local_anchor is True


# -- timezone -------------------------------------------------------------

def test_empty_timezone_restrictions_pass(profile):
    assert filters.evaluate(make_job(timezone_restrictions=()), profile, NOW).passed


def test_incompatible_timezone_rejected(profile):
    verdict = filters.evaluate(
        make_job(timezone_restrictions=(-8, -7, -6)), profile, NOW
    )
    assert verdict.rejected_by == "timezone"


def test_overlapping_timezone_passes(profile):
    verdict = filters.evaluate(
        make_job(timezone_restrictions=(-5, 1, 8)), profile, NOW
    )
    assert verdict.passed


# -- salary ---------------------------------------------------------------

def test_unstated_salary_passes(profile):
    """Guards the worst failure mode: most listings state no salary at all.

    If unknown were treated as below-floor, the tool would ship and then
    return nothing forever, and it would look like a quiet market.
    """
    verdict = filters.evaluate(make_job(salary_max=None), profile, NOW)
    assert verdict.passed
    assert verdict.salary_signal == filters.SALARY_UNKNOWN


def test_zero_salary_is_unknown_not_zero(profile):
    """Feeds write 0 to mean "not stated". Treating it as $0 rejects everything."""
    verdict = filters.evaluate(
        make_job(salary_min=0, salary_max=0, currency="USD", salary_period="annual"),
        profile, NOW,
    )
    assert verdict.passed
    assert verdict.salary_signal == filters.SALARY_UNKNOWN


def test_clearly_below_floor_rejected(profile):
    verdict = filters.evaluate(
        make_job(salary_max=20000, currency="USD", salary_period="annual"),
        profile, NOW,
    )
    assert verdict.rejected_by == "salary"


def test_above_target_flagged_above(profile):
    verdict = filters.evaluate(
        make_job(salary_max=150000, currency="USD", salary_period="annual"),
        profile, NOW,
    )
    assert verdict.passed
    assert verdict.salary_signal == filters.SALARY_ABOVE


def test_foreign_currency_is_unknown_never_below(profile):
    """An unconvertible currency must never be guessed into a rejection."""
    verdict = filters.evaluate(
        make_job(salary_max=1000, currency="EUR", salary_period="annual"),
        profile, NOW,
    )
    assert verdict.passed
    assert verdict.salary_signal == filters.SALARY_UNKNOWN


def test_hourly_and_monthly_normalise_to_annual():
    hourly = make_job(salary_max=50, currency="USD", salary_period="hourly")
    monthly = make_job(salary_max=5000, currency="USD", salary_period="monthly")
    assert hourly.annual_usd_max() == 104000
    assert monthly.annual_usd_max() == 60000


# -- role and employer ----------------------------------------------------

def test_offtarget_title_rejected(profile):
    verdict = filters.evaluate(
        make_job(title="Home-Based Accounting Coordinator"), profile, NOW
    )
    assert verdict.rejected_by == "role"


def test_excluded_title_rejected(profile):
    verdict = filters.evaluate(
        make_job(title="Backend Engineering Intern"), profile, NOW
    )
    assert verdict.rejected_by == "role"


def test_blocklisted_employer_rejected():
    profile = targeting.load("profile.example.yaml")
    blocked = targeting.Profile(
        **{**profile.__dict__, "employer_blocklist": ("Acme",)}
    )
    verdict = filters.evaluate(make_job(company="Acme Staffing"), blocked, NOW)
    assert verdict.rejected_by == "employer"


# -- expiry ---------------------------------------------------------------

def test_expired_rejected(profile):
    verdict = filters.evaluate(
        make_job(expires=NOW - timedelta(days=1)), profile, NOW
    )
    assert verdict.rejected_by == "expired"


# -- stage ordering -------------------------------------------------------

def test_region_beats_role(profile):
    """Cheapest-and-most-eliminating first: region must report before role."""
    verdict = filters.evaluate(
        make_job(title="Accounting Coordinator", location_restrictions=("Japan",)),
        profile, NOW,
    )
    assert verdict.rejected_by == "region"


# -- eligibility extraction ----------------------------------------------

def test_eligibility_sentence_found_at_the_bottom():
    """The disqualifier lives in the boilerplate, which head-truncation eats."""
    body = ("We are a great team. " * 200) + (
        "This role is open to candidates located in British Columbia or Ontario, "
        "Canada. At this time, we are only able to hire employees residing in "
        "these provinces."
    )
    found = filters.eligibility_sentences(body)
    assert any("only able to hire" in s.lower() for s in found)


def test_no_eligibility_text_returns_empty():
    assert filters.eligibility_sentences("We build web apps in Python.") == []


# -- funnel ---------------------------------------------------------------

def test_funnel_counts_and_near_misses(profile):
    funnel = filters.Funnel()
    for job in [
        make_job(source_id="a", location_restrictions=("United States",)),
        make_job(source_id="b", title="Data Entry Clerk"),
        make_job(source_id="c"),
    ]:
        funnel.fetched += 1
        funnel.add(filters.evaluate(job, profile, NOW))

    assert len(funnel.survivors) == 1
    assert funnel.by_stage["region"] == 1
    assert funnel.by_stage["role"] == 1
    # Role dies latest in the chain, so it is the more informative near miss.
    assert funnel.near_misses(1)[0].rejected_by == "role"
    assert "3 fetched" in funnel.line()


# -- empty include list ---------------------------------------------------

def _no_include(profile):
    return targeting.Profile(**{**profile.__dict__, "role_include": ()})


def test_empty_include_list_passes_any_title(profile):
    """`include: []` means "let the scorer rank", not "reject everything".

    This is the live configuration: once earlier stages cut the pool to a few
    dozen a day, title regexes cost more in missed matches than they save in
    tokens.
    """
    loose = _no_include(profile)
    for title in [
        "Conversational AI & Voice AI Specialist",
        "Senior Design Engineer, AI Platforms",
        "Staff / Principal Cryptographer (IC)",
        "Data Entry Specialist",
    ]:
        assert filters.evaluate(make_job(title=title), loose, NOW).passed, title


def test_exclude_still_applies_without_include(profile):
    """Excludes are independent of includes — the impossible stays impossible."""
    loose = _no_include(profile)
    verdict = filters.evaluate(make_job(title="Backend Engineering Intern"), loose, NOW)
    assert verdict.rejected_by == "role"


# -- exclude_unless_paid --------------------------------------------------

def _unless_paid(profile, *patterns):
    return targeting.Profile(
        **{**profile.__dict__,
           "role_exclude_unless_paid": tuple(re.compile(p, re.I) for p in patterns)}
    )


def test_junior_without_stated_salary_is_rejected(profile):
    p = _unless_paid(profile, r"\bjunior\b")
    verdict = filters.evaluate(make_job(title="Junior Backend Engineer"), p, NOW)
    assert verdict.rejected_by == "role"
    assert "no salary stated" in verdict.reason


def test_junior_with_salary_above_floor_passes(profile):
    """Salary runs before role, so a stated figure here already cleared the floor."""
    p = _unless_paid(profile, r"\bjunior\b")
    verdict = filters.evaluate(
        make_job(title="Junior Backend Engineer", salary_max=100000,
                 currency="USD", salary_period="annual"),
        p, NOW,
    )
    assert verdict.passed


def test_junior_with_salary_below_floor_dies_at_salary_not_role(profile):
    """The earlier stage must own the rejection, so the funnel blames the rate."""
    p = _unless_paid(profile, r"\bjunior\b")
    verdict = filters.evaluate(
        make_job(title="Junior Backend Engineer", salary_max=20000,
                 currency="USD", salary_period="annual"),
        p, NOW,
    )
    assert verdict.rejected_by == "salary"


def test_non_usd_salary_counts_as_unconfirmed(profile):
    """Deliberate over-rejection: the rule is about confirmation, not optimism."""
    p = _unless_paid(profile, r"\bjunior\b")
    verdict = filters.evaluate(
        make_job(title="Junior Backend Engineer", salary_max=90000,
                 currency="EUR", salary_period="annual"),
        p, NOW,
    )
    assert verdict.rejected_by == "role"


def test_senior_title_unaffected_by_the_rule(profile):
    p = _unless_paid(profile, r"\bjunior\b")
    assert filters.evaluate(make_job(title="Senior Backend Engineer"), p, NOW).passed
