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
nav{display:flex;gap:.35rem;margin-top:.6rem;overflow-x:auto;-webkit-overflow-scrolling:touch}
nav a{flex:0 0 auto;padding:.35rem .7rem;border-radius:999px;text-decoration:none;
color:var(--dim);background:var(--card);border:1px solid var(--line);font-size:.82rem;white-space:nowrap}
nav a.on{color:var(--bg);background:var(--fg);border-color:var(--fg);font-weight:600}
nav a b{font-weight:600}
main{padding:.75rem;max-width:44rem;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:.8rem;margin-bottom:.7rem}
.card.busy{opacity:.45}
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
.why{margin:.55rem 0 0;font-size:.86rem;color:var(--dim)}
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

function card(job) {
  const region = regionText(job.regions);
  const salary = SALARY[job.salary_signal] || null;
  const moves = STATES.filter((s) => s !== job.state && s !== "new");
  const applyUrl = safeUrl(job.url);
  return `<article class="card" data-uid="${esc(job.uid)}">
<div class="top">
  <div class="score ${band(job.score)}">${job.score ?? "&ndash;"}</div>
  <div>
    <div class="ttl">${esc(job.title)}</div>
    <div class="co">${esc(job.company || "unknown company")}</div>
  </div>
</div>
<div class="tags">
  <span class="tag src">${esc(job.source)}</span>
  ${region ? `<span class="tag">${esc(region)}</span>` : ""}
  ${salary ? `<span class="tag">${esc(salary)}</span>` : ""}
  ${job.cv_variant ? `<span class="tag cv">CV: ${esc(job.cv_variant)}</span>` : ""}
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

const SCRIPT = `
document.addEventListener('click', async (e) => {
  const btn = e.target.closest('button[data-to]');
  if (!btn) return;
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
});`;

export function renderList(jobs, tab, counts) {
  const tabs = STATES.map((s) =>
    `<a href="/?tab=${s}" class="${s === tab ? "on" : ""}">${LABEL[s]} <b>${counts[s] || 0}</b></a>`,
  ).join("");
  const body = jobs.length
    ? jobs.map(card).join("")
    : `<p class="empty">Nothing in ${esc(LABEL[tab])}.</p>`;
  return shell(`joblist — ${LABEL[tab]}`, `<header>
<h1>joblist <span>&middot; triage</span></h1>
<nav>${tabs}</nav></header>
<main>${body}</main>
<footer>${counts.total || 0} scored jobs &middot; <a href="/logout" style="color:inherit">sign out</a></footer>
<script>${SCRIPT}</script>`);
}
