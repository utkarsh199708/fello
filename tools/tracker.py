# ruff: noqa: E501  (embedded HTML template and data tables read better unwrapped)
"""Build tracker: renders docs/TASKS.md + docs/tracker_state.json into docs/tracker.html.

Run: python tools/tracker.py
TASKS.md checkboxes are the source of truth for "done". tracker_state.json holds
agent assignments, in-progress/blocked/cut flags, and the phase log.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT / "docs" / "TASKS.md"
STATE = ROOT / "docs" / "tracker_state.json"
OUT = ROOT / "docs" / "tracker.html"

ROLES = {
    "lead": (
        "Tech lead",
        "Plans each step, reviews every diff, owns the 10x analysis",
        "opus",
        "xhigh",
    ),
    "platform": (
        "Platform engineer",
        "Schema, roles, row-level security, tenant context",
        "opus",
        "high",
    ),
    "data": ("Data engineer", "Synthetic generator and data report", "sonnet", "high"),
    "pipeline": (
        "Pipeline engineer",
        "Loader, identity resolution, dedup, drift, quality gate",
        "sonnet",
        "high",
    ),
    "ml": ("ML engineer", "Features, labels, model, evaluation", "opus", "high"),
    "jobs": ("Jobs engineer", "Nightly run, policy, tasks, briefings", "opus", "high"),
    "web": ("Web engineer", "FastAPI pages and the CSM workflow", "sonnet", "medium"),
    "qa": ("QA engineer", "Tests and done-when verification", "sonnet", "high"),
    "writer": ("Technical writer", "README, deck, Loom script", "sonnet", "medium"),
    "ops": ("DevOps engineer", "Docker, config, CI", "haiku", "medium"),
    "you": ("You", "Accounts, payment, secrets, recording, submitting", "-", "-"),
}

ASSIGN = {
    **{f"D{i}": "you" for i in range(1, 7)},
    "0.1": "writer",
    "0.2": "writer",
    "0.3": "ops",
    "0.4": "ops",
    "0.5": "ops",
    "0.6": "writer",
    "1.1": "data",
    "1.2": "data",
    "1.3": "platform",
    "1.4": "platform",
    "1.5": "pipeline",
    "1.6": "pipeline",
    "1.7": "ml",
    "1.8": "ml",
    "1.9": "ml",
    "1.10": "jobs",
    "1.11": "jobs",
    "1.12": "web",
    "1.13": "jobs",
    "1.14": "qa",
    "1.15": "you",
    **{f"2.{i}": "data" for i in range(1, 14)},
    "2.14": "qa",
    "2.15": "you",
    **{f"3.{i}": "pipeline" for i in range(1, 7)},
    "3.7": "qa",
    **{f"4.{i}": "ml" for i in range(1, 11)},
    "4.9": "qa",
    **{f"5.{i}": "jobs" for i in range(1, 7)},
    "5.7": "web",
    "5.8": "qa",
    **{f"6.{i}": "web" for i in range(1, 9)},
    "6.9": "qa",
    "7.1": "you",
    "7.2": "lead",
    "7.3": "writer",
    "7.4": "writer",
    "7.5": "writer",
    "7.6": "you",
    "7.7": "you",
    "8.1": "web",
}
# Effort overrides where a task is harder or easier than its role's default.
EFFORT = {
    "1.3": "xhigh",
    "1.4": "xhigh",
    "2.2": "xhigh",
    "4.6": "xhigh",
    "5.4": "xhigh",
    "1.11": "medium",
    "2.13": "medium",
    "0.5": "low",
    "6.2": "low",
}

LINE = re.compile(r"^- \[(?P<x>[ x])\] \*\*(?P<id>D\d|\d+\.\d+)\.? (?P<title>[^*]+)\*\*")
STEP = re.compile(r"^## (Step (\d)|Decisions|Optional)")


def parse():
    tasks, step = [], "D"
    for line in TASKS.read_text(encoding="utf-8").splitlines():
        m = STEP.match(line)
        if m:
            step = m.group(2) or ("D" if m.group(1) == "Decisions" else "8")
        m = LINE.match(line)
        if m:
            tasks.append(
                {
                    "id": m["id"],
                    "title": m["title"].strip().rstrip("."),
                    "done": m["x"] == "x",
                    "step": step,
                }
            )
    return tasks


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"status": {}, "log": [], "updated": ""}


STEP_NAMES = {
    "D": "Your decisions",
    "0": "Brief and repo",
    "1": "Walking skeleton",
    "2": "Generator to spec",
    "3": "Pipeline hardening",
    "4": "Model and evaluation",
    "5": "Agentic job",
    "6": "Dashboard deepening",
    "7": "Ship",
    "8": "Optional",
}


def status_of(t, st):
    if t["done"]:
        return "done"
    s = st["status"].get(t["id"])
    if s:
        return s
    return "you" if ASSIGN.get(t["id"]) == "you" else "todo"


def render():
    tasks, st = parse(), load_state()
    for t in tasks:
        role = ASSIGN.get(t["id"], "lead")
        name, _, model, effort = ROLES[role]
        t.update(
            role=role,
            agent=name,
            model=model,
            effort=EFFORT.get(t["id"], effort),
            status=status_of(t, st),
        )
    e = html.escape
    agent_tasks = [t for t in tasks if t["role"] != "you"]
    done = sum(t["status"] == "done" for t in agent_tasks)
    total = len(agent_tasks)
    pct = round(100 * done / total) if total else 0
    yours_open = [t for t in tasks if t["role"] == "you" and t["status"] != "done"]

    steps_html = []
    for key, label in STEP_NAMES.items():
        ts = [t for t in tasks if t["step"] == key]
        if not ts:
            continue
        d = sum(t["status"] == "done" for t in ts)
        state = (
            "done"
            if d == len(ts)
            else "active"
            if any(t["status"] in ("in_progress", "done") for t in ts)
            else "idle"
        )
        steps_html.append(
            f'<li class="step {state}"><span class="sk">{"D" if key == "D" else key}</span>'
            f'<span class="sn">{e(label)}</span><span class="sc">{d}/{len(ts)}</span>'
            f'<span class="bar"><i style="width:{round(100 * d / len(ts))}%"></i></span></li>'
        )

    roster = []
    for key, (name, job, model, effort) in ROLES.items():
        if key == "you":
            continue
        mine = [t for t in tasks if t["role"] == key]
        d = sum(t["status"] == "done" for t in mine)
        live = [t["id"] for t in mine if t["status"] == "in_progress"]
        roster.append(
            f'<tr><td><b>{e(name)}</b><div class="muted">{e(job)}</div></td>'
            f'<td><span class="chip m-{model}">{model}</span></td><td>{effort}</td>'
            f'<td class="num">{d}/{len(mine)}</td>'
            f"<td>{'<span class=pill-live>working on ' + e(', '.join(live)) + '</span>' if live else ''}</td></tr>"
        )

    rows = []
    for t in tasks:
        rows.append(
            f'<tr data-step="{t["step"]}" data-status="{t["status"]}">'
            f'<td class="num id">{e(t["id"])}</td><td>{e(t["title"])}</td>'
            f'<td>{e(t["agent"])}</td><td><span class="chip m-{t["model"]}">{t["model"]}</span></td>'
            f'<td>{t["effort"]}</td><td><span class="st st-{t["status"]}">{t["status"].replace("_", " ")}</span></td></tr>'
        )

    log = "".join(
        f"<li><time>{e(x['at'])}</time> {e(x['msg'])}</li>" for x in reversed(st["log"][-30:])
    )
    yours = "".join(f"<li><b>{e(t['id'])}</b> {e(t['title'])}</li>" for t in yours_open)

    page = TEMPLATE.format(
        pct=pct,
        done=done,
        total=total,
        steps="".join(steps_html),
        roster="".join(roster),
        rows="".join(rows),
        log=log or "<li>No runs yet.</li>",
        yours=yours or "<li>Nothing waiting on you.</li>",
        updated=e(st.get("updated", "")),
    )
    OUT.write_text(page, encoding="utf-8", newline="\n")
    return done, total


TEMPLATE = """<title>Fello Build Tracker</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&family=Fraunces:opsz,wght@9..144,600&display=swap">
<style>
/* Layout: ops board. Summary strip, step ladder, agent roster, full task ledger. */
:root {{
  --bg:#f5f6f3; --panel:#ffffff; --fg:#1d2420; --muted:#5f6b64; --line:#dde2dc;
  --accent:#2f6f5e; --accent-soft:#dcebe5;
  --ok:#2e7d4f; --warn:#a86a00; --crit:#b3402f; --live:#2d5fa8;
  --display:"Fraunces", Georgia, serif; --body:"IBM Plex Sans", system-ui, sans-serif; --mono:"IBM Plex Mono", ui-monospace, monospace;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg:#141917; --panel:#1b211f; --fg:#e4ebe6; --muted:#9aa8a0; --line:#2c3531;
  --accent:#6fbfa6; --accent-soft:#22352f; --ok:#5cc28a; --warn:#e0a644; --crit:#ec7a66; --live:#7fa9ec; color-scheme:dark; }} }}
:root[data-theme="dark"] {{
  --bg:#141917; --panel:#1b211f; --fg:#e4ebe6; --muted:#9aa8a0; --line:#2c3531;
  --accent:#6fbfa6; --accent-soft:#22352f; --ok:#5cc28a; --warn:#e0a644; --crit:#ec7a66; --live:#7fa9ec; color-scheme:dark; }}
body {{ background:var(--bg); color:var(--fg); font:14px/1.5 var(--body); }}
.wrap {{ max-width:1100px; margin:0 auto; padding-block:28px 48px;
  padding-left:max(16px, env(safe-area-inset-left, 0px)); padding-right:max(16px, env(safe-area-inset-right, 0px));
  display:grid; gap:22px; }}
h1 {{ font:600 30px/1.1 var(--display); margin:0; text-wrap:balance; }}
h2 {{ font:600 13px var(--body); text-transform:uppercase; letter-spacing:.08em; color:var(--muted); margin:0 0 10px; }}
.muted {{ color:var(--muted); font-size:12.5px; }}
header {{ display:flex; flex-wrap:wrap; gap:16px; align-items:end; justify-content:space-between; }}
.meter {{ display:flex; align-items:center; gap:12px; min-width:0; }}
.meter b {{ font:500 34px var(--mono); font-variant-numeric:tabular-nums; }}
.track {{ width:220px; max-width:50vw; height:8px; background:var(--line); border-radius:4px; overflow:hidden; }}
.track i {{ display:block; height:100%; background:var(--accent); }}
.grid {{ display:grid; grid-template-columns: minmax(0,1fr) minmax(0,1fr); gap:22px; }}
@media (max-width:760px) {{ .grid {{ grid-template-columns:minmax(0,1fr); }} }}
section.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:16px; min-width:0; }}
ol.steps {{ list-style:none; margin:0; padding:0; display:grid; gap:8px; }}
.step {{ display:grid; grid-template-columns:28px 1fr auto 90px; gap:10px; align-items:center; }}
.sk {{ font:500 12px var(--mono); width:24px; height:24px; display:grid; place-items:center; border-radius:50%; border:1.5px solid var(--line); }}
.step.done .sk {{ background:var(--ok); border-color:var(--ok); color:var(--panel); }}
.step.active .sk {{ border-color:var(--live); color:var(--live); }}
.sc {{ font:12.5px var(--mono); font-variant-numeric:tabular-nums; color:var(--muted); }}
.bar {{ height:6px; background:var(--line); border-radius:3px; overflow:hidden; }}
.bar i {{ display:block; height:100%; background:var(--accent); }}
.scroll {{ overflow-x:auto; }}
table {{ border-collapse:collapse; width:100%; font-size:13px; }}
th {{ text-align:left; font-weight:500; color:var(--muted); font-size:12px; padding:6px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }}
td {{ padding:7px 8px; border-bottom:1px solid var(--line); vertical-align:top; }}
.num {{ font-family:var(--mono); font-variant-numeric:tabular-nums; white-space:nowrap; }}
.chip {{ font:500 11.5px var(--mono); padding:1px 7px; border-radius:10px; border:1px solid var(--line); white-space:nowrap; }}
.m-opus {{ color:var(--accent); background:var(--accent-soft); border-color:transparent; }}
.st {{ font-size:12px; padding:1px 8px; border-radius:10px; white-space:nowrap; border:1px solid currentColor; }}
.st-done {{ color:var(--ok); }} .st-in_progress {{ color:var(--live); }} .st-todo {{ color:var(--muted); }}
.st-you {{ color:var(--warn); }} .st-blocked {{ color:var(--crit); }} .st-cut {{ color:var(--muted); text-decoration:line-through; }}
.pill-live {{ font-size:12px; color:var(--live); }}
ul.plain {{ margin:0; padding-left:18px; display:grid; gap:4px; }}
ul.log {{ list-style:none; margin:0; padding:0; display:grid; gap:6px; font-size:13px; }}
ul.log time {{ font:12px var(--mono); color:var(--muted); margin-right:6px; }}
.filters {{ display:flex; flex-wrap:wrap; gap:6px; margin-bottom:10px; }}
.filters button {{ font:12.5px var(--body); padding:4px 10px; border-radius:14px; border:1px solid var(--line); background:var(--panel); color:var(--fg); cursor:pointer; }}
.filters button[aria-pressed="true"] {{ background:var(--accent); color:var(--panel); border-color:var(--accent); }}
.filters button:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; }}
</style>
<div class="wrap">
  <header>
    <div><h1>Fello Build Tracker</h1><div class="muted">Customer health Monday list &middot; updated {updated}</div></div>
    <div class="meter"><b>{pct}%</b><div><div class="track"><i style="width:{pct}%"></i></div><div class="muted">{done} of {total} agent tasks done</div></div></div>
  </header>
  <div class="grid">
    <section class="panel"><h2>Steps</h2><ol class="steps">{steps}</ol></section>
    <section class="panel"><h2>Waiting on you</h2><ul class="plain">{yours}</ul>
      <h2 style="margin-top:16px">Run log</h2><ul class="log">{log}</ul></section>
  </div>
  <section class="panel"><h2>Agents</h2><div class="scroll"><table>
    <thead><tr><th>Role</th><th>Model</th><th>Effort</th><th>Done</th><th>Now</th></tr></thead><tbody>{roster}</tbody></table></div></section>
  <section class="panel"><h2>Tasks</h2>
    <div class="filters" role="group" aria-label="Filter by status">
      <button id="f-all" aria-pressed="true" data-f="all">All</button><button id="f-open" aria-pressed="false" data-f="open">Open</button>
      <button id="f-live" aria-pressed="false" data-f="in_progress">In progress</button><button id="f-you" aria-pressed="false" data-f="you">Yours</button>
      <button id="f-done" aria-pressed="false" data-f="done">Done</button></div>
    <div class="scroll"><table id="tasks"><thead><tr><th>ID</th><th>Task</th><th>Agent</th><th>Model</th><th>Effort</th><th>Status</th></tr></thead>
    <tbody>{rows}</tbody></table></div></section>
</div>
<script>
document.querySelectorAll('.filters button').forEach(b => b.addEventListener('click', () => {{
  document.querySelectorAll('.filters button').forEach(x => x.setAttribute('aria-pressed', x === b));
  const f = b.dataset.f;
  document.querySelectorAll('#tasks tbody tr').forEach(r => {{
    const s = r.dataset.status;
    r.hidden = !(f === 'all' || s === f || (f === 'open' && s !== 'done' && s !== 'cut'));
  }});
}}));
</script>
"""

if __name__ == "__main__":
    d, t = render()
    print(f"tracker: {d}/{t} agent tasks done -> {OUT}")
