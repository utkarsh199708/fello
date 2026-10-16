# Revised plan: Monday list for CSMs (Fello take-home)

How to read this. This is the plan you build from. Every choice below is final unless marked optional. Each build step ends in a state you could submit. Narrow and finished beats broad and half-done.

## 1. Reading of the request and where to disagree

The problem. 43 CSMs own about 120 accounts each and have no prioritisation. Churn signals exist in Zendesk, product usage, Stripe and email, but nobody joins them. The two lost renewals were data-rich accounts whose signals went unread. The fix is a ranked Monday list per CSM: these 8 accounts, in this order, for these reasons, with every reason clickable down to the raw events.

What I build instead of what was asked.

1. No auto-email. Three reasons, each tied to something the system models.
   - Wrong recipient. One of our own churn signals is champion departure. The contact on file for a red account is often the person who left.
   - Wrong message. The model emits per-account reasons (ticket spike, usage decline, failed payment, silent champion). They call for different actions. A "checking in" email to an account with an open support backlog is tone-deaf.
   - Wrong trigger. Red is a boundary on a noisy score. Accounts near it flip week to week. A task to a CSM is reversible. An email to a customer is not.
   Scope of the no: payment-failure reminders are a billing concern and already exist in Stripe. This system sends no email.
2. Traffic lights are a display, not a model. Red, yellow and green are derived from an account's rank in its CSM's book (red = top 8, yellow = next 16, green = rest, grey = not enough signal). They are a work queue, not an absolute health state. The probability and the dollars at risk carry the absolute information. The trade-off is stated: each CSM always has 8 red. The alternative (absolute probability bands) drifts with base rate and every retrain. Rejected for that reason.
3. Rank by expected ARR at risk, not by probability. The VP lost two renewals, which are revenue events. The default sort is rank_score = p(next renewal lost or contracted) x contract ARR. A list by raw probability puts a $2k account at 0.6 above a $200k account at 0.5. The list shows probability, ARR, days to renewal and rank_score as separate columns, with one-click re-sort by probability or by renewal date.
4. What replaces "nothing slips". The nightly job rescores every active account, logs every rank transition, opens a task for each one that enters a CSM's top 8, and keeps that task open until the CSM closes it. An open task on an account renewing within 60 days that is older than 5 business days is marked escalated and pinned to the top of the list. The audit trail is the job_runs table. The system guarantees nothing goes unseen and nothing goes untouched silently. It does not guarantee a CSM acts. Neither would an auto-email.
5. Thin accounts are not green. An account with less than 30 days of tenure or fewer than 5 events total is shown in a separate "not enough signal" bucket with its event count, never as a confident rank. Absence of a source (tenant never connected Zendesk) is NULL, not zero tickets.
6. The data mess is mostly history and sparsity, as the VP says. One part of it is identity: five systems with five keys. The pipeline resolves them through a crosswalk and counts what it could not resolve. The count is on the dashboard.

Label trade-off, stated up front. The model predicts the outcome of the account's next renewal, whenever it falls. I rejected "renewal failure in the next 90 days" as the label because it makes every account more than 90 days from renewal negative by construction, which kills early warning. The cost of my choice is that predictions ten months out are close to base rate. The evaluation shows that by days-to-renewal bucket.

## 2. Questions for the business

Each question carries the assumption the build uses. All assumptions are config values.

| Question | Assumed default | Where it lives |
|---|---|---|
| What counts as churn: logo loss, contraction, downgrade? | Positive = next renewal lost, or renewed ARR below 70% of prior ARR | `contraction_threshold: 0.30` per tenant |
| Which two accounts were lost last quarter, their renewal dates, and when the first Zendesk and usage signals appeared? | Generator seeds 10 such accounts; the backtest reports how many weeks before the notice deadline each entered its CSM's top 8 | planted cases in `data/truth/` |
| Renewal window and notice period: annual or monthly, auto-renew, notice days? | Annual terms, 30-day notice, 90-day urgency window shown on the list | `notice_days: 30`, `urgency_days: 90` |
| How many accounts can a CSM act on per week? | K = 8 | `top_k: 8` per tenant |
| Acceptable time to first touch on a flagged account inside the renewal window? | 5 business days, then escalated | `escalate_after_business_days: 5` |
| Who are the tenants and which of the five sources does each have connected? | 5 tenants with different profiles; per (tenant, source) connected_at; three tenants missing a source | `tenants.yaml` |
| Which system is the system of record for account identity, and how are Stripe, Zendesk and product keyed to Salesforce today? | Stripe customer metadata and Zendesk organisation external_id carry the Salesforce Account Id; Gmail resolves by contact email domain | crosswalk table |
| Rank by expected loss (p x ARR) or by probability? | Expected loss, with re-sort | `ranking: expected_arr_at_risk` |
| Should tenants own K or any threshold? | K per tenant in config, no UI | config |
| What is the legal basis and which API scopes cover reading CSM mailboxes and Zendesk requester emails? | Metadata only, no bodies; email local parts HMAC-hashed per tenant at ingest, domain kept in clear | `pii.md` in docs, hashing in the ingest step |
| What is the submission deadline? | Fill in before step 0; it sets the cut order in section 3 | top of this document |

CSM ownership (Salesforce owner field) and task type are recorded as design decisions in the README, not asked.

## 3. What to build and build first

Rule: the first CSM-visible page exists on day two and is live on the internet the same day. Every later step deepens it. Tests are attached to the step that produces the property, not saved for the end.

Calendar. Steps 0 to 7 total about 11.5 working days plus a 3-day unattended soak before recording. Deadline: ______ (write it here before starting). If the deadline is shorter, cut inside steps 2 to 6 in this order, first to go first: optional step 8, the LLM paragraph, the worst-misses page, the second drift case, the third duplicate kind, the extra evaluation slices, the escalation rule. Never cut a test for a property the README claims.

Step 0. Brief and repo (half a day)
- Write the one-page brief in markdown from sections 1 and 2 as `docs/brief.md`. It is the first commit, so git history proves it came before code. The deck in step 7 is a separate artifact about what was built.
- Repo init, first commit: `.gitattributes` with `* text=auto eol=lf`, `.editorconfig`, `.gitignore` (`data/`, `.env`, `.venv`), `.dockerignore` (same plus tests and docs), MIT `LICENSE`, `.env.example` with an empty `LLM_API_KEY`, `pyproject.toml` with a `uv.lock`, `Dockerfile` on a digest-pinned `python:3.11-slim` with `libgomp1` installed for LightGBM, `compose.yaml` with a named volume for Postgres, `README.md` started as a running decisions log under `docs/decisions/`.
- GitHub Actions: ruff plus pytest against a Postgres service container. A gitleaks pre-commit hook so no key ever lands in the repo.
- Vendor `htmx.min.js` under `static/` so the live page has no third-party dependency.
- Pick the host with pricing in hand (section 4). Create the Railway project and Postgres now. Put the LLM key, if any, only in Railway variables.

Step 1. Walking skeleton (two days). Thin on every non-negotiable. Iterate locally on `config/small.yaml` (2 tenants, 500 accounts, about 20k events). Deploy on `config/full.yaml` (5 tenants, 5,000 accounts, about 200k events) so the live system meets the scale non-negotiable from the first deploy. The step 1 generator is thin in signal design, not in counts.
- Generator with the five-source schema, per-source native ids, crosswalk, contracts with renewal outcomes, received_at and occurred_at, three signals driven by a latent. Scaling to full spec is a config change.
- Migrations: tenants, csms, accounts, contracts, raw_events (one table, source column, raw payload JSONB), crosswalk, normalised_events, quarantine, snapshots, scores, model_versions, job_runs, tasks. tenant_id on every table. RLS roles and policies in the first migration.
- Resolve step (crosswalk, then email domain, else quarantine).
- One `build_features(tenant_id, as_of)` function with about ten features. Weekly training anchors and labels from renewal outcomes.
- Logistic regression behind a `Scorer` interface. Score rows store model_version, as_of, feature vector, margin, contributions.
- FastAPI + Jinja2 + HTMX: tenant and CSM switcher, ranked list, one drill-down from a score to its feature vector and the raw events behind each feature.
- Worker: APScheduler running the nightly function on a short interval locally, writing a job_runs row per tenant per run.
- `python -m app bootstrap` (migrate, generate, ingest, build anchors, train, score, write job_runs). Idempotent: it runs inside the worker container on first start when `model_versions` is empty, using batched COPY inserts, so nothing is pushed row by row from Windows through the public Postgres proxy. Compose locally. Deploy web + worker + Postgres to Railway. Confirm `/health` and that the worker fires on its own overnight.
- Tests: RLS isolation (two tests), late-event snapshot test, job_runs uniqueness.
- Stop here and you have a working, live, traceable, multi-tenant system with an unattended job. Everything after is depth.

Step 2. Generator to spec (two days). Section 5 in full: 5 tenants, 5,000 accounts, about 200k events, noise model, confounders, contracts, identity breaks, structured missingness, two drift cases, three duplicate kinds, late tail that runs 30 days past deploy date, truth isolation, data report, planted "signals were sitting right there" accounts. Redeploy with bootstrap on the full config. Tests: generator acceptance script, truth isolation grep test, byte-identical regeneration.

Step 3. Pipeline hardening (one day). Dedup by kind, schema-version dispatch, rejects table, NULL-vs-zero semantics, match_method on every normalised row, quarantine count on the dashboard, data-quality gate in the nightly run. Tests: one per duplicate kind, parametrised drift test, order-independence test, NULL-not-connected test.

Step 4. Model and evaluation (two days). LightGBM with `pred_contrib`, model_versions table with booster text and metrics, cycle-based temporal split, five-row comparison table, recall@8 and lead-time backtest, slices by tenant, CSM, days-to-renewal bucket and event-count bucket, reliability plot, leakage probes. Tests: OUTCOME_EVENT_TYPES deny-list test, score reproducibility test, lead-time AUC probe, planted-case assertion.

Step 5. Agentic job (two days). `policy.py`, transitions, tasks with status, anti-flap, per-CSM budget, escalation rule, briefing table with template renderer and optional LLM paragraph, per-tenant failure isolation, rebaseline on model change, stage timings. Tests: identical scores produce zero tasks, oscillating series produces at most one task per N days, crash-after-tenant-2 rerun test, escalation idempotence.

Step 6. Dashboard deepening (one day). Columns and re-sort, since-Monday markers, not-enough-signal bucket, task buttons (done, snooze, dismiss with reason), escalated badge, run history page with last and next run, worst-misses page, reconciliation check on the drill-down, staleness header.

Step 7. README, deck, Loom (one day). README is restructured under the three headings the exercise asks for (what I decided, what I deliberately skipped, what breaks first at 10x), with the running log moved to `docs/decisions/`. Deck of about 8 slides made with python-pptx: brief recap, what shipped, what diverged from the brief and why, the evaluation table, the 10x numbers. Deploy the final build at least three days before recording so job_runs holds several unattended rows. Script the Loom to 10 to 12 minutes and confirm the Loom plan's length limit first (the free tier caps at 5). The evening before, check that a planted case enters its CSM's top 8 on the demo night. Pre-run the four tests in a second terminal. Never show Railway variables on screen. Record against the live URL.

Step 8, optional, cut first. Customer-facing outreach draft stored next to the briefing with a "mark as sent" button that writes an audit row and sends nothing.

## 4. Architecture

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11, one repo, one Docker image | One person can own and explain all of it |
| Web | FastAPI + Jinja2 + HTMX (vendored), no JS build | Every score, snapshot and event list gets a plain GET route, so a trace is a URL you can paste into the Loom and test with TestClient |
| Storage | Postgres 16, SQLAlchemy 2 Core, Alembic | tenant_id on every table; RLS enforced for the app role; SQLite and DuckDB would not support the separate web and worker processes |
| Multi-tenancy | Three DB roles: `app_owner` (migrations only), `app_batch` with BYPASSRLS (trainer only, the one deliberate cross-tenant reader), `app` (web, worker, loader, briefing writes); FORCE ROW LEVEL SECURITY; context via `SELECT set_config('app.tenant_id', :tid, true)` inside a transaction | FORCE RLS binds the owner too, so every per-tenant writer goes through tenant_tx; the one cross-tenant reader is named and recorded in model_versions; fails closed when unset |
| Tenant context | Signed server-side session cookie (`SameSite=Strict; HttpOnly; Secure`) holding (tenant_id, csm_id) set by the switcher; never read from path, query or form; HTMX POSTs carry a CSRF token | The switcher is a dev shim for an IdP; a real login changes who sets the cookie, not DB enforcement |
| Ingest | `raw_events(tenant_id, source, source_event_id, source_schema_version, occurred_at, received_at, payload JSONB)`, append-only | Out-of-order arrival is a column, not a bug |
| Identity | `crosswalk(tenant_id, source, external_id, account_id)`; resolve step stamps match_method; unmatched rows go to `quarantine` | The mess is counted, never silently dropped |
| Account state | `build_features(tenant_id, as_of)` over normalised events: visibility `received_at <= as_of`, windows on `occurred_at`; no daily table | Same code at training and serving; a daily table over three years is millions of mostly duplicate rows |
| Snapshots | Weekly training anchors plus one nightly row per active account; rows carry as_of, built_at, ingest_watermark, feature_set_version; past rows are never restated | Immutable rows are what make a score reproducible and a trace honest |
| Model | LightGBM, natural prevalence, contributions from `booster.predict(pred_contrib=True)`; no `shap` package | Fitted on the data; exact additive reasons; no numba in the image |
| Model store | `model_versions` table: booster text, feature list, cycle range, tenant row counts, metrics JSON; never overwritten | Container disks are ephemeral; old score rows must still resolve to the model that produced them |
| Scheduler | `worker` service running APScheduler 3.x BlockingScheduler, UTC CronTrigger from env (default 02:00), max_instances=1; never inside uvicorn | One process, one schedule, provable in job_runs |
| Agentic job | `python -m app nightly --as-of <ts> [--tenant <id>]`; scheduler calls it with the fire time; per-tenant unit of work; `policy.py` decides actions | Reruns are deterministic because as_of is an input, not a clock read |
| LLM | Optional narration only; input is the stored reason list as JSON; output stored with prompt_version and model id; template fallback; key only from `LLM_API_KEY` env (Railway variables, `.env` gitignored); off when the key is absent and always off in tests; `LLM_MAX_CALLS_PER_RUN` (default 50) and `max_tokens` enforced in the briefing stage; provider and model id in the README | The model ranks, the LLM narrates, and the narration cannot cite what the model did not compute; the only place this demo can cost money is capped |
| Clock | `CLOCK_OVERRIDE` env or `--as-of`, honoured by job and dashboard; `POST /admin/jobs/nightly` accepts `as_of` and `tenant_id` so a live demo run needs no redeploy | Lets the Loom show two consecutive nights in minutes on one small tenant and lets the backtest replay history on the same code path |
| Local | Docker Compose: db (named volume), web, worker | Reproducible on the Windows dev machine; generated files written in binary with LF newlines so Windows and container hashes match |
| Prod | Railway Hobby: web service and worker service from the same image (different CMD), Postgres plugin, app sleeping off; budget $5 to $10 a month, checked on day one and written in the README | No free host keeps a worker awake and a database unpaused; a sleeping host silently fails the "runs without a human" non-negotiable |
| Health | `GET /health`: DB reachable, tenant and account counts, active model_version, last run status and time | The reviewer and the Loom can see the system is alive and the job fired |
| Access | One shared basic-auth password on the deployment, given in the README | Keeps a public URL from being an open multi-tenant demo |
| Web image | Reads stored rows only; no lightgbm or pandas import at module load; the drill-down shows the stored arithmetic (expected_value + sum of contributions = margin), and re-scoring is a CLI, `python -m app rescore <score_id>`, run in the worker image | Small web process; heavy imports live in the worker |
| Tests | pytest against a Compose Postgres; ruff | Properties the plan claims are each asserted once |

## 5. Synthetic data design

Reproducibility. One seed and one YAML parameter file (tenants, accounts, events, base churn, lag distributions, duplicate rate, missing-field rate, and every date the generator and evaluator need: `horizon_start`, `horizon_end`, `train_as_of`, `eval_as_of`). No code path reads the wall clock. Files are written in binary with LF newlines. `python -m app data` must reproduce byte-identical output on Windows and in the container. The generated dataset is not committed; the YAML, seed, data report and output hashes are. `python -m app data-report` prints one screen per tenant and its output is committed to the README: accounts, events per source, events-per-account p5/p50/p95, share of accounts with fewer than 5 events, received_at minus occurred_at lag quantiles per source, duplicate rate per source, churn rate per year, label counts, quarantine rate.

Tenants are profiles, not a count.

| Tenant | Accounts | Segment | Base annual churn | Sources connected | History |
|---|---|---|---|---|---|
| T1 | 2,000 | enterprise | 8% | all five | 36 months |
| T2 | 1,200 | mid-market | 12% | no Gmail | 36 months |
| T3 | 900 | mid-market | 12% | no Stripe (ARR and renewal outcome from Salesforce only) | 36 months |
| T4 | 600 | SMB | 20% | all five, Zendesk connected_at 14 months ago | 36 months |
| T5 | 300 | startup | unknown yet | no Zendesk | 5 months |

CSMs. A csms table per tenant, about 120 accounts each (43 CSMs total), assigned through the Salesforce owner field. A handful of owner_changed events so owner is resolved as of a date. A few accounts unassigned, shown under a virtual "Unassigned" owner whose tasks route to a CS-lead queue flagged `owner_missing` rather than to nobody.

Contracts. Per account: start_date staggered across the horizon with a recent-cohort tail, annual term, ARR (log-normal, segment-dependent), next_renewal_date, and a renewal_outcome row at each term end in {renewed, contracted, churned}. After churn the account emits nothing. Each renewal appears where it really would: a Salesforce renewal opportunity stage change and, where Stripe is connected, a renewal invoice paid or failed plus subscription.updated or subscription.deleted. The generator asserts both sources agree. Mid-term payment failures (a signal) are generated separately from the renewal invoice (the outcome).

Latent and noise model. Each account has a monthly health trajectory: drift plus Gaussian noise plus discrete shocks (champion departure, budget cut). Each source observes health with its own noise, lag and dropout. Churn at renewal is drawn from Bernoulli(sigmoid(a_tenant + b x health_at_renewal + u)), where u is an unobserved per-account factor. About 20 to 25% of churn is driven by u and shows no visible signal. About 10% of healthy accounts show an alarming signal and renew anyway. Contraction is a separate draw conditioned on health. Exact parameters are chosen when the generator is written and recorded in the README.

Confounders that fool a flag-counting rule.
- Onboarding ticket spike in the first 60 days, benign.
- A failed payment that recovers on retry within 7 days, benign (about 70% of mid-term failures).
- Ticket volume scales with usage, so busy healthy accounts file many tickets (decoy).

Event vocabulary and budget. 5,000 accounts over three years is about 5.5M account-days, so daily product rollups cannot fit in 200k. Product usage is a monthly per-account rollup emitted only while active. Rough mix, asserted to land within 10% of 200k: product 38%, Zendesk 22%, Stripe 18%, Gmail 12%, Salesforce 8%. Per-account counts are heavy-tailed; about 8% of accounts carry fewer than 5 events total and get a `thin_history` flag in truth.

| Source | Native key | Event types |
|---|---|---|
| Salesforce | 18-char Account Id | account_upserted (carries website_domain, which seeds the Gmail crosswalk), opportunity_stage_changed (renewal), owner_changed |
| Zendesk | organization_id plus requester email | ticket_created, ticket_updated with full ticket state (status, priority, csat, reopened), one row per change |
| Product | workspace_id | usage_rollup_monthly (active_users, sessions, key_actions, seats_used, seats_purchased) |
| Stripe | cus_ id | invoice.paid, invoice.payment_failed, subscription.updated, subscription.deleted, each with an idempotent event id |
| Gmail | from and to addresses, local part HMAC-hashed per tenant at ingest, domain in clear | message metadata only: thread_id, direction, counterpart domain, sent_at, bounce_or_autoreply flag; no bodies |

Mutable objects. Zendesk tickets and Salesforce records are stored as change events. The normalise step resolves current state by (source, object_id) ordered by updated_at, keeping both timestamps.

Identity. Raw events never carry a canonical account_id. A per-tenant crosswalk maps each native key to an account. Gmail crosswalk rows are derived from the Salesforce website_domain field; a domain shared by two accounts is ambiguous and its mail is quarantined. It is broken in exactly two ways: 2 to 5% of Stripe customers and Zendesk organisations have no back-reference, and some Gmail contacts are on personal domains. Duplicate Salesforce accounts are not generated because nothing in the pipeline would consume them; merges are a listed skip.

Structured missingness. Each (tenant, source) has a connected_at date; no events before it. Three tenants lack a source entirely (T2 no Gmail, T3 no Stripe, T5 no Zendesk). Missing Salesforce fields (industry, ARR custom field) at a configurable rate. T5 has five months of history. These are structural causes for "accounts with almost nothing", not random sparsity.

Schema drift, two cases. Zendesk priority changes from int to string in year 2. The Salesforce ARR custom field is renamed in year 3. Each raw row carries source_schema_version.

Duplicates, three kinds. Exact replays with the same source_event_id (drop on id). Re-sent object snapshots with a newer updated_at (upsert, not a duplicate). The same renewal visible in Salesforce and Stripe (reconcile to one outcome).

Late arrival and the live tail. Every event gets received_at = occurred_at + a per-source lag with a multi-day late tail. The generated horizon runs to `horizon_end` in the YAML, set 30 days past the planned deploy date, so a tranche of events has received_at in the future at deploy time. The nightly job only sees events with received_at <= as_of, so each real night ingests the newly arrived tranche. No feeder job.

Planted cases. Ten "signals were sitting right there" accounts: ticket spike and usage decline starting 10 to 12 weeks before a lost renewal, with the renewal in the holdout period and the account's ARR above its CSM's book median so the expected-loss ranking cannot hide a correct probability. The arithmetic: notice deadline is 30 days before renewal, the test wants top-8 entry 4 weeks before that, so the signal must exist at least 9 weeks out. Their ids and planted cause live in truth. These are regression cases, not evidence of realism.

Truth isolation. The generator writes latent health, unobserved factor, planted cause and thin_history per account to `data/truth/`. The ingest, feature and scoring packages never read it. Enforced by a test that greps those packages for the truth path and a test that no feature column correlates 1.0 with latent health. Only the evaluation script reads truth.

Time and money. All timestamps UTC. Single currency, USD.

PII. Email addresses in Gmail and Zendesk payloads are personal data even without bodies. At ingest the local part is HMAC-hashed with a per-tenant key (the hash is the join key, the domain stays in clear for identity). Every synthetic address lives under RFC 2606 reserved domains (`*.example`, `*.test`) plus one synthetic freemail domain for the personal-domain case, so no generated address can collide with a real mailbox.

## 6. Model and evaluation design

Target. One label: the outcome of the account's next renewal decision after as_of. Positive if that renewal is lost (Stripe subscription.deleted, renewal invoice failed and not recovered, or Salesforce renewal opportunity Closed Lost) or contracted (renewed ARR below 70% of prior ARR). Negative if renewed. Censored and dropped if the next renewal date is after the last observed date. Population: accounts active at as_of. Outcome events are listed in one `OUTCOME_EVENT_TYPES` constant that the labeler reads and the featurizer deny-lists.

Training rows. One anchor per active account per week (Monday 00:00 UTC), built by `build_features`, about 450k rows. Not daily: with about 40 events per account over three years, daily rows would be 96% duplicates.

Features, all computed from the account's own events only, NULL when the source was not connected at as_of.
- Coverage: account_tenure_days, total_event_count, n_sources_with_any_event, events_per_source_90d, one `<source>_connected` boolean per source.
- Contract: arr, days_to_renewal, cycle_number, prior_contraction.
- Product: active_users_last_month, sessions_last_month, usage_change_last_month_vs_prior_3, seat_utilisation, months_since_last_activity.
- Zendesk: tickets_30d, tickets_90d, open_tickets, high_priority_30d, reopened_90d, median_csat_90d, ticket_rate_30d_vs_prior_90d, days_since_last_ticket.
- Stripe: failed_payments_90d, unrecovered_failure, days_since_last_failure.
- Gmail: threads_90d, outbound_unanswered_30d, bounce_or_autoreply_90d, days_since_last_inbound.
- Salesforce: owner_changed_180d, missing_field_count.
No tenant_id. No cross-tenant ranks or percentiles. Reason: no per-account reason shown to a CSM should be "your tenant", and one tenant's base rate must not shift another's scores. Per-tenant calibration is reported, not corrected.

Model scope. One global LightGBM model on all five tenants. Per-tenant models are not viable at about 1,000 accounts and a few dozen positives each. Training is the single deliberate cross-tenant reader and runs as app_batch (BYPASSRLS). The model_versions row records the tenant list and per-tenant row counts.

Two fits, both recorded. The evaluation fit trains on cycles up to T and is scored on the holdout; its metrics are what the README reports. The live model_version is a refit on all observed cycles with the same hyperparameters and links to the evaluation model_version it was validated by. Scores on the live system come from the refit.

Split. Cycle-based and temporal. Cutoff T = `train_as_of` minus 12 months, with `train_as_of` and `eval_as_of` from the YAML and recorded in model_versions, never read from the clock. Train on renewal cycles whose renewal date is on or before T. Test on cycles whose renewal date falls in (T, `eval_as_of`]. Every weekly anchor of a cycle goes to the same side, so an account's near-identical rows never straddle train and test, and every test outcome occurs after every training outcome. Never random. The evaluation script also prints the random-row-split AUC on one line to show why it is not reported.

Model fit. Natural prevalence, no scale_pos_weight, deterministic=True with force_row_wise=True (deterministic alone is not honoured without it), num_threads recorded in model_versions, fixed seed, early stopping on a time-ordered validation tail of the training cycles. Logistic regression on the same matrix stays in the repo as the comparison and the skeleton scorer.

Score row. probability, margin (logit), shap_expected_value, full contribution vector as JSONB keyed by feature, arr, days_to_renewal, rank_score = probability x arr, rank_in_book, K, colour, evidence_state (scored or insufficient), coverage fields, model_version, snapshot_id, as_of. Top reasons are derived in the UI. The drill-down shows expected_value + sum(contributions) = margin so reasons reconcile to the number shown.

Ranking. Within a CSM's book, ORDER BY rank_score DESC among scored accounts. Insufficient-evidence accounts are listed separately. One-click re-sort by probability or by days_to_renewal.

Evaluation, all on the test cycles, run by `python -m app evaluate`, output committed to the README.
- Headline: recall@8 per CSM-week. For each weekly cutoff in the holdout and each CSM, the share of accounts whose renewal falls within 90 days and is lost or contracted that appear in the CSM's top 8 by rank_score. Averaged across 43 CSMs and all holdout weeks. Also reported for the probability ranking and as ARR-weighted recall@8.
- precision@8 alongside, with its ceiling stated (about 4 positives per CSM-quarter).
- PR-AUC primary, ROC-AUC secondary. Reliability plot with 10 bins. Brier score.
- Lead time: for every churned account in the holdout, the first week it entered its CSM's top 8; share caught at least 30 and 60 days before the notice deadline; median lead time. This is the direct answer to "nobody saw coming".
- Five-row comparison on the same slice: oracle (rank by true latent health from truth), LightGBM, logistic regression, hand-written rules over the four obvious signals (NULL treated as unknown), days_to_renewal alone. Report what comes out. Expect the model between rules and oracle. If LR is within noise of the GBM, say so and say why the GBM was kept or dropped.
- Slices: by tenant, by CSM, by days-to-renewal bucket (0 to 90, 91 to 180, over 180), by total-event-count bucket (under 10, 10 to 100, over 100). T5 has annual terms and five months of history, so it has no observed renewals; its slice prints "no observed renewals" and the README says so rather than hiding an empty row.
- days_to_renewal: its share of global importance and how often it is the top per-account contribution. This pre-answers "is this a calendar".
- Truth-based checks: planted cause vs the model's top contribution for churned accounts (match rate); recall@8 split by whether the planted signal had arrived before the anchor.
- Worst misses: churned accounts never in their CSM's top 8, listed on a dashboard page with their raw events.

Leakage probes. AUC at 90, 60, 30, 7 and 1 days before renewal; a jump toward 1.0 at short lead is the signature of an outcome-bearing feature and fails the check. Shuffled-label AUC near 0.5 as a cheap pipeline sanity check.

README sentence. These metrics validate that point-in-time labelling, the pipeline and the ranking mechanics are correct on data whose generating process is known. They are not evidence about real-world churn. The first real-data step is re-fitting and re-baselining on actual renewal outcomes.

## 7. Unattended/agentic job design

Trigger. APScheduler in the worker service, UTC CronTrigger from `NIGHTLY_CRON` (default 02:00), max_instances=1. It calls `run_nightly(as_of=fire_time)`. `POST /admin/jobs/nightly` (behind the basic auth) accepts optional `as_of` and `tenant_id` and calls the same function with trigger='manual', so the Loom can run one small tenant live in seconds, with no redeploy, while job_runs shows earlier unattended rows. Locally the schedule can be set to every minute.

Unit of work. (tenant_id, run_date). Each tenant runs inside its own `tenant_tx` and its own try/except. One tenant's malformed data fails that tenant's row, not the run. job_runs columns: job_name, tenant_id, run_date, as_of, trigger, status (running, succeeded, failed, blocked, no_op), started_at, finished_at, error, model_version, policy_version, K, ingest_watermark, counts (events ingested, late events applied, accounts scored, transitions, tasks created, tasks escalated, briefings drafted, quarantined, call count to the LLM), and elapsed ms per stage. Unique on (job_name, tenant_id, run_date).

Rerun semantics. A rerun with the same as_of produces identical rows because visibility is received_at <= as_of. If the tenant's row is succeeded, the rerun is a no_op. If failed or stale running, it re-executes. Scores upsert on (tenant_id, account_id, as_of, model_version). Tasks are unique on (tenant_id, account_id, run_date). `--force` redoes a succeeded run. Past as_of rows are never touched, so `--force` is accepted only for the latest run_date per tenant. Recovery for a missed night is `nightly --as-of <that night>`, which creates rows for a night that has none; it never rewrites a night that has them. Automatic backfill is a skip.

Stages per tenant, each timed.
1. Ingest: load raw events with received_at <= as_of not yet normalised; dedup; resolve; quarantine.
2. Data-quality gate: duplicate rate after dedup must be 0; quarantine rate under 10%; null rate per feature within bounds; mean probability shift vs previous run under 0.10. The shift check is skipped and logged on a rebaseline run, since a new model is expected to move the mean. A tripped check writes status=blocked with the reason and publishes no new scores. The dashboard keeps the previous as_of with a warning.
3. Snapshot: `build_features(tenant_id, as_of)` for active accounts; write rows with ingest_watermark and feature_set_version.
4. Score with the active model_version; write score rows; compute rank_in_book per CSM; set evidence_state.
5. Transitions: compare rank against the previous completed run for the tenant. Insufficient-evidence accounts are excluded.
6. Policy and tasks.
7. Escalation.
8. Briefings: template always; LLM paragraph only for accounts that got a task this run, run last in its own tenant_tx after the main one commits (FORCE RLS means no write happens outside a tenant context); a failed LLM call leaves the template; calls stop at `LLM_MAX_CALLS_PER_RUN` with the count in job_runs.

Policy (`policy.py`, POLICY_VERSION = "1").
- Trigger: the account is in the CSM's top K tonight and was outside the top K on each of the previous 3 completed runs (fewer than 3 prior runs counts as outside); or its probability rose by more than 0.15 since the last task on that account.
- Seed: on bootstrap and on every rebaseline, create a task for each account currently in its CSM's top K that has no open, snoozed or escalated task, with rule_fired='seed', under the budget. Without this, accounts already red at launch or after a model deploy would never be tasked and never escalate, which is the "nothing slips" hole.
- Budget: at most K new tasks per CSM per run, highest rank_score first. On breach, an audit row, not more tasks.
- Dedup: no new task while one is open, snoozed or escalated for the account; append tonight's reasons to it instead.
- Dismissed tasks suppress re-tasking until probability rises by 0.15 over the dismissed value.
- Rebaseline: on the first run after model_version or feature_set_version changes, write scores, write one "rebaselined" audit row, skip transition-based tasks, run the seed rule.
- Every transition row stores from_score_id, to_score_id, from_rank, to_rank, rule_fired, policy_version, K, run_id. Every task stores score_id, transition_id, briefing_id, run_id, rule_fired, policy_version.

Task lifecycle. status in {open, done, snoozed_until, dismissed, escalated}; dismissed carries a one-line reason. Buttons on the list write the status and an audit row. The list shows status and age beside each account. Escalation: an open task older than 5 business days on an account renewing within 60 days flips to escalated, writes an audit row, and is pinned to the top with a badge. Idempotent on rerun. Dismissal reasons are the real-world precision signal for tuning K and the delta; noted in the README.

Briefing. The briefing is the deterministic reason list on the score row: feature, value, direction, contribution, and links to the 3 to 5 raw events behind it. That is the whole briefing when the LLM is off. If on, its only input is that list serialised as JSON; the briefing row stores input_json, prompt_version, model id, output_text, generated_at; the UI renders the paragraph under the list labelled "generated from the reasons above". NULL features render as "Zendesk not connected for this tenant", never "0 tickets". Insufficient-evidence accounts get an "onboarding check-in" template only. Nightly LLM call count equals tasks created, roughly 43 x 8 at most; stated in the README.

Staleness. The list header reads "scores as of <as_of>, last run <status> at <finished_at>", styled as a warning if status is not succeeded or the newest success is older than one day. A failed Sunday run shows Friday's scores with a visible stamp, never a blank list. Run history page shows the last 30 job_runs rows and the next scheduled fire time.

Cadence. Nightly rescore, weekly anchor, durable tasks. The list is live from the latest run. Each row is compared with the week's first run and marked new since Monday, moved up, or moved down. No frozen list. Tasks are the CSM's stable work list.

After the generated tail is exhausted the job correctly writes no_op rows. The README says so.

## 8. Multi-tenancy and traceability design

RLS, real.
- First migration creates `app_owner` (owns tables, runs migrations only), `app_batch` (BYPASSRLS, used only by the trainer, which is the one deliberate cross-tenant reader) and `app` (web, worker, loader, briefing writes; ordinary role; never the Compose superuser). FORCE ROW LEVEL SECURITY applies to the table owner too, so the loader cannot run as app_owner without a tenant context; it loads per tenant inside tenant_tx like everything else.
- Every tenant-scoped table: ENABLE and FORCE ROW LEVEL SECURITY with one policy USING and WITH CHECK `tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid`. Unset context returns zero rows, not everything.
- One helper `tenant_tx(tenant_id)` opens a transaction and runs `SELECT set_config('app.tenant_id', :tid, true)` (SET LOCAL cannot take a bind parameter). Every request handler and every per-tenant job stage goes through it. No other code sets the context. Never session-level SET on a pooled connection.
- Tenant context comes from the signed session cookie set by the switcher. Routes are `/accounts/{account_id}`, `/scores/{score_id}`, `/runs/{run_id}`, with globally unique UUIDs. A link opened under the wrong tenant returns zero rows and a 404.
- Cross-tenant paths, named: the nightly job loops tenants inside tenant_tx; training runs as app_batch and records what it saw in model_versions.
- README has a short table of which process connects as which role.

Trace chain. Every CSM-visible thing has a foreign key back to the computed thing.

| What the CSM sees | Row behind it | What it carries |
|---|---|---|
| A raw event on the timeline | raw_events | source, source_event_id, occurred_at, received_at, schema version, payload; "received late" marker when received_at is after the snapshot's watermark |
| A normalised event under a feature | normalised_events | match_method (crosswalk, email_domain), link to raw row |
| A feature value | snapshots | as_of, built_at, ingest_watermark, feature_set_version; the drill-down event query uses the same predicate (occurred_at in window AND received_at <= ingest_watermark) so the list reconciles exactly to the stored value, plus "N events arrived after this snapshot" |
| The probability, rank and colour | scores | margin, expected_value, full contribution vector, arr, days_to_renewal, rank_score, rank_in_book, K, evidence_state, coverage, model_version |
| The model | model_versions | feature list and order, cycle range, tenant row counts, metrics, booster text |
| "Went red" | transitions | from and to score ids and ranks, rule, policy_version, K, run_id |
| The paragraph | briefings | input_json, prompt_version, model id, output_text |
| The task and its badge | tasks and audit rows | score_id, transition_id, rule, status history |
| "Last night the system did X" | job_runs | counts, stage timings, config snapshot, ingest watermark |

Immutability. Past-as_of snapshot and score rows are never rebuilt. A late event enters tonight's snapshot and no earlier one, so no training row changes when late data arrives. The 10x section's "nightly feature build" means tonight's build, not history recomputation.

Drill-down pages. Monday list, account page (score history, tasks, timeline with match_method and late markers), score page (reasons, reconciliation check, feature table with NULL semantics, link per feature to its events), run page, quarantine summary per tenant, worst misses.

## 9. Tests and how we know it works

| Property claimed | Test | Step |
|---|---|---|
| Generator meets spec | Acceptance script: counts within 10%, non-zero late share, non-zero duplicate rate per kind, accounts with under 5 events exist, three tenants missing a source | 2 |
| Byte-identical regeneration | Run twice with the seed, compare hashes | 2 |
| Truth never reaches the pipeline | grep test for the truth path in ingest, features, scoring; no feature correlates 1.0 with latent | 2 |
| Duplicates collapse correctly | One test per kind: same id dropped, newer updated_at upserts, SF and Stripe renewal reconcile to one label | 3 |
| Drift normalises | Parametrised test: year-1 and year-3 rows produce identical normalised rows; unparseable row lands in rejects with a reason | 3 |
| Order independence | Ingest a batch shuffled with fixed received_at, snapshot tables byte-identical | 3 |
| Identity | Unmatched Stripe customer lands in quarantine and in the run counts; personal-domain Gmail is quarantined | 3 |
| NULL is not zero | Account in a tenant without Zendesk: tickets_30d IS NULL, zendesk_connected false, briefing text never says "no tickets" | 3 |
| Point-in-time | Event with occurred_at <= D and received_at > D leaves snapshot D unchanged and changes the next one | 1 |
| Outcome never a feature | Featurizer deny-lists OUTCOME_EVENT_TYPES; adding an outcome event changes no feature | 4 |
| No short-lead leakage | AUC at 90/60/30/7/1 days does not jump toward 1.0 | 4 |
| Score reproducible | Load model by model_version, rescore the stored vector, probability matches, expected_value + sum(contrib) = margin | 4 |
| Planted cases caught | The ten seeded accounts enter their CSM's top 8, by rank_score and by probability, at least 4 weeks before the notice deadline (signals are planted 10 to 12 weeks before renewal, so this is reachable) | 4 |
| Tenant isolation | Under tenant A, zero rows with tenant_id B on every scoped table; no context, zero rows; nightly for A writes nothing for B | 1 |
| Rerun is safe | Run, raise after tenant 2 of 5, rerun: each tenant has exactly one succeeded row, no duplicate tasks, scores equal a clean run | 5 |
| No flapping | Identical scores two nights produce zero tasks; a series oscillating across the K boundary produces at most one task per 3 runs | 5 |
| Escalation | Fires once, no-op on rerun | 5 |
| Data-quality gate | A tripped check writes blocked and no new scores | 5 |
| Routes | TestClient: list, account, score, run pages render for a seeded tenant; wrong tenant 404s | 6 |

How we know it works, in the README and the Loom: the evaluation tables of section 6 (recall@8, lead time, the five-row comparison, slices), the four tests shown running (point-in-time, isolation, rerun, no-flap), job_runs rows from nights nobody triggered, the reconciliation arithmetic on the live drill-down, and a score reproduced from the terminal with `python -m app rescore <score_id>`.

## 10. Deliberate skips

- Real connectors to the five sources (generator stands in; the raw table shape is what a connector would write to).
- Email sending: the job produces an internal briefing and a task; any customer outreach is composed and sent by the CSM from their own client; if the optional draft is built, "mark as sent" writes an audit row and dispatches nothing.
- Authentication beyond the tenant and CSM switcher plus one shared basic-auth password; a real login changes who sets the session value, not DB enforcement.
- Task workflow beyond one escalation status: SLA configuration, escalation digests, CS-lead view.
- Model retraining automation and drift monitoring (PSI, null-rate alerts).
- LLM output validation: per-sentence citation checks, numeric post-checks, prompt versioning beyond a constant.
- Identity: fuzzy name matching, match confidence, renamed companies, parent and subsidiary, multi-domain accounts, duplicate CRM records and merges.
- Per-tenant calibration layers, per-tenant SHAP baselines, per-tenant policy overrides, cooldown windows, renewal-date task expiry.
- CSM reassignment history as a feature (owner is resolved as of a date; that is all).
- Monthly contract terms, auto-renew flags, seats and plan tiers, multi-currency, seasonality (weekday, December, quarter-end clustering).
- Composite (tenant_id, account_id) foreign keys; RLS plus the isolation tests cover the leak that matters.
- Second worker replica and advisory locks; automatic backfill of missed nights; data drift once the synthetic tail is exhausted.
- Daily feature materialisation over history; recomputable on demand through `build_features`.

## 11. What breaks first at 10x

State the axis. Three axes, one line each, with the 1x numbers from job_runs stage timings so the README quotes measurements and a linear extrapolation, not an assertion.

- 10x events (2M): the nightly feature build is a full pandas pass over each tenant's events per as_of, O(events) per night and O(weeks x events) for the training backfill. Hypothesis: this is the first thing to break, at bootstrap time on the small host, before anything else. Mitigation named, not built: incremental features from a received_at watermark and per-account event partitions.
- 10x accounts (50,000): score rows grow 50,000 a night with a full contribution vector; the per-CSM list is unchanged only if the 120-per-CSM ratio holds. Mitigation: retention on nightly snapshot rows, contributions stored top-10 plus residual.
- 10x tenants (50): the per-tenant loop in one worker makes the run 50x longer and a single failure isolation unit; RLS policy evaluation is not the bottleneck. Mitigation: a queue with per-tenant jobs and pg_try_advisory_lock per tenant, then a second replica.
- LLM calls scale with tasks created (K per CSM), not accounts, so narration is not the first failure. Said in one line.
- Say whether the measured numbers confirmed or contradicted this hypothesis.

## 12. Deliverables checklist and walkthrough questions to prepare for

Checklist.
- `docs/brief.md` as the first commit, before any code, and `docs/deck.pptx` (about 8 slides) made in step 7 about what was built: brief recap, what shipped, what diverged and why, evaluation table, 10x numbers.
- Repository with MIT LICENSE, CI (ruff + pytest on a Postgres service), `.env.example`, no generated data committed, one README line that every dependency is permissively licensed and all data is synthetic.
- README under the three asked headings (decided, skipped, 10x) with the running log in `docs/decisions/`: decisions, the mess-handling block (identity, duplicates, thin accounts, NULL vs zero, UTC, data-quality checks and their consequence), the role table, the data report, the evaluation tables, deliberate skips, 10x analysis with measured numbers, host and cost, the "how we know it works" section citing the tests.
- Live URL with basic auth, `/health`, worker deployed at least three days before recording, job_runs showing unattended runs.
- Loom, scripted to 10 to 12 minutes: open the live list as a CSM; click one reason down to raw events, show the reconciliation check and a late-arrival marker; open the run history and the unattended rows; trigger one run live on the smallest tenant through the admin endpoint with an `as_of` one night ahead and show the transition, task and briefing it produced (checked the evening before that a planted case enters that night); show the evaluation tables and the four tests already run in a second terminal; close on the 10x numbers. Railway variables never on screen.

Questions to be ready for.
1. Your top contribution is days_to_renewal. Is this a model or a date diff? (Answer with the importance share, the per-bucket slices, and the calendar-only baseline row.)
2. An event dated 30 days ago arrives tonight. Which snapshots change, and does your training set change? (Tonight's only; no.)
3. The job crashed halfway on Sunday. What does the CSM see Monday and what do you run? (Stale stamp over Friday's scores; `nightly --as-of`.)
4. Why does the model beat your rules baseline, given you wrote both? (Oracle ceiling, unobserved churn share, confounders, shared-author caveat in the README.)
5. What does "transition" mean when you said thresholds drift? (Rank-based entry into top K with a 3-run anti-flap rule; stored on every task.)
6. Why not auto-email? (The three reasons and the scoping sentence.)
7. What guarantees nothing slips now? (Every account scored nightly, every entry tasked, open tasks escalated at 5 business days, all in job_runs; no guarantee a human acts.)
8. Why one model across tenants, and why is tenant_id not a feature?
9. How does tenant context reach Postgres, and what happens if it is unset? (Session cookie, SET LOCAL, zero rows.)
10. Why no daily snapshot table?
11. How do you know the model is not reading the generator's answer? (Truth isolation tests, oracle gap below 1.0, lead-time probe.)
12. Why is a Stripe payment failure a signal and not the label? (Mid-term failures vs the renewal invoice; OUTCOME_EVENT_TYPES.)
13. What does the pipeline do with a Zendesk ticket it cannot map to an account?
14. Why monthly product rollups? (The budget arithmetic; 10x events is weekly rollups.)
15. What happens to the live system when the synthetic tail ends?
16. Is a nightly cron job agentic? (Do not argue the word. Show the policy module deciding, with a budget, dedup, escalation and an audit trail, and that the exercise defines agentic as unattended.)
17. Where is the LLM in the ranking path? (Nowhere. Show the briefing row with input_json.)
18. What would you build next with real data? (Re-fit on actual renewals, dismissal reasons as labels, identity confidence.)