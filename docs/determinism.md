# What is reproducible, and what is not

This is the design decision the rest of the repo is arranged around, so it is
worth stating on its own page rather than leaving it implicit in the code.

Every stage of a run falls into one of two categories: it either returns the
same answer every time for the same input, or it does not. Almost every other
choice here — where the model sits, what is stored, what a flag re-derives
versus refetches, why the personal list never enters a prompt — follows from
keeping those two categories apart and pushing as much work as possible into
the first one.

## The two halves

| | Deterministic | Non-deterministic |
|---|---|---|
| Stages | `sources/*` parsing, `filters.py`, `blockers.py`, `digest.py`, `report.py` | `scorer.py`, `cover.py` |
| Cost | zero | one batched call per run, plus up to `COVER_MAX_PER_RUN` letters |
| Re-running | free, and safe | costs money, and the answer changes |
| Needs a key | no (`--no-llm` runs the whole first half) | yes |

The measured payoff, from a real run on 2026-09-03:

```
2698 fetched  1 already seen  -2279 region  -236 salary  -18 role  164 to score
```

94% of the work was done by the half that costs nothing and never varies. The
model saw 164 of 2,698 listings. That ratio is the reason the split exists.

## What "deterministic" means here, precisely

`filters.evaluate(job, profile, now, source)` is a pure function of its
arguments. It reads no clock of its own — `main.py` passes one `started_at`
for the whole run — consults no global state, and uses no randomness. Given
the same four arguments it returns the same `Verdict` forever.

That matters because it is what makes the funnel line above *evidence*. If the
region stage could return a different answer on a second run, "-2279 region"
would be an anecdote rather than a measurement.

`blockers.evaluate(result, profile)` is deterministic in the same way, but
note what it is deterministic *over*: the requirement rows the model already
returned, plus the current `profile.yaml`. It does not call anything. This is
the hinge the whole design turns on, and the next section is about it.

`digest.py` and `report.py` are pure renderers over stored rows. Nothing is
deleted for being stale or low-scoring — both cuts happen at render time — so
changing a threshold and re-rendering costs nothing and rewrites no history.

## What "non-deterministic" means here, precisely

`scorer.py` and `cover.py` call a language model. No `temperature`, `top_p` or
seed is pinned anywhere in this repo, so both run at the provider's defaults
and **the same input can produce different output on two calls**.

This is not a theoretical caveat. On 2026-09-03 the run drafted letters for
what it believed were two jobs. They were in fact one advert that Himalayas
had listed twice under different ids — same company, same 4,215-character
description, same score, two `uid`s. So the model received effectively
identical input twice, in the same run, seconds apart. It returned:

- once: `NO STRONG MATCH: the resume shows strong platform and workflow
  automation ... but no evidence of accounting or finance process knowledge`
- once: **nothing at all** — an empty response

Both are now handled. Repostings are collapsed on `(source, title, company)`
before the cap is applied, and an empty response is never written to disk.
That second guard matters more than it looks: a draft file's *existence* is
what marks a job already drafted, so writing the empty one would have skipped
that advert on every future run instead of leaving it to try again.

The general rule that incident illustrates: **anything downstream of a model
call must treat a malformed or empty response as expected, not exceptional.**

## Where the boundary is drawn, and why there

The model's job is **detection**. It reads an advert and reports what the
advert *asks the applicant to produce* — work samples, a video intro, a test
task, references.

Deciding what those asks *cost* is local and deterministic. `blockers.py`
intersects them with `cannot_provide` from `profile.yaml` and applies the
penalty arithmetic.

Three things follow from putting the line exactly there, and all three are
load-bearing:

1. **The personal list never enters a prompt.** The scorer already carries a
   resume; "here is what I cannot produce" is a different kind of disclosure
   and is not needed to rank a job.
2. **Editing that list re-prices the entire stored history for free.**
   `py report.py --reapply` re-derives every stored score from its
   `score_raw` and the current list. No API calls, no re-scoring.
3. **The cached prompt prefix stays intact.** `scorer.build_system()` must be
   byte-identical across batches and runs for prompt caching to work. A
   personal list living inside it would invalidate the cache on every edit.

`score_raw` and `score` are stored separately for the same reason: `score_raw`
is what the model said, `score` is what the local penalty made of it, and the
arithmetic between them is visible and re-runnable. A single stored number
that had already been docked could never be re-derived later, because absolute
model scores drift with prompt and model version.

## Practical consequences

**Free to re-run, as often as you like:**

- `py main.py --no-llm` — the entire deterministic funnel, no key required
- re-rendering a digest, or retuning any board's `display_threshold` or
  `max_age_days`
- `py report.py` and `py report.py --blocked`
- `py report.py --reapply` after editing `cannot_provide` or
  `needs_manual_step`

**Costs money and may return a different answer:**

- `py main.py` — one batched scoring call over the survivors
- `py main.py --rescore` — re-scores jobs already seen
- `py cover.py <uid>` and auto-drafting during a run

**Neither, but worth knowing:** `sources/indeed.py` reads a capture file, so it
is deterministic — but the capture itself is a photograph taken by hand in a
browser, and taking a new one is a manual step that no run performs. See
[`indeed-capture.md`](indeed-capture.md).

## The rule for anyone changing this

Do not move work from `filters.py` into `scorer.py`. That was v1's mistake and
it is what made v1 both expensive and useless — it handed the whole job to an
agentic loop that called a scoring tool per listing, grew a transcript it
resent every turn, and never completed a run.

The direction of travel is the other way: when a stage can be made
deterministic, make it deterministic. Every listing eliminated by a field
comparison is a listing you never pay to think about.
