"""Tests for the two drafting triggers and the store-wide backfill.

The rule these pin down: a job gets a letter when its score clears its OWN
board's `draft_at`, or when the advert asks for a letter and the score clears
that board's lower `draft_if_asked_at`. Both bars are per board, because a
score means nothing across boards -- the same governing rule as everywhere
else in this repo.
"""
from __future__ import annotations

import pathlib
import sys
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import cover  # noqa: E402


def board(name="himalayas", draft_at=70, draft_if_asked_at=60):
    return types.SimpleNamespace(
        name=name, draft_at=draft_at, draft_if_asked_at=draft_if_asked_at
    )


class FakeProfile:
    def __init__(self, **boards):
        self._boards = boards

    def board(self, name):
        return self._boards[name]


# --- the detector ----------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Please send a cover letter with your CV.",
    "Submit your resume and a covering letter.",
    "Include a letter of motivation.",
    "Attach a letter of interest explaining your fit.",
    "We want a resume. A cover letter helps you stand out.",
])
def test_an_advert_that_asks_is_detected(text):
    assert cover.asks_for_cover_letter(text) is True


@pytest.mark.parametrize("text", [
    # Negation before the phrase.
    "No cover letter needed, just your GitHub.",
    "Do not send a cover letter.",
    "Send a portfolio instead of a cover letter.",
    "Apply without a cover letter.",
    # Negation after it.
    "Cover letters are not required for this role.",
    "A cover letter is not necessary.",
    "A cover letter is optional but welcome.",
])
def test_an_advert_that_declines_one_is_not_a_match(text):
    """"No cover letter needed" is a real sentence that means the opposite.

    Treating it as a request is worse than missing a genuine ask: it spends a
    long call producing a document the employer said not to send.
    """
    assert cover.asks_for_cover_letter(text) is False


@pytest.mark.parametrize("text", [None, "", "   ", "Remote first, great team, strong benefits."])
def test_silence_is_not_an_ask(text):
    assert cover.asks_for_cover_letter(text) is False


def test_known_miss_a_double_negative_reads_as_a_refusal():
    """A documented false negative, asserted so it cannot change silently.

    "Applications without a cover letter will not be read" is a REQUIREMENT,
    but "without" sits right before the phrase and the heuristic reads it as a
    refusal. Fixing it properly means parsing the second negation ("will not
    be read") and inverting, which is more machinery than a 1.5%-frequency
    signal is worth.

    The direction of the error is the reason this is acceptable: a miss means
    the letter is not auto-drafted, and `py cover.py <uid>` still drafts it on
    request. The opposite error -- writing a letter an advert explicitly said
    not to send -- is the expensive one, and that is the case the negation
    check exists to prevent.
    """
    text = "Applications without a cover letter will not be read."
    assert cover.asks_for_cover_letter(text) is False


def test_a_negation_in_a_different_sentence_does_not_carry_over():
    """Clause-scoped, deliberately.

    "We do not offer visa sponsorship. Please send a cover letter." must still
    be a match: the "not" belongs to the sentence before it.
    """
    text = "We do not offer visa sponsorship. Please send a cover letter."
    assert cover.asks_for_cover_letter(text) is True


# --- the two triggers ------------------------------------------------------

def test_score_at_the_board_bar_qualifies():
    reason = cover.draft_reason(70, board(draft_at=70), "nothing relevant here")
    assert reason and "draft_at 70" in reason


def test_score_below_the_bar_without_an_ask_does_not():
    assert cover.draft_reason(69, board(draft_at=70), "nothing relevant here") is None


def test_an_ask_lowers_the_bar_but_does_not_remove_it():
    """The second trigger is a lower bar, not the absence of one.

    An advert scoring 20 that demands a cover letter is not an application
    anyone is going to send, and drafting it spends a long call on a file
    nobody opens.
    """
    asks = "Please include a cover letter."
    assert cover.draft_reason(65, board(draft_at=70, draft_if_asked_at=60), asks)
    assert cover.draft_reason(20, board(draft_at=70, draft_if_asked_at=60), asks) is None


def test_the_bars_are_per_board():
    """The governing rule, asserted rather than assumed.

    The same score and the same advert must be able to qualify on one board
    and not on another, because the two boards' scores were never on one scale.
    """
    text = "Please include a cover letter."
    generous = board(name="onlinejobs", draft_at=70, draft_if_asked_at=60)
    strict = board(name="himalayas", draft_at=90, draft_if_asked_at=85)
    assert cover.draft_reason(72, generous, text)
    assert cover.draft_reason(72, strict, text) is None


def test_an_unscored_job_never_qualifies():
    assert cover.draft_reason(None, board(), "Please include a cover letter.") is None


# --- the backfill ----------------------------------------------------------

def _row(uid, score, title="Backend Engineer", company="Acme",
         source="himalayas", description="a real advert"):
    return {
        "uid": uid, "score": score, "title": title, "company": company,
        "source": source, "description": description,
    }


def test_backfill_picks_qualifying_jobs_without_a_letter(tmp_path, monkeypatch):
    monkeypatch.setattr(cover.config, "COVER_DIR", str(tmp_path))
    profile = FakeProfile(himalayas=board())
    rows = [_row("a", 80), _row("b", 40, title="Junior Dev")]

    picked = cover.select_backfill(rows, profile)
    assert [r["uid"] for r, _ in picked] == ["a"]


def test_backfill_skips_a_job_that_already_has_a_letter(tmp_path, monkeypatch):
    monkeypatch.setattr(cover.config, "COVER_DIR", str(tmp_path))
    cover.out_path("a").write_text("already drafted", encoding="utf-8")
    profile = FakeProfile(himalayas=board())

    assert cover.select_backfill([_row("a", 80)], profile) == []


def test_a_reposting_whose_twin_has_a_letter_is_not_drafted_again(tmp_path, monkeypatch):
    """The Spotter Labs case, which cost two API calls for one advert.

    Himalayas listed one job under two uids -- same company, same title, same
    4215-character description -- and the letter landed on only one of them.
    Checking "does THIS uid have a file" would draft the twin and bill twice
    for a single job, so the check is per (source, title, company).
    """
    monkeypatch.setattr(cover.config, "COVER_DIR", str(tmp_path))
    cover.out_path("first").write_text("already drafted", encoding="utf-8")
    profile = FakeProfile(himalayas=board())
    rows = [
        _row("first", 72, title="Accounting Automation Engineer", company="Spotter Labs"),
        _row("second", 72, title="Accounting Automation Engineer", company="Spotter Labs"),
    ]

    assert cover.select_backfill(rows, profile) == []


def test_two_undrafted_copies_of_one_advert_yield_one_draft(tmp_path, monkeypatch):
    monkeypatch.setattr(cover.config, "COVER_DIR", str(tmp_path))
    profile = FakeProfile(himalayas=board())
    rows = [_row("first", 72), _row("second", 72)]

    picked = cover.select_backfill(rows, profile)
    assert len(picked) == 1


def test_a_board_with_no_policy_is_skipped_not_crashed(tmp_path, monkeypatch):
    """An unconfigured board must not take down a backfill of the others.

    `targeting.load()` already refuses to start with a board missing from
    `profile.yaml`, so this is the belt to that braces -- a source registered
    after the profile was written must not raise mid-run.
    """
    monkeypatch.setattr(cover.config, "COVER_DIR", str(tmp_path))
    profile = FakeProfile(himalayas=board())
    rows = [_row("a", 80), _row("b", 80, source="brandnewboard")]

    picked = cover.select_backfill(rows, profile)
    assert [r["uid"] for r, _ in picked] == ["a"]
