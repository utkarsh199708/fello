# Step 1 design: walking skeleton

Date: 2026-10-16. Owner: tech lead. Status: accepted for Step 1 (tasks 1.1 to 1.14; 1.15 is out of scope).
[../PLAN.md](../PLAN.md) stays the source of truth. This note fixes the file layout, the interfaces between the seven
builder groups and the few points where PLAN.md cannot be followed literally. Builders: read sections 1 to 3, then your
own group's rows. Anything this note leaves open is yours to decide. Log it in `docs/decisions/step-1-log.md`.

## 1. Ground rules for this step

- **Who owns the transaction.** A function that takes `conn` never opens or commits a transaction. The caller wraps
  it in `tenant_tx` (or, for training only, an `app_batch` connection). This rule lets the nightly job run ingest,
  snapshot and score for one tenant atomically.
- **Clock.** Only `app/clock.py` reads the wall clock, and only the worker calls it. CLIs take `--as-of`. Audit columns
  (`built_at`, `started_at`, `loaded_at`, `created_at`) take the database default `now()`. No logic ever reads them.
- **Web stays light.** Package `__init__.py` files stay empty (docstring only), so `import app.ml.feature_spec` does not
  pull in pandas. `app/vocab.py`, `app/ids.py`, `app/db.py` and `app/ml/feature_spec.py` import no numpy, pandas or
  sklearn.
- **Single owner per file** (section 3). Shared files: only `qa` edits `README.md`. Only `jobs` edits `compose.yaml`.
  Only `platform` edits `.env.example`, and it adds every key in section 8 at once. Nobody edits `pyproject.toml` or
  `uv.lock`, because every dependency is already locked. Anyone may append to `docs/decisions/step-1-log.md`. Re-read
  it just before appending.
- Run `uv run ruff check` and `uv run ruff format` on your own files. CI also runs `ruff format --check`.
- **Ids.** Rows have deterministic UUIDs from `app.ids.stable_uuid(*parts)`, so a rerun produces identical rows. Raw
  and normalised event rows are the exception: they use bigint identity keys.
- **Out of scope here (later steps).** Dedup, drift dispatch, HMAC hashing, the quality gate, transitions, policy and
  tasks logic, LightGBM, `evaluate`, CSRF tokens, the insufficient-evidence bucket, `owner_changed`, identity breaks.

## 2. Deviations from PLAN.md decided here

1. **COPY into an RLS table is impossible.** I checked this on our Postgres 16: `COPY FROM not supported with
   row-level security`. `db.copy_insert` therefore COPYs into a `TEMP` staging table and then runs
   `INSERT ... SELECT` inside the same `tenant_tx`. The policy's `WITH CHECK` still validates every row: a staged row
   carrying another tenant's id was rejected in the probe.
2. **Roles are created by `migrate`, not by the Alembic revision.** Creating a `BYPASSRLS` role needs a superuser, and
   passwords must come from env. `db.prepare_database()` runs as the admin URL. It creates the database if missing,
   creates the three roles if missing (catching `duplicate_object`, since roles are cluster-wide and agents run in
   parallel) and grants schema `public` `CREATE` to `app_owner`. Alembic then runs as `app_owner`, so `app_owner` owns
   every table and FORCE RLS binds it. There is no compose initdb script: the existing `pgdata` volume would never run
   it, and Railway has none.
3. **`raw_events.event_type`** is an extra column. Connectors know the type, and it saves `payload->>'type'`
   everywhere.
4. **Step 1 loads reference data from generator CSVs**: tenants, csms, accounts, contracts and crosswalk. This stands
   in for a CRM sync. Step 2 and 3 derive more of it from events.
5. **`tenants` and `model_versions` are global, with no RLS.** `tenants` is the directory the switcher and the nightly
   loop read before any tenant context exists. It holds no customer data. `model_versions` holds the one global
   model. Every other table has `tenant_id` and FORCE RLS.
6. **Labels come from the Salesforce renewal event** (`opportunity_stage_changed`) visible by the label cutoff. Stripe
   outcome events are generated and deny-listed, but the labeler ignores them until 3.1 reconciles the two sources.
7. **A rerun of a succeeded night writes nothing and returns `no_op`.** The unique key allows one row per night. A
   `failed` or `running` row is re-claimed and re-executed. Detecting stale `running` rows is Step 5.

## 3. Layout and owners

| Group (role) | Files it creates |
|---|---|
| gen (data) | `app/__main__.py`, `app/ids.py`, `app/config.py`, `app/vocab.py`, `app/generator/{__init__,generate,cli}.py` (plus any private modules), `config/small.yaml`, `config/full.yaml`, `tests/fixtures/tiny.yaml`, `tests/test_generator.py` |
| schema (platform) | `app/db.py`, `app/migrate.py`, `alembic.ini`, `migrations/env.py`, `migrations/script.py.mako`, `migrations/versions/0001_initial.py`, `.env.example` (edit), `tests/conftest.py`, `tests/test_db.py` |
| ingest (pipeline) | `app/ingest/{__init__,loader,resolve,cli}.py`, `tests/helpers.py` (create), `tests/test_loader.py`, `tests/test_resolve.py` |
| model (ml) | `app/ml/{__init__,feature_spec,features,labels,scorer,train,score,cli}.py`, `tests/helpers.py` (extend), `tests/test_features.py`, `tests/test_labels.py`, `tests/test_scorer.py`, `tests/test_train_score.py` |
| job (jobs) | `app/clock.py`, `app/jobs/{__init__,nightly,bootstrap,worker,cli}.py`, `compose.yaml` (edit), `tests/test_nightly.py`, `tests/test_bootstrap.py`, `tests/test_worker.py` |
| web (web) | `app/web/{__init__,main,auth,queries}.py`, `app/web/templates/*.html`, `tests/test_web.py` |
| tests (qa) | `tests/test_rls_isolation.py`, `tests/test_point_in_time.py`, `tests/test_job_runs.py`, `README.md` (Local development), the full-run entry in `step-1-log.md` |

`app/__main__.py` holds one static table, `COMMANDS = {name: module}`. It imports `module` lazily and calls its
`main(argv: list[str]) -> int`. It is written once, complete, by gen. Later groups only add the module the table
points at. The lazy import keeps `python -m app web` free of pandas.

| Command | Module | Args |
|---|---|---|
| `data` | `app.generator.cli` | `--config <yaml> [--out data]`. Prints counts per tenant and source, plus totals |
| `migrate` | `app.migrate` | (none) |
| `load` | `app.ingest.cli` | `[--data data] [--tenant <slug or uuid>]` |
| `ingest` | `app.ingest.cli` | `--as-of <ts> [--tenant ...]`. Runs the resolve stage only |
| `anchors` | `app.ml.cli` | `--config <yaml> [--tenant ...]` |
| `train` | `app.ml.cli` | `--config <yaml>` |
| `score` | `app.ml.cli` | `--as-of <ts> [--tenant ...]`. Writes the nightly snapshot, then the scores |
| `nightly` | `app.jobs.cli` | `--as-of <ts> [--tenant ...]`. Exits 1 if any tenant failed |
| `bootstrap` | `app.jobs.cli` | `--config <yaml> [--data data]` |
| `worker` | `app.jobs.worker` | (none) |
| `web` | `app.web.main` | (none). Serves on `0.0.0.0:$PORT`, default 8000 |

A `--as-of` value is parsed by `config.parse_utc`: ISO 8601, made timezone-aware in UTC, a naive value read as UTC.

## 4. Shared vocabulary (gen owns; everyone imports)

- `app/ids.py`: `stable_uuid(*parts) -> UUID`, which is `uuid5` of a fixed namespace over `"|".join(map(str, parts))`.
  Id parts: tenant `(cfg.name, cfg.seed, "tenant", slug)`, csm, account and contract alike; snapshot
  `("snapshot", tenant_id, account_id, as_of.isoformat(), fsv)`; score `("score", tenant_id, account_id, as_of, model_version)`;
  run `("run", job_name, tenant_id, run_date)`.
- `app/config.py`: frozen dataclasses `Config(name, seed, horizon_start, horizon_end, train_as_of, eval_as_of,
  anchor_warmup_weeks, tenants: tuple[TenantConfig, ...], generator: dict, sha256)` and
  `TenantConfig(slug, name, segment, accounts, base_churn, history_months, connected_at: dict[str, datetime], top_k,
  contraction_threshold, tenant_id)`. Also `load_config(path) -> Config` and `parse_utc(value) -> datetime`.
- `app/vocab.py` holds data only:
  - `SOURCES = ("salesforce", "zendesk", "product", "stripe", "gmail")`
  - `NATIVE_KEY = {"salesforce": "account_id", "zendesk": "organization_id", "product": "workspace_id", "stripe": "customer"}`. Gmail has no native key.
  - `EMAIL_FIELD = {"zendesk": "requester_email", "gmail": "counterpart_email"}`
  - `EVENT_TYPES[source]`, with the payload fields each type must carry:
    - salesforce `account_upserted {account_id, name, website_domain, owner_email}`
    - salesforce `opportunity_stage_changed {account_id, opportunity_id, stage: closed_won|closed_lost, renewal_date: YYYY-MM-DD, prior_arr, renewed_arr}`
    - zendesk `ticket_created` and `ticket_updated {organization_id, ticket_id, requester_email, status, priority, csat|null}`
    - product `usage_rollup_monthly {workspace_id, month: YYYY-MM, active_users, sessions, key_actions, seats_used, seats_purchased}`. `occurred_at` is the first instant of the following month.
    - stripe `invoice.paid` and `invoice.payment_failed {customer, invoice_id, amount_usd, attempt}`, mid-term only
    - stripe `subscription.updated` and `subscription.deleted {customer, subscription_id, arr_usd}`, emitted at renewal
    - gmail `message {thread_id, direction: inbound|outbound, counterpart_email, bounce_or_autoreply}`
  - `OUTCOME_EVENT_TYPES = frozenset({("salesforce", "opportunity_stage_changed"), ("stripe", "subscription.updated"), ("stripe", "subscription.deleted")})`.
    The labeler reads these events. `compute_features` drops them before computing any feature, `total_event_count`
    included.
- Synthetic addresses: account domains are `<slug>-<n>.example`. CSM and vendor mailboxes are `@<tenant-slug>.test`.
  No other domains appear in Step 1.

## 5. Config and generated files (gen to ingest)

YAML keys (1.1):
- `name`, `seed`
- `dates: {horizon_start, horizon_end, train_as_of, eval_as_of}`: ISO UTC timestamps. `train_as_of` and `eval_as_of`
  fall on a Monday at 00:00.
- `model: {anchor_warmup_weeks: 12}`
- `defaults: {top_k: 8, contraction_threshold: 0.30, accounts_per_csm: 120, history_months: 36}`
- `tenants: [{slug, name, segment, accounts, base_churn, sources: {<source>: connected_at}, ...overrides}]`. A source
  that is absent was never connected.
- `generator: {...}`: free-form knobs owned by gen, such as per-source lag, event rates and signal strength. Record
  the chosen values in the log.

Both configs use these dates: `horizon_start 2023-10-02`, `train_as_of = eval_as_of = 2026-10-12`, `horizon_end
2026-12-14`. The tail past today is what lets the nightly job ingest new events.
- `small.yaml`: t1 has 300 accounts and all five sources. t2 has 200 accounts and no Gmail. About 20k events.
- `full.yaml`: five tenants sized as in PLAN.md section 5 (2000, 1200, 900, 600, 300). Sources follow the same table:
  T2 has no Gmail, T3 no Stripe, T5 no Zendesk, and T4's Zendesk connects 14 months before `train_as_of`. About 200k
  events, within 10%.
- `tests/fixtures/tiny.yaml`: 2 tenants of 60 and 40 accounts, `accounts_per_csm: 25`, the second without Gmail.
  `horizon_start 2024-10-07`, the same other dates.

Output of `generate(cfg, out_dir)`. Files are opened `"wb"`, written as UTF-8 with LF line endings, and rows are
sorted, so two runs give identical bytes.
- `tenants.csv`: `tenant_id,slug,name,segment,top_k,contraction_threshold,connected_at`. The last column is JSON
  `{source: ts}`.
- `csms.csv`: `csm_id,tenant_id,name,email`
- `accounts.csv`: `account_id,tenant_id,name,website_domain,csm_id,started_at`
- `contracts.csv`: `contract_id,tenant_id,account_id,cycle_number,start_date,end_date,arr`. Annual terms, no outcome
  column. Churn means there is no next cycle. A renewal starts the next cycle at `renewed_arr`.
- `crosswalk.csv`: `tenant_id,source,external_id,account_id`, covering every native key from salesforce (18-char Id),
  stripe (`cus_...`), zendesk (organisation id) and product (workspace id).
- `events/<slug>.jsonl`: one compact JSON object per line,
  `{source, source_event_id, source_schema_version, event_type, occurred_at, received_at, payload}`, sorted by
  `(received_at, source, source_event_id)`. `received_at >= occurred_at`, with a per-source lag.
- `truth/health_monthly.csv`: `tenant_id,account_id,month,health`. Only evaluation code ever reads `truth/`.
- `manifest.json`: `{config_name, seed, config_sha256, counts: {slug: {source: n}}, files: {path: sha256}}`

## 6. Database (platform)

Roles: `app_owner` (LOGIN, owns everything, runs migrations), `app_batch` (LOGIN BYPASSRLS, the trainer only) and
`app` (LOGIN, web, worker, loader). None of them is a superuser.

Every tenant-scoped table gets ENABLE and FORCE RLS and one policy `tenant_isolation`, with USING and WITH CHECK both
`tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid`. Each such table has `tenant_id uuid NOT NULL
REFERENCES tenants`. All timestamps are `timestamptz`, and connections set `TimeZone=UTC`.

Grants. Neither runtime role gets DELETE or TRUNCATE anywhere, so past rows are immutable by privilege.

| Role | tenants | model_versions | csms, accounts, contracts, crosswalk, raw_events, normalised_events, quarantine, snapshots, scores | job_runs, tasks |
|---|---|---|---|---|
| app | SELECT, INSERT | SELECT | SELECT, INSERT | SELECT, INSERT, UPDATE |
| app_batch | SELECT | SELECT, INSERT, UPDATE | SELECT | SELECT |

Tables in `0001_initial.py`:
- `tenants(tenant_id uuid PK, slug text UNIQUE, name, segment, top_k int, contraction_threshold numeric, connected_at jsonb)`
- `model_versions(model_version text PK, kind text, feature_set_version text, features jsonb, artifact jsonb, train_as_of, label_cutoff, anchor_first, anchor_last, tenant_row_counts jsonb, metrics jsonb, is_active bool, created_at)`, plus a unique partial index so at most one row is active
- `csms(csm_id uuid PK, tenant_id, name, email)`
- `accounts(account_id uuid PK, tenant_id, name, website_domain, csm_id uuid NULL REFERENCES csms, started_at)`
- `contracts(contract_id uuid PK, tenant_id, account_id, cycle_number int, start_date date, end_date date, arr numeric(14,2))`, UNIQUE `(account_id, cycle_number)`
- `crosswalk(tenant_id, source, external_id, account_id)`, PK `(tenant_id, source, external_id)`
- `raw_events(raw_event_id bigint identity PK, tenant_id, source, source_event_id, source_schema_version int, event_type, occurred_at, received_at, payload jsonb, loaded_at)`. Index `(tenant_id, received_at)`. No unique key: a duplicate is a raw fact, and Step 3 drops it.
- `normalised_events(normalised_event_id bigint identity PK, tenant_id, raw_event_id UNIQUE REFERENCES raw_events, account_id, source, event_type, occurred_at, received_at, match_method CHECK IN ('crosswalk','email_domain'), attrs jsonb, resolved_as_of)`. Index `(tenant_id, account_id, occurred_at)`.
- `quarantine(quarantine_id bigint identity PK, tenant_id, raw_event_id UNIQUE, source, reason, detail jsonb, resolved_as_of)`. Reasons: `unknown_native_key`, `unknown_domain`, `ambiguous_domain`, `no_identity`.
- `snapshots(snapshot_id uuid PK, tenant_id, account_id, as_of, kind CHECK IN ('anchor','nightly'), feature_set_version, features jsonb, ingest_watermark NULL, built_at)`, UNIQUE `(tenant_id, account_id, as_of, feature_set_version)`
- `scores(score_id uuid PK, tenant_id, account_id, snapshot_id, model_version, as_of, csm_id NULL, probability float8, margin float8, expected_value float8, contributions jsonb, arr numeric, days_to_renewal int, rank_score float8, rank_in_book int, k int, evidence_state text DEFAULT 'scored', run_id NULL REFERENCES job_runs, created_at)`. UNIQUE `(tenant_id, account_id, as_of, model_version)`. Index `(tenant_id, csm_id, as_of)`.
- `job_runs(run_id uuid PK, job_name, tenant_id, run_date date, as_of, trigger CHECK IN ('schedule','cli','bootstrap','manual'), status CHECK IN ('running','succeeded','failed','blocked','no_op'), model_version NULL, ingest_watermark NULL, counts jsonb, stage_ms jsonb, error text, started_at, finished_at)`, UNIQUE `(job_name, tenant_id, run_date)`
- `tasks(task_id uuid PK, tenant_id, account_id, run_date date, score_id, run_id, status text DEFAULT 'open', rule_fired, policy_version, created_at, updated_at)`, UNIQUE `(tenant_id, account_id, run_date)`. Step 5 extends it with a new revision.

## 7. Public interfaces

**platform: `app/db.py`, `app/migrate.py`**
- `admin_url() -> str` reads `DATABASE_URL` and rewrites `postgresql://` to `postgresql+psycopg://`.
- `role_url(role) -> str` takes the admin URL with user `role` and password `env[<ROLE>_PASSWORD]`. It fails loudly if
  the password is unset.
- `get_engine(role="app") -> Engine` is cached per `(role, url)`, so tests can repoint `DATABASE_URL`.
- `tenant_tx(tenant_id, engine=None)` is a context manager yielding a `Connection`. It opens `engine.begin()` and runs
  `SELECT set_config('app.tenant_id', :tid, true)`. No other code sets the tenant context.
- `copy_insert(conn, table, columns, rows, *, on_conflict="") -> int` COPYs into a temp stage, then inserts into
  `table`, returning the number of rows inserted. Pass jsonb values as JSON text.
- `tenant_ids(tenant=None, engine=None) -> list[UUID]` returns all tenants ordered by slug, or the one matching a slug
  or UUID.
- `prepare_database()` and `migrate()` are idempotent: `migrate` runs `prepare_database` and then
  `alembic upgrade head` as `app_owner`.

**pipeline: `app/ingest/loader.py`, `app/ingest/resolve.py`**
- `load_reference(data_dir) -> dict[str, int]` inserts the tenants as `app` without a tenant context, then the other
  reference CSVs per tenant inside `tenant_tx`, with `ON CONFLICT DO NOTHING`.
- `load_events(data_dir, tenant_id) -> int` runs in one `tenant_tx` and calls `copy_insert` in batches of 10k. It
  skips a tenant that already has `raw_events`.
- `load_all(data_dir) -> dict` runs both.
- `resolve(conn, tenant_id, as_of) -> ResolveCounts(normalised, quarantined, by_method)` handles the raw rows with
  `received_at <= as_of` that have neither a normalised nor a quarantine row. It tries the crosswalk on
  `payload[NATIVE_KEY]` first. Failing that, it matches `payload[EMAIL_FIELD]`'s domain against
  `accounts.website_domain`, which must match exactly one account. Otherwise the row goes to quarantine with a reason.
  Rerunning with the same `as_of` changes nothing.
- `ingest_watermark(conn, as_of) -> datetime | None` returns `max(received_at)` of the tenant's normalised events with
  `received_at <= as_of`.

**ml: `app/ml/*`**
- `feature_spec.py` has no heavy imports. It holds `FEATURE_SET_VERSION = "fs1"`, the ordered tuple `FEATURES`,
  `EventFilter(sources, event_types, window_days)` and `FEATURE_EVENTS: dict[str, EventFilter | None]`, which says
  which events fed each feature. `None` means the feature comes from the contract or tenant row.
- Starting features: `account_tenure_days`, `arr`, `days_to_renewal`, `cycle_number`, `total_event_count`,
  `tickets_30d`, `ticket_rate_30d_vs_prior_90d`, `active_users_last_month`, `usage_change_last_month_vs_prior_3`,
  `failed_payments_90d`, `threads_90d`, and `zendesk_connected`, `product_connected`, `stripe_connected`,
  `gmail_connected` (each 0 or 1, never NULL).
- Visibility is `received_at <= as_of`. Windows are `(as_of - window, as_of]` on `occurred_at`. A source feature is
  NULL when the tenant's `connected_at[source]` is missing or later than `as_of`. Contract values come only from the
  contract with `start_date <= as_of < end_date`, which is also the definition of an active account.
- `features.py`:
  - `load_feature_inputs(conn, tenant_id, upto) -> FeatureInputs`
  - `compute_features(inputs, as_of) -> DataFrame`: pure, indexed by `account_id`, columns in `FEATURES` order
  - `build_features(conn, tenant_id, as_of)`: loads, then computes
  - `write_snapshots(conn, tenant_id, as_of, kind, features=None) -> int`: `ON CONFLICT DO NOTHING`, and stores
    `ingest_watermark`
- `labels.py`:
  - `monday_anchors(start, end) -> list[datetime]`
  - `build_anchors(conn, tenant_id, start, end) -> int`: loads inputs once, then calls `compute_features` per Monday
  - `label_anchors(anchors, contracts, outcomes, thresholds, label_cutoff) -> Series` returns 1, 0 or NaN. NaN means
    censored, and the row is dropped. The next renewal is the active contract's `end_date`. The outcome is the
    Salesforce event with `payload.renewal_date == end_date` and `received_at <= label_cutoff`. A row is positive if
    `closed_lost` or if `renewed_arr < (1 - contraction_threshold) * prior_arr`.
- `scorer.py`: a `Scorer` Protocol with `kind`, `features`, `fit(X, y)`, `score(X) -> ndarray` (probabilities),
  `margin(X) -> ndarray`, `contributions(X) -> (expected_value, DataFrame)` and `to_artifact() -> dict`, plus
  `scorer_from_artifact(kind, artifact)`.
  - `LogisticScorer` imputes NULL to the training mean, standardises, then fits sklearn `LogisticRegression`. Its
    artifact is plain JSON: means, scales, coefficients and the intercept.
  - `expected_value` is the intercept, and a feature's contribution is `coef * z`. So `expected_value + sum = margin`,
    and a NULL feature contributes 0.
- `train.py`: `train(cfg) -> str` runs as `app_batch`. It reads anchor snapshots with `as_of <= train_as_of`, the
  contracts, the `OUTCOME_EVENT_TYPES` events received by the cutoff and each tenant's threshold. It fits the model.
  The model version is `logreg-<sha256(artifact)[:12]>`; if that version already exists, `train` only makes it active.
- `score.py`:
  - `active_model(conn) -> (model_version, Scorer)`
  - `score_tenant(conn, tenant_id, as_of, run_id=None) -> int` scores the snapshot rows at `as_of`. `rank_score` is
    `probability * arr`, and `rank_in_book` is `row_number()` over `csm_id` ordered by `rank_score` descending. NULL
    `csm_id` forms one book. Writes use `ON CONFLICT DO NOTHING`.
- `tests/helpers.py`: pipeline adds `load_tiny(data_dir) -> list[UUID]`, which generates, loads and resolves tiny at
  its `train_as_of`. ml adds `seed_tiny(data_dir)`, which runs that plus anchors, `train` and nightly snapshots and
  scores at `train_as_of`. Both are idempotent.

**jobs: `app/clock.py`, `app/jobs/*`**
- `clock.now_utc() -> datetime` returns `CLOCK_OVERRIDE` if it is set, otherwise the wall clock. It is the only
  wall-clock read in `app/`.
- `run_nightly(as_of, tenant=None, trigger="cli") -> list[TenantRun(tenant_id, run_id, status, counts, error)]`. For
  each tenant:
  - It claims the night in its own `tenant_tx`: `INSERT ... ON CONFLICT (job_name, tenant_id, run_date) DO UPDATE SET
    status='running', ... WHERE job_runs.status IN ('failed','running') RETURNING run_id`. No row back means `no_op`.
  - It runs the work in a second `tenant_tx`: `resolve`, then `write_snapshots(kind='nightly')`, then `score_tenant`.
  - It finalises in a third `tenant_tx` with status `succeeded` or `failed`, plus the error text. Each tenant has its
    own try/except.
  - `run_date = as_of.date()`. `counts` holds `{events_normalised, events_quarantined, snapshots_written,
    accounts_scored}`, and `stage_ms` holds `{ingest, snapshot, score}`.
- `bootstrap(config_path, data_dir)` uses `as_of = cfg.train_as_of`. Each step is idempotent:
  1. `migrate`.
  2. `generate`, unless `manifest.json` already has this `config_sha256`.
  3. Refuse if the database already holds tenants that are not in this config.
  4. `load_all`.
  5. `resolve` at `as_of`.
  6. `build_anchors` from `horizon_start + warmup` to `as_of`.
  7. `train`.
  8. `run_nightly(as_of, trigger="bootstrap")`.

  It prints the elapsed time of each step.
- `worker.main` runs `migrate`. If `model_versions` is empty, it runs `bootstrap($BOOTSTRAP_CONFIG, $DATA_DIR)`.
  Then it starts a `BlockingScheduler(timezone=UTC)` with one job: `CronTrigger.from_crontab($NIGHTLY_CRON,
  timezone="UTC")`, `max_instances=1`, `coalesce=True`. The job calls
  `run_nightly(as_of=now_utc() truncated to the minute, trigger="schedule")`. With `CLOCK_OVERRIDE` set, every fire
  after the first is a `no_op`.
- `compose.yaml`: worker runs `python -m app worker` and web runs `python -m app web`. Both get the env keys in
  section 8, with `BOOTSTRAP_CONFIG=config/full.yaml` and `DATA_DIR=/srv/data`. Only the worker migrates.

**web: `app/web/*`** (FastAPI, Jinja2, vendored htmx at `/static`)
- `create_app() -> FastAPI`. Starlette `SessionMiddleware(secret=SESSION_SECRET, same_site="strict",
  https_only=COOKIE_SECURE)` keeps `{tenant_id, csm_id}` in the session.
- Basic auth covers every route except `/health`, which a platform health check must reach and which shows counts
  only. It checks the password only, with `secrets.compare_digest`. The app refuses to start without
  `BASIC_AUTH_PASSWORD`.
- Routes:
  - `GET /` and `GET|POST /switch`. The switcher is the only route that reads a tenant id from the request, and all it
    does is write the cookie. It validates the CSM inside `tenant_tx`.
  - `GET /list` shows the session CSM's rows at the tenant's latest `scores.as_of`, ordered by `rank_in_book`.
  - `GET /scores/{score_id}` shows features, contributions and the check `expected_value + sum = margin`.
  - `GET /scores/{score_id}/events/{feature}` is an HTMX partial. It uses the `FEATURE_EVENTS` predicate plus
    `received_at <= snapshot.ingest_watermark`, joined to `raw_events`.
  - `GET /accounts/{account_id}` shows the account, its contract, its score history and a timeline with
    `match_method`, limited to `received_at <= latest as_of`.
  - `GET /health` returns JSON with the DB status, counts of tenants and accounts, the active `model_version` and the
    latest `job_runs` row, found by looping over tenants in `tenant_tx`. It returns 503 if the DB is down.
- All tenant reads go through `tenant_tx` as `app`. An id that belongs to another tenant finds no row and returns 404.

## 8. Environment keys

Platform adds every key below to `.env.example`, with dev-only values:
- `DATABASE_URL`: the admin (superuser) URL that the role URLs are derived from
- `APP_OWNER_PASSWORD`, `APP_BATCH_PASSWORD`, `APP_PASSWORD`
- `NIGHTLY_CRON`, `CLOCK_OVERRIDE`
- `BOOTSTRAP_CONFIG=config/full.yaml`, `DATA_DIR=data`
- `SESSION_SECRET`, `BASIC_AUTH_PASSWORD`, `COOKIE_SECURE=0`, `PORT=8000`
- `TEST_DATABASE_URL` (tests only)

To run a CLI from Git Bash:

```bash
set -a; . ./.env.example; set +a
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/fello_dev_<role> uv run python -m app ...
```

`migrate` creates that database if it is missing.

## 9. Tests

- `tests/conftest.py` (platform) reads `TEST_DATABASE_URL`, falling back to
  `postgresql+psycopg://postgres:postgres@localhost:5432/fello_test`. It refuses any database whose name does not
  start with `fello_test`. Once per session it drops and recreates that database, sets `DATABASE_URL` to it, sets the
  three dev passwords with `setdefault` and runs `migrate()`. If Postgres is unreachable, it skips the DB tests.
- Each agent sets `TEST_DATABASE_URL=.../fello_test_<role>`.
- Fixtures other than the database live in the test module that uses them. Shared seeding goes through
  `tests/helpers.py`, imported as `from tests.helpers import seed_tiny`; each module seeds into its own `tmp_path`.

| Group | Files | Must assert |
|---|---|---|
| gen | `test_generator.py` | tiny run twice gives identical bytes and only LF; id formats (`001...` 18 chars, `cus_`); only `.example` and `.test` domains; `truth/` is the only place health appears; small counts land near target |
| platform | `test_db.py` | role attributes; context unset gives 0 rows; `set_config` is transaction-local; WITH CHECK rejects a wrong-tenant insert; `copy_insert` works under RLS; `migrate` is idempotent |
| pipeline | `test_loader.py`, `test_resolve.py` | load is idempotent; one row per match method; each quarantine reason; `received_at > as_of` is not processed; a rerun changes nothing |
| ml | `test_features.py`, `test_labels.py`, `test_scorer.py`, `test_train_score.py` | NULL when not connected; window edges; adding an outcome event changes no feature; anchors fall on Monday 00:00 UTC; label rules and censoring; `expected_value + sum = margin`; artifact round-trip; `model_versions` row; ranks within a book |
| jobs | `test_nightly.py`, `test_bootstrap.py`, `test_worker.py` | one `job_runs` row per tenant; a rerun returns `no_op`; a failing tenant does not stop the others; bootstrap on tiny twice is idempotent; scheduler is UTC cron with `max_instances=1`, checked without starting it |
| web | `test_web.py` | list, score and health pages render; wrong tenant gives 404; no auth gives 401; importing `app.web.main` in a subprocess loads no pandas |
| qa | `test_rls_isolation.py`, `test_point_in_time.py`, `test_job_runs.py` | for every table with a `tenant_id` column (from the catalog; only `tenants` exempt): under tenant A no B rows, and with no context 0 rows; a late event leaves snapshot D unchanged and changes D+1, and recomputing D gives the stored value; a duplicate `job_runs` insert raises; a rerun is a `no_op` |

## 10. Order and integration points

1. **gen and schema run in parallel.** Gen lands `__main__.py`, `vocab.py`, `config.py` and `ids.py` early in its
   first commit, so the others can import them.
2. **ingest** needs gen's files (section 5) and schema's `tenant_tx` and `copy_insert`.
3. **model** needs normalised events and `ingest_watermark`.
4. **job and web run in parallel.** Both call ml's functions and `seed_tiny`. Neither edits the other's files.
5. **qa** runs the three property tests. It then runs `bootstrap --config config/full.yaml` on `fello_test_qa_full`,
   then `docker compose up` (db, web, worker) to check the end state: the list renders, a score drills down to its
   events and the worker writes a `job_runs` row with `trigger='schedule'` (use `NIGHTLY_CRON="*/2 * * * *"` in
   `.env`). It records counts and stage timings in `step-1-log.md`.
