"""Tests for push.py, concentrated on the one rule that must never slip.

Cover letters are written from the resume and speak in Gus's own voice about
his own history. `push.py --with-covers` is the only thing that moves them off
this machine, and its target is a command-line flag that changes between
invocations -- so the localhost check is the whole of the protection. A bug in
it is not a wrong number on a dashboard, it is a personal document on the
public internet.

The hostile cases below are the point of the file. `"127.0.0.1" in url` and
`url.startswith("http://localhost")` both pass the friendly cases and fail
these.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import cover  # noqa: E402
import push  # noqa: E402


@pytest.mark.parametrize("url", [
    "http://localhost:8787",
    "http://localhost:8787/",
    "http://127.0.0.1:8787",
    "http://127.0.0.1",
    "https://localhost:8787/ingest/covers",
    "http://[::1]:8787",
    "HTTP://LOCALHOST:8787",
])
def test_local_targets_are_allowed(url):
    assert push.is_local(url) is True


@pytest.mark.parametrize("url", [
    # The deployed dashboard. The case this exists to stop.
    "https://joblist.kagedev.workers.dev",
    # Substring traps: every one of these contains "localhost" or "127.0.0.1"
    # somewhere, and every one of them resolves off this machine.
    "http://127.0.0.1.evil.example",
    "http://localhost.evil.example",
    "https://evil.example/?host=127.0.0.1",
    "https://evil.example/#localhost",
    "https://evil.example/localhost",
    # Userinfo: the hostname is evil.example, not the part before the @.
    "http://localhost@evil.example",
    "http://127.0.0.1@evil.example:8787",
    # A bare LAN address is still another machine on the network.
    "http://192.168.1.14:8787",
    "http://0.0.0.0:8787",
])
def test_remote_targets_are_refused(url):
    assert push.is_local(url) is False


@pytest.mark.parametrize("url", ["", None, "not a url", "://", "ftp://"])
def test_unparseable_targets_are_refused(url):
    """Anything we cannot read as a local host is treated as remote.

    Failing closed matters more here than accepting an odd but harmless URL:
    the cost of a false negative is a refused push, and the cost of a false
    positive is a published letter.
    """
    assert push.is_local(url) is False


def test_collect_covers_reads_through_the_same_slug_as_cover(tmp_path, monkeypatch):
    """The letter on disk must be found by the uid, not by a parallel scheme.

    cover.py slugifies a uid (a Himalayas uid is a full URL, unusable as a
    Windows filename) and appends a hash. If push.py grew its own naming rule
    the two would drift and letters would silently stop being found.
    """
    monkeypatch.setattr(push.config, "COVER_DIR", str(tmp_path))
    monkeypatch.setattr(cover.config, "COVER_DIR", str(tmp_path))

    uid = "himalayas:https://himalayas.app/companies/acme/jobs/backend"
    cover.out_path(uid).write_text("# Draft\n\nDear Acme,\n", encoding="utf-8")

    rows = push.collect_covers([uid])
    assert len(rows) == 1
    assert rows[0]["uid"] == uid
    assert "Dear Acme," in rows[0]["body"]
    assert rows[0]["drafted_at"].endswith("+00:00")


def test_collect_covers_skips_missing_and_empty(tmp_path, monkeypatch):
    """An empty file is not a letter.

    cover.py already refuses to write one, because a draft file's existence is
    what marks a job as done. Should one exist anyway, pushing it would put a
    blank "letter drafted" marker on a card that has nothing behind it.
    """
    monkeypatch.setattr(push.config, "COVER_DIR", str(tmp_path))
    monkeypatch.setattr(cover.config, "COVER_DIR", str(tmp_path))

    written = "himalayas:https://himalayas.app/companies/acme/jobs/one"
    blank = "himalayas:https://himalayas.app/companies/acme/jobs/two"
    absent = "himalayas:https://himalayas.app/companies/acme/jobs/three"
    cover.out_path(written).write_text("real letter", encoding="utf-8")
    cover.out_path(blank).write_text("   \n\n", encoding="utf-8")

    rows = push.collect_covers([written, blank, absent])
    assert [r["uid"] for r in rows] == [written]
