"""Parsing tests against a committed fixture, plus a live contract test.

Same split as test_himalayas: the fixture makes assertions deterministic and
offline, the live test asserts only the response *shape* the parser depends on.

The weight here is on parse_salary(). On Himalayas, salary is a structured
field and the interesting tests are about regions. Here region and timezone
reject nothing at all -- every listing hires Filipinos -- so salary is the only
load-bearing filter this source has, and it arrives as an unvalidated text box
holding 60+ distinct formats. If these tests go soft, the source stops paying
for itself and quietly bills the scorer for a board that is ~96% under floor.
"""

from datetime import UTC, datetime, timedelta

import pytest

from filters import HOURS_PER_YEAR
from sources.onlinejobs import OnlineJobs, parse_detail, parse_page, parse_salary

FIXTURE = "fixtures/onlinejobs.html"


@pytest.fixture
def html():
    with open(FIXTURE, encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture
def jobs(html):
    return parse_page(html)


def hourly(job) -> float | None:
    annual = job.annual_usd_max()
    return None if annual is None else annual / HOURS_PER_YEAR


# --- card parsing ---------------------------------------------------------

def test_fixture_yields_jobs(jobs):
    assert len(jobs) >= 6


def test_required_fields_present(jobs):
    for job in jobs:
        assert job.source == "onlinejobs"
        assert job.source_id.isdigit(), "source_id is the numeric id off the URL slug"
        assert job.title and job.url.startswith("https://www.onlinejobs.ph/")
        assert job.key.startswith("onlinejobs:")
        assert job.posted.tzinfo is not None, "posted must be timezone-aware"


def test_posted_is_utc_not_manila_local():
    """The card carries both; reading the wrong one shifts the watermark 8h.

    data-temp is UTC+8 and data-temp-2 is UTC. If this ever flips, a run either
    re-fetches a day it already has or skips eight hours of listings, and both
    failures are invisible in the digest.
    """
    card = (
        '<div class="jobpost-cat-box">'
        '<a href="/jobseekers/job/thing-999"></a>'
        '<h4>Thing<span class="badge">Full Time</span></h4>'
        '<p data-temp="2026-08-30 19:54:57" data-temp-2="2026-08-30 11:54:57"></p>'
        '<dd class="col">$500</dd></div>'
    )
    job = parse_page(card)[0]
    assert job.posted == datetime(2026, 8, 30, 11, 54, 57, tzinfo=UTC)


def test_badge_is_not_swallowed_into_the_title(jobs):
    for job in jobs:
        assert "Full Time" not in job.title
        assert "Part Time" not in job.title


def test_region_is_asserted_as_philippines(jobs):
    """Not scraped -- asserted, because the board only hires Filipinos.

    This is what puts these listings in the profile's `local_anchor` bucket so
    they read as domestic-rate rather than as worldwide roles.
    """
    for job in jobs:
        assert job.location_restrictions == ("Philippines",)


def test_stated_pay_is_handed_to_the_scorer(jobs):
    """parse_salary commits to one reading; the model needs the raw text to
    be able to contradict it."""
    for job in jobs:
        if job.raw.get("salary_text"):
            assert job.description.startswith("Stated pay: ")


def test_parse_never_raises_on_junk():
    for junk in ("", "<html", "<div class='jobpost-cat-box'></div>", "\x00\xff"):
        assert parse_page(junk) == []


def test_card_missing_a_timestamp_is_dropped_not_raised():
    card = (
        '<div class="jobpost-cat-box"><a href="/jobseekers/job/x-1"></a>'
        '<h4>No date</h4><dd class="col">$500</dd></div>'
    )
    assert parse_page(card) == []


# --- salary normalisation -------------------------------------------------

@pytest.mark.parametrize("text,expected_hourly", [
    # explicit period, explicit currency
    ("$7.00 - $8.00 an hour", 8.00),
    ("$10–$25 USD/Hour", 25.00),          # en-dash range
    ("$6.00 ~ $15 / HR", 15.00),
    ("5hrly", 5.00),                            # no separator before the unit
    ("$275 USD/Weekly", 275 * 52 / HOURS_PER_YEAR),
    ("1000 month", 1000 * 12 / HOURS_PER_YEAR),
    ("$1800-$3000 USD/Month", 3000 * 12 / HOURS_PER_YEAR),
])
def test_parse_salary_explicit(text, expected_hourly):
    _, top = parse_salary(text)
    assert top is not None, f"{text!r} should resolve"
    assert top / HOURS_PER_YEAR == pytest.approx(expected_hourly, rel=0.01)


def test_php_without_a_word_boundary_is_still_php():
    """"25,000PHP" and "PHP100k" are both real on this board.

    A digit-to-letter transition is not a word boundary, so \\bphp\\b misses
    both and reads them as dollars -- overstating a wage by ~58x and turning a
    $2/hr listing into a pass. This is the single most expensive parsing bug
    available here, hence its own test.
    """
    for text in ("15,000PHP to 25,000PHP", "PHP100k month 1; PHP120k from month 2"):
        _, top = parse_salary(text)
        assert top is not None
        assert top / HOURS_PER_YEAR < 15, f"{text!r} must not read as USD"


def test_dual_currency_line_keeps_the_usd_half():
    """"$1,900-2,400/mo | PHP 120,00-150,000/mo" is one figure written twice.

    Sniffing currency across the whole string sees the PHP and then converts
    the dollar figures as pesos too.
    """
    _, top = parse_salary("$1,900-2,400/mo | PHP 120,00–150,000/mo")
    assert top / HOURS_PER_YEAR == pytest.approx(2400 * 12 / HOURS_PER_YEAR, rel=0.01)


@pytest.mark.parametrize("text", [
    "TBD", "TO BE DISCUSSED", "Depending on experience", "?", "", None,
])
def test_unstated_pay_stays_unknown(text):
    """Tri-state, per filters.stage_salary: unknown must not collapse to zero.

    Rejecting on unknown is how this tool ships and then returns nothing
    forever, which looks identical to a quiet market.
    """
    assert parse_salary(text) == (None, None)


@pytest.mark.parametrize("text,job_type", [
    ("$20 fixed price for this homepage redesign", ""),
    ("$4 per listing @ 75 listings in 2 weeks", ""),
    ("$300", "Gig"),          # bare amount on a gig: a project price
])
def test_piecework_is_not_a_rate(text, job_type):
    """A project price has no period to divide by, so it is not a wage."""
    assert parse_salary(text, job_type) == (None, None)


@pytest.mark.parametrize("text,expected_hourly", [
    ("5hrly", 5.0),
    ("$100/week", 100 * 52 / HOURS_PER_YEAR),
])
def test_gig_that_states_a_period_is_still_a_rate(text, expected_hourly):
    """"Gig" describes the engagement, not the units the pay is quoted in.

    Both of these are real Gig listings well under the floor. Suppressing them
    as project prices sent them to the scorer to be read, when the floor could
    have rejected them for nothing.
    """
    _, top = parse_salary(text, "Gig")
    assert top is not None
    assert top / HOURS_PER_YEAR == pytest.approx(expected_hourly, rel=0.01)


def test_bare_amounts_take_the_most_generous_reading():
    """A bare number has no period, so magnitude is the only evidence.

    Reading it generously is what makes rejecting on it sound: if the BEST
    reading is under the floor, every other reading is too. Small numbers read
    as hourly (an "$80" that really is $80/hr is worth surfacing); anything
    from $200 up cannot be hourly on this board and reads as monthly.
    """
    _, small = parse_salary("80")
    assert small / HOURS_PER_YEAR == pytest.approx(80.0)

    _, large = parse_salary("500")
    assert large / HOURS_PER_YEAR == pytest.approx(500 * 12 / HOURS_PER_YEAR, rel=0.01)


def test_range_returns_both_ends():
    low, high = parse_salary("$1800-$3000 USD/Month")
    assert low < high


# --- live contract --------------------------------------------------------

@pytest.mark.live
def test_live_search_page_still_parses():
    """Asserts shape, never counts. Scraped markup has no contract behind it,
    so this is how the class rename gets found before the 7am run does."""
    source = OnlineJobs()
    since = datetime.now(UTC) - timedelta(hours=6)
    jobs = list(source.fetch(since))
    assert jobs, "no jobs in a 6h window means the markup moved"
    for job in jobs[:5]:
        assert job.title and job.source_id.isdigit()
        assert job.posted >= since


# --- detail pages ---------------------------------------------------------

DETAIL_FIXTURE = "fixtures/onlinejobs_detail.html"


def test_detail_page_yields_the_full_advert():
    """The search card is a ~280-char teaser cut at "See More".

    Everything that decides applicability -- the must-have list, equipment
    demands, and the hiring restrictions CLAUDE.md notes live in the
    boilerplate at the BOTTOM of an ad -- exists only on the detail page.
    Scoring on the teaser was judging a job on a truncated sentence.
    """
    with open(DETAIL_FIXTURE, encoding="utf-8") as fh:
        full = parse_detail(fh.read())
    assert full and len(full) > 600
    # Requirements the teaser cannot possibly contain:
    assert "Degreed required" in full
    assert "remote work equipments" in full


@pytest.mark.parametrize("junk", ["", "<html", "<div>no job description</div>"])
def test_parse_detail_returns_none_on_unusable_input(junk):
    """Never raises: a scheduled run must degrade to the teaser, not crash."""
    assert parse_detail(junk) is None


def test_hydrate_leaves_other_sources_alone():
    from sources.base import Job
    other = Job(source="himalayas", source_id="1", title="t", company="c",
                url="https://example.com", posted=datetime(2026, 1, 1, tzinfo=UTC),
                description="original")
    assert OnlineJobs().hydrate(other) is other
