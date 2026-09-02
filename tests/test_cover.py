"""Offline tests for cover.py. Nothing here touches the network or the API."""

from __future__ import annotations

import sqlite3
import sys
import types
from datetime import UTC, datetime

import pytest

import cover
from sources.base import Job
from store import Store


@pytest.fixture()
def store(tmp_path):
    with Store(str(tmp_path / "test.sqlite3")) as s:
        yield s


# The shape uids actually take in production: `source:source_id`, and on
# Himalayas the source_id is the advert's full URL. Tests that use a tidy
# `himalayas:1` prove nothing about the filesystem.
URL_UID = "himalayas:https://himalayas.app/companies/acme/jobs/backend-engineer"


def _job(uid=URL_UID, title="Senior Backend Engineer", description="We need Python."):
    source, source_id = uid.split(":", 1)
    return Job(
        source=source,
        source_id=source_id,
        title=title,
        company="Acme",
        url="https://example.com/j/1",
        posted=datetime(2026, 9, 1, tzinfo=UTC),
        description=description,
    )


def _row(store, job, **kw):
    run_id = store.start_run()
    store.record_job(job, run_id, "unknown", **kw)
    store.commit()
    return store.get_job(job.key)


class FakeProfile:
    name = "Sam Okafor"
    based_in = "Lagos, Nigeria"
    utc_offset = 1
    resume = "# Sam Okafor\nBuilt a thing that cut costs 95%."
    cannot_provide = frozenset({"work_samples"})


# --- storage -------------------------------------------------------------


def test_description_is_persisted(store):
    row = _row(store, _job(description="Full advert text."))
    assert row["description"] == "Full advert text."


def test_description_column_is_added_to_an_older_database(tmp_path):
    """The migration is additive: a database written before this feature opens."""
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE jobs (uid TEXT PRIMARY KEY, source TEXT NOT NULL,"
        " title TEXT NOT NULL, company TEXT, url TEXT NOT NULL,"
        " posted_at TEXT NOT NULL, regions TEXT, salary_signal TEXT,"
        " score INTEGER, verdict TEXT, why TEXT, cv_variant TEXT,"
        " run_id INTEGER, created_at TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()

    with Store(str(path)) as s:
        columns = {r[1] for r in s.conn.execute("PRAGMA table_info(jobs)")}
        assert "description" in columns


def test_description_is_not_pushed_to_the_dashboard():
    """Employer advert text is local only. push.FIELDS is the allowlist."""
    import push

    assert "description" not in push.FIELDS


# --- uid resolution ------------------------------------------------------


def test_get_job_accepts_a_unique_prefix(store):
    _row(store, _job(uid="himalayas:abc123"))
    assert store.get_job("himalayas:abc")["uid"] == "himalayas:abc123"


def test_get_job_refuses_an_ambiguous_prefix(store):
    _row(store, _job(uid="himalayas:abc111"))
    _row(store, _job(uid="himalayas:abc222"))
    with pytest.raises(LookupError):
        store.get_job("himalayas:abc")


def test_get_job_returns_none_when_nothing_matches(store):
    assert store.get_job("nope") is None


# --- prompt --------------------------------------------------------------


def test_prompt_carries_the_full_advert_not_a_clip(store):
    long_advert = "A" * 4000 + "ONLY_AT_THE_END"
    row = _row(store, _job(description=long_advert))
    prompt = cover.build_prompt(row)
    assert "ONLY_AT_THE_END" in prompt
    assert len(prompt) > 4000


def test_prompt_lists_what_the_advert_demands(store):
    row = _row(
        store,
        _job(),
        requirements=[{"kind": "work_samples", "mandatory": True, "detail": "send 5 samples"}],
        blockers=["work_samples"],
    )
    prompt = cover.build_prompt(row, store.requirements_for(row["uid"]))
    assert "work_samples" in prompt
    assert "MANDATORY" in prompt
    assert "send 5 samples" in prompt


def test_system_block_holds_the_resume_and_the_voice_rules():
    system = cover.build_system(FakeProfile())
    assert len(system) == 1
    body = system[0]["text"]
    assert "cut costs 95%" in body
    assert "No em dashes" in body
    assert "years-of-experience" in body
    assert system[0]["cache_control"] == {"type": "ephemeral"}


def test_system_block_is_byte_identical_across_calls():
    """It is the cached prefix. One varying byte restores the full cost."""
    assert cover.build_system(FakeProfile()) == cover.build_system(FakeProfile())


def test_resume_is_not_repeated_in_the_per_job_turn(store):
    row = _row(store, _job())
    assert "cut costs 95%" not in cover.build_prompt(row)


# --- rendering -----------------------------------------------------------


def test_rendered_draft_is_labelled_unsent(store):
    row = _row(store, _job())
    out = cover.render(row, "Letter body.", [])
    assert "has not been sent" in out
    assert "Letter body." in out


def test_rendered_draft_warns_about_a_blocker(store):
    row = _row(store, _job())
    out = cover.render(row, "Letter body.", ["work_samples"])
    assert "BLOCKED" in out
    assert "work_samples" in out


# --- CLI guards ----------------------------------------------------------


def test_cli_requires_a_target():
    with pytest.raises(SystemExit):
        cover.main([])


class APIError(Exception):
    """Stands in for anthropic.APIError."""


def _exploding_module():
    """A stand-in `anthropic` module where any use fails the test."""

    def boom(*a, **k):
        raise AssertionError("the API was called")

    return types.SimpleNamespace(Anthropic=boom, APIError=APIError)


def _wire(monkeypatch, store, tmp_path, client=None):
    """Point cover.py at a temp store, a fake profile and a temp output dir."""
    monkeypatch.setattr(cover, "targeting", type("T", (), {"load": staticmethod(FakeProfile)}))
    monkeypatch.setattr("store.Store", lambda *a, **k: store)
    monkeypatch.setattr(store, "close", lambda: None)
    monkeypatch.setattr(cover.config, "COVER_DIR", str(tmp_path / "covers"))
    monkeypatch.setitem(sys.modules, "anthropic", client or _exploding_module())


def test_dry_run_makes_no_api_call_and_writes_nothing(store, tmp_path, monkeypatch, capsys):
    """--dry-run must not construct a client, call the API, or touch the disk.

    The guard is a stubbed `anthropic` module that raises on any use, not a
    sentinel on the key reader: moving the key check would defeat that.
    """
    row = _row(store, _job(description="Advert body here."))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-be-used")
    _wire(monkeypatch, store, tmp_path)

    assert cover.main([row["uid"], "--dry-run"]) == 0
    assert "Advert body here." in capsys.readouterr().out
    assert not (tmp_path / "covers").exists()


class _FakeAnthropic(types.SimpleNamespace):
    """Minimal stand-in for the SDK module: records calls, returns a letter."""

    def __init__(self):
        calls = []

        def create(**kw):
            calls.append(kw)
            block = types.SimpleNamespace(type="text", text="Drafted letter body.")
            return types.SimpleNamespace(content=[block])

        client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
        super().__init__(
            calls=calls,
            APIError=APIError,
            Anthropic=lambda **kw: client,
            _client=client,
        )


def test_a_url_shaped_uid_actually_lands_on_disk(store, tmp_path, monkeypatch):
    """Regression for the real uid shape: the draft must be a findable file.

    This is the test whose absence let a broken `out_path` pass review: the
    old suite only ever used `himalayas:1` and never called `write_text`.
    """
    row = _row(store, _job(description="Advert body here."))
    assert ":" in row["uid"] and "/" in row["uid"], "fixture must use a real uid shape"
    fake = _FakeAnthropic()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _wire(monkeypatch, store, tmp_path, client=fake)

    assert cover.main([row["uid"]]) == 0
    written = list((tmp_path / "covers").glob("*.md"))
    assert len(written) == 1
    body = written[0].read_text(encoding="utf-8")
    assert "Drafted letter body." in body
    assert "has not been sent" in body
    assert len(fake.calls) == 1


def test_an_existing_draft_is_skipped_without_force(store, tmp_path, monkeypatch):
    row = _row(store, _job(description="Advert body here."))
    fake = _FakeAnthropic()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _wire(monkeypatch, store, tmp_path, client=fake)

    assert cover.main([row["uid"]]) == 0
    assert cover.main([row["uid"]]) == 0
    assert len(fake.calls) == 1, "second run must not pay for a redraft"
    assert cover.main([row["uid"], "--force"]) == 0
    assert len(fake.calls) == 2


def test_a_repeated_uid_is_drafted_once(store, tmp_path, monkeypatch):
    row = _row(store, _job(description="Advert body here."))
    fake = _FakeAnthropic()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _wire(monkeypatch, store, tmp_path, client=fake)

    assert cover.main([row["uid"], row["uid"], "--force"]) == 0
    assert len(fake.calls) == 1


def test_an_api_failure_does_not_discard_the_drafts_already_written(
    store, tmp_path, monkeypatch
):
    rows = [
        _row(store, _job(uid=f"himalayas:https://x.app/j/{n}", description="Advert."))
        for n in range(2)
    ]
    fake = _FakeAnthropic()
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise APIError("rate limited")
        block = type("B", (), {"type": "text", "text": "Drafted letter body."})()
        return type("R", (), {"content": [block]})()

    fake._client.messages.create = flaky
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _wire(monkeypatch, store, tmp_path, client=fake)

    assert cover.main([rows[0]["uid"], rows[1]["uid"]]) == 1
    assert len(list((tmp_path / "covers").glob("*.md"))) == 1


def test_top_must_be_positive():
    """A negative LIMIT is unbounded in SQLite, so this would bill every job."""
    with pytest.raises(SystemExit):
        cover.main(["--top", "-1"])


def test_a_wildcard_prefix_does_not_match_an_unrelated_job(store):
    """`%` is a LIKE wildcard; an unescaped prefix would match anything."""
    _row(store, _job(uid="himalayas:https://x.app/j/real"))
    assert store.get_job("himalayas:%") is None


def test_a_job_with_no_advert_text_is_skipped_not_drafted(store, monkeypatch, capsys):
    row = _row(store, _job(description=""))
    monkeypatch.setattr(cover, "targeting", type("T", (), {"load": staticmethod(FakeProfile)}))
    monkeypatch.setattr("store.Store", lambda *a, **k: store)
    monkeypatch.setattr(store, "close", lambda: None)

    assert cover.main([row["uid"], "--dry-run"]) == 1
    assert "no stored advert text" in capsys.readouterr().err


def test_unknown_uid_fails_with_an_actionable_message(store, monkeypatch, capsys):
    monkeypatch.setattr(cover, "targeting", type("T", (), {"load": staticmethod(FakeProfile)}))
    monkeypatch.setattr("store.Store", lambda *a, **k: store)
    monkeypatch.setattr(store, "close", lambda: None)

    assert cover.main(["does-not-exist", "--dry-run"]) == 1
    assert "no stored job matches" in capsys.readouterr().err


def test_out_path_is_under_the_cover_dir():
    assert cover.out_path(URL_UID).parent.name == "covers"


def test_slug_strips_everything_a_filesystem_rejects():
    """Regression: uids are `source:source_id`, and source_id is a full URL.

    A colon in a Windows filename is an NTFS alternate data stream, so the
    write silently lands somewhere unopenable; a slash raises OSError. Either
    way the letter is lost after the call has been paid for.
    """
    stem = cover.out_path(URL_UID).name
    for char in ':/\\?*"<>|':
        assert char not in stem, f"{char!r} survived into {stem!r}"


def test_slug_does_not_collide_for_uids_that_flatten_alike():
    a = cover.out_path("himalayas:https://x.app/a/b")
    b = cover.out_path("himalayas:https://x.app/a-b")
    assert a != b


def test_slug_survives_a_very_long_uid():
    stem = cover.out_path("himalayas:https://x.app/" + "a" * 500).name
    assert len(stem) < 120


def test_blockers_are_intersected_with_the_live_profile(store):
    """A stale blocker on the row must not warn if cannot_provide no longer lists it."""
    row = _row(store, _job(), blockers=["video_intro"])
    assert cover.live_blockers(row, FakeProfile()) == []


def test_blockers_still_warn_when_the_profile_declares_them(store):
    row = _row(store, _job(), blockers=["work_samples"])
    assert cover.live_blockers(row, FakeProfile()) == ["work_samples"]
