# Task breakdown

[PLAN.md](PLAN.md) is the source of truth for scope and design. This file only orders the work. Each task says how you know it is done.

## Critical path

Decisions D1 to D6, then Step 0, then Step 1 (milestone M1: live and submittable), then Steps 2 to 6, then Step 7 with a 3-day soak, then submit.

After M1, Step 5 (agentic job) can start against the logistic-regression scorer while Step 4 (LightGBM and evaluation) is in progress. Step 6 can start once Step 5 has created the tasks table. Everything else is sequential.

| Step | Name | Working days | Ends with |
|---|---|---|---|
| 0 | Brief and repo | 0.5 | Brief committed before any code |
| 1 | Walking skeleton | 2 | M1: live, multi-tenant, traceable, unattended |
| 2 | Generator to spec | 2 | Full messy data and a data report |
| 3 | Pipeline hardening | 1 | Every kind of mess handled and tested |
| 4 | Model and evaluation | 2 | LightGBM live, evaluation tables committed |
| 5 | Agentic job | 2 | Policy, tasks, escalation, briefings |
| 6 | Dashboard deepening | 1 | Full CSM workflow in the UI |
| 7 | Ship | 1, plus 3-day soak | README, deck, Loom, submission |
| | Total | 11.5, plus soak | |

## Decisions and setup

- [ ] **D1. Set the deadline.** Write it at the top of PLAN.md section 3. It decides whether the cut order at the bottom of this file applies.
- [x] **D2. Approve hosting spend.** Done differently: one GCP e2-small VM, about $21 a month (docs/decisions/0002-host-on-gcp-vm.md). Railway Hobby, about $5 to $10 a month. No free host keeps a worker awake overnight. Another host changes tasks 1.15, 2.15 and 7.1.
- [x] **D3. Create the Railway project and its Postgres.** Superseded by the GCP VM, with Postgres in Docker Compose. Needs your account and payment method.
- [x] **D4. Create the GitHub repository**, public or private as Fello prefers, so CI runs and reviewers can read it.
- [x] **D5. Decide on LLM narration.** Decided: off, cut first (README, "What I deliberately skipped"). Off by default. If on, put the key into Railway variables yourself. It never goes in the repo or in chat.
- [ ] **D6. Check your Loom plan's length limit.** The free plan has capped videos at 5 minutes. The script needs 10 to 12.

## Step 0. Brief and repo (0.5 day)

- [x] **0.1 Write the one-page brief** in `docs/brief.md` from PLAN.md sections 1 and 2. You review it.
  Done when: one page covers the real problem, what to build first, the three disagreements with the VP, and the business questions with assumed defaults.
- [x] **0.2 Commit the brief alone as the first commit.**
  Done when: `git log` shows it before any code.
- [x] **0.3 Scaffold the repo.** `.gitattributes`, `.editorconfig`, `.gitignore`, `.dockerignore`, MIT `LICENSE`, `.env.example`, `pyproject.toml` with `uv.lock`, `Dockerfile` on a pinned slim image with `libgomp1`, `compose.yaml` with db on a named volume, web and worker.
  Done when: `docker compose config` validates and the db service starts.
  Verified: `docker compose config` validates, the db service starts healthy (Postgres 16.15), and the image builds and trains LightGBM.
- [x] **0.4 Add CI and secret scanning.** GitHub Actions running ruff and pytest against a Postgres service. Gitleaks pre-commit hook. CI runs remotely only after D4.
- [x] **0.5 Vendor htmx** into `static/`, with version and hash in the README.
- [x] **0.6 Start the README** with the three required headings, and `docs/decisions/` as the running log.

## Step 1. Walking skeleton (2 days), ending at milestone M1

Thin on depth, complete on every non-negotiable. Iterate on `config/small.yaml`. Deploy on `config/full.yaml`.

- [x] **1.1 Config files.** `small.yaml` and `full.yaml` with the seed and every date: `horizon_start`, `horizon_end`, `train_as_of`, `eval_as_of`. No code reads the wall clock.
- [x] **1.2 Thin generator.** Tenants, CSMs, accounts, contracts with renewal outcomes, five sources with native ids, crosswalk, `occurred_at` and `received_at`, three signals driven by a latent. Writes `data/` and `data/truth/` in binary with LF newlines.
  Done when: the full config produces about 5,000 accounts and 200,000 events.
- [x] **1.3 Schema and roles migration.** Every table listed in PLAN.md step 1, `tenant_id` on each, roles `app_owner`, `app_batch` and `app`, FORCE RLS policies.
- [x] **1.4 `tenant_tx` helper** using `set_config('app.tenant_id', :tid, true)`. The only place tenant context is set.
- [x] **1.5 Loader.** Raw events loaded per tenant inside `tenant_tx` with batched COPY.
- [x] **1.6 Resolve step.** Crosswalk, then email domain, else quarantine. `match_method` on every normalised row.
- [x] **1.7 `build_features(tenant_id, as_of)`** with about ten features. Visibility is `received_at <= as_of`, windows are on `occurred_at`.
- [x] **1.8 Labels and weekly anchors.** One `OUTCOME_EVENT_TYPES` constant used by the labeler and deny-listed by the featurizer.
- [x] **1.9 Logistic regression scorer** behind a `Scorer` interface. A `model_versions` row. Score rows with feature vector, margin and contributions.
- [x] **1.10 Nightly job v0.** Per tenant: ingest, snapshot, score, one `job_runs` row.
- [x] **1.11 Worker.** APScheduler BlockingScheduler, CronTrigger from `NIGHTLY_CRON`, `max_instances=1`.
- [x] **1.12 Web v0.** Signed-cookie tenant and CSM switcher, ranked list, score drill-down to features and raw events, `/health`, shared basic-auth password.
- [x] **1.13 `bootstrap` command.** Migrate, generate, ingest, build anchors, train, score. Idempotent. Runs inside the worker on first start when `model_versions` is empty.
- [x] **1.14 Tests.** Two RLS isolation tests, a late event leaves the old snapshot unchanged, `job_runs` uniqueness.
- [x] **1.15 Deploy to Railway** on the full config. Needs D2 and D3.
  Done when: `/health` is green and the next morning `job_runs` has a row nobody triggered.
  Done on GCP instead of Railway (docs/decisions/0002-host-on-gcp-vm.md): the 2026-10-17 02:00 UTC night ran unattended.

**M1 check.** All five non-negotiables hold on the live system. If time runs out after this point, you still have a complete narrow slice.

## Step 2. Generator to spec (2 days)

- [x] **2.1 Tenant profiles** T1 to T5 exactly as the PLAN.md table: sizes, segments, churn rates, missing sources, the late Zendesk connection for T4, five months of history for T5.
- [x] **2.2 Health model.** Monthly latent trajectory with drift, noise and shocks. Per-source noise, lag and dropout. Churn draw with an unobserved factor so 20 to 25% of churn shows no signal. Separate contraction draw.
- [x] **2.3 Confounders.** Onboarding ticket spike, failed payments that recover within 7 days, ticket volume that scales with usage.
- [x] **2.4 Event vocabulary and budget.** Event types per source, monthly product rollups, source mix within 10% of target, heavy-tailed counts, about 8% of accounts with fewer than 5 events.
- [x] **2.5 Mutable records** for Zendesk tickets and Salesforce records, emitted as change events.
- [x] **2.6 Identity breaks.** 2 to 5% missing back-references, personal-domain contacts, `website_domain` on Salesforce, ambiguous shared domains.
- [x] **2.7 Structured missingness.** `connected_at` per tenant and source, missing Salesforce fields.
- [x] **2.8 Schema drift**, two cases, with `source_schema_version` on every raw row.
- [x] **2.9 Duplicates**, three kinds.
- [x] **2.10 Late arrival.** Lag per source and the live tail out to `horizon_end`.
- [x] **2.11 Ten planted cases.** Signals start 10 to 12 weeks before a lost renewal in the holdout. ARR above the book median of the CSM.
- [x] **2.12 Safe synthetic addresses.** Everything under reserved domains, plus one synthetic freemail domain.
- [x] **2.13 `data-report` command**, output committed to the README.
- [x] **2.14 Tests.** Acceptance script, byte-identical regeneration on Windows and in the container, truth-isolation grep, no feature correlated 1.0 with latent health.
- [x] **2.15 Redeploy** with the full generator.
  Done 2026-10-16 with Step 3 (commit 0f94757, fresh bootstrap on the GCP VM).

Step done when: the data report shows every kind of mess at its target rate and regeneration is byte-identical.

## Step 3. Pipeline hardening (1 day)

- [x] **3.1 Dedup by kind.** Drop repeated event ids, upsert newer `updated_at`, reconcile Salesforce and Stripe renewals into one outcome.
- [x] **3.2 Schema-version dispatch** and a rejects table with a reason per row.
- [x] **3.3 NULL versus zero.** An unconnected source gives NULL features and a `connected` flag of false.
- [x] **3.4 Hash email local parts** at ingest with a per-tenant HMAC key.
- [x] **3.5 Quarantine count** on the dashboard.
- [x] **3.6 Data-quality gate** in the nightly run. A tripped check writes `blocked` and publishes no new scores.
- [x] **3.7 Tests.** One per duplicate kind, drift, order independence, NULL is not zero, unmatched identities land in quarantine.

Step done when: every kind of mess in the data report has a handler and a test.

## Step 4. Model and evaluation (2 days)

- [x] **4.1 Full feature set** from PLAN.md section 6, with no `tenant_id`.
- [x] **4.2 Cycle-based temporal split** driven by the YAML dates.
- [x] **4.3 LightGBM fit.** `deterministic=True`, `force_row_wise=True`, fixed seed, early stopping, contributions from `pred_contrib`.
- [x] **4.4 Two linked model versions.** An evaluation fit and a live refit, each with booster text, feature list, tenant row counts, metrics and `num_threads`.
- [x] **4.5 Full score rows.** `rank_score` as probability times ARR, `rank_in_book`, colour, `evidence_state`, coverage fields.
- [x] **4.6 `evaluate` command.** Recall@8 per CSM-week, plain and ARR-weighted. Precision@8, PR-AUC, ROC-AUC, reliability plot, Brier score, lead time. The five-row comparison: oracle, LightGBM, logistic regression, rules, days-to-renewal alone. Slices, days-to-renewal importance, planted-cause match rate, worst misses.
- [x] **4.7 Leakage probes.** AUC by days before renewal, shuffled-label AUC, the random-split line.
- [x] **4.8 `rescore` CLI** that reproduces a stored score from its model version.
- [x] **4.9 Tests.** Outcome deny-list, score reproducibility and reconciliation, lead-time probe, planted cases caught.
- [x] **4.10 Commit the evaluation output** to the README.

Step done when: the evaluation tables are committed, the leakage probes pass, and the README says honestly whether the model beats the rules and calendar baselines.

## Step 5. Agentic job (2 days). Can start after M1, in parallel with Step 4

- [x] **5.1 Staged nightly run.** Eight stages per tenant with timings, failure isolation per tenant, full `job_runs` columns and statuses.
- [x] **5.2 Rerun rules.** A succeeded night is a no-op, a failed one re-executes, `--force` works only on the latest night, `--as-of` fills a missed night.
- [x] **5.3 Transitions table** with from and to score ids and ranks.
- [x] **5.4 `policy.py`.** Three-run anti-flap trigger, seed rule, per-CSM budget, dedup on open, snoozed or escalated tasks, dismissal suppression, rebaseline.
- [x] **5.5 Tasks.** Lifecycle statuses, escalation after 5 business days inside 60 days of renewal, Unassigned accounts routed to a CS-lead queue.
- [x] **5.6 Briefings.** The deterministic reason list always. The optional LLM paragraph in its own `tenant_tx`, with a call cap and template fallback.
  Done without the LLM paragraph, which was cut first per the cut order (narration off, D5).
- [x] **5.7 Admin trigger.** `POST /admin/jobs/nightly` taking `as_of` and `tenant_id`.
- [x] **5.8 Tests.** Identical scores make zero tasks, oscillation makes at most one task per three runs, crash and rerun, escalation idempotence, the gate blocks, seed on rebaseline.

Step done when: two simulated nights produce transitions, tasks and briefings, and the crash-and-rerun test passes.

## Step 6. Dashboard deepening (1 day)

- [x] **6.1 List columns and re-sort** by probability, ARR, days to renewal and `rank_score`.
- [x] **6.2 Since-Monday markers**: new, moved up, moved down.
- [x] **6.3 Not-enough-signal bucket** with event counts.
- [x] **6.4 Task buttons**: done, snooze, dismiss with reason. Escalated badge. CSRF tokens on every POST.
- [x] **6.5 Account page**: score history, tasks, timeline with match method and late-arrival markers.
- [x] **6.6 Score page**: reconciliation check, NULL shown as "not connected".
- [x] **6.7 Run history page** and the staleness header.
- [x] **6.8 Quarantine summary and worst-misses pages.**
- [x] **6.9 Route tests.** Every page renders for a seeded tenant. A wrong-tenant link returns 404.

Step done when: a CSM can work the whole Monday list without leaving the app.

## Step 7. Ship (1 day, plus a 3-day soak)

- [x] **7.1 Final deploy at least 3 days before recording.**
  Done when: `job_runs` shows three nights nobody triggered.
- [x] **7.2 Measure for 10x.** Read stage timings, extrapolate on events, accounts and tenants, and say whether the hypothesis held.
- [x] **7.3 Finish the README** under the three headings, plus role table, mess handling, data report, evaluation, skips, host and cost, licences.
- [x] **7.4 Build the deck.** About 8 slides about what was built and where it diverged from the brief.
- [x] **7.5 Prepare the Loom.** 10 to 12 minute script. Check that a planted case enters a top 8 on demo night. Pre-run the four headline tests.
- [ ] **7.6 Record the Loom.**
- [ ] **7.7 Submit.** Repo link, live URL and password, deck, Loom.

## Optional, cut first

- [ ] **8.1 Customer outreach draft** beside the briefing, with a "mark as sent" button that sends nothing.

## Cut order if the deadline is short

Cut from the top: 8.1, the LLM paragraph in 5.6, the worst-misses page in 6.8, the second drift case in 2.8, the third duplicate kind in 2.9, the extra slices in 4.6, escalation in 5.5. Never cut a test for something the README claims.
