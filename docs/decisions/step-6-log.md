# Step 6 log: the dashboard

Date: 2026-10-17. Tasks 6.1 to 6.9 of [../TASKS.md](../TASKS.md). A CSM can now work the whole Monday list without leaving the app.

| Task | Page | What it shows |
|---|---|---|
| 6.1 | `/list?sort=` | Rank, colour, account, probability, ARR, days to renewal and rank score. It re-sorts by rank score (the default), probability, ARR or days to renewal. Escalated tasks are pinned first whatever the sort |
| 6.2 | `/list` | "new", "moved up" or "moved down" against the first night of the current week, by the same model |
| 6.3 | `/list` | A separate "Not enough signal" table: accounts under 30 days old or with fewer than 5 events, with their event count. They are never ranked and never green |
| 6.4 | `/list`, `POST /tasks/{id}/status` | The task, its rule and its age in business days, with the briefing behind a "Why" disclosure. Buttons: done, snooze a week, and dismiss with a required reason. Every change writes a `task_events` row. A dismissal records the probability it suppresses below. An escalated task has a red badge |
| 6.5 | `/accounts/{id}` | Tasks with their briefing and audit trail, score history with colour, contracts, and a timeline with the match method and the received time. An "arrived late" marker names the score that missed the event |
| 6.6 | `/scores/{id}` | The reconciliation check stated as a result, the `rescore` command to reproduce it, colour and coverage, the count of events that arrived after the snapshot, the briefing, and "Zendesk not connected" instead of a number |
| 6.7 | `/runs`, `/runs/{id}` | The last 30 nights (status, trigger, events in, scored, tasks, escalations, duration, model) and the next scheduled fire from `NIGHTLY_CRON`. A night shows its stage timings, transitions, the tasks it raised and its counts. Every page carries the staleness header |
| 6.8 | `/quarantine`, `/misses` | Quarantine and rejects by reason and source, with the latest quarantined events and what was tried. Worst misses: lost renewals the live system scored in the 90 days before but never put in the top 8, largest ARR first |
| 6.9 | tests | `tests/test_dashboard.py`: every page renders for a seeded tenant, and the staleness warning, escalation pin, CSRF refusal, task buttons and audit rows all work. Another tenant's run, account, score and task answer 404. The marker and business-day rules are unit-tested |

## Decisions

- **CSRF.** A per-session token sits in the signed cookie. It goes in a hidden field on every form and is compared with `secrets.compare_digest`. The switcher needs it too. The admin trigger is JSON-only instead (Step 5).
- **The staleness header is on every tenant page,** not only the list. It turns into a warning when the newest night did not succeed, or the last success is over a day old by the page's clock (`CLOCK_OVERRIDE` honoured).
- **Briefing reasons are actionable signals only.** Previewing the full config showed reasons like "contract size" and "days to renewal". They are true contributions, and they stay on the score page, but they are not a reason to call. Reasons are now drawn from the source-backed features and `prior_contraction`. Ratios read as percentages.
- **The web process still imports no numpy or pandas.** Task age uses a plain-Python business-day count, which is checked against `np.busday_count` in a test.
- **Worst misses on the live system** count only renewals that were scored beforehand. Before the first deploy there was nothing to miss with. The offline evaluation lists misses across the whole holdout.

## Verification

- 231 tests pass, 13 of them new in `tests/test_dashboard.py`.
- Previewed locally against the full config after one Step 5 night. t1 got 144 seed tasks: 17 CSMs times 8, plus 8 in the CS-lead queue. The list, runs, quarantine and score pages showed the expected data.
