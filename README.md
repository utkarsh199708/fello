# Monday list

A ranked Monday list for customer success managers: the accounts most worth working this week, in order, each with reasons that click through to the raw events behind them.

A nightly job rescores every account with nobody pressing a button, opens a task when an account enters its CSM's top 8, escalates tasks nobody touched, and logs every decision.

Built for the Fello GTM AI take-home. The solution brief, written before any code, is [docs/brief.md](docs/brief.md) (it is the first commit). The design is [docs/PLAN.md](docs/PLAN.md), and the build log is [docs/decisions/](docs/decisions/).

- **Live:** https://136-83-131-193.sslip.io, behind one shared password. The username is ignored. The password is given with the submission, and `/health` is open.
- **In the app:** pick an organisation and a CSM on the switcher. The list, an account, a score's drill-down, the run history, quarantine and worst misses are one click away. Every number links back to what was computed from what.
- **All data is synthetic:** 5 organisations, 43 CSMs, 5,000 accounts and about 192,000 events from five sources.

## What I decided

**The problem, and so the product.** The request was a red/yellow/green health score with auto-email on red. The problem is prioritisation: each CSM can work perhaps 8 of 120 accounts a week, and the signals that predict a lost renewal are spread over five systems that nobody joins. So the product is a ranked list per CSM with reasons, not a score per account ([docs/brief.md](docs/brief.md)).

**Where I disagreed with the request, and what replaced it.**

- **No email to customers.** It fails three ways:
  - wrong recipient: the contact on file is often the champion who left;
  - wrong message: a ticket backlog and a failed payment need different conversations;
  - wrong trigger: accounts near a line on a noisy score flip week to week.

  An account entering a CSM's top 8 gets a task instead. A task untouched for 5 business days within 60 days of renewal is escalated and pinned to the top. That is what "nothing slips" means here: nothing goes unseen, and nothing is dropped silently.
- **Colour is a display derived from rank.** Red is the top 8 of the CSM's book, yellow the next 16, green the rest. An account with not enough signal (under 30 days or under 5 events) is grey and never green. The probability and the dollars at risk carry the absolute picture.
- **Rank by expected ARR at risk**, probability times ARR, with one-click re-sort by probability, ARR or renewal date.
- **The label is the outcome of the next renewal,** lost or contracted below 70% of the prior ARR, whenever that renewal falls. A 90-day window would mark every account further out as safe.

**How it is built.** Python 3.11, FastAPI with Jinja2 and vendored htmx, Postgres 16, SQLAlchemy Core and Alembic, LightGBM, and an APScheduler worker. It is one image, run as a web process and a worker process.

- **Multi-tenancy is enforced in the database.** Every tenant table has `FORCE ROW LEVEL SECURITY` with one policy on `app.tenant_id`, set only by `tenant_tx` through `set_config(..., true)`. With no context set, a query sees zero rows. The tenant comes from a signed, SameSite=Strict session cookie, never from a URL. Another tenant's id answers 404.
- **Point in time.** A feature at `as_of` sees only events received by `as_of`, with windows on `occurred_at`. Snapshots and scores are never rewritten, and the database grants no UPDATE or DELETE on them. A late event changes tonight's snapshot, never last week's.
- **The model is fitted, global and reproducible.** One LightGBM over all tenants, without `tenant_id` as a feature. It is deterministic, with reasons from `pred_contrib` that add up exactly to the margin. Each version stores its booster text, features, row counts per tenant and metrics. An evaluation fit on older renewal cycles is stored beside the live refit, and logistic regression stays as the challenger. `python -m app rescore <score_id>` reproduces any stored score. The same config trains the same version hash on Windows and in the Linux image.
- **The night is a staged, idempotent job:** ingest, snapshot, score, data-quality gate, transitions, policy and tasks, escalation.
  - A rerun of a succeeded night is a no-op, and a failed or blocked night is re-run.
  - A tripped gate publishes nothing.
  - `policy.py` (pure, version 1) holds the three-night anti-flap rule, seeding, dedup, dismissal suppression and a per-queue budget.
- **Traceability.** Every CSM-visible thing has a row and a foreign key back to what produced it: raw event, normalised event (with how it was matched), snapshot (with its ingest watermark), score (with the full contribution vector), model version, transition, task, briefing, task events and job run.

**Who connects as which database role.**

| Process | Role | Can do |
|---|---|---|
| `migrate` (worker start) | `app_owner` | Owns the tables and runs Alembic. FORCE RLS binds it too |
| web, nightly job, loader | `app` | Reads and writes within one tenant through `tenant_tx`. No UPDATE or DELETE on snapshots and scores |
| trainer, `evaluate`, `rescore` | `app_batch` | `BYPASSRLS`, the one deliberate cross-tenant reader. Writes only `model_versions` |
| test teardown | the Postgres superuser | Purges test tenants. Never used by the app |

**What the pipeline does with the mess** ([docs/decisions/step-2-log.md](docs/decisions/step-2-log.md), [step-3-log.md](docs/decisions/step-3-log.md)):

- **Identity.** Each event is matched by the crosswalk, then by email domain. Otherwise it goes to quarantine with a reason, and the count is on the list page.
- **Duplicates.** An exact replay is rejected and the first delivery kept. A re-sent ticket snapshot resolves to the newest state. A renewal reported by Salesforce and Stripe becomes one outcome.
- **Schema drift.** Each version is mapped to one shape, and an unreadable payload is rejected with a reason.
- **Thin accounts** are not green.
- **NULL is not zero.** An unconnected source is NULL and named as such on screen.
- **Time** is UTC everywhere, and no code reads the clock except the scheduler.
- **Email addresses** are HMAC-hashed per tenant at load, with the domain kept in clear.

**Host and cost.** One GCP e2-small VM in Mumbai runs the same Docker Compose stack as a laptop, behind Caddy for HTTPS. It costs about $21 a month ([docs/decisions/0002-host-on-gcp-vm.md](docs/decisions/0002-host-on-gcp-vm.md)). The worker has been running unattended since 2026-10-17, and `/health` shows the last night with `trigger: schedule`.

## What I deliberately skipped

- **Real connectors.** The generator writes exactly the raw-event shape a connector would.
- **Sending any email.** The job produces a task and a briefing, and outreach stays with the CSM in their own client.
- **LLM narration of the briefing.** It was first in the cut order. The briefing is the model's reason list in a template, so it cannot claim anything the model did not compute.
- **Real login.** One shared password and a tenant and CSM switcher stand in. A real login changes who sets the session, not how the database enforces it.
- **Task workflow beyond one escalation level:** SLA configuration, digests and a CS-lead view.
- **Retraining automation and drift monitoring** (PSI, null-rate alerts). The data-quality gate is the only automatic guard.
- **Identity beyond keys and domains:** fuzzy name matching, match confidence, renamed companies, parent and subsidiary, and CRM duplicates and merges.
- **Per-tenant calibration and per-tenant policy settings.** Per-tenant metrics are reported in the evaluation slices, not corrected.
- **CSM reassignment history.** `owner_changed` is not generated, so ownership is the current owner.
- **The `reopened_90d` and `owner_changed_180d` features.** The generator emits neither event.
- **Contract shapes:** monthly terms, auto-renew flags, seat tiers, multiple currencies and seasonality.
- **Composite tenant foreign keys.** RLS and the isolation tests cover the leak that matters.
- **Second worker replica, advisory locks and automatic backfill** of missed nights. Recovery is `nightly --as-of <night>`.
- **Daily feature tables over history.** Features are recomputable for any `as_of` through `build_features`.

## What breaks first at 10x

Measured from the live nights and a local profile of the feature build ([docs/decisions/step-7-10x.md](docs/decisions/step-7-10x.md)). Today a night takes 21 s for all five tenants. For t1 (2,000 accounts, 84,000 events) it is 9.5 s: 2.7 s ingest, 4.4 s snapshot, 2.0 s score and 0.3 s gate. The snapshot scales at about 0.05 ms per event, and scoring at about 1 ms per account.

- **10x events (2M):** the nightly run stays fine, at about 4 minutes. **The first thing to break is the bootstrap's weekly anchor build.** It re-reads every event for every Monday (about 0.4 s plus 7 µs per event, 157 Mondays), so t1 alone would take about 40 minutes on the VM, and its events frame would peak near 1 GB. On a 2 GB host that also runs Postgres, it runs out of memory before it runs out of time.
- **10x accounts (50,000):** scoring stays fast, but storage binds. Snapshots grow to about 4.5 GB, and scores to about 3 GB a month with full contribution vectors, which fills a 20 GB disk within weeks. Training then has to read 3.2M JSON feature rows at once.
- **10x tenants (50):** the sequential per-tenant night is about 3.5 minutes and still isolates failures. The bootstrap grows to hours, and training memory binds as above.
- **LLM calls** would scale with tasks (at most k per CSM a night), not accounts, so they are not a first failure.

This confirms the plan's hypothesis, that the pandas feature pass breaks first at bootstrap on the small host, and sharpens it: memory goes before time. The named fix is incremental anchors from per-account running aggregates, which turns O(weeks × events) into O(events). Then come retention, top-10-plus-residual contributions, and a queue with per-tenant advisory locks. Until then, an 8 GB VM is the cheap stopgap at about $35 a month more.

## How we know it works

- **The evaluation below.** It runs on a cycle-based temporal holdout, with five methods compared on one slice, recall@8 per CSM-week, lead time before the notice deadline, slices, and leakage probes.
- **231 tests** (`uv run --env-file .env pytest`). They include:
  - point in time: a late event leaves the old snapshot unchanged;
  - tenant isolation on every table, with no context meaning zero rows;
  - crash part-way, then rerun: one clean night and no duplicate tasks;
  - no flapping: identical scores give zero tasks, and an oscillating score gives at most one task per three nights;
  - escalation fires once;
  - one test per duplicate kind;
  - drift normalises;
  - the gate blocks and publishes nothing;
  - byte-identical regeneration;
  - truth never reaches the pipeline;
  - every page renders, and another tenant's ids answer 404.
- **Unattended `job_runs` rows** from nights nobody triggered, on the run history page.
- **On every score page,** the reconciliation `expected value + sum of contributions = margin`, and `python -m app rescore <score_id>` from a terminal.

**Licences.** The code is MIT ([LICENSE](LICENSE)). Every dependency is permissively licensed (MIT, BSD, Apache-2.0 or 0BSD) except psycopg, which is LGPL-3.0 and used unmodified as a library, as the LGPL allows. All data is synthetic, and every email address uses the reserved `.example` and `.test` domains.

## Local development

Needs uv 0.12.17 or newer and Docker.

```bash
uv sync                          # .venv with Python 3.11 and the locked dependencies
cp .env.example .env             # dev-only values; .env is gitignored
docker compose up -d db          # Postgres 16 on 127.0.0.1:${DB_PORT:-5432}, data in a named volume
uv run ruff check .
uv run --env-file .env pytest    # database tests create and drop their own fello_test database
```

If another program already holds port 5432, set `DB_PORT` in `.env` to a free port (for example `DB_PORT=55433`) and use the same port in `DATABASE_URL` and `TEST_DATABASE_URL`. Check the binding with `docker compose port db 5432`: Compose can report the db healthy even when the host port was taken, and then host-side tools silently reach the other program.

The app reads only process environment variables and never opens `.env` itself, so every command that needs it goes through `uv run --env-file .env`.

Run the whole system on the full dataset (5 tenants, 5,000 accounts, about 192,000 events):

```bash
uv run --env-file .env python -m app bootstrap --config config/full.yaml   # about 2 minutes: migrate, generate, load, resolve, anchors, train, first night
uv run --env-file .env python -m app nightly --as-of 2026-10-13T02:00:00Z  # one more night, a few seconds; run again and it is a no-op
uv run --env-file .env python -m app web                                   # http://127.0.0.1:8000, password BASIC_AUTH_PASSWORD from .env, username ignored
```

Or run everything in containers: `docker compose up -d` starts db, web and worker. The worker bootstraps on first start (`BOOTSTRAP_CONFIG`) and then fires the nightly job on `NIGHTLY_CRON` (UTC); set `NIGHTLY_CRON="*/2 * * * *"` in `.env` to watch it fire. `curl localhost:8000/health` shows the model version and the newest job run.

Tests that need Postgres read `TEST_DATABASE_URL` (default `.../fello_test`), refuse any database not named `fello_test*`, and drop and recreate it each session. Give each parallel worker its own, for example `.../fello_test_qa`. Without a reachable Postgres they skip.

Secret scanning runs as a gitleaks pre-commit hook. Set it up once per clone with `uv tool install pre-commit`, then `pre-commit install`.

## Data report

All data is synthetic. `python -m app data --config config/full.yaml` writes it, and `python -m app data-report` prints the report below. The report reads only what the pipeline would see, never `data/truth/`. It uses the resolve step's own matching rules, so the quarantine line is what ingest will produce. Regenerating gives identical bytes on Windows and in the Linux image. What each kind of mess is, and which step handles it, is in [docs/decisions/step-2-log.md](docs/decisions/step-2-log.md).

Notes for reading it:

- Totals count distinct events. "Replays" are the extra deliveries of an event that already arrived (duplicate kind 1).
- Versions `v1` and `v2` are the two payload schemas of a drifting source.
- "Re-sent unchanged" counts ticket snapshots identical to the ticket's previous one (duplicate kind 2).
- A renewal counts as contracted when it was renewed below 70% of the prior ARR.

From truth, checked by `tests/test_generator_mess.py`:

- 20.8% of churn shows no signal beforehand (target 20 to 25%).
- 7.4% of accounts have fewer than 5 events.
- 10 planted "signals were sitting right there" cases.

```text
== t1 Northwind Cloud: 2,000 accounts, 83,246 events
   connected: gmail, product, salesforce, stripe, zendesk
   events per account p5/p50/p95: 2/36/95; fewer than 5 events: 7.1%
   source       events  replays  versions        lag hours p50/p95/p99
   salesforce    6,780   1.09%  v1:2764 v2:4016 4.3/19.9/68.9
   zendesk      20,082   0.92%  v1:2053 v2:18029 2.9/33.2/144.1
   product      32,061   0.94%  v1:32061        26.8/146.8/241.8
   stripe       10,916   1.06%  v1:10916        1.4/6.7/49.0
   gmail        13,407   0.92%  v1:13407        9.0/53.7/111.0
   zendesk snapshots re-sent unchanged: 2,967
   quarantine: 0.85% (ambiguous_domain 42, unknown_domain 251, unknown_native_key 418)
   renewals 2024: 122, lost 14 (11.5%), contracted 7, renewed 101
   renewals 2025: 666, lost 49 (7.4%), contracted 63, renewed 554
   renewals 2026: 1,144, lost 78 (6.8%), contracted 109, renewed 957

== t2 Harbor Software: 1,200 accounts, 45,307 events
   connected: product, salesforce, stripe, zendesk
   events per account p5/p50/p95: 2/33/83; fewer than 5 events: 7.8%
   source       events  replays  versions        lag hours p50/p95/p99
   salesforce    4,041   1.11%  v1:1687 v2:2354 4.2/19.9/53.8
   zendesk      11,873   0.86%  v1:1404 v2:10469 2.9/33.5/140.2
   product      19,216   0.95%  v1:19216        27.4/145.1/237.4
   stripe       10,177   1.14%  v1:10177        1.4/6.9/47.5
   zendesk snapshots re-sent unchanged: 1,781
   quarantine: 0.87% (unknown_native_key 396)
   renewals 2024: 78, lost 5 (6.4%), contracted 10, renewed 63
   renewals 2025: 417, lost 54 (12.9%), contracted 33, renewed 330
   renewals 2026: 682, lost 65 (9.5%), contracted 47, renewed 570

== t3 Lumen Retail Systems: 900 accounts, 31,181 events
   connected: gmail, product, salesforce, zendesk
   events per account p5/p50/p95: 2/28/83; fewer than 5 events: 7.6%
   source       events  replays  versions        lag hours p50/p95/p99
   salesforce    3,051   1.25%  v1:1256 v2:1795 4.2/19.7/50.2
   zendesk       8,253   0.81%  v1:990 v2:7263  2.9/23.1/143.3
   product      14,260   0.95%  v1:14260        27.2/147.9/247.9
   gmail         5,617   0.78%  v1:5617         8.9/54.7/111.5
   zendesk snapshots re-sent unchanged: 1,168
   quarantine: 0.42% (ambiguous_domain 47, unknown_domain 78, unknown_native_key 6)
   renewals 2024: 64, lost 8 (12.5%), contracted 6, renewed 50
   renewals 2025: 319, lost 36 (11.3%), contracted 30, renewed 253
   renewals 2026: 503, lost 57 (11.3%), contracted 33, renewed 413

== t4 Copper Desk: 600 accounts, 28,634 events
   connected: gmail, product, salesforce, stripe, zendesk
   events per account p5/p50/p95: 2/41/98; fewer than 5 events: 7.0%
   source       events  replays  versions        lag hours p50/p95/p99
   salesforce    2,015   0.79%  v1:886 v2:1129  4.2/21.3/60.3
   zendesk       3,726   1.05%  v2:3726         3.0/34.8/141.5
   product       9,218   0.92%  v1:9218         26.7/143.2/236.3
   stripe        9,920   1.19%  v1:9920         1.4/7.1/51.4
   gmail         3,755   1.01%  v1:3755         9.0/51.9/109.6
   zendesk snapshots re-sent unchanged: 542
   quarantine: 1.13% (ambiguous_domain 14, unknown_domain 83, unknown_native_key 226)
   renewals 2024: 45, lost 8 (17.8%), contracted 1, renewed 36
   renewals 2025: 206, lost 40 (19.4%), contracted 10, renewed 156
   renewals 2026: 322, lost 57 (17.7%), contracted 23, renewed 242

== t5 Pine Launch: 300 accounts, 3,712 events
   connected: gmail, product, salesforce, stripe
   events per account p5/p50/p95: 1/12/20; fewer than 5 events: 8.3%
   source       events  replays  versions        lag hours p50/p95/p99
   salesforce      434   0.46%  v2:434          4.6/22.1/69.3
   product       1,366   1.32%  v1:1366         26.1/148.8/254.9
   stripe        1,324   1.21%  v1:1324         1.4/7.1/47.9
   gmail           588   1.36%  v1:588          9.6/59.0/117.2
   zendesk snapshots re-sent unchanged: 0
   quarantine: 1.51% (ambiguous_domain 4, unknown_domain 4, unknown_native_key 48)
   renewals: none observed

== all tenants: 5,000 accounts, 192,080 events
   salesforce   16,321    8.5%  (target 8%, off by 6%)
   zendesk      43,934   22.9%  (target 22%, off by 4%)
   product      76,121   39.6%  (target 38%, off by 4%)
   stripe       32,337   16.8%  (target 18%, off by 6%)
   gmail        23,367   12.2%  (target 12%, off by 1%)
```

## Evaluation

`python -m app evaluate --config config/full.yaml` prints the evaluation below. It runs on the test cycles of a cycle-based temporal split: the training cycles renew on or before 2025-10-12, and the test cycles renew after it, up to 2026-10-12. Every weekly row of one account's cycle sits on one side. The model under test is the evaluation fit, trained on the training cycles only. The live model is a refit on every cycle, with the same settings. How it is built is in [docs/decisions/step-4-log.md](docs/decisions/step-4-log.md).

What it says, plainly:

- **Both models beat the baselines that matter.** LightGBM's ROC-AUC is 0.600 and its PR-AUC 0.248. The hand-written rules score 0.547 and 0.201. Days to renewal alone, the "is this a calendar" row, scores 0.500 and 0.180. Days to renewal carries under 1% of the model's contributions and is never an account's top reason.
- **LightGBM and logistic regression are within noise of each other.** The ROC-AUC difference is +0.009, with a 95% interval of -0.009 to +0.028, from a paired bootstrap over accounts. Logistic regression is slightly ahead on PR-AUC and recall@8. LightGBM is live because of a rule fixed in `app/ml/train.py`, not a choice made from this table: LightGBM is kept unless logistic regression wins the validation tail clearly, by at least 1% lower log loss over at least 100 renewal cycles. Here the two are 0.4% apart.
- **The ceiling is low on purpose.** The oracle, which ranks by the generator's latent health, reaches only 0.650. About 21% of churn shows no signal, and health moves between the anchor and the renewal. Recall@8 is 21.8% against the oracle's 23.6%, so most of what is learnable is learned.
- **Planted cases.** 7 of the 10 planted "signals were sitting right there" cases enter their CSM's top 8 at least 4 weeks before the notice deadline. For 9 of them the top reason is a ticket or usage feature.
- **Calibration** is good up to a predicted probability of 0.3, which covers 89% of rows. Above that the model is too pessimistic.
- **Leakage probes pass.** AUC rises gently toward the renewal date, with no jump toward 1.0. A model trained on shuffled labels scores 0.475.

These metrics validate the point-in-time labelling, the pipeline and the ranking on data whose generating process is known. They are not evidence about real-world churn. The first real-data step is refitting and re-baselining on actual renewal outcomes.

Model under test: lightgbm-694a83671c18, the evaluation fit of the kind chosen on the validation tail (lightgbm). The live version is its refit on every cycle.
Training cycles renew on or before 2025-10-12; test cycles renew after it, up to 2026-10-12.
68,989 training rows; 135,613 test rows with 24,344 positives; 40 holdout Mondays; 466 renewals lost or contracted.

| Method | PR-AUC | ROC-AUC | Brier | recall@8 | ARR recall@8 | precision@8 | caught 30d before notice | caught 60d | median lead (days) |
|---|---|---|---|---|---|---|---|---|---|
| oracle | 0.296 | 0.650 | 0.141 | 23.6% | 35.6% | 8.3% | 36.1% | 35.0% | 287 |
| lightgbm (live kind) | 0.248 | 0.600 | 0.146 | 21.8% | 32.2% | 7.6% | 38.4% | 37.3% | 272 |
| logistic regression | 0.256 | 0.591 | 0.145 | 23.7% | 34.5% | 8.2% | 39.5% | 39.1% | 270 |
| rules | 0.201 | 0.547 | 0.147 | 14.8% | 26.7% | 5.0% | 30.3% | 30.0% | 294 |
| days to renewal | 0.180 | 0.500 | 0.148 | 12.4% | 25.1% | 4.2% | 18.0% | 18.0% | 304 |

precision@8 ceiling (if every due loss were in the top 8): 37.5%.
The live kind ranked by probability alone instead of probability x ARR: recall@8 24.7%, ARR recall@8 20.0%.
LightGBM minus logistic regression, ROC-AUC: +0.009, 95% interval -0.009 to +0.028 (paired bootstrap over accounts): within noise.

Reliability (lightgbm, test rows):

| Predicted bin | Rows | Mean predicted | Observed |
|---|---|---|---|
| 0.0-0.1 | 18,221 | 0.080 | 0.109 |
| 0.1-0.2 | 61,414 | 0.150 | 0.153 |
| 0.2-0.3 | 41,739 | 0.239 | 0.217 |
| 0.3-0.4 | 9,397 | 0.340 | 0.245 |
| 0.4-0.5 | 3,016 | 0.443 | 0.268 |
| 0.5-0.6 | 1,433 | 0.543 | 0.380 |
| 0.6-0.7 | 350 | 0.638 | 0.580 |
| 0.7-0.8 | 43 | 0.726 | 0.605 |

Slices (lightgbm, test rows):

| Slice | Rows | Positives | PR-AUC | ROC-AUC |
|---|---|---|---|---|
| tenant t1 | 57,963 | 9,874 | 0.214 | 0.585 |
| tenant t2 | 35,090 | 5,958 | 0.244 | 0.606 |
| tenant t3 | 25,802 | 4,490 | 0.248 | 0.613 |
| tenant t4 | 16,758 | 4,022 | 0.345 | 0.605 |
| tenant t5 | - | - | no observed renewals | |
| days_to_renewal 0-90 | 33,786 | 6,064 | 0.291 | 0.646 |
| days_to_renewal 91-180 | 33,389 | 5,994 | 0.255 | 0.606 |
| days_to_renewal over 180 | 68,438 | 12,286 | 0.222 | 0.572 |
| event_count under 10 | 25,794 | 4,583 | 0.212 | 0.571 |
| event_count 10-100 | 109,282 | 19,525 | 0.254 | 0.607 |
| event_count over 100 | 537 | 236 | 0.467 | 0.515 |

Is it a calendar? days_to_renewal carries 0.8% of the absolute contributions and is the top reason on 0.0% of test rows.
Planted cases: 7 of 10 entered their CSM's top 8 at least 4 weeks before the notice deadline; the top reason was a ticket or usage feature for 9 of 10.
Worst misses: 277 lost renewals never reached their CSM's top 8. Largest:
- t1 Lumen Supply LLC, ARR 207,240, renewal 2026-02-03
- t1 Clear Media Ltd, ARR 188,800, renewal 2026-08-04
- t1 Blue Analytics Group, ARR 183,600, renewal 2026-06-26
- t1 Vertex Foods Co, ARR 178,200, renewal 2026-09-10
- t1 Silver Energy GmbH, ARR 174,800, renewal 2026-02-02

Leakage probes:

- AUC by days before renewal: 90 days 0.629, 60 days 0.637, 30 days 0.655, 7 days 0.667 (no jump toward 1.0: ok)
- Shuffled-label AUC: 0.475 (near 0.5: ok)
- Random row split AUC: 0.690, not reported: rows of one account would sit on both sides.

## Dependencies and pins

| What | Pin | Where |
|---|---|---|
| Python packages | Exact versions and hashes in `uv.lock`, written by uv 0.12.17. CI and the image install with `uv sync --locked`, which fails if the lock is stale | `pyproject.toml`, `uv.lock` |
| Base image | `python:3.11-slim` by digest (Python 3.11.17, Debian trixie) | `Dockerfile` |
| uv in the image | `ghcr.io/astral-sh/uv:0.12.17` by digest | `Dockerfile` |
| GitHub Actions | Commit SHAs, release named in a comment | `.github/workflows/ci.yml` |
| gitleaks | v8.30.1 by commit SHA | `.pre-commit-config.yaml` |

## Vendored files

| File | Upstream | Version | Licence | SHA-256 |
|---|---|---|---|---|
| `static/htmx.min.js` | npm package `htmx.org`, file `dist/htmx.min.js` | 2.0.11 | 0BSD | `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717` |

The npm tarball was checked against the registry's sha512 integrity before extraction, and the file matches the jsDelivr copy of the same release. Git stores it as binary so its bytes never change. Check it with `sha256sum static/htmx.min.js`.

## Licence

MIT. See [LICENSE](LICENSE).
