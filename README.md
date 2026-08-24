# joblist

Finds the remote jobs that will actually hire you, and pay.

Job boards are full of listings labelled "remote" that will not hire you. They
want a US resident, or an overlap with Pacific time, or they pay a local band.
Reading past those by hand is the whole cost of a job hunt.

`joblist` pulls the feed, throws away everything you are ineligible for using
the board's own structured fields, and spends a language model only on the
handful left over.

## Why it is built this way

On a measured 300-job sample from Himalayas on 2026-08-24, filtering on hiring
region and required timezone alone removed **97.7%** of listings — for zero
tokens, because those are just fields. A representative run:

```
2400 fetched  -2310 region  -1 employer  -9 salary  -66 role  14 to score
```

So the model is not a filter. It is a ranker for the ~0.6% that survive, and
it reads the parts a field cannot capture: a listing with an empty
`locationRestrictions` whose description turns out to be Spanish-language and
LATAM-only, or a "worldwide" role that is really a staffing agency placement.

That division is the entire design. Deterministic where the data is
trustworthy, model where it demonstrably lies.

### An earlier version of this repo did the opposite

v1 (commit `c143977`) handed the whole job to an agentic loop: a tool the model
had to call exactly once, then a scoring tool it had to call per job, then a
compile tool. That is a `for` loop with an API bill. It also grew a transcript
it resent every turn, so cost scaled quadratically with the number of jobs, and
the final digest could truncate into nothing. It never completed a run.

The rewrite keeps the model and deletes the ceremony.

## Prerequisites

- Python 3.12+
- An Anthropic API key (only for scoring — `--no-llm` runs without one)

## Setup

```bash
pip install -r requirements.txt
cp profile.example.yaml profile.yaml
```

Then edit `profile.yaml`. It holds your timezone, your rate floor, the regions
that can hire you, the titles you want, and any employers you would rather not
see. It is gitignored and never leaves your machine.

Point `scoring.resume_path` at a plain-text or markdown copy of your CV, and
put your key in `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
```

## Usage

```bash
py main.py                  # fetch, filter, score, write today's digest
py main.py --no-llm         # filters only, no API key needed
py main.py --dry-run        # run everything, persist nothing, print to stdout
py main.py --explain        # print every rejection and why
py main.py --since 72h      # override the watermark
py main.py --json           # also emit results as JSON on stdout
py main.py --fast           # score with the cheaper model
```

There is no `--daily` flag. Point your OS scheduler at `py main.py` — the tool
tracks its own watermark from the last successful run, so a missed day is
caught up automatically rather than silently skipped.

## Output

A markdown digest at `data/digest/YYYY-MM-DD.md`:

```markdown
# Job digest — 2026-08-24 09:05 UTC

`244 fetched  -238 region  -1 employer  -3 role  2 to score`

## No matches above 60

2 job(s) were scored but none cleared the threshold:

- **30** (Hudson Manpower) — Backend Developer Level III — Requires 8-10+ years
  of production Kubernetes/Azure work that does not match this profile.

## Near misses

Rejected latest in the chain — the most informative cuts.

- `role` **Operations Coordinator (part-time)** — title matches no target role
- `employer` **Home-Based Accounting Coordinator** — blocklisted employer
- `region` **Founding AI Training Partnerships Manager** — hiring limited to United States
```

The funnel line and the near misses are load-bearing, not decoration. With a
real rate floor, **most days legitimately return nothing**. An empty list with
no explanation reads as a broken tool, and a tool that looks broken stops
getting opened. Showing where the cut happened is usually more useful than the
matches.

## Project structure

```
main.py                CLI and run orchestration
targeting.py           loads profile.yaml
filters.py             the deterministic funnel — the heart of it
scorer.py              one batched Claude call per group of survivors
store.py               SQLite: seen ids, watermark, run history, rejects
digest.py              markdown rendering
sources/
  base.py              Job model and the Source protocol
  himalayas.py         cursor pagination over the Himalayas feed
fixtures/              committed API captures, so tests run offline
tests/
```

### Adding a source

Implement `fetch(since) -> Iterator[Job]` and set `regions_authoritative`.

That flag is the one that matters. Himalayas publishes hiring regions you can
reject on. We Work Remotely does not: a listing marked
`<region>Anywhere in the World</region>` was found to say, in its description,
*"we are only able to hire employees residing in British Columbia or Ontario."*
A source that lies must set the flag `False` so its region field informs the
model instead of silently deleting jobs.

## Testing

```bash
py -m pytest -m "not live"    # offline, deterministic, runs on fixtures
py -m pytest -m live          # hits real APIs, asserts shape only
py -m ruff check .
```

The live tier deliberately asserts **no** counts, titles or specific jobs —
only the response shape the parser depends on. Himalayas deprecated its
`offset` parameter on 2026-08-21 with no notice, so upstream drift is expected
rather than exceptional, and that test is how you learn about it before the
morning run does.

## Troubleshooting

**`python: command not found`, or Python opens the Microsoft Store.**
On Windows, `python` often resolves to the Store alias stub at
`%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe`. Use `py` instead.

**`UnicodeEncodeError` writing the digest.** The tool forces UTF-8 on stdout
and stderr at startup. If you are piping through another script, that script
needs the same.

**`profile.yaml not found`.** Copy `profile.example.yaml` and edit it.

**Everything is rejected at `region`.** Expected — that stage removes ~95% by
design. Run `--explain` to see the reasons, and check `regions.allow` in your
profile actually lists how the board spells your region.

**Nothing is ever returned at all.** Check the salary stage in `--explain`. A
salary filter that treats "not stated" as "below floor" rejects the majority of
the market; this one deliberately passes unknowns through to scoring.

## Licence

MIT. See [LICENSE](LICENSE).
