# CLAUDE.md

## Commands

```bash
pip install -r requirements.txt

py main.py                  # full run: fetch, filter, score, write digest
py main.py --no-llm         # deterministic filters only, no API key needed
py main.py --dry-run        # persist nothing, print the digest to stdout
py main.py --explain        # every rejection and its reason
py main.py --since 72h      # override the watermark
py main.py --source onlinejobs   # one source only
py main.py --rescore        # re-evaluate jobs already marked seen

py report.py                # what employers keep asking you to produce
py report.py --blocked      # listings you cannot currently apply to
py report.py --detail work_samples   # the actual sentences
py report.py --reapply      # re-price stored scores after editing the list

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
  -> sources/onlinejobs.py  offset pagination, scraped HTML, salary normaliser
  -> filters.py             6 deterministic stages, ~99% eliminated here
  -> source.hydrate()       full advert text, SURVIVORS ONLY
  -> scorer.py              ONE batched Claude call over the survivors
  -> blockers.py            local: what you cannot supply, and what it costs
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

### `sources/onlinejobs.py` inverts that idea, and pays for itself differently

OnlineJobs.ph is the Philippine domestic board, so **region and timezone reject
nothing there** — every listing hires Filipinos and so does the profile. The two
stages that do 97.7% of the free work on Himalayas are inert, which leaves
**salary as the only load-bearing filter this source has**.

And OLJ states pay as an unvalidated text box: 60+ distinct formats over 113
listings (`$500`, `TBD`, `300usd/month`, `6$`, `5hrly`, `4 aud/hr`,
`PHP 350 - 500 PER HOUR`, `?`). `annual_usd_max()` reads none of it, so without
`parse_salary()` every listing arrives as salary `unknown` — correct per the
tri-state rule, and ruinous, because ~96% of that board pays under the floor.

`parse_salary()` normalises to annual USD inside the source, which is the only
shape `annual_usd_max()` accepts without a currency or period to interpret.
Two rules carry it, and both are load-bearing:

- **Currency codes match without ``.** `25,000PHP` and `PHP100k` are both
  real, and a digit-to-letter transition is not a word boundary — `php`
  misses both and reads them as dollars, overstating a wage ~58x.
- **An unstated period is read the way most generous to the listing.** That is
  what makes rejecting on it sound: if the *best* reading is under the floor,
  every reading is. Under $200 reads hourly, $200+ reads monthly ($200/hr is
  $416k/yr on a VA board — nobody means that).

The stated pay string is also prepended to the description, because
`parse_salary()` must commit to one reading of an ambiguous figure, and only the
model can contradict it.

Measured 2026-08-30: **82 fetched -> 61 rejected on salary for zero tokens.**

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

### `hydrate()` — full text, survivors only

Some feeds publish a teaser, not an advert. OnlineJobs.ph cards carry ~280
characters cut off mid-sentence at "See More", and everything that decides
applicability — the must-have list, the equipment demands, the hiring
restrictions `eligibility_sentences()` exists to find — is only on the job's
own page.

So a source may implement `hydrate(job) -> Job`, called by `main.py` **after
the funnel and before scoring**. That ordering is the whole point: hydrating at
fetch time would spend a request on every listing the salary floor throws away
(at OLJ's 5s crawl delay, 20+ minutes to re-read jobs we reject for free),
while not hydrating at all means the model ranks a job on a truncated sentence.
Measured 2026-08-31: 53 survivors out of 177 fetched.

`hydrate()` must never raise. A thinner description scores worse; an exception
loses the run.

### Requirements and blockers — `scorer.py` reports, `blockers.py` prices

What a posting demands an applicant PRODUCE — a portfolio, a video intro, an
unpaid test task, a certification — is prose, so the model extracts it against
the fixed vocabulary in `config.REQUIREMENT_KINDS`.

**The vocabulary is fixed on purpose.** The point is counting across months of
listings; in free text, "screenshots of your GHL builds" and "portfolio of
GoHighLevel work" are two rows and the tally is worthless.

**`cannot_provide` never goes in the prompt.** The model says what each ad asks
for; `blockers.py` intersects that with the personal list in `profile.yaml`.
Three things follow, and all three are load-bearing:

* the list never leaves this machine;
* editing it re-prices the whole stored history with **no re-scoring and no API
  call** (`py report.py --reapply`), which would be impossible if the list were
  in the cached system block — one changed byte invalidates it;
* detection stays separable from penalty. `score_raw` is what the model said,
  `score` is what the penalty made of it. Absolute model scores drift with
  prompt and model version, so a score the model had already docked could never
  be re-derived.

Only a **mandatory** ask blocks. A preference is a cover-note line, not a closed
door, and over-calling mandatory removes jobs that were takeable. And a blocker
**lowers, never rejects** — ads restate requirements they do not enforce, and
only the candidate knows which. Raise `blocker_penalty` to bury them instead.

Measured 2026-08-31, and the split is the useful part: **OnlineJobs.ph asks for
proof of work in 37% of listings, Himalayas in 5%.** A marketplace screens
unknown freelancers; a job board lets the CV do it.

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
| `profile.yaml` | targeting rules: regions, timezone, rate floor, blocklist, `deliverables.cannot_provide` |
| `data/resume.md` | the CV the scorer reads |
| `data/joblist.sqlite3` | seen ids, watermark, run history, rejects |
| `data/digest/*.md` | daily output |
| `data/cv/` | the CV builder (`node build.js`) — unrelated to this tool |

`data/resume.md` is **generated** by `data/cv/build.js`. Do not hand-edit it;
the next CV build overwrites it. Edit `data/cv/content.js` instead.

## Adding a source

Implement `fetch(since) -> Iterator[Job]` and set `regions_authoritative`.
Add it to the `sources` list in `main.py`; that loop isolates each source in its
own try/except, so one feed breaking cannot throw away another's results, and
the run only fails when every source failed.

Set it `False` when the source's region field cannot be trusted to reject on.
We Work Remotely marks jobs `<region>Anywhere in the World</region>` whose
descriptions say *"only able to hire employees residing in British Columbia or
Ontario"* — rejecting on that field would be fine, but *passing* on it means
the model must read the description instead.

Commit a fixture under `fixtures/` and a parse test. Parse functions must
return `None` on unusable input, never raise: Himalayas deprecated `offset`
on 2026-08-21 with no notice, so upstream drift is expected.
