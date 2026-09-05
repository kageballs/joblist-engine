# Spec-driven development, for one person and a tier of agents

The process in `how-this-was-built.md` describes *who does what*. This page is
the missing half: **what the work is written down as before anyone writes code**,
and why that document is the thing that makes agent-written code reviewable.

The short version: an agent is very good at hitting a target and very bad at
choosing one. A spec is the target. Without it you get code that is locally
plausible and globally wrong, and you find out three waves later.

## The loop

```
decide  →  SPEC-NNN written and read  →  waves  →  each wave has its own gate
   ↑                                                        │
   └──────────  spec amended in place, dated  ←─────────────┘
```

Four properties matter, and they are what separate this from "write a design doc
and then ignore it":

1. **The spec is approved before code, not after.** For this repo that means the
   spec is read and agreed by the one human before an implementer agent is
   given anything. What is being approved is the *behaviour and its boundary*,
   never the implementation.
2. **The spec is amended, never replaced.** When a decision is overturned, the
   old line stays, struck through, with the date and the reason it was
   inverted. A spec that gets rewritten clean loses the only record of why the
   obvious thing was rejected — and the obvious thing gets re-proposed.
3. **The spec names its own test obligations.** Not "add tests", but the
   specific assertions, including the ones that exist to stop a future change
   from quietly undoing a decision.
4. **Work is cut into waves, and each wave has a gate.** A wave is done when its
   gate is green, not when it looks finished.

## Anatomy of a spec

Numbered `SPEC-000`, `SPEC-001`, … in `docs/specs/`. Sections, in this order,
because the order is itself an argument — the change is stated before the schema,
the schema before the surfaces, the surfaces before the plan:

| § | Section | What goes in it |
|---|---|---|
| 1 | **The change, in one paragraph** | If it cannot be said in a paragraph, the spec is two specs |
| 2 | **Target state** | Schema, data shapes, config keys. What it looks like when done |
| 3 | **Rules and invariants** | What must always be true. The things a test will assert |
| 4 | **Explicitly out of scope** | Named, not implied. This is what stops a wave from sprawling |
| 5 | **The gate** | The one condition that decides whether this is correct |
| 6 | **Surfaces** | Every place a human sees or triggers it: CLI flags, digest lines, dashboard |
| 7 | **Build waves** | A table: wave, contents, gate. Which waves block which |
| 8 | **Answers** | Edge cases resolved during the work, **each one dated** |
| 9 | **Superseded** | What this spec used to say, struck through, with why and when |
| 10 | **Test obligations** | The assertions this spec owes, named individually |

Sections 8, 9 and 10 are the ones people skip, and they are the ones that make
the document worth keeping after the code lands.

## Waves, and why one PR is often three

A wave is a slice that can be gated on its own. The rule for splitting is not
size — it is **reviewability**:

> A change that mixes a large additive change with a repair sweep *and* a test
> rewrite is unreviewable in one diff. Split it until each piece can be read.

The pattern that works, in order:

- **Additive only first.** Add the new thing. Remove nothing. Everything still
  runs and every existing test still passes, because nothing was taken away.
  This wave is boring by design and it is where the risk actually lives.
- **Scaffolding.** The new paths exist but nothing depends on them yet.
- **The swap.** Point the callers at the new thing. Old code still present.
- **The removal.** Delete the old thing and its tests, alone, in its own change.
- **The mechanical rename.** Last, and alone, so its diff can be read as a pure
  rename with no behaviour hiding in it.

Two hard-won details:

**A transitional shim is allowed to outlive its wave.** If a shim lets old
callers keep working during the swap, removing it is a *behaviour change* and
therefore its own spec item — not a tidy-up appended to the rename. Retiring one
needs a sweep of every caller first.

**Naming and behaviour never change in the same commit.** A rename that also
alters behaviour cannot be reviewed, because the reviewer cannot tell which
lines are the rename.

## Traceability is a naming convention, not a tool

Carry the spec id and the wave id into the filenames of whatever the wave
produces, so the build order is readable from `ls` and every artifact traces
back to the section that asked for it:

```
20260813111100_spec_003_shift_skeleton.sql      wave A1, additive only
20260813122222_spec_003_a2_scaffolding.sql      wave A2
20260813124404_spec_003_review_fixes.sql        review findings, on their own
20260813132923_spec_003_a3a_the_swap.sql        wave A3a
```

Two things fall out of this for free. The order is self-documenting, so nobody
has to reconstruct it from commit dates. And **review findings get their own
change**, named as findings, rather than being folded silently back into the
wave they corrected — which is the difference between a repo where you can see
what review caught and one where you cannot.

This repo has no migrations, so the equivalent is the branch and commit subject:
`spec-001/a1-auth-tests`, `spec-001: A2 ingest route tests`. The convention is
worth as much here; it just attaches to a different noun.

## Test obligations are part of the spec

The pattern worth stealing wholesale: **a spec lists the assertions it owes,
and some of those assertions exist to defend a decision rather than to catch a
bug.**

Two kinds are easy to miss:

- **Positive assertions that protect a rejected design.** If a decision was
  "this stays visible, do not gate it", write a test that asserts it *is*
  visible. Without it, a later change will "fix" the absence of a gate into a
  gate, and nothing will fail. This repo already needs one: the dashboard has
  no age filter *on purpose* — nothing asserts that.
- **Fixture notes for whoever touches this next.** "This fixture must set X, or
  every assertion in the file passes for the wrong reason." A test that passes
  for the wrong reason is worse than a missing test, because it is counted.

## What this replaces here

Today a piece of work in this repo starts as a plan in a chat session and ends
as a HANDOFF entry. That is not nothing — the HANDOFF genuinely records failed
approaches, which is the rarest and most useful part — but it has two holes a
spec closes:

- **Scope is never written down before the work**, so it drifts, and the only
  record of the boundary is whatever the session happened to do.
- **Decisions are recorded after the fact, in prose, mixed with status.** Six
  months on you cannot tell which lines were the decision and which were the
  narration.

The HANDOFF stays as the status log. Specs carry the *intent* and the
*obligations*, and they outlive the session that produced them.

## When not to write one

Most changes do not need a spec, and writing one for them is theatre. The test
is whether the change has a **boundary worth arguing about**:

| Write a spec | Do not |
|---|---|
| A new source, a new stored column, anything touching the privacy split | A bugfix with a known cause |
| Anything with waves, i.e. it cannot land in one reviewable change | A rename |
| Anything that changes what the model is asked, or what it costs | Adding a test |
| Anything where "out of scope" is doing real work | Anything a single session finishes |

## The first one to write here

`SPEC-001 — dashboard test coverage`, because it is the largest gap in
`quality.md` and it is exactly the shape that needs a boundary: the interesting
question is not "add tests", it is *which* of auth, ingest, SQL and escaping get
covered, what a route-level smoke pass asserts, and what is explicitly left
uncovered so the spec cannot quietly become "test the whole dashboard".
