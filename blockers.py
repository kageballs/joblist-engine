"""What a posting demands that this candidate cannot hand over.

Split deliberately from the scorer. The model reads each ad and reports what it
ASKS FOR; this module decides what that costs, by intersecting those asks with
the `cannot_provide` list in profile.yaml.

Keeping the halves apart buys three things:

* The personal list never goes to the API. The scorer already carries a resume,
  but "here is what I cannot produce" is a different kind of disclosure, and it
  does not need to be made to rank a job.
* Editing the list re-flags the entire stored history immediately. Nothing is
  re-scored, no tokens are spent, and the cached system block is untouched --
  it would be invalidated by a single changed byte if the list lived in it.
* Detection stays separable from penalty. Absolute model scores drift with
  prompt and model version (see store.py), so a score the model had already
  docked would be impossible to re-derive later. Here `score_raw` is what the
  model said and `score` is what the penalty made of it, and the arithmetic
  between them is visible and re-runnable.

A blocker only ever lowers a score. It never rejects: a listing can restate a
"requirement" it does not enforce, and the candidate is the one who knows
which. Set `blocker_penalty` high to bury them instead.
"""

from __future__ import annotations

import config


def _kind(entry) -> str | None:
    """The vocabulary key from one model-returned requirement, or None."""
    if not isinstance(entry, dict):
        return None
    kind = entry.get("kind")
    if not isinstance(kind, str):
        return None
    kind = kind.strip()
    # The tool schema constrains this to the enum, but a model can still answer
    # off-menu, and an unknown key silently matching nothing is worse than
    # dropping it visibly at the one place that knows the vocabulary.
    return kind if kind in config.REQUIREMENT_KINDS else None


def requirements_of(result: dict) -> list[dict]:
    """Normalised requirement rows from one score result.

    Never raises and never returns junk: this reads model output, which is
    schema-constrained but not guaranteed.
    """
    rows = []
    seen = set()
    for entry in result.get("requirements") or []:
        kind = _kind(entry)
        if kind is None or kind in seen:
            continue
        seen.add(kind)
        detail = entry.get("detail")
        rows.append({
            "kind": kind,
            "mandatory": bool(entry.get("mandatory")),
            "detail": (str(detail).strip()[:200] if detail else ""),
        })
    return rows


def evaluate(result: dict, profile) -> dict:
    """Annotate one score result in place-ish, returning a new dict.

    Adds:
      requirements  normalised rows, always a list
      blockers      the mandatory ones the candidate cannot supply
      manual_steps  ones they CAN supply, but only after doing some work
      score_raw     what the model said, before any penalty
      score         score_raw minus the penalty, floored at 0
    """
    out = dict(result)
    rows = requirements_of(result)
    out["requirements"] = rows

    cannot = profile.cannot_provide
    # Only a MANDATORY ask can block. A "nice to have" portfolio is a reason to
    # mention the gap in a cover note, not a reason to score the job down --
    # so those are reported separately rather than folded in or dropped.
    blockers = [r["kind"] for r in rows if r["mandatory"] and r["kind"] in cannot]
    out["blockers"] = blockers
    out["soft_blockers"] = [
        r["kind"] for r in rows if not r["mandatory"] and r["kind"] in cannot
    ]

    # A third category, and deliberately NOT priced. A posting that wants a
    # video intro or a test task is not worth fewer points: the work is
    # perfectly winnable, it just cannot be applied to in one sitting. Pricing
    # it would bury exactly the jobs that are worth the extra hour, so this
    # only ever surfaces a checklist. Nothing below this line may read it.
    out["manual_steps"] = [
        r["kind"] for r in rows if r["kind"] in profile.needs_manual_step
    ]

    raw = result.get("score")
    out["score_raw"] = raw
    if raw is None or not blockers:
        out["score"] = raw
        return out

    # Per distinct blocker, not a flat fee: two unmeetable conditions really is
    # worse than one. Floored at 0 rather than going negative, because the
    # digest and dashboard both render this as a 0-100 band.
    penalty = profile.blocker_penalty * len(blockers)
    out["score"] = max(0, int(raw) - penalty)
    return out


def apply(results: list[dict], profile) -> list[dict]:
    """Annotate a whole batch of score results."""
    return [evaluate(r, profile) for r in results]


def describe(kinds) -> str:
    """Human phrasing for a set of requirement keys, for digests and cards."""
    return ", ".join(config.REQUIREMENT_KINDS.get(k, k) for k in kinds)
