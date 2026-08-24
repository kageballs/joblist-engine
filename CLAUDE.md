# CLAUDE.md

## Commands

```bash
pip install -r requirements.txt

py main.py                  # full run: fetch, filter, score, write digest
py main.py --no-llm         # deterministic filters only, no API key needed
py main.py --dry-run        # persist nothing, print the digest to stdout
py main.py --explain        # every rejection and its reason
py main.py --since 72h      # override the watermark

py -m pytest -m "not live"  # offline, runs against fixtures/
py -m pytest -m live        # hits the real feed, asserts shape only
py -m ruff check .
```

Use `py`, not `python`. On this machine `python` resolves to the Microsoft
Store alias stub at `%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe` and does
nothing. CI uses `python -m` because runners have no `py` launcher.

Progress goes to stderr, results to stdout, the digest to `data/digest/`.

## Architecture

```
main.py
  -> sources/himalayas.py   cursor pagination, watermark cutoff
  -> filters.py             6 deterministic stages, ~99% eliminated here
  -> scorer.py              ONE batched Claude call over the survivors
  -> digest.py              markdown
  -> store.py               SQLite: seen, watermark, runs, rejects
```

### The load-bearing idea

Himalayas publishes `locationRestrictions`, `timezoneRestrictions` and salary
as structured fields. Measured 2026-08-24 on a 300-job sample: region plus
timezone alone remove **97.7%**, for zero tokens. The model never sees them.

So the model is a ranker, not a filter. It reads what fields cannot express —
a listing with empty `locationRestrictions` whose description is Spanish and
LATAM-only, or a ZAR salary quoted in prose that no currency field captured.

Do not move work from `filters.py` into `scorer.py`. That is the mistake v1
made, and it is what made v1 both expensive and useless.

### `filters.py`

Six stages, ordered cheapest-and-most-eliminating first: `expired`, `region`,
`timezone`, `employer`, `salary`, `role`. Each returns `None` to pass or a
short reason to reject. Order is deliberate — `region` must report before
`role` so the funnel attributes cuts to the real cause.

**The salary stage is tri-state and must stay that way.** Most listings state
no salary. `unknown` passes through to scoring. Rejecting on unknown is the
single most likely way this tool ships and then silently returns nothing
forever, which looks identical to a quiet market.

`eligibility_sentences()` extracts hiring restrictions from anywhere in the
description, because they live in the boilerplate at the *bottom* of an ad,
which head-truncation eats.

### `scorer.py`

One `messages.create` per batch of `SCORE_BATCH_SIZE`. No transcript, no
accumulation. The `system=` block carries rubric + resume + targeting and is
marked `cache_control: ephemeral`, so it must be **byte-identical** across
batches and runs. Never interpolate a timestamp, run id, or set iteration into
it — one varying byte silently restores the full per-call cost.

Results are keyed by the `i` the model echoes back, never by array position.
Missing entries become explicit `unscored` rows; they are never dropped.
`stop_reason == "max_tokens"` bisects the batch and retries.

### `store.py`

Jobs are marked seen **after** filtering, not at fetch. Marking at fetch makes
`seen` a one-way ratchet: widen a rule later and everything it wrongly
rejected is already suppressed forever. Rejects are logged with the killing
stage so a filter change can be replayed against real history.

Every scored survivor is stored regardless of score. `display_threshold` is a
rendering concern only — absolute model scores drift with prompt and model
version, so filtering at write time would make history incomparable.

The watermark is the last successful run, clamped by `MIN/MAX_LOOKBACK_HOURS`.
Never reintroduce a fixed lookback window: v1 had `MAX_POST_AGE_HOURS = 1` on a
daily schedule and therefore saw 1/24th of the board.

## Privacy split

The repo is the engine. `profile.yaml` is the person, and it is gitignored.

Never commit: the rate floor, the employer blocklist, the target-employer list,
or the resume. `profile.example.yaml` plus `examples/resume.example.md` are a
deliberately fictional persona so the repo runs out of the box and the
committed sample digest contains no real data.

Reject rows carry reasons like `blocklisted employer (X)`. They stay local. If
a dashboard is ever added, rejects must not be pushed — the blocklist would be
inferable from them.

## Key files not in the repo

| File | Purpose |
|------|---------|
| `.env` | `ANTHROPIC_API_KEY=sk-ant-...` |
| `profile.yaml` | targeting rules: regions, timezone, rate floor, blocklist |
| `data/resume.md` | the CV the scorer reads |
| `data/joblist.sqlite3` | seen ids, watermark, run history, rejects |
| `data/digest/*.md` | daily output |
| `data/cv/` | the CV builder (`node build.js`) — unrelated to this tool |

`data/resume.md` is **generated** by `data/cv/build.js`. Do not hand-edit it;
the next CV build overwrites it. Edit `data/cv/content.js` instead.

## Adding a source

Implement `fetch(since) -> Iterator[Job]` and set `regions_authoritative`.

Set it `False` when the source's region field cannot be trusted to reject on.
We Work Remotely marks jobs `<region>Anywhere in the World</region>` whose
descriptions say *"only able to hire employees residing in British Columbia or
Ontario"* — rejecting on that field would be fine, but *passing* on it means
the model must read the description instead.

Commit a fixture under `fixtures/` and a parse test. Parse functions must
return `None` on unusable input, never raise: Himalayas deprecated `offset`
on 2026-08-21 with no notice, so upstream drift is expected.
