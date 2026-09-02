# Capturing Indeed

> **Status: the capture half only.** `sources/indeed.py` does not exist yet,
> and neither does an `indeed:` block in `profile.yaml`. Capturing today gives
> you a JSON file and nothing that reads it — and because
> `Profile.board("indeed")` raises for an undeclared board, adding the source
> before declaring its policy would abort the run rather than silently guess.
> This document exists so the capture format is settled before the parser is
> written against it.

Indeed is the one board this tool cannot fetch by itself, and that is a
property of Indeed rather than a gap in the code. A plain `requests` GET —
with the repo's own user agent and with an ordinary desktop Chrome one — is
answered by a Cloudflare challenge page on the very first request. No header
tuning gets past it, because the check is on the TLS and JS fingerprint, not
the headers.

A real browser walks straight through, so that is what we use. The board
splits across two stages that never run in the same process:

| Stage | Where it runs | Produces |
|---|---|---|
| Capture | A real Chrome session, driven by hand or by an agent | `data/captures/indeed-<date>.json` |
| Parse | `sources/indeed.py` — **not built yet** — offline, no network | `Job` objects, like every other source |

The split is not a workaround, it is the only shape that works: the browser is
attached to an interactive session and `main.py` cannot drive it. It also buys
the usual benefit — the parser will be testable against a fixture with no
network at all, exactly like `sources/onlinejobs.py`.

**So `py main.py` will not refresh Indeed even once the parser lands.**
Himalayas and OnlineJobs stay fully automatic; Indeed needs a capture first.

## Why the capture scrubs before it returns

Indeed decorates every job card with click-tracking: `ctk` session tokens,
per-impression beacon ids, and long signed strings in the link fields. None of
it is needed to describe a job, and all of it is a fingerprint of the browsing
session that produced the capture.

It is stripped **inside the page**, before the data ever leaves Chrome, rather
than on the way in to the parser. Two reasons and both matter: a capture file
outlives the session that made it, and the extraction is refused outright by
the agent's own safety filter if the payload looks like session data — which,
unscrubbed, it does.

The canonical URL is rebuilt from `jobkey` alone, so the stored link is the
job, not a record of how this machine arrived at it.

## The capture snippet

Open an Indeed search in Chrome — set the query, location and age window in
the UI, so the URL carries the search you actually want:

```
https://www.indeed.com/jobs?q=python+developer&l=Remote&fromage=7
```

Then evaluate this in the page. It returns JSON; write it to
`data/captures/indeed-<YYYY-MM-DD>.json`.

```js
(() => {
  const model = window.mosaic
    ?.providerData?.["mosaic-provider-jobcards"]
    ?.metaData?.mosaicProviderJobCardsModel;
  if (!model) throw new Error("job card model absent - page shape changed, or a challenge page");

  // Match WHOLE WORDS, splitting camelCase first. A substring test is wrong in
  // both directions and was verified to be: unanchored, /link|sig|adId/ eats
  // `linkedin`, `design` and `threadId`; end-anchored, it misses
  // `trackingUrls` and `mosaicProviderCtx`. Splitting on the case boundary
  // gets all five right.
  const TRACKING_WORDS = new Set([
    "link", "links", "url", "urls", "href", "beacon", "beacons",
    "tracking", "track", "mosaic", "ctk", "tk", "sig", "signature",
    "adid", "clk", "click", "clickid", "impression", "impressionid",
    "token", "tokens", "ctx", "uuid", "session",
  ]);
  const words = (k) => k
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .split(/[^A-Za-z0-9]+/).filter(Boolean).map((w) => w.toLowerCase());
  const isTracking = (k) => words(k).some((w) => TRACKING_WORDS.has(w));

  // A tracking token is a long unbroken run of URL-safe characters with no
  // spaces. Testing for "?" and "=" instead would blank every description on
  // the page: Indeed's `snippet` is HTML so it always contains "=", and any
  // advert asking "Ready to join?" contains both. The 24-char floor also
  // keeps `jobkey`, which is shorter and which the URL is rebuilt from.
  const TOKENISH = /^[A-Za-z0-9_-]{24,}$/;

  const scrub = (v) => {
    if (Array.isArray(v)) return v.map(scrub);
    if (v && typeof v === "object") {
      const out = {};
      for (const [k, val] of Object.entries(v)) {
        if (isTracking(k)) continue;
        out[k] = scrub(val);
      }
      return out;
    }
    if (typeof v === "string" && TOKENISH.test(v)) return "";
    return v;
  };

  const results = (model.results || []).map((j) => {
    const clean = scrub(j);
    // Rebuilt from the key alone, never copied from the page's own link field.
    clean.url = j.jobkey ? `https://www.indeed.com/viewjob?jk=${j.jobkey}` : null;
    return clean;
  });

  const q = new URLSearchParams(location.search);
  return JSON.stringify({
    captured_at: new Date().toISOString(),
    query: q.get("q") || "",
    location: q.get("l") || "",
    fromage: q.get("fromage") || "",
    start: q.get("start") || "0",
    count: results.length,
    results,
  }, null, 1);
})()
```

Wrapped in an IIFE deliberately: a bare top-level `const model` throws
`Identifier 'model' has already been declared` the second time it is pasted
into the same console, and paging through results means running it several
times in one tab.

One search page holds about 16 cards. For more, page with `&start=10`,
`&start=20` … and capture each. The parser should treat a capture as a list of
jobs and not care which page they came from.

## What the parser will have to handle

Written down now so the two halves cannot drift apart:

- **A capture is a photograph, not a feed.** `captured_at` must be read, and a
  capture older than the board's own `max_age_days` has nothing left to say.
  The posting dates inside it are still subject to that window regardless.
- **`sources/indeed.py` must declare its capability flags honestly.** Indeed
  publishes no reliable worldwide-remote field, so `regions_authoritative`
  will be `False` — which is what makes `scorer.render_batch()` tell the model
  not to read an empty region list as "worldwide".
- **An `indeed:` block in `profile.yaml` is mandatory before the source is
  registered**, with its own floor and thresholds. That is the isolation rule:
  the board cannot inherit another board's tuning.
- **Parse functions return `None` on unusable input, never raise.** A capture
  taken while Indeed was mid-experiment is expected, not exceptional.
