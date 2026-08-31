"""The deterministic half of the requirements feature.

The split under test: the model reports what a posting ASKS FOR, and this
module decides what that costs against a local list. Tests here assert the
seam holds — that nothing about the candidate's own gaps is needed to read a
posting, and that the penalty stays re-derivable from `score_raw`.
"""

from dataclasses import replace

import pytest

import blockers
import config
import targeting


@pytest.fixture
def profile():
    base = targeting.load("profile.example.yaml")
    return replace(
        base,
        cannot_provide=frozenset({"work_samples", "named_clients"}),
        blocker_penalty=30,
    )


def result(score=70, requirements=None):
    return {
        "i": 0, "score": score, "verdict": "maybe", "why": "x",
        "cv_variant": "engineering", "requirements": requirements or [],
    }


def req(kind, mandatory=True, detail="d"):
    return {"kind": kind, "mandatory": mandatory, "detail": detail}


# --- the penalty -----------------------------------------------------------

def test_mandatory_unmeetable_ask_docks_the_score(profile):
    out = blockers.evaluate(result(70, [req("work_samples")]), profile)
    assert out["blockers"] == ["work_samples"]
    assert out["score_raw"] == 70
    assert out["score"] == 40


def test_penalty_is_per_blocker_not_a_flat_fee(profile):
    out = blockers.evaluate(
        result(90, [req("work_samples"), req("named_clients")]), profile
    )
    assert sorted(out["blockers"]) == ["named_clients", "work_samples"]
    assert out["score"] == 30


def test_score_floors_at_zero_never_negative(profile):
    """The digest and dashboard both render this as a 0-100 band."""
    out = blockers.evaluate(result(10, [req("work_samples")]), profile)
    assert out["score"] == 0


def test_raw_score_is_preserved_so_the_penalty_stays_re_derivable(profile):
    """Model scores drift with prompt and model version, so a score that had
    already been docked could not be recomputed later."""
    out = blockers.evaluate(result(70, [req("named_clients")]), profile)
    assert out["score_raw"] == 70 and out["score"] == 40


def test_unscored_job_is_left_alone(profile):
    out = blockers.evaluate(result(None, [req("work_samples")]), profile)
    assert out["score"] is None and out["score_raw"] is None


# --- what must NOT block ---------------------------------------------------

def test_a_preference_never_blocks(profile):
    """"A portfolio is a plus" is a cover-note line, not a closed door.

    This is the asymmetry that keeps the feature safe: over-calling mandatory
    removes jobs the candidate could have taken.
    """
    out = blockers.evaluate(
        result(70, [req("work_samples", mandatory=False)]), profile
    )
    assert out["blockers"] == []
    assert out["soft_blockers"] == ["work_samples"]
    assert out["score"] == 70


def test_asks_the_candidate_can_meet_do_not_block(profile):
    out = blockers.evaluate(result(70, [req("public_code"), req("references")]), profile)
    assert out["blockers"] == []
    assert out["score"] == 70


def test_blockers_lower_but_never_reject(profile):
    """A listing can restate a requirement it does not enforce; only the
    candidate knows which. Nothing here removes a job."""
    out = blockers.evaluate(result(100, [req("work_samples")]), profile)
    assert out["score"] > 0
    assert "rejected" not in out


# --- reading untrusted model output ----------------------------------------

def test_unknown_kind_is_dropped_not_matched(profile):
    """Off-menu keys are dropped at the one place that knows the vocabulary."""
    out = blockers.evaluate(result(70, [req("portfolio_screenshotz")]), profile)
    assert out["requirements"] == []
    assert out["score"] == 70


@pytest.mark.parametrize("junk", [
    None, [], "not a list", [None], ["string"], [{}], [{"kind": None}], [{"mandatory": True}],
])
def test_malformed_requirements_never_raise(profile, junk):
    out = blockers.evaluate(result(70, junk), profile)
    assert out["requirements"] == [] and out["score"] == 70


def test_duplicate_kinds_collapse(profile):
    """Otherwise one posting repeating itself would be penalised twice."""
    out = blockers.evaluate(
        result(90, [req("work_samples"), req("work_samples")]), profile
    )
    assert out["blockers"] == ["work_samples"]
    assert out["score"] == 60


def test_detail_is_clipped(profile):
    out = blockers.evaluate(result(70, [req("public_code", detail="x" * 500)]), profile)
    assert len(out["requirements"][0]["detail"]) == 200


# --- the seam itself -------------------------------------------------------

def test_empty_cannot_provide_blocks_nothing(profile):
    empty = replace(profile, cannot_provide=frozenset())
    out = blockers.evaluate(result(70, [req("work_samples")]), empty)
    assert out["blockers"] == [] and out["score"] == 70


def test_editing_the_list_reflags_without_rescoring(profile):
    """The point of keeping the list out of the prompt: the same stored model
    output re-evaluates against a changed profile, for free."""
    stored = result(70, [req("certification")])
    assert blockers.evaluate(stored, profile)["score"] == 70

    widened = replace(profile, cannot_provide=profile.cannot_provide | {"certification"})
    assert blockers.evaluate(stored, widened)["score"] == 40


def test_the_prompt_does_not_depend_on_what_the_candidate_lacks(profile):
    """The invariant is independence, not absence of the words.

    Every vocabulary key appears in the prompt by design — the model needs the
    whole menu to pick from. What must not appear is WHICH of them this person
    cannot supply. Asserting the prompt is byte-identical across two different
    `cannot_provide` sets tests exactly that, and simultaneously proves editing
    the list cannot invalidate the cache.
    """
    import scorer
    a = replace(profile, cannot_provide=frozenset({"work_samples"}))
    b = replace(profile, cannot_provide=frozenset(config.REQUIREMENT_KINDS))
    assert scorer.build_system(a)[0]["text"] == scorer.build_system(b)[0]["text"]
    assert "cannot_provide" not in scorer.build_system(a)[0]["text"]


def test_system_block_is_byte_identical_across_calls(profile):
    """Prompt caching depends on it, and the new vocabulary block is built
    from a dict — sorted() rather than dict order."""
    import scorer
    assert scorer.build_system(profile)[0]["text"] == scorer.build_system(profile)[0]["text"]


def test_every_vocabulary_key_has_a_meaning():
    for key, meaning in config.REQUIREMENT_KINDS.items():
        assert key.islower() and " " not in key
        assert meaning and meaning[0].islower()
