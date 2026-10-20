# Monday list

Every Monday, each customer success manager (CSM) gets a short, ranked list: the accounts most worth working this week, in order, and why. Every reason traces through to the raw events behind it. A nightly job rescores every account with nobody pressing a button. It opens a task when an account enters its CSM's top 8, escalates a task nobody touched near renewal, and logs every decision.

Built for the Fello GTM AI take-home. An organisation (a tenant) is one customer of the product, with its own CSMs, accounts and data.

## For reviewers: the two-minute version

- **Try it live:** https://136-83-131-193.sslip.io. The password comes with the submission, and the username is ignored. Pick an organisation, then a CSM. Click any probability to see every factor behind it, then "raw events" to see what a factor was computed from. "Nightly runs" shows the nights nobody triggered.
- **It ranks better than the obvious alternatives.** On a year of held-out renewals, each CSM's weekly top 8 contained 21.8% of the renewals that were then lost or contracted. Hand-written rules reach 14.8%, and a baseline built from days to renewal 12.4%. By ARR, the list covers 32.2% of the revenue at stake, against 26.7% and 25.1%.
- **It warns early.** 7 of the 10 planted "the signals were sitting right there" accounts reach their CSM's top 8 at least four weeks before the notice deadline.
- **It runs itself.** The nightly job has run on the live server with nobody involved since 2026-10-17 (`trigger: schedule` on the runs page and on `/health`).
- **Every number is traceable.** A score page lists each factor's contribution and checks that they add up to the model's output. Each factor links to the events it was computed from. `python -m app rescore <score_id>` reproduces any stored score.
- **Five organisations, one database, no leaks.** Row-level security is forced on every tenant table. Another organisation's id answers 404, and a query with no organisation set sees no rows.
- **Honest limits.** All data is synthetic, so these numbers validate the pipeline and the ranking, not real-world churn. LightGBM and logistic regression tie on this data. What breaks first at 10x is measured, further down.

## What I decided

**The problem, and so the product.** The request was a red/yellow/green health score with an automatic email when an account goes red. The problem is prioritisation. Each CSM can work perhaps 8 of 120 accounts a week, and the signals that predict a lost renewal are spread over five systems that nobody joins. So the product is a ranked list per CSM with reasons, not a score per account.

**Where I disagreed with the request, and what replaced it.**

- **No email to customers.** It fails three ways:
  - wrong recipient: the contact on file is often the champion who left;
  - wrong message: a ticket backlog and a failed payment need different conversations;
  - wrong trigger: accounts near a line on a noisy score flip week to week.

  An account entering a CSM's top 8 gets a task instead. A task untouched for 5 business days within 60 days of renewal is escalated and pinned to the top. The request asked that "nothing slips". Here that means nothing goes unseen, and nothing is dropped silently.
- **Colour is a display derived from rank.** Red is the top 8 of the CSM's book, yellow the next 16, green the rest. An account with not enough signal (under 30 days or under 5 events) is grey and never green. The probability and the dollars at risk carry the absolute picture.
- **Rank by expected ARR at risk**, probability times ARR, with one-click re-sort by probability, ARR or renewal date. A $2k account at 60% should not outrank a $200k account at 50%.
- **The label is the outcome of the next renewal,** lost or contracted below 70% of the prior ARR, whenever that renewal falls. A 90-day window would mark every account further out as safe.

**Where AI fits, and where it deliberately does not.**

- **The ranking is a fitted model**, LightGBM. Its reasons are the model's own per-account contributions (`pred_contrib`, the tree version of SHAP), not hand-written rules.
- **The nightly job is the agent.** It decides with a written policy, acts (tasks, escalations, briefings) and records every decision, with nobody triggering it.
- **A language model is not in the ranking path**, so nothing a CSM reads can claim more than the model computed. The briefing is a template over the reason list.
- **An LLM writes a two-sentence summary on top of that template, when a key is set** ([app/jobs/narrate.py](app/jobs/narrate.py)):
  - It runs after the night has committed, so it can never fail or delay one.
  - It is sent only the reason list and three numbers: no names, emails or raw events.
  - Every number in its reply must appear in that input, or the reply is stored as rejected and not shown.
  - What was sent, which model, the prompt version and the outcome are stored per briefing, and the summary is labelled as model-written on the page.
  - It is off on the live site until an API key is set in the VM's `.env`; until then the briefing is the template alone. Tests cover it with a fake client.

**How it is built.** The interface was designed in Penpot, the open-source design tool: a light, neutral layout with one accent colour, red kept for urgency, a sidebar, and the Work Sans typeface. The design file has the list, score, account and runs pages and a component library. Python 3.11, FastAPI with Jinja2 and vendored htmx, Postgres 16, SQLAlchemy Core and Alembic, LightGBM, and an APScheduler worker. It is one image, run as a web process and a worker process.

- **Multi-tenancy is enforced in the database.** Every tenant table has `FORCE ROW LEVEL SECURITY`, with one policy on the session setting `app.tenant_id`. Only `tenant_tx` sets it, for one transaction. With no organisation set, a query sees zero rows. Every page takes the organisation from a signed, SameSite=Strict session cookie. Only the switcher reads an organisation id from the request (to list its CSMs and set the cookie), and the admin trigger takes one in its JSON body. Another organisation's id answers 404.
- **Point in time.** A feature at `as_of` sees only events received by `as_of`, with windows on when they happened. A late event therefore changes tonight's snapshot, never last week's. Training anchors follow the same rule, so a late event never changes a past training row either. The runtime roles have no UPDATE or DELETE on snapshots and scores; only a migration, as the table owner, can change them.
- **One global model, fitted and reproducible.** One LightGBM over all organisations, because the small ones cannot support a model of their own (one has 300 accounts and no renewal outcome yet). `tenant_id` is not a feature, so an account is scored by what it does, not by whose it is. The model is deterministic, and its contributions add up exactly to its output (the margin). Each version stores its booster text, features, row counts per organisation and metrics. An evaluation fit on older renewal cycles is stored beside the live refit, and logistic regression stays as the challenger. `python -m app rescore <score_id>` reproduces any stored score.
- **No daily feature table.** Features are computed for any `as_of` by one function, `build_features`, the same code at training and serving. Snapshots are stored per night and per weekly training anchor. A daily table over three years would be millions of mostly identical rows.
- **The night is a staged, idempotent job:** ingest, snapshot, score, data-quality gate, transitions, policy and tasks, escalation.
  - A rerun of a succeeded night is a no-op, and a failed or blocked night is re-run.
  - A tripped gate publishes nothing.
  - `policy.py` (pure, version 1) holds the three-night anti-flap rule, seeding, dedup, dismissal suppression and a per-queue budget.
- **Traceability.** Every CSM-visible thing has a row and a foreign key back to what produced it:
  - raw event, then normalised event (with how it was matched);
  - snapshot (with its ingest watermark), then score (with the full contribution vector) and model version;
  - transition, task, briefing and narration, task events, and the job run.

**Who connects as which database role.** The roles make the isolation real: a bug in a query cannot read another organisation's rows, because the database refuses.

| Process | Role | Can do |
|---|---|---|
| `migrate` (each worker start) | the Postgres superuser, then `app_owner` | The superuser creates the database and the three roles and applies their attributes and grants. Alembic then runs as `app_owner`, which owns the tables. FORCE RLS binds the owner too |
| web, nightly job, loader | `app` | Reads and writes within one organisation through `tenant_tx`. No UPDATE or DELETE on snapshots and scores |
| trainer, `evaluate`, `rescore` | `app_batch` | `BYPASSRLS`, the deliberate cross-tenant readers. Writes only `model_versions` |
| test teardown | the Postgres superuser | Purges test tenants |

Web and worker are given the superuser URL to derive the role URLs from, and the worker uses it for the first `migrate` step. No request or job query runs as the superuser. A production setup would give the web process only the `app` credentials.

**What the pipeline does with the mess:**

- **Identity.** Each event is matched by the crosswalk of system keys, then by email domain. Otherwise it goes to quarantine with a reason, and the count is on the list page.
- **Duplicates.** An exact replay is rejected and the first delivery kept. A re-sent ticket snapshot resolves to the newest state. A renewal reported by Salesforce and Stripe becomes one outcome.
- **Schema drift.** Each version is mapped to one shape, and an unreadable payload is rejected with a reason.
- **Thin accounts** are not green.
- **NULL is not zero.** An unconnected source is NULL and named as such on screen.
- **Time** is UTC everywhere. The nightly job never reads the clock: its time is the night's `as_of`, which the scheduler or the command line passes in. The web app reads the clock only for display (freshness, task age, next run) and to date a snooze, and `CLOCK_OVERRIDE` pins it for tests and demos.
- **Customer email addresses** in event payloads are HMAC-hashed per organisation at load, with the domain kept in clear. CSM emails (staff, not customer data) are stored in clear for the switcher.
- **Payment failures are signals, not the label.** A failed mid-term payment is a feature. The renewal invoice failing for good is part of the outcome, and outcome event types are deny-listed from every feature in one constant (`OUTCOME_EVENT_TYPES`).

**Host and cost.** One GCP e2-small VM in Mumbai runs the same Docker Compose stack as a laptop, behind Caddy for HTTPS. It costs about $21 a month. The worker has run unattended since 2026-10-17, and `/health` shows the last night with `trigger: schedule`. The synthetic data keeps arriving until 2026-12-14. After that the nights keep running and rescoring, on no new events.

## What I deliberately skipped

- **Real connectors.** The generator writes exactly the raw-event shape a connector would.
- **Sending any email.** The job produces a task and a briefing, and outreach stays with the CSM in their own client.
- **The LLM summary on the live site.** It is built and tested, and off until an API key is set (see "Where AI fits").
- **Real login.** One shared password and an organisation and CSM switcher stand in. A real login changes who sets the session, not how the database enforces it.
- **Task workflow beyond one escalation level:** SLA configuration, digests and a CS-lead view.
- **Retraining automation and drift monitoring beyond the nightly gate** (PSI, alerting). The gate checks the quarantine rate, NULL rates and the shift in the mean probability, and it is the only automatic guard.
- **Monotone constraints** for features with a known direction (usage change, seats in use, CSAT). Without them the model sometimes treats a rise in usage as a small risk factor. The list shows reasons as the model computed them and only those above a minimum size; every contribution is on the score page.
- **Identity beyond keys and domains:** fuzzy name matching, match confidence, renamed companies, parent and subsidiary, and CRM duplicates and merges.
- **Per-tenant calibration and per-tenant policy settings.** Per-tenant metrics are reported in the evaluation slices, not corrected.
- **CSM reassignment history.** `owner_changed` is not generated, so ownership is the current owner.
- **The `reopened_90d` and `owner_changed_180d` features.** The generator emits neither event.
- **Contract shapes:** monthly terms, auto-renew flags, seat tiers, multiple currencies and seasonality.
- **Overdue renewals.** A renewal that passes its date with no outcome event keeps the account on its current cycle; nothing flags it.
- **Composite tenant foreign keys.** RLS and the isolation tests cover the leak that matters.
- **A second worker replica, per-tenant locks for the nightly job, and automatic backfill** of missed nights. Recovery is `nightly --as-of <night>`.

## What breaks first at 10x

Measured from the live nights and a local profile of the feature build. The first unattended night took 21 s for all five organisations. For t1 (2,000 accounts, 84,000 raw event rows) it was 9.5 s: 2.7 s ingest, 4.4 s snapshot, 2.0 s score and 0.3 s gate. That night ran before the decision stages were deployed; locally they add under a second for t1 (0.2 s transitions, 0.6 s policy and tasks). The snapshot scales at about 0.05 ms per event, and scoring at about 1 ms per account.

- **10x events (2M):** the nightly run stays fine, at about 4 minutes. **The first thing to break is the bootstrap's weekly anchor build.** It re-reads every event for every Monday (about 0.4 s plus 7 µs per event, 147 Mondays), so t1 alone would take about 40 minutes on the VM, and the feature build would peak near 0.9 GB. On a 2 GB host that also runs Postgres, it runs out of memory before it runs out of time.
- **10x accounts (50,000):** scoring stays fast, but storage binds. Snapshots grow to about 4.5 GB, and scores to about 3 GB a month with full contribution vectors, which fills a 20 GB disk within weeks. Training then has to read 3.2M JSON feature rows at once.
- **10x tenants (50):** the sequential per-tenant night is about 3.5 minutes and still isolates failures. The bootstrap grows to hours, and training memory binds as above.
- **LLM calls** scale with new tasks (at most 8 per CSM a night, capped by `LLM_MAX_CALLS_PER_RUN`), not with accounts, so they are not a first failure.

This confirms the plan's hypothesis, that the pandas feature pass breaks first at bootstrap on the small host, and sharpens it: memory goes before time. The named fix is incremental anchors from per-account running aggregates, which turns O(weeks × events) into O(events). Then come retention, top-10-plus-residual contributions, and a queue with per-tenant advisory locks. Until then, an 8 GB VM is the cheap stopgap at about $35 a month more.

## How we know it works

- **The evaluation below.** It runs on a cycle-based temporal holdout, with five methods compared on one slice, recall@8 per CSM-week, lead time before the notice deadline, slices, and leakage probes.
- **240 tests** (`uv run --env-file .env pytest`). They include:
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
  - every page renders, and another tenant's ids answer 404;
  - the LLM summary: only the reason list is sent, an invented number is rejected, a failure never touches the night.
- **Unattended `job_runs` rows** from nights nobody triggered, on the run history page.
- **On every score page,** the reconciliation `expected value + sum of contributions = margin`, and `python -m app rescore <score_id>` from a terminal.

**Licences.** The code is MIT ([LICENSE](LICENSE)). The vendored Work Sans fonts are under the SIL Open Font License 1.1, with its text in `static/fonts/OFL.txt`. Every runtime dependency is permissively licensed (MIT, BSD, Apache-2.0, PSF-2.0 or 0BSD, plus Zlib and CC0 parts inside numpy), except psycopg and psycopg-binary. Those are LGPL-3.0 and used unmodified as libraries, as the LGPL allows. certifi (MPL-2.0) comes in only with the test client. All data is synthetic, and every email address uses the reserved `.example` and `.test` domains.

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
uv run --env-file .env python -m app bootstrap --config config/full.yaml   # about 4 minutes: migrate, generate, load, resolve, anchors, train, first night
uv run --env-file .env python -m app nightly --as-of 2026-10-13T02:00:00Z  # one more night, a few seconds; run again and it is a no-op
uv run --env-file .env python -m app web                                   # http://127.0.0.1:8000, password BASIC_AUTH_PASSWORD from .env, username ignored
```

With `LLM_API_KEY` set, each night's new briefings also get the LLM summary, and `python -m app narrate` adds it to the latest night's briefings.

Or run everything in containers: `docker compose up -d` starts db, web and worker. The worker bootstraps on first start (`BOOTSTRAP_CONFIG`) and then fires the nightly job on `NIGHTLY_CRON` (UTC); set `NIGHTLY_CRON="*/2 * * * *"` in `.env` to watch it fire. `curl localhost:8000/health` shows the model version and the newest job run.

Tests that need Postgres read `TEST_DATABASE_URL` (default `.../fello_test`), refuse any database not named `fello_test*`, and drop and recreate it each session. Give each parallel worker its own, for example `.../fello_test_qa`. Without a reachable Postgres they skip.

Secret scanning runs as a gitleaks pre-commit hook. Set it up once per clone with `uv tool install pre-commit`, then `pre-commit install`.

## Data report

All data is synthetic. `python -m app data --config config/full.yaml` writes it, and `python -m app data-report` prints the report below. The report reads only what the pipeline would see, never `data/truth/`. It uses the resolve step's own matching rules, so the quarantine line is what ingest will produce. Regenerating gives identical bytes on Windows and in the Linux image.
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

`python -m app evaluate --config config/full.yaml` prints the evaluation below. It runs on the test cycles of a cycle-based temporal split: the training cycles renew on or before 2025-10-12, and the test cycles renew after it, up to 2026-10-12. Every weekly row of one account's cycle sits on one side. The model under test is the evaluation fit, trained on the training cycles only. The live model is a refit on every cycle, with the same settings.
What it says, plainly:

- **Both models beat the hand-written rules.** LightGBM's ROC-AUC is 0.600 and its PR-AUC 0.248. The rules score 0.547 and 0.201. I wrote both the rules and the generator, so this shows the model learns combinations of signals the rules miss. It says nothing about beating a CSM's judgement.
- **Days to renewal carries no signal on its own.** Its ROC-AUC is 0.500 and its PR-AUC equals the base rate, so its calibrated probability is flat. Multiplied by ARR for the ranking, that row is in effect a sort by ARR. The model beats it on recall@8 (21.8% against 12.4%) and on ARR recall@8 (32.2% against 25.1%). Inside the model, days to renewal carries 0.8% of the contributions and is the top reason on under 0.05% of test rows, so the model is not a calendar.
- **LightGBM and logistic regression are within noise of each other.** The ROC-AUC difference is +0.009, with a 95% interval of -0.009 to +0.028, from a paired bootstrap over accounts. Logistic regression is slightly ahead on PR-AUC and recall@8. The rule in `app/ml/train.py` keeps LightGBM unless logistic regression wins the validation tail by at least 1% lower log loss over at least 100 renewal cycles; here they are 0.4% apart. I set that margin after the first validation run showed them this close: the first rule would have switched models on noise. Either model is defensible. LightGBM keeps a missing source as missing, where logistic regression fills it with the average.
- **The oracle is a reference, not a ceiling.** It ranks by the generator's hidden account health and reaches a ROC-AUC of only 0.650: about 21% of churn shows no signal, and health moves between the anchor and the renewal. It ignores the organisation-level churn rate that the models learn, so on recall@8 and lead time the models are level with it (21.8% and 23.7% against 23.6%) or ahead.
- **Planted cases.** 7 of the 10 planted "signals were sitting right there" cases enter their CSM's top 8 at least 4 weeks before the notice deadline. For 9 of them the top reason is a ticket or usage feature.
- **Calibration** is good up to a predicted probability of 0.3, which covers about 90% of rows. Above that the model is too pessimistic.
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
Worst misses: 277 renewals lost or contracted never reached their CSM's top 8. Largest:
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
| `static/fonts/WorkSans-Regular.woff2` | Work Sans, as shipped in Penpot's `frontend/resources/fonts` | 2.010 | SIL OFL 1.1 ([static/fonts/OFL.txt](static/fonts/OFL.txt)) | `ec25aa373e738b1ad6169d98558f9e3404099698162d0115f8ec93d5f1cd81e2` |
| `static/fonts/WorkSans-Medium.woff2` | as above | 2.010 | SIL OFL 1.1 | `358764933509f90ae8911088a6d4353c3227beef22d94fd66f7818ed8d7b6081` |
| `static/fonts/WorkSans-SemiBold.woff2` | as above | 2.010 | SIL OFL 1.1 | `99b5a2e4fdfa19daec110eacd85786b6d8954380c3688b6285030cdcdf50e0e6` |

The npm tarball was checked against the registry's sha512 integrity before extraction, and the file matches the jsDelivr copy of the same release. Git stores it as binary so its bytes never change. Check it with `sha256sum static/htmx.min.js`.

## Licence

MIT. See [LICENSE](LICENSE).
