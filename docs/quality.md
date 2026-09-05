# Quality: what is tested, what is not, and why

`docs/how-this-was-built.md` describes the process that writes this code. This
page is the narrower question: **what stops a wrong thing from shipping**, and
where that net currently has holes.

Gaps are stated here rather than implied. A testing page that lists only what
exists reads as coverage, and the honest version of this project's history is
that every serious bug so far got through a suite that was green.

## What can actually hurt this product

The pyramid is worth building around real failure modes, not around a shape.
Five things can do damage here, in rough order of how bad they are:

1. **A private thing gets published.** Reject reasons name flagged employers, so
   publishing them makes that list inferable. Advert text is bulk employer copy.
   Cover letters are written from the resume in the candidate's own voice. Each
   is withheld by a different mechanism, and a regression in any of them is not
   recoverable after the fact.
2. **A wrong number is believed.** A currency regex read `25,000PHP` as dollars
   and overstated a wage by ~58x. The README claimed `~0.6%` of listings survive
   the funnel when its own figures said 6%. Nothing crashed in either case.
3. **Money is spent on nothing.** An empty response written to disk permanently
   marks a job as drafted. One advert listed under two ids buys two API calls and
   two of five slots. Extended thinking consumed a whole 1,200-token budget and
   returned no letter, three times, before anyone measured `stop_reason`.
4. **Boards contaminate each other.** The governing rule is that a score is only
   meaningful inside the board that produced it. Every pooled sort, shared
   threshold or global default is a quiet violation, and the symptom is not an
   error — it is one board silently taking every slot.
5. **Upstream drifts.** A feed deprecates a parameter, a selector disappears, a
   site starts answering 403. The pipeline keeps running and returns less.

## The layers that exist

### 1. Offline tests — 265, the whole suite

`py -m pytest -m "not live" -q`. No network, no API key, no clock of their own.
Every source parses from a committed fixture. This is the layer that has to
carry the weight, and it does: a bug that can be expressed as "given this row,
the answer should be X" belongs here and nowhere else.

The rule that keeps it honest: **a test is added because something was measured
to be wrong, not because a function exists.** Coverage percentage is not tracked
and is not a goal.

### 2. Live contract tests — weekly, no key required

`.github/workflows/live.yml`, Mondays 06:00 UTC plus manual dispatch. Runs the
three `@pytest.mark.live` tests against the real feeds and then a full
`main.py --no-llm --dry-run`, which exercises fetch, parse and the entire
deterministic funnel **without an API key**.

This exists because upstream drift is invisible to offline tests by
construction: one board deprecated its `offset` parameter with no notice, and a
fixture-based suite would have stayed green forever.

### 3. Mutation checks on rules that must not weaken

Applied by hand to the small number of rules where a silently-weakened check is
the dangerous outcome rather than a crash. The method: break the implementation
deliberately, confirm the tests fail, restore.

Done so far on the localhost gate in `push.py` — replacing
`urlsplit().hostname in LOCAL_HOSTS` with a substring check fails 8 of the 24
tests in `tests/test_push.py`, which is the evidence those tests are real and
not shaped around the implementation.

### 4. The gates

| Gate | Runs | Blocks a merge |
|---|---|---|
| `ruff check .` | every push and PR | **yes** |
| `pytest -m "not live"` | every push and PR | **yes** |
| live contract suite | weekly cron | no — it reports drift, it does not gate |

`ruff` was `continue-on-error: true` until 2026-09-04, which meant lint could
fail while CI reported success, while the docs described it as part of the gate.
**Trust exit codes, not printed output.** A gate that reports success on failure
is worse than no gate, because it is believed.

## The layers that do not exist

### The dashboard has zero tests

`dashboard/src/` is roughly 900 lines of JavaScript with no test of any kind:
no runner, no fixtures, nothing in CI. It contains the HMAC session cookie, the
login throttle, the bearer-token check on three ingest routes, every SQL
statement the UI runs, and all the HTML escaping. The Python half of the privacy
contract is tested; **the half that actually serves the data to a browser is
not.**

This is the largest gap and the one whose failure modes are worst, because
`auth.js` failing open does not look like anything from the outside.

### Nothing checks what the model produces

`scorer.py` and `cover.py` are the two stages that cost money and can change
their answer, and there is **no regression test on either prompt.** A prompt
edit, a model version change, or a provider default flipping can degrade scoring
quality silently and indefinitely — there is no fixture of known adverts with
expected score bands, and no assertion that a blatant mismatch still scores low.

The extended-thinking incident is the proof this matters: three letters came
back empty over two days and were misread as the model declining, because
nothing was watching the shape of the response.

### No browser check

Every stage above tests a function or a feed. Nothing loads the dashboard and
clicks anything. The detail panel, the state buttons and the board tabs are
verified by hand each time they change, which does not scale and does not survive
a refactor.

### No end-to-end run against a fresh store

`main.py --no-llm --dry-run` runs weekly, but no test builds a store from empty,
runs the funnel, and asserts what came out the other end.

## Definition of done

Not aspirational — this is what "finished" means for a change here.

- [ ] `ruff check .` and `pytest -m "not live"` pass locally, checked by **exit
      code**, not by reading the last line of output
- [ ] A test exists for the behaviour, and it was confirmed to **fail** against
      the unfixed code
- [ ] Any measured figure quoted in a comment or doc was re-measured today, not
      copied forward
- [ ] If the change touches a risk zone below, it was reviewed in a fresh
      context that did not write it
- [ ] `HANDOFF.md` records what was decided and, more importantly, **what was
      tried and failed**

## Risk zones — never merged on a suite alone

Changes here get a deliberate second look, because the failure mode is silent:

| Zone | Why |
|---|---|
| `push.py` FIELDS, `is_local`, `dashboard/schema.sql` | the privacy contract; a mistake publishes something unrecoverable |
| `dashboard/src/auth.js` | fails open without looking broken |
| anything reading `profile.yaml` thresholds | a pooled or global value violates board isolation and produces no error |
| `filters.py` stage order and the tri-state salary rule | a stage that rejects on unknown silently discards good listings |
| `cover.py` cap allocation and write paths | spends money, and a bad write permanently marks a job done |

## The order these gaps should be closed

1. **Dashboard tests.** Auth, the three ingest routes, and HTML escaping.
   Biggest surface, worst failure modes, currently zero. This one gets a spec
   first — see [`spec-driven.md`](spec-driven.md) — because the interesting
   question is which parts are deliberately left uncovered.
2. **Scorer eval fixtures.** A small golden set of adverts with expected score
   bands, run on any change to `scorer.py` or its prompt. New production
   failures become new cases; the set only grows.
3. **A route-level smoke pass** on the dashboard — every page returns 200 and
   contains its landmark — before any journey-style browser tests. Breadth is
   worth more than depth here and costs far less wall-clock.
4. **A fresh-store end-to-end test** using the existing fixtures.
