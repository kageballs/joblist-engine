"""One batched Claude call per group of survivors.

This replaces v1's agentic loop, which asked a model to execute a fixed
sequence -- call search once, then score each job, then compile -- and paid
for the privilege. Worse, every per-job result was appended to a growing
transcript that was resent on each turn, so cost scaled quadratically with the
number of jobs and a long run could truncate the final digest into nothing.

Here there is no transcript. Each batch is an independent single-turn request.
The system block is byte-identical across batches and across runs so prompt
caching makes the resume nearly free after the first call.
"""

from __future__ import annotations

import json
import sys

import anthropic

import config

RUBRIC = """You rank remote job listings for one candidate. You are the last
stage of a pipeline: hard eligibility, salary floor and timezone have already
been checked deterministically. Do not re-litigate them. Judge fit.

Score 0-100 on how well this candidate would do in the role AND how well the
role serves the candidate:

  85-100  strong   directly matches their stack and seniority, worth applying today
  60-84   maybe    plausible, some gap in stack, seniority or domain
  0-59    no       wrong discipline, wrong level, or the role would waste them

Weigh these down:
  - Listings restricted to the candidate's own country. These usually come
    from local-market employers paying local rates, which is the exact thing
    this candidate is trying to avoid. Say so in `concerns`.
  - Body shops, outsourcing intermediaries and agencies hiring "for our client".
  - Roles far below their seniority.

Weigh these up:
  - No hiring-location restriction at all, which means genuinely worldwide.
  - Employers who state location-independent pay.
  - Work matching the candidate's demonstrated impact, not just their keywords.

`why` is one sentence, concrete, naming the specific overlap or gap. Never
generic praise. `cv_variant` picks which CV to send: "automation" for
AI/automation, solutions or integration engineering posts, "engineering" for
everything else."""

TOOL = {
    "name": "submit_scores",
    "description": "Return one entry for every job in the batch.",
    "input_schema": {
        "type": "object",
        "properties": {
            "matches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "i": {"type": "integer", "description": "The [n] index from the batch"},
                        "score": {"type": "integer", "minimum": 0, "maximum": 100},
                        "verdict": {"type": "string", "enum": ["strong", "maybe", "no"]},
                        "why": {"type": "string"},
                        "matched_skills": {"type": "array", "items": {"type": "string"}},
                        "concerns": {"type": "array", "items": {"type": "string"}},
                        "cv_variant": {"type": "string", "enum": ["engineering", "automation"]},
                    },
                    "required": ["i", "score", "verdict", "why", "cv_variant"],
                },
            }
        },
        "required": ["matches"],
    },
}


def build_system(profile) -> list[dict]:
    """The cached prefix.

    Must be deterministic: no timestamps, no run ids, no set iteration order.
    A single varying byte invalidates the cache and quietly restores the
    per-call cost of resending the whole resume.
    """
    targeting = "\n".join(
        [
            "CANDIDATE TARGETING",
            f"Based in: {profile.based_in} (UTC{profile.utc_offset:+d})",
            f"Asking rate: {profile.target_hourly_usd:.0f} USD/hour. Will not go below {profile.absolute_floor_hourly_usd:.0f}.",
            "Target roles: " + ", ".join(p.pattern for p in profile.role_include),
            "Treats as local-rate anchored: " + ", ".join(sorted(profile.local_anchor_regions)),
        ]
    )
    body = "\n\n".join([RUBRIC, targeting, "CANDIDATE RESUME\n" + profile.resume])
    return [{"type": "text", "text": body, "cache_control": {"type": "ephemeral"}}]


def _clip(text: str, head: int = 800, tail: int = 400) -> str:
    """Keep both ends of a description.

    Eligibility restrictions live in the boilerplate at the bottom of an ad,
    so head-only truncation removes exactly the sentences that decide whether
    the candidate can be hired at all.
    """
    text = (text or "").strip()
    if len(text) <= head + tail:
        return text
    return text[:head] + "\n[...]\n" + text[-tail:]


def render_batch(verdicts) -> str:
    lines = []
    for i, v in enumerate(verdicts):
        job = v.job
        regions = ", ".join(job.location_restrictions) or "none stated (worldwide)"
        salary = "not stated"
        annual = job.annual_usd_max()
        if annual:
            salary = f"up to {annual:,} USD/yr"
        block = [
            f"[{i}] {job.title}",
            "  company: {}".format(job.company or "unknown"),
            f"  hiring regions: {regions}",
            f"  salary: {salary} ({v.salary_signal})",
        ]
        if v.local_anchor:
            block.append("  NOTE: restricted to the candidate's own country")
        if v.eligibility_notes:
            block.append("  eligibility text found: " + " | ".join(v.eligibility_notes))
        block.append("  description: " + _clip(job.description or job.excerpt))
        lines.append("\n".join(block))
    return "\n\n".join(lines)


def _call(client, model, system, batch, max_tokens):
    return client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        tools=[TOOL],
        tool_choice={"type": "tool", "name": "submit_scores"},
        messages=[{"role": "user", "content": render_batch(batch)}],
    )


def score_batch(client, model, system, batch) -> dict[int, dict]:
    """Score one batch. Returns index -> result, keyed by position in `batch`.

    Keyed by the index the model echoes back, never by array position, so a
    short or reordered response cannot silently shift every result onto the
    wrong job.
    """
    max_tokens = len(batch) * config.TOKENS_PER_JOB + 500
    resp = _call(client, model, system, batch, max_tokens)

    if resp.stop_reason == "max_tokens":
        if len(batch) == 1:
            print("[scorer] single job overran max_tokens; skipping", file=sys.stderr)
            return {}
        mid = len(batch) // 2
        print(
            f"[scorer] hit max_tokens on {len(batch)} jobs, bisecting",
            file=sys.stderr,
        )
        left = score_batch(client, model, system, batch[:mid])
        right = score_batch(client, model, system, batch[mid:])
        return {**left, **{k + mid: v for k, v in right.items()}}

    out: dict[int, dict] = {}
    for block in resp.content:
        if getattr(block, "type", None) != "tool_use":
            continue
        for match in (block.input or {}).get("matches", []):
            try:
                idx = int(match["i"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= idx < len(batch):
                out[idx] = match

    usage = getattr(resp, "usage", None)
    if usage is not None:
        cached = getattr(usage, "cache_read_input_tokens", 0) or 0
        print(
            f"[scorer] {len(batch)} jobs, {usage.input_tokens} in / {usage.output_tokens} out, {cached} cached",
            file=sys.stderr,
        )
    return out


def score(verdicts, profile, api_key, model=None):
    """Score every survivor. Returns a list parallel to `verdicts`.

    Any job the model omits comes back as an explicit unscored entry rather
    than being dropped -- a silently missing job is worse than a visible null.
    """
    if not verdicts:
        return []

    model = model or config.SCORING_MODEL
    client = anthropic.Anthropic(api_key=api_key)
    system = build_system(profile)

    results: list[dict | None] = [None] * len(verdicts)
    size = config.SCORE_BATCH_SIZE
    for start in range(0, len(verdicts), size):
        batch = verdicts[start : start + size]
        scored = score_batch(client, model, system, batch)
        for local_idx, payload in scored.items():
            results[start + local_idx] = payload

    for i, payload in enumerate(results):
        if payload is None:
            results[i] = {
                "i": i,
                "score": None,
                "verdict": "unscored",
                "why": "model returned no entry for this job",
                "matched_skills": [],
                "concerns": [],
                "cv_variant": "engineering",
            }
    return results


def dump(results) -> str:
    return json.dumps(results, indent=2)
