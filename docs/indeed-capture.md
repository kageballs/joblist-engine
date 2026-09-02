# Capturing Indeed

> **Status: measured, and not recommended. Nothing is built.**
> `sources/indeed.py` does not exist, and neither does an `indeed:` block in
> `profile.yaml`. Read "What the measurement found" below before writing
> either — on a real sample, **15 of 15** US listings were hireable only from
> the United States, which this profile's region stage would reject in full.
> The capture mechanics below are correct and were tested; the open question
> is whether the board is worth capturing at all.

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
  // spaces. An earlier version tested for "?" and "=" instead, which blanks
  // any prose containing a question mark and an attribute — fatal once this
  // is pointed at the job page, whose description IS HTML. The 24-char floor
  // also keeps `jobkey`, which is shorter and which the URL is rebuilt from.
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
- **The two pages deserve different capability flags.** The search model has
  no hiring-region field at all and an *inferred* salary, so a source built on
  it alone would set `regions_authoritative = False` and
  `salary_authoritative = False`. The job page publishes
  `applicantLocationRequirements`, `baseSalary` and `validThrough`, which
  would earn `True`, `True` and `publishes_expiry = True`. Do not average the
  two: declare the flags for whichever page the source actually reads.
- **An `indeed:` block in `profile.yaml` is mandatory before the source is
  registered**, with its own floor and thresholds. That is the isolation rule:
  the board cannot inherit another board's tuning.
- **Parse functions return `None` on unusable input, never raise.** A capture
  taken while Indeed was mid-experiment is expected, not exceptional.

## What the measurement found

Probed 2026-09-03 from a real Chrome session. These numbers are the reason
this document ends in a recommendation rather than a parser.

### The search page carries no advert text

`mosaicProviderJobCardsModel.results[]` is rich — title, company, location,
`pubDate`, `extractedSalary`, `remoteWorkModel`, `expired` — but its `snippet`
field was **empty on all 15 cards**, and the page has no snippet nodes in the
DOM either. So a search capture alone cannot feed the scorer: with no
description there is nothing for `eligibility_sentences()` to read, nothing
for the model to extract requirements from, and nothing to draft a letter
against. Indeed is therefore a two-request board at minimum: the search page,
then one page per surviving job.

Its `salarySnippet.source` was `EXTRACTION` on all 15 — Indeed inferred those
figures from the advert text rather than the employer declaring them. A board
whose salary is inferred is exactly the case `salary_authoritative = False`
exists for; rejecting on it would discard jobs on Indeed's parsing mistakes.

### The job page carries a real contract

`/viewjob?jk=<key>` embeds a schema.org `JobPosting` in
`<script type="application/ld+json">`, with `description`,
`applicantLocationRequirements`, `baseSalary`, `datePosted`, `validThrough`
and `employmentType`.

Parse that, not the DOM. The rendered description sits in a container whose
only handle is a hashed CSS-in-JS class (`css-g5y9jx` on the day of the
probe); `#jobDescriptionText` and every other documented Indeed selector are
gone. A hashed class changes on the next deploy, where the JSON-LD block is a
published standard.

`applicantLocationRequirements` is a genuinely authoritative hiring-region
field, better than what most boards publish, and `validThrough` means
`publishes_expiry` would be `True`. Those flags belong on the **job page**
path; the search page deserves neither.

### And that authoritative field is what kills it

Sampled all 15 results of `q=python+developer&l=Remote&fromage=7` on
indeed.com, reading `applicantLocationRequirements` from each job page:

```
United States   15
anything else    0
```

`filters.stage_region` would reject 100% of that sample. Building the source
would produce a funnel whose correct output is zero rows.

`ph.indeed.com` is the site that would actually serve this profile, and it is
a different proposition: the same query returned **6** cards rather than 15,
locations were `Work from Home` and `Philippines`, the sampled card carried no
salary at all, and the job-page fetch that worked on the US site answered
**403 Security Check** there. Thinner inventory, less structured data, and a
harder fetch.

### The rate limiting is itself the answer

The probe above stopped early, and how it stopped matters more than the rows
it did not collect. After roughly twenty job-page requests spread over a few
minutes -- with a one second delay between them, from a real browser, in a
real signed-in session -- `ph.indeed.com` began answering `403 Security Check`
and then served a Cloudflare "Additional Verification Required" interstitial
to ordinary search URLs. The block outlived a reload.

That is the shape of the whole problem. Reading this board at all requires one
request per job, a scheduled run makes exactly the regular machine-like
pattern Cloudflare exists to catch, and the address it gets caught at is the
operator's home connection -- the same one used to browse Indeed by hand and
to apply. The cost of being wrong here is not a failed run, it is losing
ordinary access to a job board during a job hunt.

Nothing in this repo has that property today. Himalayas is a JSON feed,
OnlineJobs is ordinary HTML, and neither retaliates.

### ph.indeed.com measured properly, and it changes the answer

The US numbers above do not transfer. Measured on `ph.indeed.com`, which is
the site that would actually serve this profile:

| | |
|---|---|
| `q=automation&l=Remote&fromage=7` | 14 cards, all `country: PH`, all `REMOTE_ALWAYS` |
| same query at `fromage=14` | 15 cards |
| `"work from home" developer`, 7d | 7 cards |
| Companies already in this store | **1 of 16** |
| Job-page description | 10,345 characters, full advert |

The roles are the right ones -- AI Engineer, Backend Engineer, DevOps
Engineer, a GoHighLevel specialist, a Salesforce BA -- and GoHighLevel work is
this profile's single highest scorer anywhere at 75. Fifteen of sixteen
sampled employers appear nowhere in the existing store, so this is close to
entirely new inventory rather than OnlineJobs reposted.

**But the funnel inverts here, and that is the real objection.** A PH job page
carries `description`, `datePosted` and `validThrough` -- and no `baseSalary`
and no `applicantLocationRequirements` at all. The search card states pay on
roughly 7% of listings. So on this board:

- region rejects nothing, exactly as on OnlineJobs, because both are domestic
- salary rejects nothing either, because it is simply absent

For contrast, on the stored history `filters.py` rejected 512 OnlineJobs
listings and **510 of those were the salary stage** -- 99.6% of all the free
work done on that board. OnlineJobs survives because OLJ puts pay in a text
box on nearly every advert, badly formatted but present, and `parse_salary()`
rescues it. Indeed does not state it at all, before or after hydration.

So every fetched Indeed listing would reach the model, with nothing removed
for free. That is the v1 architecture this rewrite deleted.

What rescues it is the thing that first looked like a weakness: the board is
thin. Roughly 15 results per query per week is a single scoring batch, and a
funnel that filters nothing is only ruinous at Himalayas' 2400-per-run scale.
At this volume the arithmetic is affordable.

### Recommendation

Viable on `ph.indeed.com`, not worth it on `indeed.com`, and blocked on
something other than economics.

The US site fails outright: 15 of 15 sampled listings hire only from the
United States. Do not build against it.

The PH site is a genuine source of relevant, almost entirely new listings with
full advert text. The obstacle is not cost and not the region stage, it is
that **the capture cannot be automated from inside this repo**. Reading the
board needs a real browser session, `main.py` cannot drive one, and so Indeed
can never be a source in the sense the other two are. It would be a manual
step producing a file, on a board where nothing filters for free.

If that trade is acceptable, build it as a capture-plus-parser and keep the
per-run volume small and the pacing slow -- the verification wall in the
previous section arrived after about twenty job-page requests in a couple of
minutes, and it arrives on the operator's own address.
