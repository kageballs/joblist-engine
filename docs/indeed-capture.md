# Capturing Indeed

> **Status: built and in use, on `ph.indeed.com` only.**
> `sources/indeed.py` reads the captures described here, `boards.indeed` is
> declared in `profile.example.yaml`, and `tests/test_indeed.py` covers the
> parser. What is NOT built is any way to take a capture automatically — that
> stays a manual step, for the reasons in "Recommendation" at the end.
>
> Do not point this at `indeed.com`. On a real sample, 15 of 15 US listings
> were hireable only from the United States, which the region stage rejects in
> full.

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
| Parse | `sources/indeed.py`, offline, no network | `Job` objects, like every other source |

The split is not a workaround, it is the only shape that works: the browser is
attached to an interactive session and `main.py` cannot drive it. It also buys
the usual benefit — the parser is tested against `fixtures/indeed.json` with no
network at all, exactly like `sources/onlinejobs.py`.

**So `py main.py` never refreshes Indeed.**
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

Two stages, because the search page carries no advert text (see the
measurements below). The snippet reads the search cards, then fetches each
job page for its `ld+json` description, and emits one capture object matching
`fixtures/indeed.json`.

Open the search in Chrome so the URL carries the query you want:

```
https://ph.indeed.com/jobs?q=automation&l=Remote&fromage=7
```

Then evaluate this in the page and save the result as
`data/captures/indeed-<YYYY-MM-DD>.json`.

```js
(async () => {
  // Edit these two between runs to walk a large result set in chunks.
  const START = 0, LIMIT = 5;
  const DELAY_MS = 3000;
  const DESC_CAP = 6000;

  const model = window.mosaic
    ?.providerData?.["mosaic-provider-jobcards"]
    ?.metaData?.mosaicProviderJobCardsModel;
  if (!model) throw new Error("no job card model - challenge page, or the shape moved");

  const cards = (model.results || []).slice(START, START + LIMIT);
  const site = location.host;

  // Only the fields the parser reads. An allowlist, not a scrub: the shape is
  // known now, and copying the whole card would drag every tracking token and
  // session id into a file that outlives the session that made it.
  // Flatten to text and drop every URL here, not in the parser. An advert body
  // carries apply links with tracking parameters on them, and a capture file
  // outlives the session that made it -- so the link never reaches disk at all.
  const clean = (h) => (h || "")
    .replace(/<(br|\/p|\/li|\/h[1-6]|\/div)[^>]*>/gi, "\n")
    .replace(/<[^>]*>/g, " ")
    .replace(/https?:\/\/\S+/gi, " ")
    .replace(/\bwww\.\S+/gi, " ")
    .replace(/&nbsp;/g, " ").replace(/&amp;/g, "&")
    .replace(/[ \t]+/g, " ").replace(/\n\s*\n\s*/g, "\n\n").trim();

  const project = (r) => ({
    jobkey: r.jobkey || "",
    title: r.displayTitle || r.title || "",
    company: r.company || null,
    location: r.formattedLocation || null,
    country: r.country || null,
    remote_type: r.remoteWorkModel?.type || null,
    remote_flag: !!r.remoteLocation,
    pub_date: r.pubDate || null,
    relative: r.formattedRelativeTime || null,
    expired: !!r.expired,
    salary_text: r.salarySnippet?.text || null,
    salary_source: r.salarySnippet?.source || null,
    salary_min: r.extractedSalary?.min ?? null,
    salary_max: r.extractedSalary?.max ?? null,
    salary_period: r.extractedSalary?.type || null,
    job_types: (r.jobTypes || []).map((t) => t.label || t).filter((x) => typeof x === "string"),
    url: r.jobkey ? `https://${site}/viewjob?jk=${r.jobkey}` : null,
    date_posted: null,
    valid_through: null,
    description: "",
  });

  // Parse the schema.org JobPosting, never the DOM. The rendered description
  // has no stable handle left -- #jobDescriptionText and every other
  // documented Indeed selector is gone, and what remains is a hashed
  // CSS-in-JS class that changes on their next deploy.
  const hydrate = async (row) => {
    if (!row.jobkey) return row;
    const res = await fetch(`/viewjob?jk=${row.jobkey}`, { credentials: "include" });
    if (!res.ok) { row.description = ""; return row; }
    const html = await res.text();
    for (const block of (html.match(/<script type="application\/ld\+json">[\s\S]*?<\/script>/g) || [])) {
      try {
        const o = JSON.parse(block.replace(/<script[^>]*>/, "").replace(/<\/script>/, ""));
        if (o["@type"] !== "JobPosting") continue;
        row.description = clean(o.description).slice(0, DESC_CAP);
        row.date_posted = o.datePosted || null;
        row.valid_through = o.validThrough || null;
        return row;
      } catch (e) { /* a malformed block is not a reason to lose the row */ }
    }
    return row;
  };

  const results = [];
  for (const card of cards) {
    results.push(await hydrate(project(card)));
    await new Promise((r) => setTimeout(r, DELAY_MS));
  }

  const q = new URLSearchParams(location.search);
  return JSON.stringify({
    captured_at: new Date().toISOString(),
    site,
    query: q.get("q") || "",
    location: q.get("l") || "",
    fromage: q.get("fromage") || "",
    start: String(START),
    count: results.length,
    results,
  }, null, 1);
})()
```

Wrapped in an async IIFE deliberately. A bare top-level `const` throws
`Identifier has already been declared` the second time it is pasted into the
same console, and walking a result set in chunks means running it repeatedly
in one tab.

### Run it yourself, in DevTools

Open the console on the search page, paste the snippet, and save what it
returns. `copy($_)` puts the last result on the clipboard, or wrap the call in
`console.log()` and use the console's own save.

This is a job for the operator rather than for an agent, and not only by
preference. An assistant driving the browser has its result inspected on the
way back, and a payload of bulk text scraped out of a third-party page is
refused — correctly, since that is exactly the shape of an exfiltration. Rows
come back one at a time or not at all, which is fine for checking the format
and useless for a real capture. Running it in your own console has no such
limit.

### Pacing is not optional

`DELAY_MS` is 3000 and `LIMIT` is 5 because this is the part that gets
punished. Twenty job-page requests at one second apart earned a `403 Security
Check` and then a Cloudflare interstitial that outlived a reload, on ordinary
search URLs — and it lands on the operator's own address, the one used to
browse Indeed by hand and to apply through. Losing that is a worse outcome
than a thin capture.

If a run returns descriptions that are all empty, stop. That is the block, not
a parsing bug.

### Several captures are fine

`sources/indeed.py` reads every `indeed-*.json` in the directory, dedupes on
`jobkey` keeping the newest, and ignores a capture older than
`config.INDEED_MAX_CAPTURE_AGE_DAYS`. So chunks, repeated queries and overlapping
searches all compose without any care about ordering.

## What the parser does with a capture

The invariants that keep the two halves from drifting apart:

- **A capture is a photograph, not a feed.** `_load_captures()` reads
  `captured_at` and skips any file older than
  `config.INDEED_MAX_CAPTURE_AGE_DAYS` (14) with a warning, because a silently
  ignored capture looks exactly like a board with no jobs. That is a separate
  number from `boards.indeed.max_age_days`, which still applies to the posting
  dates *inside* a capture at render time.
- **The flags describe the PH pages, because those are the ones read.** All
  three are `False`, `False`, `True`: a ph.indeed job page carries no
  `applicantLocationRequirements` and no `baseSalary` — only `validThrough` is
  real. The US job page does publish region and salary, which is what makes it
  tempting to declare `True`; do not, unless the source is actually changed to
  read `indeed.com`, which the region measurement says it should not be.
- **Region comes from each card's own `country`, never asserted board-wide.**
  `onlinejobs.py` can hard-code `("Philippines",)` because that is what the
  board is; ph.indeed.com is a localisation of a global site. An unrecognised
  country yields an EMPTY tuple on purpose — that is what gives
  `regions_authoritative = False` something to do, since `filters.py` then
  marks the verdict `region_unverified`.
- **Salary is converted to USD inside the source.** `annual_usd_max()` returns
  `None` for any non-USD currency, so an unconverted peso figure would read as
  "unknown". Currency is inferred from the stated text, not the site: a dollar
  figure on the PH site must not be divided by 58.5.
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
- salary is idle on the ~93% that state no figure. It works normally on the
  rest -- against the fixture's salaried rows it correctly rejects a PHP
  150,000/month listing at $14.79/hr and a $8-12/hr contract, and passes a PHP
  1.79-2.39M/year one at $19.67/hr -- there is simply almost never a figure

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

### Recommendation, and what was decided

Viable on `ph.indeed.com`, not worth it on `indeed.com`.

The US site fails outright: 15 of 15 sampled listings hire only from the
United States. Nothing is built against it and nothing should be.

The PH site is a genuine source of relevant, almost entirely new listings with
full advert text — 15 of 16 sampled employers appeared nowhere in the existing
store. The obstacle was never cost and never the region stage. It is that the
capture cannot be automated from inside this repo: reading the board needs a
real browser session, `main.py` cannot drive one, and so Indeed is not a
source in the sense the other two are.

That trade was accepted and the parser was built. What it means in practice:

- `py main.py` will fetch Indeed's captures but never create one. A run does
  not refresh this board; a person does.
- Keep the per-run volume small and the pacing slow. The verification wall
  described above arrived after about twenty job-page requests in a couple of
  minutes, and it arrives on the operator's own address.
- Almost nothing filters for free here, so most captured listings reach the
  model. That is affordable only while the board stays thin at roughly 15
  results per query per week. If a capture ever gets large, check the cost
  before widening a query.
