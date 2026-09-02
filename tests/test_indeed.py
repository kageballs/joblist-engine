"""Parsing tests against a committed fixture. Offline, no network at all.

sources/indeed.py makes no HTTP requests, ever -- see its module docstring --
so unlike test_himalayas.py and test_onlinejobs.py there is no live contract
test here to fall back on. The fixture *is* the contract, and it deliberately
carries the edge cases: PHP annual and PHP monthly rows that must convert to
USD, a USD hourly row that must NOT be read as pesos just because the site is
PH, an expired listing, a row with no `jobkey`, and a row that was never
hydrated (empty description, null company, null date_posted).
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from sources.indeed import Indeed, _load_captures, parse_salary, to_job

FIXTURE = "fixtures/indeed.json"


@pytest.fixture
def payload():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def rows(payload):
    return payload["results"]


def by_key(rows, jobkey):
    return next(r for r in rows if r.get("jobkey") == jobkey)


# --- row parsing -----------------------------------------------------------

def test_php_annual_row_converts_to_usd_and_survives_the_floor(rows):
    row = by_key(rows, "c36ca7b03e89b359")  # PHP 1,794,984 - 2,393,299 a year
    job = to_job(row)
    assert job is not None
    assert job.currency == "USD"
    assert job.salary_period == "annual"
    # 2,393,299 / 58.5 ~= 40,911
    assert job.annual_usd_max() == pytest.approx(2393299 / 58.5, rel=0.01)


def test_php_monthly_row_converts_to_usd(rows):
    row = by_key(rows, "a91f0c22b7d43e08")  # From PHP 150,000 a month
    job = to_job(row)
    assert job is not None
    assert job.currency == "USD"
    assert job.salary_period == "monthly"
    expected_annual = (150000 / 58.5) * 12
    assert job.annual_usd_max() == pytest.approx(expected_annual, rel=0.01)


def test_usd_hourly_row_is_not_converted_as_if_it_were_pesos(rows):
    """The one row this fixture exists to catch: a PH-site listing stated in
    dollars must not be run through the PHP rate just because of the site."""
    row = by_key(rows, "b0287d4419aa6f31")  # $8 - $12 an hour
    job = to_job(row)
    assert job is not None
    assert job.currency == "USD"
    assert job.salary_period == "hourly"
    assert job.salary_max == 12
    assert job.annual_usd_max() == 12 * 2080


def test_row_with_no_stated_salary_yields_no_salary_at_all(rows):
    row = by_key(rows, "ddc24970021a848e")  # AI Engineer, salary_text null
    job = to_job(row)
    assert job is not None
    assert job.salary_min is None
    assert job.salary_max is None
    assert job.currency is None
    assert job.annual_usd_max() is None


def test_expired_row_is_dropped(rows):
    row = by_key(rows, "d55e1a930cc7b284")
    assert row["expired"] is True
    assert to_job(row) is None


def test_row_with_no_jobkey_is_dropped(rows):
    row = next(r for r in rows if r.get("jobkey") == "")
    assert to_job(row) is None


def test_never_hydrated_row_does_not_crash(rows):
    """Empty description, null company, null date_posted -- but pub_date is
    still present, so this must parse via the epoch-ms fallback, not raise."""
    row = by_key(rows, "e73b8f01d2c94a6f")
    job = to_job(row)
    assert job is not None
    assert job.company == ""
    assert job.description == ""


def test_required_fields_present_on_every_usable_row(rows):
    for row in rows:
        job = to_job(row)
        if job is None:
            continue
        assert job.source == "indeed"
        assert job.source_id == row["jobkey"]
        assert job.title
        assert job.url.startswith("https://")
        assert job.posted.tzinfo is not None, "posted must be timezone-aware"
        assert job.key == f"indeed:{row['jobkey']}"


def test_location_restrictions_is_philippines_not_empty(rows):
    """Empty here would mean genuinely worldwide, and it is not true: this is
    the PH site, every listing hires locally."""
    for row in rows:
        job = to_job(row)
        if job is not None:
            assert job.location_restrictions == ("Philippines",)


def test_url_is_rebuilt_when_missing(rows):
    row = dict(by_key(rows, "ddc24970021a848e"))
    row["url"] = None
    job = to_job(row)
    assert job.url == "https://ph.indeed.com/viewjob?jk=ddc24970021a848e"


def test_posted_prefers_date_posted_over_pub_date(rows):
    row = by_key(rows, "ddc24970021a848e")
    job = to_job(row)
    assert job.posted == datetime(2026, 9, 2, 9, 12, 44, 117000, tzinfo=UTC)


def test_posted_falls_back_to_pub_date_epoch_ms():
    row = {
        "jobkey": "x1", "title": "Fallback Job", "company": "C",
        "expired": False, "date_posted": None,
        "pub_date": 1788393600000,  # 2026-09-02T00:00:00Z
        "description": "", "url": "https://ph.indeed.com/viewjob?jk=x1",
    }
    job = to_job(row)
    assert job is not None
    assert job.posted == datetime(2026, 9, 3, 0, 0, 0, tzinfo=UTC)


def test_row_with_neither_date_is_skipped():
    row = {"jobkey": "x2", "title": "No Dates", "expired": False,
           "date_posted": None, "pub_date": None}
    assert to_job(row) is None


def test_expires_parses_valid_through(rows):
    row = by_key(rows, "c36ca7b03e89b359")
    job = to_job(row)
    assert job.expires == datetime(2026, 12, 30, 0, 0, 0, tzinfo=UTC)


def test_description_html_is_stripped(rows):
    row = by_key(rows, "ddc24970021a848e")
    job = to_job(row)
    assert "<" not in job.description
    assert "AI Engineer" in job.description


def test_to_job_never_raises_on_junk():
    """A malformed row must degrade to a skip, never a crashed run."""
    assert to_job({}) is None
    assert to_job({"jobkey": "x"}) is None
    # salary_max present but not a number -- must still parse the rest.
    row = {"jobkey": "x", "title": "t", "date_posted": "2026-09-02T00:00:00Z",
           "salary_max": "not-a-number", "salary_period": "HOURLY", "salary_text": "$5"}
    job = to_job(row)
    assert job is not None
    assert job.salary_max is None


# --- salary parsing ----------------------------------------------------

def test_parse_salary_unknown_currency_yields_no_salary():
    row = {"salary_text": "12 gold coins a year", "salary_max": 12,
           "salary_min": 12, "salary_period": "YEARLY"}
    assert parse_salary(row) == (None, None, None, None)


def test_parse_salary_unknown_period_yields_no_salary():
    row = {"salary_text": "$50,000", "salary_max": 50000,
           "salary_min": 50000, "salary_period": "FORTNIGHTLY"}
    assert parse_salary(row) == (None, None, None, None)


def test_parse_salary_missing_max_yields_no_salary():
    assert parse_salary({"salary_text": "$10/hr", "salary_period": "HOURLY"}) == (
        None, None, None, None,
    )


# --- capability flags --------------------------------------------------

def test_capability_flags_match_the_measured_evidence():
    source = Indeed()
    assert source.name == "indeed"
    # A ph.indeed.com job page has no applicantLocationRequirements field at
    # all -- an empty restriction list would prove nothing.
    assert source.regions_authoritative is False
    # Pay is stated on ~7% of cards and is always salary_source EXTRACTION --
    # inferred by Indeed, not declared by the employer.
    assert source.salary_authoritative is False
    # valid_through is real when present.
    assert source.publishes_expiry is True


def test_init_sets_the_flags_main_reads_after_every_source_runs():
    source = Indeed()
    assert source.pages_fetched == 0
    assert source.hit_page_cap is False


def test_hit_page_cap_stays_false_after_a_fetch(tmp_path, monkeypatch):
    """No pagination here -- a fetch must never flip this True."""
    import config

    monkeypatch.setattr(config, "INDEED_CAPTURE_DIR", str(tmp_path))
    _write_capture(tmp_path / "indeed-2026-09-03.json", captured_at="2026-09-03T00:00:00Z", results=[])
    source = Indeed()
    list(source.fetch(datetime(2020, 1, 1, tzinfo=UTC)))
    assert source.hit_page_cap is False


# --- capture-file handling ----------------------------------------------

def _write_capture(path, captured_at, results, site="ph.indeed.com"):
    path.write_text(
        json.dumps({"captured_at": captured_at, "site": site, "results": results}),
        encoding="utf-8",
    )


def _row(jobkey, title, date_posted, expired=False):
    return {
        "jobkey": jobkey, "title": title, "company": "C",
        "expired": expired, "date_posted": date_posted, "pub_date": None,
        "description": "", "url": None,
    }


def test_missing_capture_directory_yields_nothing(tmp_path):
    missing = tmp_path / "does-not-exist"
    captures, files_read = _load_captures(str(missing), 14)
    assert captures == []
    assert files_read == 0


def test_stale_capture_is_skipped_with_a_warning(tmp_path, capsys):
    stale_at = (datetime.now(UTC) - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    _write_capture(
        tmp_path / "indeed-old.json", captured_at=stale_at,
        results=[_row("k1", "Old Job", stale_at)],
    )
    captures, files_read = _load_captures(str(tmp_path), 14)
    assert captures == []
    assert files_read == 1
    err = capsys.readouterr().err
    assert "indeed-old.json" in err
    assert "skipping" in err


def test_malformed_json_file_is_skipped_not_fatal(tmp_path, capsys):
    (tmp_path / "indeed-broken.json").write_text("{not valid json", encoding="utf-8")
    fresh_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    _write_capture(
        tmp_path / "indeed-good.json", captured_at=fresh_at,
        results=[_row("k1", "Good Job", fresh_at)],
    )
    captures, files_read = _load_captures(str(tmp_path), 14)
    assert files_read == 1  # only the well-formed file was actually read
    assert len(captures) == 1
    err = capsys.readouterr().err
    assert "indeed-broken.json" in err


def test_dedupe_across_files_keeps_the_newest_capture(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "INDEED_CAPTURE_DIR", str(tmp_path))
    now = datetime.now(UTC)
    older = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    newer = now.isoformat().replace("+00:00", "Z")

    _write_capture(
        tmp_path / "indeed-1.json", captured_at=older,
        results=[_row("dup", "Old Title", older)],
    )
    _write_capture(
        tmp_path / "indeed-2.json", captured_at=newer,
        results=[_row("dup", "New Title", newer)],
    )

    source = Indeed()
    jobs = list(source.fetch(datetime(2020, 1, 1, tzinfo=UTC)))
    assert len(jobs) == 1
    assert jobs[0].title == "New Title"
    assert source.pages_fetched == 2


def test_since_filtering_excludes_older_jobs(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "INDEED_CAPTURE_DIR", str(tmp_path))
    now = datetime.now(UTC)
    recent = now.isoformat().replace("+00:00", "Z")
    old = (now - timedelta(days=10)).isoformat().replace("+00:00", "Z")

    _write_capture(
        tmp_path / "indeed-1.json", captured_at=recent,
        results=[_row("a", "Recent", recent), _row("b", "Old", old)],
    )

    source = Indeed()
    since = now - timedelta(days=1)
    jobs = list(source.fetch(since))
    titles = {j.title for j in jobs}
    assert titles == {"Recent"}


def test_fixture_captures_load_end_to_end(monkeypatch, tmp_path):
    """The committed fixture itself, read through Indeed.fetch()."""
    import config

    monkeypatch.setattr(config, "INDEED_CAPTURE_DIR", str(tmp_path))
    # The fixture's own captured_at is a fixed date in the repo, which will
    # eventually fall outside the freshness window -- rewrite it to "now" so
    # this test does not silently start skipping the fixture as stale.
    with open(FIXTURE, encoding="utf-8") as fh:
        payload = json.load(fh)
    payload["captured_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    (tmp_path / "indeed-fixture.json").write_text(json.dumps(payload), encoding="utf-8")

    source = Indeed()
    jobs = list(source.fetch(datetime(2000, 1, 1, tzinfo=UTC)))
    # 7 rows in the fixture; the empty-jobkey row is unusable and the expired
    # row is dropped -- the other 5 survive, including the never-hydrated row
    # (it still carries pub_date even with no date_posted).
    assert len(jobs) == 5
    assert all(j.source == "indeed" for j in jobs)


def test_a_bad_row_in_the_newest_capture_falls_back_to_an_older_one(tmp_path, monkeypatch):
    """A capture is made by hand, so one broken row must not lose the advert.

    The newest capture wins normally. But if its copy of a job will not parse,
    marking the key seen would shadow a perfectly good copy in yesterday's
    capture, and the only way to get it back is another browser session.
    """
    import config as cfg
    from sources.indeed import Indeed

    def cap(name, captured_at, row):
        (tmp_path / name).write_text(json.dumps({
            "captured_at": captured_at, "site": "ph.indeed.com",
            "count": 1, "results": [row],
        }), encoding="utf-8")

    good = {
        "jobkey": "shared1", "title": "Backend Engineer", "company": "Acme",
        "location": "Work from Home", "country": "PH", "expired": False,
        "date_posted": "2026-09-01T00:00:00.000Z", "description": "Go and Postgres.",
    }
    # Same key, newer capture, but no usable date at all -> to_job returns None.
    broken = {**good, "date_posted": None, "pub_date": None, "title": "Broken Copy"}

    cap("indeed-2026-09-01.json", "2026-09-01T00:00:00.000Z", good)
    cap("indeed-2026-09-02.json", "2026-09-02T00:00:00.000Z", broken)
    monkeypatch.setattr(cfg, "INDEED_CAPTURE_DIR", str(tmp_path))

    jobs = list(Indeed().fetch(datetime(2026, 8, 1, tzinfo=UTC)))
    assert [j.title for j in jobs] == ["Backend Engineer"], "the older good copy must survive"


def _card(**kw):
    base = {
        "jobkey": "k1", "title": "Backend Engineer", "company": "Acme",
        "country": "PH", "expired": False,
        "date_posted": "2026-09-01T00:00:00.000Z", "description": "Go and Postgres.",
    }
    return {**base, **kw}


def test_the_country_on_the_card_decides_the_region_not_the_site():
    """ph.indeed.com is a localisation of a global site, not a PH-only board.

    onlinejobs.ph can assert Philippines for every listing because that is what
    the board IS. Indeed cannot: the same job-card model served country "US"
    rows on indeed.com, so the region has to come from the row.
    """
    assert to_job(_card(country="PH")).location_restrictions == ("Philippines",)
    assert to_job(_card(country="us")).location_restrictions == ("United States",)


def test_an_unknown_country_leaves_the_region_empty_rather_than_guessing():
    """A populated tuple is a hard gate, so a guessed country rejects real jobs."""
    for unknown in (None, "", "ZZ", "  "):
        assert to_job(_card(country=unknown)).location_restrictions == ()


def test_an_unknown_country_is_what_makes_the_capability_flag_do_work():
    """The flag must be load-bearing, not decorative.

    If this source asserted a country on every row the tuple would never be
    empty, `regions_authoritative = False` could never fire, and the scorer
    would never be warned. This is the test that would fail if someone
    reintroduced an unconditional ("Philippines",).
    """
    import filters
    import targeting

    profile = targeting.load("profile.example.yaml")
    now = datetime(2026, 9, 3, tzinfo=UTC)
    source = Indeed()

    known = filters.evaluate(to_job(_card(country="PH")), profile, now, source)
    unknown = filters.evaluate(to_job(_card(country=None)), profile, now, source)

    assert known.region_unverified is False, "a stated country needs no caveat"
    assert unknown.region_unverified is True, "silence here must reach the scorer"
