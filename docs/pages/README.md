# Standalone pages

Two self-contained HTML pages, each explaining one half of the project to a
reader who has not seen the repo. They duplicate no logic — the numbers in them
are measured from real runs and the diagrams are the same mermaid sources as
`../../README.md` and `../how-this-was-built.md`.

| File | Explains | Published at |
|---|---|---|
| `pipeline.html` | The system: 2,690 listings in, 178 to the model, and why the boundary sits there | https://claude.ai/code/artifact/6092d1cb-ca78-40b7-b151-246d33bc5eea |
| `build-process.html` | The process: the agent control structure, the four rules, the three bugs only measurement caught | https://claude.ai/code/artifact/90138527-590c-487b-911b-83e6693b0eb0 |

**These are snapshots, not generated output.** Their figures (funnel counts,
test count, per-board totals) were correct on 2026-09-04 and will drift. If you
update a diagram in the repo, update it here too, or delete these rather than
let them make stale claims — a page that says `265 offline tests` when the
suite has moved on is worse than no page.

Republishing after an edit: pass the URL above as `url` so the link is kept
rather than creating a second artifact.
