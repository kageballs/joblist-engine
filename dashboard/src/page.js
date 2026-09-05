const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// A job's url is `applicationLink` straight off the Himalayas feed, so its
// scheme is employer-controlled. esc() escapes HTML entities and therefore does
// nothing to `javascript:` in an href -- that would run in this origin the
// moment Apply is clicked. Anything that is not http(s) is not a link.
const safeUrl = (u) => {
  try {
    const parsed = new URL(String(u));
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.href : null;
  } catch {
    return null;
  }
};

const STATES = ["new", "interested", "applied", "passed"];
const LABEL = { new: "New", interested: "Interested", applied: "Applied", passed: "Passed" };
// salary_signal is filters.py's tri-state enum; "unknown" is the common case and
// says nothing useful on a card, so it renders as no tag at all.
const SALARY = { above: "above floor", below: "below floor" };

const CSS = `
:root{--bg:#0f1115;--card:#181b22;--line:#262b36;--fg:#e6e8ee;--dim:#9aa3b2;
--hi:#3ddc97;--mid:#f5c451;--lo:#6b7280;--accent:#5b9dff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
-webkit-text-size-adjust:100%}
header{position:sticky;top:0;background:rgba(15,17,21,.94);
backdrop-filter:blur(8px);border-bottom:1px solid var(--line);padding:.75rem 1rem;z-index:10}
h1{margin:0;font-size:1rem;letter-spacing:.02em}
h1 span{color:var(--dim);font-weight:400}
nav{display:flex;gap:.35rem;margin-top:.55rem;overflow-x:auto;-webkit-overflow-scrolling:touch}
nav a{flex:0 0 auto;padding:.35rem .7rem;border-radius:999px;text-decoration:none;
color:var(--dim);background:var(--card);border:1px solid var(--line);font-size:.82rem;white-space:nowrap}
nav a.on{color:var(--bg);background:var(--fg);border-color:var(--fg);font-weight:600}
nav a b{font-weight:600}
/* Board tabs: the outer axis. Underlined tabs rather than pills so they read
   as the frame the pills below sit inside, not as a second row of filters. */
nav.src{display:flex;gap:.1rem;margin-top:.55rem;border-bottom:1px solid var(--line);
overflow-x:auto;-webkit-overflow-scrolling:touch}
nav.src a{flex:0 0 auto;padding:.4rem .8rem;border:0;background:none;border-radius:0;
color:var(--dim);font-size:.9rem;white-space:nowrap;text-decoration:none;
border-bottom:2px solid transparent;margin-bottom:-1px}
nav.src a:hover{color:var(--fg)}
nav.src a.on{color:var(--fg);background:none;border-bottom-color:var(--accent);font-weight:600}
nav.src a b{font-weight:500;opacity:.55;margin-left:.15rem}
p.scope{margin:0 0 .7rem;font-size:.76rem;color:var(--dim)}
p.scope b{color:var(--fg);font-weight:600}
main{padding:.75rem;max-width:44rem;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:.8rem;margin-bottom:.7rem;cursor:pointer}
.card:hover{border-color:#39404f}
.card.busy{opacity:.45}
/* The triage row is the one part of a card that is NOT a way into the detail
   panel, so it says so by cursor as well as by behaviour. */
.acts{cursor:auto}
.top{display:flex;gap:.6rem;align-items:flex-start}
.score{flex:0 0 auto;width:2.6rem;height:2.6rem;border-radius:8px;display:grid;
place-items:center;font-weight:700;font-size:1.05rem;background:#20242e}
.s-hi{color:var(--hi);box-shadow:inset 0 0 0 1px var(--hi)}
.s-mid{color:var(--mid);box-shadow:inset 0 0 0 1px var(--mid)}
.s-lo{color:var(--lo);box-shadow:inset 0 0 0 1px var(--line)}
.ttl{font-weight:600;line-height:1.3}
.co{color:var(--dim);font-size:.86rem;margin-top:.15rem}
.tags{display:flex;flex-wrap:wrap;gap:.3rem;margin:.55rem 0 0}
.tag{font-size:.72rem;padding:.16rem .45rem;border-radius:4px;background:#20242e;color:var(--dim)}
.tag.src{background:#1d2a3d;color:#8fb8ff}
.tag.cv{background:#2a2333;color:#c9a7ff}
.tag.cov{background:#1b2c24;color:#7fd8a6}
.why{margin:.55rem 0 0;font-size:.86rem;color:var(--dim)}
/* Muted, not alarming. These are real jobs you simply cannot apply to as
   written, not errors, and the card stays fully readable. */
.card.blocked{border-color:#4a3630}
.blk{margin:.55rem 0 0;font-size:.78rem;color:#e0a07a;background:#2a1f1a;
border-radius:6px;padding:.3rem .5rem}
.blk b{color:#f2c4a4;font-weight:600}
nav a.alt{margin-left:auto;background:none;border-color:transparent;color:var(--accent)}
nav a.alt:hover{border-color:var(--line)}
.imph{font-size:.8rem;font-weight:600;color:var(--fg);margin:1.2rem 0 .6rem;
text-transform:uppercase;letter-spacing:.04em}
.imph span{text-transform:none;letter-spacing:0;font-weight:400;color:var(--dim)}
.imp{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:.8rem;margin-bottom:.6rem}
.imp.on{border-color:#4a3630}
.impt{display:flex;gap:.6rem;align-items:flex-start}
.impn{flex:0 0 auto;width:2.6rem;height:2.6rem;border-radius:8px;display:grid;
place-items:center;font-weight:700;font-size:1.05rem;background:#20242e;color:var(--dim)}
.imp.on .impn{color:#f2c4a4;box-shadow:inset 0 0 0 1px #6b4a3a}
.impbar{height:4px;background:#20242e;border-radius:3px;margin-top:.6rem;overflow:hidden}
.impbar i{display:block;height:100%;background:var(--dim);border-radius:3px}
.imp.on .impbar i{background:#c47a4e}
.impeg{margin:.55rem 0 0;padding-left:1rem;font-size:.79rem;color:var(--dim)}
.impeg li{margin:.15rem 0}
code{font-size:.9em;color:var(--dim)}
.acts{display:flex;gap:.4rem;margin-top:.7rem;flex-wrap:wrap}
button,.apply{font:inherit;font-size:.82rem;padding:.4rem .7rem;border-radius:6px;
border:1px solid var(--line);background:#20242e;color:var(--fg);cursor:pointer;
text-decoration:none;display:inline-block}
button:hover{border-color:var(--dim)}
.apply{background:var(--accent);border-color:var(--accent);color:#08111f;font-weight:600}
.empty{color:var(--dim);text-align:center;padding:3rem 1rem}
form.login{max-width:20rem;margin:18vh auto;padding:0 1rem}
form.login input{width:100%;padding:.6rem;border-radius:6px;border:1px solid var(--line);
background:var(--card);color:var(--fg);font:inherit}
form.login button{width:100%;margin-top:.6rem;padding:.6rem}
.err{color:#ff8080;font-size:.85rem;margin-top:.6rem}
footer{color:var(--dim);font-size:.75rem;text-align:center;padding:1.5rem 1rem}
/* Detail panel. A right-hand drawer rather than a centred modal: the list stays
   visible behind it, so opening a card does not lose your place in a triage
   pass of 200 rows. */
#ov{position:fixed;inset:0;background:rgba(8,10,14,.7);z-index:20;display:flex;
justify-content:flex-end}
#pane{background:var(--card);border-left:1px solid var(--line);width:min(40rem,100%);
height:100%;overflow-y:auto;padding:1rem 1.1rem 3rem;position:relative}
#pane h2{margin:.2rem 0 .1rem;font-size:1.05rem;line-height:1.3}
#pane .co{font-size:.9rem}
#x{position:sticky;top:0;float:right;margin:-.2rem -.3rem 0 .6rem}
.dsec{margin-top:1.1rem}
.dsec h3{margin:0 0 .4rem;font-size:.74rem;text-transform:uppercase;
letter-spacing:.05em;color:var(--dim);font-weight:600}
.dsec p{margin:0;font-size:.88rem}
.arith{font-size:.82rem;color:var(--dim)}
.arith b{color:var(--fg)}
.rq{list-style:none;margin:0;padding:0}
.rq li{border-top:1px solid var(--line);padding:.45rem 0;font-size:.85rem}
.rq li:first-child{border-top:0}
.rq .k{font-weight:600}
.rq .m{font-size:.7rem;color:var(--mid);margin-left:.4rem}
.rq .b{font-size:.7rem;color:#e0a07a;margin-left:.4rem}
.rq .d{color:var(--dim);margin-top:.15rem}
/* The letter is a draft to be read and edited, not a rendered document. It is
   shown as the exact text on disk -- markdown banner included -- because that
   is what gets copied out. */
.letter{white-space:pre-wrap;word-wrap:break-word;font:inherit;font-size:.86rem;
background:#13161c;border:1px solid var(--line);border-radius:8px;
padding:.75rem;margin:0;max-height:none}
.nolet{color:var(--dim);font-size:.85rem;margin:0}
.note{color:var(--dim);font-size:.76rem;margin:.45rem 0 0}
`;

function shell(title, body) {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex,nofollow">
<meta name="color-scheme" content="dark">
<title>${esc(title)}</title><style>${CSS}</style></head><body>${body}</body></html>`;
}

export function renderLogin(error) {
  return shell("joblist", `<form class="login" method="POST" action="/login">
<input type="password" name="password" placeholder="Password" autofocus
 autocomplete="current-password" aria-label="Password">
<button type="submit">Sign in</button>
${error ? `<p class="err">${esc(error)}</p>` : ""}</form>`);
}

function band(score) {
  if (score === null || score === undefined) return "s-lo";
  if (score >= 65) return "s-hi";
  if (score >= 45) return "s-mid";
  return "s-lo";
}

function regionText(raw) {
  let list;
  try { list = JSON.parse(raw || "[]"); } catch { return null; }
  if (!Array.isArray(list) || list.length === 0) return "worldwide";
  if (list.length > 3) return `${list.slice(0, 3).join(", ")} +${list.length - 3}`;
  return list.join(", ");
}

// A requirement key -> the short phrase a card has room for. Mirrors
// config.REQUIREMENT_KINDS on the Python side; an unmapped key falls back to
// the raw key rather than vanishing, so a new kind is visible before it is
// pretty.
const BLOCKER_LABEL = {
  work_samples: "work samples", public_code: "public code",
  video_intro: "video intro", test_task: "test task", timed_trial: "trial period",
  certification: "certification", degree: "degree", years_experience: "years of experience",
  references: "references", named_clients: "named clients", own_tooling: "own tooling",
  equipment: "equipment", time_tracker: "time tracker",
  background_check: "background check", id_verification: "ID verification",
  language_other: "another language", onsite_presence: "on-site presence",
};

function blockerList(raw) {
  let list;
  try { list = JSON.parse(raw || "[]"); } catch { return []; }
  return Array.isArray(list) ? list.map((k) => BLOCKER_LABEL[k] || k) : [];
}

function card(job) {
  const region = regionText(job.regions);
  const salary = SALARY[job.salary_signal] || null;
  const moves = STATES.filter((s) => s !== job.state && s !== "new");
  const applyUrl = safeUrl(job.url);
  const blocked = blockerList(job.blockers);
  // Show the arithmetic. A silently docked score reads as the model rating the
  // work poorly, when the work may be a fine match you simply cannot apply to.
  const docked = blocked.length && job.score_raw != null && job.score_raw !== job.score;
  return `<article class="card${blocked.length ? " blocked" : ""}" data-uid="${esc(job.uid)}">
<div class="top">
  <div class="score ${band(job.score)}">${job.score ?? "&ndash;"}</div>
  <div>
    <div class="ttl">${esc(job.title)}</div>
    <div class="co">${esc(job.company || "unknown company")}</div>
  </div>
</div>
${blocked.length ? `<p class="blk">Requires what you cannot supply: <b>${esc(blocked.join(", "))}</b>${
  docked ? ` &middot; scored ${esc(String(job.score_raw))} before this` : ""}</p>` : ""}
<div class="tags">
  <span class="tag src">${esc(job.source)}</span>
  ${region ? `<span class="tag">${esc(region)}</span>` : ""}
  ${salary ? `<span class="tag">${esc(salary)}</span>` : ""}
  ${job.cv_variant ? `<span class="tag cv">CV: ${esc(job.cv_variant)}</span>` : ""}
  ${job.has_cover ? `<span class="tag cov">letter drafted</span>` : ""}
  ${job.posted_at ? `<span class="tag">${esc(String(job.posted_at).slice(0, 10))}</span>` : ""}
</div>
${job.why ? `<p class="why">${esc(job.why)}</p>` : ""}
<div class="acts">
  ${applyUrl ? `<a class="apply" href="${esc(applyUrl)}" target="_blank" rel="noopener noreferrer">Apply &rarr;</a>` : ""}
  ${!applyUrl && job.url ? `<span class="tag">link rejected</span>` : ""}
  ${moves.map((s) => `<button data-to="${s}">${LABEL[s]}</button>`).join("")}
  ${job.state !== "new" ? `<button data-to="new">Reset</button>` : ""}
</div></article>`;
}

// Kind labels are shared with the server render rather than duplicated: a new
// requirement kind must not be pretty on a card and raw in the panel.
const SCRIPT = `
var LBL = ${JSON.stringify(BLOCKER_LABEL)};

function el(tag, cls, text) {
  var n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

var ov = null;
function onKey(e) { if (e.key === 'Escape') closeDetail(); }
function closeDetail() {
  if (!ov) return;
  ov.remove();
  ov = null;
  document.removeEventListener('keydown', onKey);
}
function section(title, node) {
  var s = el('div', 'dsec');
  s.appendChild(el('h3', null, title));
  s.appendChild(node);
  return s;
}

// Everything below builds nodes and sets textContent. None of this content is
// ever assigned as HTML: a letter is model output and a requirement detail is
// employer copy, so both are treated as text no matter what they contain.
async function openDetail(uid) {
  closeDetail();
  ov = el('div');
  ov.id = 'ov';
  ov.addEventListener('click', function (e) { if (e.target === ov) closeDetail(); });
  var pane = el('div');
  pane.id = 'pane';
  pane.setAttribute('role', 'dialog');
  pane.setAttribute('aria-modal', 'true');
  pane.setAttribute('aria-label', 'Job detail');
  var x = el('button', null, 'Close');
  x.id = 'x';
  x.addEventListener('click', closeDetail);
  pane.appendChild(x);
  var body = el('div');
  body.appendChild(el('p', 'nolet', 'Loading...'));
  pane.appendChild(body);
  ov.appendChild(pane);
  document.body.appendChild(ov);
  document.addEventListener('keydown', onKey);
  x.focus();

  var data;
  try {
    var res = await fetch('/api/job?uid=' + encodeURIComponent(uid));
    if (!res.ok) throw new Error('HTTP ' + res.status);
    data = await res.json();
  } catch (err) {
    body.textContent = '';
    body.appendChild(el('p', 'nolet', 'Could not load this job: ' + err.message));
    return;
  }

  body.textContent = '';
  var j = data.job;
  body.appendChild(el('h2', null, j.title));
  body.appendChild(el('div', 'co', (j.company || 'unknown company') + ' · ' + j.source +
    (j.posted_at ? ' · posted ' + String(j.posted_at).slice(0, 10) : '')));

  if (j.score !== null && j.score !== undefined) {
    var a = el('p', 'arith');
    a.appendChild(el('b', null, 'Score ' + j.score));
    if (j.score_raw !== null && j.score_raw !== undefined && j.score_raw !== j.score) {
      a.appendChild(document.createTextNode(
        ' · the model scored ' + j.score_raw + '; ' + (j.score_raw - j.score) +
        ' was docked here, not by the model'));
    }
    body.appendChild(a);
  }

  if (j.why) body.appendChild(section('Why it scored that', el('p', null, j.why)));

  var reqs = data.requirements || [];
  if (reqs.length) {
    var ul = el('ul', 'rq');
    reqs.forEach(function (r) {
      var li = el('li');
      li.appendChild(el('span', 'k', LBL[r.kind] || r.kind));
      if (r.mandatory) li.appendChild(el('span', 'm', 'required'));
      if (r.blocking) li.appendChild(el('span', 'b', 'you cannot supply this'));
      if (r.detail) li.appendChild(el('div', 'd', r.detail));
      ul.appendChild(li);
    });
    body.appendChild(section('What this posting asks you to produce', ul));
  }

  var wrap = el('div');
  if (data.cover && data.cover.body) {
    wrap.appendChild(el('pre', 'letter', data.cover.body));
    var copy = el('button', null, 'Copy letter');
    copy.style.marginTop = '.6rem';
    copy.addEventListener('click', function () {
      navigator.clipboard.writeText(data.cover.body).then(
        function () { copy.textContent = 'Copied'; },
        function () { copy.textContent = 'Copy blocked — select the text instead'; });
    });
    wrap.appendChild(copy);
    wrap.appendChild(el('p', 'note', 'This is a draft and has not been sent.' +
      (data.cover.drafted_at ? ' Drafted ' + String(data.cover.drafted_at).slice(0, 10) + '.' : '') +
      ' The file on disk is the original; editing here changes nothing.'));
  } else {
    wrap.appendChild(el('p', 'nolet', 'No letter drafted for this job.'));
    wrap.appendChild(el('p', 'note', 'Letters are drafted during a run for jobs at or above ' +
      'their own board draft_at, then sent here with: py push.py --with-covers ' +
      '--url http://127.0.0.1:8787'));
  }
  body.appendChild(section('Cover letter', wrap));
}

document.addEventListener('click', async (e) => {
  const btn = e.target.closest('button[data-to]');
  if (btn) {
    const card = btn.closest('.card');
    card.classList.add('busy');
    try {
      const res = await fetch('/api/state', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ uid: card.dataset.uid, state: btn.dataset.to }),
      });
      if (!res.ok) throw new Error(await res.text());
      card.remove();
      if (!document.querySelector('.card')) location.reload();
    } catch (err) {
      card.classList.remove('busy');
      alert('Could not save: ' + err.message);
    }
    return;
  }
  // Anything already interactive keeps its own meaning. Only the inert parts of
  // a card open the panel, so Apply still applies and Passed still passes.
  if (e.target.closest('#ov') || e.target.closest('a') || e.target.closest('button')) return;
  const open = e.target.closest('.card');
  if (open && open.dataset.uid) openDetail(open.dataset.uid);
});`;

// Shared header chrome, so the Improve view sits inside the same board tabs as
// the list rather than becoming a page that loses your place.
function chrome(q, sources, source, stateRow, view) {
  const boardTabs = sources.length > 1
    ? `<nav class="src">` +
      `<a href="${q({ view })}" class="${source ? "" : "on"}">All</a>` +
      sources.map((s) =>
        `<a href="${q({ view, src: s.source })}" class="${s.source === source ? "on" : ""}">` +
        `${esc(s.source)} <b>${s.n}</b></a>`).join("") + `</nav>`
    : "";
  return `<header>
<h1>joblist <span>&middot; triage</span></h1>
${boardTabs}${stateRow}</header>`;
}

export function renderImprove(kinds, samples, sources, source, scored, covered) {
  const q = (params) => {
    const path = params.view === "improve" ? "/improve" : "/";
    const qs = Object.entries(params).filter(([k, v]) => v && k !== "view")
      .map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");
    return qs ? `${path}?${qs}` : path;
  };

  const stateRow = `<nav><a href="${q({ src: source })}">&larr; Jobs</a>` +
    `<a href="${q({ view: "improve", src: source })}" class="on">To improve</a></nav>`;

  // Split, not sorted. The things that cost you a listing today are a different
  // kind of item from the things that merely came up, and burying the first
  // group inside a single frequency ranking is what makes a backlog unreadable.
  const blocking = kinds.filter((k) => k.blocking);
  const others = kinds.filter((k) => !k.blocking);
  const top = Math.max(1, ...kinds.map((k) => k.listings));

  const row = (k) => {
    const pct = scored ? Math.round((k.listings / scored) * 100) : 0;
    const eg = (samples[k.kind] || []).slice(0, 2);
    return `<article class="imp${k.blocking ? " on" : ""}">
<div class="impt">
  <div class="impn">${k.listings}</div>
  <div>
    <div class="ttl">${esc(BLOCKER_LABEL[k.kind] || k.kind)}</div>
    <div class="co">${k.must} of ${k.listings} made it a condition &middot; ${pct}% of all listings</div>
  </div>
</div>
<div class="impbar"><i style="width:${Math.round((k.listings / top) * 100)}%"></i></div>
${eg.length ? `<ul class="impeg">${eg.map((d) => `<li>${esc(d)}</li>`).join("")}</ul>` : ""}
</article>`;
  };

  const scope = source ? ` on <b>${esc(source)}</b>` : "";
  const body = kinds.length
    ? (blocking.length
        ? `<h2 class="imph">Blocking you<span> &mdash; asked as a condition, and flagged as something you cannot supply</span></h2>`
          + blocking.map(row).join("")
        : `<p class="scope">Nothing you have flagged is currently being demanded.</p>`)
      + (others.length
        ? `<h2 class="imph">Also asked for<span> &mdash; you are not blocked on these, but they keep coming up</span></h2>`
          + others.map(row).join("")
        : "")
    : `<p class="empty">No requirements recorded yet${scope}.</p>`;

  return shell("joblist — to improve", chrome(q, sources, source, stateRow, "improve") +
`<main><p class="scope">${covered} of ${scored} listings${scope} asked for something to be produced.
One row per requirement, counted once per listing.</p>
${body}</main>
<footer>Flagged items come from <code>deliverables.cannot_provide</code> in profile.yaml
&middot; <a href="/logout" style="color:inherit">sign out</a></footer>`);
}

export function renderList(jobs, tab, counts, sources = [], source = null) {
  const q = (params) =>
    "/?" + Object.entries(params).filter(([, v]) => v).map(([k, v]) =>
      `${k}=${encodeURIComponent(v)}`).join("&");

  // Platform is the OUTER axis, state the inner one, and that order is the
  // point rather than a layout preference. A score is only meaningful against
  // the board it was given on: onlinejobs.ph tops out around $25/hr while
  // himalayas routinely clears $45, so a 78 on one is not a 78 on the other.
  // Ranking a mixed list by score therefore compares numbers that were never
  // on the same scale. Each tab is one board, ranked within itself.
  const boardTabs = sources.length > 1
    ? `<nav class="src">` +
      `<a href="${q({ tab })}" class="${source ? "" : "on"}">All <b>${counts.allTotal || 0}</b></a>` +
      sources.map((s) =>
        `<a href="${q({ tab, src: s.source })}" class="${s.source === source ? "on" : ""}">` +
        `${esc(s.source)} <b>${s.n}</b></a>`).join("") + `</nav>`
    : "";

  const tabs = STATES.map((s) =>
    `<a href="${q({ tab: s, src: source })}" class="${s === tab ? "on" : ""}">` +
    `${LABEL[s]} <b>${counts[s] || 0}</b></a>`,
  ).join("") +
    // Not a triage state, so it is set apart rather than lined up with them --
    // it changes what you are looking at, not which pile you are looking in.
    `<a class="alt" href="${"/improve" + (source ? `?src=${encodeURIComponent(source)}` : "")}">To improve</a>`;

  // Say what the ranking is relative to. Without this the "All" view silently
  // interleaves two incomparable scales and reads as one ranked list.
  //
  // Also say so when the query's LIMIT cut the tail off. Picking a board keeps
  // each list well under it, but "All" can exceed it, and a truncated list that
  // claims the tab's full count is a quiet lie about what you have triaged.
  const shown = jobs.length;
  const total = counts[tab] || shown;
  const count = shown < total
    ? `showing top <b>${shown}</b> of ${total} ${esc(LABEL[tab]).toLowerCase()}`
    : `${shown} ${esc(LABEL[tab]).toLowerCase()}`;
  const caption = shown
    ? `<p class="scope">${count} · ranked by score` +
      (source
        ? ` within <b>${esc(source)}</b>`
        : `, <b>across all boards — scores are not comparable between them</b>`) +
      `</p>`
    : "";

  const scope = source ? ` in ${esc(source)}` : "";
  const body = jobs.length
    ? jobs.map(card).join("")
    : `<p class="empty">Nothing in ${esc(LABEL[tab])}${scope}.</p>`;
  const title = source ? `joblist — ${esc(source)} · ${LABEL[tab]}` : `joblist — ${LABEL[tab]}`;
  return shell(title, `<header>
<h1>joblist <span>&middot; triage</span></h1>
${boardTabs}<nav>${tabs}</nav></header>
<main>${caption}${body}</main>
<footer>${counts.total || 0} scored jobs${scope} &middot; <a href="/logout" style="color:inherit">sign out</a></footer>
<script>${SCRIPT}</script>`);
}
