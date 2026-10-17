# Step 5 log: the agentic job

Date: 2026-10-17. Tasks 5.1 to 5.8 of [../TASKS.md](../TASKS.md).

## What the night now does

Seven timed stages per tenant, in one transaction. The timings are in `job_runs.stage_ms`:

1. ingest
2. snapshot
3. score
4. gate
5. transitions
6. policy and tasks
7. escalation

If the gate trips, stages 5 to 7 never run, and the night rolls back as `blocked`. Every decision is a row:

| Thing | Table | Key |
|---|---|---|
| Rank change against the previous completed night | `transitions` (from and to score ids, ranks, entered or left the top k) | (account, run) |
| A task | `tasks` (rule, policy version, queue, probability, reasons per night, links to score, transition, briefing) | (account, run_date) |
| Why it was raised | `briefings` (the deterministic reason list, and the template text rendered from it) | score |
| What happened to it | `task_events`: created, reasons appended, escalated, snooze ended, status changed, budget exceeded, rebaselined | append-only |

## The policy (`app/jobs/policy.py`, version 1)

It is pure, and its unit tests cover every rule:

- the three-night anti-flap trigger;
- the probability rise of more than 0.15 since the last task;
- the seed rule on the bootstrap night, on a rebaseline and on the first night under a new policy version;
- dedup: tonight's reasons go to the active task;
- dismissal suppression until the probability rises more than 0.15 past the dismissed value;
- a budget of k new tasks per queue per night, the highest `rank_score` first, with the breach recorded;
- accounts with no owner go to the CS-lead queue (`owner_missing`);
- not-enough-signal accounts are never tasked.

Escalation moves an open task to `escalated` after 5 business days if its account renews within 60 days. It is recorded once, and is a no-op on a re-run.

## Rerun rules (5.2)

A succeeded night is a `no_op`. A failed, blocked or crashed (`running`) night is re-claimed and re-executed. `nightly --force` re-runs a succeeded night only if it is the tenant's latest; an older night is refused. `nightly --as-of <night>` fills a missed night. Every write is keyed or guarded, so a re-run adds only what is missing.

## Admin trigger (5.7)

`POST /admin/jobs/nightly` takes the JSON body `{"as_of": ..., "tenant": ...}` and sits behind the shared password. It calls the scheduler's own `run_nightly` with `trigger="manual"`. It accepts JSON only: a cross-site HTML form cannot send it without a CORS preflight, so it is not open to CSRF. Use it on a night that has not run yet. A forced re-run of a scheduled night replaces its `trigger` with the new one, which would erase the "nobody triggered it" record.

## Decisions

- **The gate stays before the actions but after scoring.** Its mean-shift check needs tonight's scores. PLAN.md lists the gate as stage 2. What matters is the same: nothing a CSM sees is written for a blocked night.
- **Briefings are written with their tasks** (in stage 6), only for tasks created tonight.
- **The LLM paragraph is cut** (part of 5.6). It is first in the plan's cut order, and narration is still off (D5). The briefing is the reason list plus a template, which can say nothing the model did not compute. `job_runs.counts.actions.llm_calls` is 0.
- **Seeding on the first night under a policy version.** The live VM had two nights before this policy existed. Without the rule, accounts already in a top 8 would never get a seed task. A future policy change reseeds the same way a model change does.

## Verification

- 218 tests pass, 16 of them new in `tests/test_agentic.py`. They cover:
  - identical scores give zero new tasks;
  - an oscillating score gives at most one task per three nights;
  - a crash then a re-run gives one clean night and no duplicate task;
  - escalation fires once;
  - a new model rebaselines and seeds;
  - the budget, dismissal, dedup and queue rules;
  - a briefing names an unconnected source and never shows zero;
  - the admin trigger runs as `manual` and refuses forms, unknown tenants and strangers.
