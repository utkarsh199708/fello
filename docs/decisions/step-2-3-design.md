# Step 2-3 design: generator to spec and pipeline hardening

Date: 2026-10-17. Owner: tech lead. Scope: tasks 2.1-2.14, 3.1-3.7 and the 1.13 carry items (2.15 is out). Most of PLAN.md
section 5 and Step 3 already shipped in d3ea503 and 0f94757 ([step-2-log.md](step-2-log.md), [step-3-log.md](step-3-log.md)).
This note covers only what this pass adds or changes. [step-1-design.md](step-1-design.md) rules still hold: the caller owns
the transaction, nothing reads the clock, imports stay light. `world` creates `docs/decisions/step-2-3-log.md`; every
group appends `## 2026-10-17 <role> (<ids>): <topic>` after re-reading it.

## 1. Rules

- **Extend, never rename.** Keep the signatures of `generate`, `decide(source, payload, crosswalk, domains)`, `resolve`,
  `build_report`, `render`, `run_checks`, `run_nightly`, `bootstrap`, and all report and `job_runs.counts` keys.
- **Random streams.** Each new generator effect gets its own `PURPOSE`: `shocks` 14, `owners` 15, `dropout` 16 (world),
  `resend_salesforce` 17 (mess). New knobs are optional; leaving one out means clean data, which `tests/fixtures/tiny.yaml`
  relies on. Every new knob goes in both `full.yaml` and `small.yaml`.
- **Word bans (tested).** No "truth" in `app/{ingest,ml,jobs,web}`; no "health" or "unobserved" in generated files outside
  `truth/`. Test data: `TEST_DATABASE_URL=.../fello_test_<role>`, generated into `tmp_path` or the scratchpad; never
  commit `data/`.
- **Targets after every generator commit.** 200k events within 10% (192,080 now) and about 5,000 accounts. Each source
  within 10% of its share: Salesforce is already 6% over, so keep it at or below 8.6% by lowering
  `salesforce.upserts_per_month`. Thin share 5-11%, silent churn 18-27%, 10 planted cases. The whole suite stays green,
  `test_evaluation.py` included.
- **Schema.** The only DDL is `tenants.events_sha256 text NULL` (carry). Two meanings change without DDL (harden):
  `scores.csm_id` becomes the owner as of `as_of`, and `job_runs.counts` gains `quarantined_by_reason`. There are no new
  columns or reasons on `normalised_events`, `quarantine` or `rejects`, and no new tenant table. `FEATURE_SET_VERSION`
  stays `fs4`; bump it, and log why, only if a feature definition changes.

## 2. Groups: files, interfaces, tests

**world (data: 2.1-2.4, 2.11).** Files: `app/generator/{world,events,generate,cli}.py`, new `app/generator/stats.py`,
`app/vocab.py`, both configs, new `tests/test_generator_world.py`. Check the T1-T5 table, the confounders and the planted
cases against PLAN.md, and log every parameter with its measured value.
- **Shocks.** `World.shocks: list[Shock(account_index, month, kind, size)]`. The `hrng` order is unchanged; the kind comes
  from stream `shocks`, per `generator.health.shock_kinds: {champion_departure: 0.5, budget_cut: 0.5}`. From a champion
  departure on, Gmail to the account's first contact has `bounce_or_autoreply=true` (no new draw). A budget cut stays
  latent. Truth: `truth/shocks.csv` (`tenant_id,account_id,month,kind,size`).
- **Owner changes.** Vocab: `EVENT_TYPES["salesforce"]["owner_changed"] = ("account_id", "owner_email",
  "last_modified_date")`. `salesforce.owner_change_rate: 0.02` is the share of assigned, non-planted accounts that get
  one reassignment (stream `owners`). `accounts.csv csm_id` stays the owner at creation. Each `account_upserted` carries
  the owner at its own time. Truth: `truth/owner_changes.csv` (`tenant_id,account_id,changed_at,from_csm_id,to_csm_id`).
- **Dropout.** `generator.<source>.dropout` drops that share of a source's events in `_event_rows` (stream `dropout`),
  never an `OUTCOME_EVENT_TYPES` event: gmail 0.02, zendesk 0.01. Product keeps its own dropout.
- **Noise shares.** `stats.signal_shares(world, rows) -> {"silent_churn", "false_alarm"}`, stored in `manifest["stats"]`
  (never on disk) and printed by `python -m app data`. `silent_churn` keeps today's test definition. `false_alarm` is the
  share of renewed renewals (not contracted) with latent at or above the tenant median that show an alarm in the 90 days
  before. An alarm is any of: at least 2 `ticket_created` in 30 days and at least twice the prior-90-day monthly pace;
  newest `active_users` at most 0.7 times the mean of the 3 before; any `invoice.payment_failed`. Target 0.07-0.13:
  tune the confounder knobs, never the churn model.

**mess (data: 2.5-2.10, 2.12).** Files: `app/generator/{events,generate}.py`, both configs, `tests/test_generator_mess.py`.
- **Kind 2 for Salesforce.** `duplicates.salesforce_resend_rate: 0.01` re-sends an `account_upserted` unchanged except for
  a newer `last_modified_date` and `occurred_at` (stream `resend_salesforce`). The ticket re-send delay moves to
  `duplicates.resend_delay_minutes: [10, 2880]`, today's values, so no bytes change.
- **Full-config assertions per kind.** Missing back-references 2-5% per source; personal and shared domains present;
  missing fields within 2 points of the knob; about 1% replays per source; both drift versions in t1-t3; late tails.

**report (data: 2.13).** Files: `app/generator/report.py`, new `docs/data-report.txt`, the README "Data report" section
only, new `tests/test_data_report.py`.
- **CLI.** `python -m app data-report (--config <yaml> | --data <dir>) [--json] [--out <path>]`. `--config` generates into
  a `TemporaryDirectory`; `--out` writes UTF-8 with LF. A header line gives the config, seed, `config_sha256[:12]` and
  `generator_version[:12]`. There are no timings, so the output is deterministic.
- **New `tenant_report` keys.** `duplicates: {replay: {source: rate}, resent: {zendesk: n, salesforce: n},
  renewal_both_sources: n}`; `missing_fields: {industry: rate, arr: rate}` (shares of `account_upserted`);
  `labels: {year: {positive, negative}}`, where positive is `closed_lost` or `renewed_arr < (1 - threshold) * prior_arr`
  with the tenant's threshold from `tenants.csv`, not 0.70; `identity_breaks: {stripe_customers_unmatched,
  zendesk_orgs_unmatched, unknown_domain_addresses, shared_domains}`, as distinct counts from events checked against the
  crosswalk and `accounts.csv`, never against truth.
- **Commit** `--config config/full.yaml --out docs/data-report.txt`; a test asserts it equals a fresh render.

**harden (pipeline: 3.1-3.6).** Files: `app/ingest/objects.py`, `app/ml/score.py` (the CSM lookup only),
`app/jobs/nightly.py` (counts only). Touch `app/ingest/{resolve,schema,pii}.py` or `app/jobs/gate.py` only for a real gap.
Tests: `tests/test_hardening.py`, new `tests/test_owner_as_of.py`.
- **`objects.account_owners(conn, tenant_id, as_of) -> dict[UUID, UUID | None]`.** Per account, take the newest
  `account_upserted` or `owner_changed` received by `as_of`, ordered by `attrs->>'last_modified_date'` then `received_at`,
  so a late older record never wins. Match its hashed `owner_email` to `pii.hash_email(csms.email,
  pii.tenant_key(tenant_id))`. An empty or unknown owner gives None, which is Unassigned and goes to the CS-lead queue. An
  account with no visible record keeps `accounts.csm_id`.
- **Callers.** `score_tenant` takes `csm_id` from `account_owners`; nothing else in `score.py` changes. Evaluation keeps
  `accounts.csm_id`; log it. `job_runs.counts` gains `quarantined_by_reason: {reason: n}`.
- **Re-check 3.1-3.6** against the task text: dedup kinds 1-3, rejects reasons, NULL versus zero, the HMAC keyed by
  `PII_HASH_SECRET`, `web.queries.data_quality` and its list line, and the gate. Fix only real gaps, each with a test. No
  migration is expected; if one is needed, name it `0005_<name>.py`.

**carry (jobs: 1.13 a-d).** Files: `app/jobs/{worker,bootstrap}.py`, `app/ml/cli.py`, `app/ingest/loader.py`,
`app/web/{main,queries}.py`, migration `<next>_tenant_events_sha256.py`. Tests: `tests/test_worker.py`, new
`tests/test_ml_cli.py`, `tests/test_bootstrap.py`, `tests/test_web.py`.
- **(a) Re-bootstrap.** `worker.bootstrap_reason() -> str | None` replaces `has_model()`. It returns "no active model" or
  "active model <v> uses <fsv>, code builds <FEATURE_SET_VERSION>". Either runs `bootstrap()`, which rebuilds anchors under
  the new key, and `train` activates the new model. "Rescore" means the next night scores with that model. A published
  night is never rewritten, because two models at one `as_of` would double the list. Log it.
- **(b) Resolve first.** `anchors` runs `resolve(conn, tid, cfg.train_as_of)` and `score` runs `resolve(conn, tid, as_of)`,
  each in the same `tenant_tx`, before writing snapshots.
- **(c) Refuse, don't replace.** The runtime roles cannot DELETE, and loaded rows are immutable; log it. `load_reference`
  sets `tenants.events_sha256` to the SHA-256 of `events/<slug>.jsonl` on insert.
  `loader.check_loaded_data(data_dir, tenant_rows)` raises `StaleDataError(RuntimeError)` for a config tenant already in
  the DB whose hash is NULL or different. The message names the tenant and both hashes, and says "reset the database
  (docker compose down -v, or drop it) and run bootstrap again". `load_all` calls it first, so `bootstrap` and `load` both
  refuse. Fix any fixture it trips.
- **(d) Per-tenant health.** `/health` adds `tenants_last_run: [{slug, run_date, as_of, status, trigger, finished_at}]`,
  one per tenant in slug order (nulls if a tenant has no run). `last_job_run` stays; still 200 while the DB answers.

**tests (qa: 2.14, 3.7).** Files: new `tests/test_step23_acceptance.py`; the coverage map and full-run entry go in the log.
- **Coverage.** Map each required test to an existing `file::test`, then add only what is missing: a non-zero late share
  per source; two `config/full.yaml` generations with identical manifest hashes and no CR; a tenant without Zendesk with
  `tickets_30d` NULL and `zendesk_connected = 0`; an unmatched Stripe customer and a personal-domain Gmail message landing
  in `quarantine` and in `counts.quarantined_by_reason`.
- **Full run.** Bootstrap `config/full.yaml` into a fresh `fello_test_qa_full`. Log row counts, step timings, per-tenant
  quarantine and rejects, the model version and the report. Flag moved README evaluation numbers; re-running Step 4 is out.

## 3. Order and integration points

1. **world, then mess**, since both edit `app/generator`. world lands `owner_changed` in vocab first. Until
   `account_owners` lands, those events only add to `total_event_count` and `events_90d`.
2. **report and harden in parallel.** report imports only `decide` and `app.vocab`, and harden keeps `decide`'s signature
   and reason names.
3. **carry after harden**, because it edits `loader.py` after harden. Its migration chains to `uv run alembic heads`.
4. **tests after report and carry**, for `quarantined_by_reason` and `tenants_last_run`. If a generator file changed after
   report landed, tests regenerates `docs/data-report.txt` and re-commits it.
