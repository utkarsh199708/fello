"""Initial schema: the Step 1 tables, row-level security and grants.

Revision ID: 0001
Revises:
Create Date: 2026-10-16

Runs as app_owner, which therefore owns every table. The roles themselves are created before this
revision by app.db.prepare_database, because a BYPASSRLS role needs a superuser and the passwords
come from the environment.

Conventions (docs/decisions/step-1-design.md section 6):
- Every table except tenants and model_versions has ``tenant_id uuid NOT NULL REFERENCES tenants``
  and FORCE ROW LEVEL SECURITY with the one policy ``tenant_isolation``.
- All timestamps are timestamptz. Audit columns (loaded_at, built_at, created_at, started_at)
  default to now() and no logic reads them.
- A column is NOT NULL unless a NULL means something, and that meaning is in a comment.
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# In dependency order: a table only references tables above it.
CREATE_TABLES = [
    # The tenant directory. Global: the switcher and the nightly loop read it before any tenant
    # context exists, and it holds no customer data.
    """
    CREATE TABLE tenants (
        tenant_id             uuid PRIMARY KEY,
        slug                  text NOT NULL UNIQUE,
        name                  text NOT NULL,
        segment               text NOT NULL,
        top_k                 int NOT NULL CHECK (top_k > 0),
        contraction_threshold numeric NOT NULL CHECK (contraction_threshold BETWEEN 0 AND 1),
        connected_at          jsonb NOT NULL DEFAULT '{}'::jsonb  -- {source: ISO timestamp}
    )
    """,
    # The one global model, trained across tenants by app_batch. Global, so no RLS.
    """
    CREATE TABLE model_versions (
        model_version       text PRIMARY KEY,
        kind                text NOT NULL,
        feature_set_version text NOT NULL,
        features            jsonb NOT NULL,
        artifact            jsonb NOT NULL,
        train_as_of         timestamptz NOT NULL,
        label_cutoff        timestamptz NOT NULL,
        anchor_first        timestamptz NOT NULL,
        anchor_last         timestamptz NOT NULL,
        tenant_row_counts   jsonb NOT NULL,
        metrics             jsonb NOT NULL DEFAULT '{}'::jsonb,
        is_active           boolean NOT NULL DEFAULT false,
        created_at          timestamptz NOT NULL DEFAULT now()
    )
    """,
    # At most one active model: a second row with is_active = true violates this index.
    "CREATE UNIQUE INDEX model_versions_one_active ON model_versions (is_active) WHERE is_active",
    """
    CREATE TABLE csms (
        csm_id    uuid PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES tenants,
        name      text NOT NULL,
        email     text NOT NULL
    )
    """,
    "CREATE INDEX csms_tenant ON csms (tenant_id)",
    """
    CREATE TABLE accounts (
        account_id     uuid PRIMARY KEY,
        tenant_id      uuid NOT NULL REFERENCES tenants,
        name           text NOT NULL,
        website_domain text,                       -- NULL: the CRM has no website on file
        csm_id         uuid REFERENCES csms,       -- NULL: unassigned
        started_at     timestamptz NOT NULL
    )
    """,
    # Resolve matches an email domain to exactly one account of the tenant.
    "CREATE INDEX accounts_tenant_domain ON accounts (tenant_id, website_domain)",
    """
    CREATE TABLE contracts (
        contract_id  uuid PRIMARY KEY,
        tenant_id    uuid NOT NULL REFERENCES tenants,
        account_id   uuid NOT NULL REFERENCES accounts,
        cycle_number int NOT NULL CHECK (cycle_number >= 1),
        start_date   date NOT NULL,
        end_date     date NOT NULL,
        arr          numeric(14, 2) NOT NULL,
        UNIQUE (account_id, cycle_number),
        CHECK (end_date > start_date)
    )
    """,
    "CREATE INDEX contracts_tenant ON contracts (tenant_id)",
    # Native ids from each source mapped to our account. Stands in for a CRM sync in Step 1.
    """
    CREATE TABLE crosswalk (
        tenant_id   uuid NOT NULL REFERENCES tenants,
        source      text NOT NULL,
        external_id text NOT NULL,
        account_id  uuid NOT NULL REFERENCES accounts,
        PRIMARY KEY (tenant_id, source, external_id)
    )
    """,
    # Append-only facts exactly as received. No unique key: a duplicate delivery is itself a raw
    # fact, and dedup (Step 3) happens downstream.
    """
    CREATE TABLE raw_events (
        raw_event_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        tenant_id             uuid NOT NULL REFERENCES tenants,
        source                text NOT NULL,
        source_event_id       text NOT NULL,
        source_schema_version int NOT NULL,
        event_type            text NOT NULL,
        occurred_at           timestamptz NOT NULL,
        received_at           timestamptz NOT NULL,
        payload               jsonb NOT NULL,
        loaded_at             timestamptz NOT NULL DEFAULT now()
    )
    """,
    # Ingest reads "everything received up to as_of" per tenant.
    "CREATE INDEX raw_events_tenant_received ON raw_events (tenant_id, received_at)",
    # A raw event matched to an account. Exactly one of normalised_events or quarantine holds a
    # given raw event, so a rerun can find the unprocessed ones.
    """
    CREATE TABLE normalised_events (
        normalised_event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        tenant_id           uuid NOT NULL REFERENCES tenants,
        raw_event_id        bigint NOT NULL UNIQUE REFERENCES raw_events,
        account_id          uuid NOT NULL REFERENCES accounts,
        source              text NOT NULL,
        event_type          text NOT NULL,
        occurred_at         timestamptz NOT NULL,
        received_at         timestamptz NOT NULL,
        match_method        text NOT NULL CHECK (match_method IN ('crosswalk', 'email_domain')),
        attrs               jsonb NOT NULL DEFAULT '{}'::jsonb,
        resolved_as_of      timestamptz NOT NULL
    )
    """,
    # Features read one account's events in an occurred_at window.
    "CREATE INDEX normalised_events_tenant_account_occurred"
    " ON normalised_events (tenant_id, account_id, occurred_at)",
    # A raw event no account could be found for, with the reason.
    """
    CREATE TABLE quarantine (
        quarantine_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        tenant_id      uuid NOT NULL REFERENCES tenants,
        raw_event_id   bigint NOT NULL UNIQUE REFERENCES raw_events,
        source         text NOT NULL,
        reason         text NOT NULL CHECK (reason IN (
                           'unknown_native_key', 'unknown_domain', 'ambiguous_domain', 'no_identity'
                       )),
        detail         jsonb NOT NULL DEFAULT '{}'::jsonb,
        resolved_as_of timestamptz NOT NULL
    )
    """,
    "CREATE INDEX quarantine_tenant ON quarantine (tenant_id)",
    # Point-in-time feature vectors. The unique key makes a rebuild at the same as_of a no-op,
    # and no runtime role may UPDATE or DELETE, so a stored snapshot never changes.
    """
    CREATE TABLE snapshots (
        snapshot_id         uuid PRIMARY KEY,
        tenant_id           uuid NOT NULL REFERENCES tenants,
        account_id          uuid NOT NULL REFERENCES accounts,
        as_of               timestamptz NOT NULL,
        kind                text NOT NULL CHECK (kind IN ('anchor', 'nightly')),
        feature_set_version text NOT NULL,
        features            jsonb NOT NULL,
        ingest_watermark    timestamptz,           -- NULL: no event had been received by as_of
        built_at            timestamptz NOT NULL DEFAULT now(),
        UNIQUE (tenant_id, account_id, as_of, feature_set_version)
    )
    """,
    # One row per tenant per night per job. The unique key is what makes a rerun safe.
    """
    CREATE TABLE job_runs (
        run_id           uuid PRIMARY KEY,
        job_name         text NOT NULL,
        tenant_id        uuid NOT NULL REFERENCES tenants,
        run_date         date NOT NULL,
        as_of            timestamptz NOT NULL,
        trigger          text NOT NULL CHECK (
                             trigger IN ('schedule', 'cli', 'bootstrap', 'manual')
                         ),
        status           text NOT NULL CHECK (
                             status IN ('running', 'succeeded', 'failed', 'blocked', 'no_op')
                         ),
        model_version    text REFERENCES model_versions,  -- NULL: no model was used
        ingest_watermark timestamptz,                     -- NULL: nothing received yet
        counts           jsonb NOT NULL DEFAULT '{}'::jsonb,
        stage_ms         jsonb NOT NULL DEFAULT '{}'::jsonb,
        error            text,                            -- NULL: no error
        started_at       timestamptz NOT NULL DEFAULT now(),
        finished_at      timestamptz,                     -- NULL: still running
        UNIQUE (job_name, tenant_id, run_date)
    )
    """,
    # One score per account per as_of per model, with everything needed to explain it.
    """
    CREATE TABLE scores (
        score_id        uuid PRIMARY KEY,
        tenant_id       uuid NOT NULL REFERENCES tenants,
        account_id      uuid NOT NULL REFERENCES accounts,
        snapshot_id     uuid NOT NULL REFERENCES snapshots,
        model_version   text NOT NULL REFERENCES model_versions,
        as_of           timestamptz NOT NULL,
        csm_id          uuid REFERENCES csms,            -- NULL: unassigned, its own book
        probability     double precision NOT NULL CHECK (probability BETWEEN 0 AND 1),
        margin          double precision NOT NULL,
        expected_value  double precision NOT NULL,
        contributions   jsonb NOT NULL,
        arr             numeric(14, 2) NOT NULL,
        days_to_renewal int NOT NULL,
        rank_score      double precision NOT NULL,
        rank_in_book    int NOT NULL,
        k               int NOT NULL,
        evidence_state  text NOT NULL DEFAULT 'scored',
        run_id          uuid REFERENCES job_runs,        -- NULL: scored outside a job run
        created_at      timestamptz NOT NULL DEFAULT now(),
        UNIQUE (tenant_id, account_id, as_of, model_version)
    )
    """,
    # The list page reads one CSM's book at the latest as_of.
    "CREATE INDEX scores_tenant_csm_as_of ON scores (tenant_id, csm_id, as_of)",
    # Step 5 extends this table with a new revision.
    """
    CREATE TABLE tasks (
        task_id        uuid PRIMARY KEY,
        tenant_id      uuid NOT NULL REFERENCES tenants,
        account_id     uuid NOT NULL REFERENCES accounts,
        run_date       date NOT NULL,
        score_id       uuid NOT NULL REFERENCES scores,
        run_id         uuid NOT NULL REFERENCES job_runs,
        status         text NOT NULL DEFAULT 'open',
        rule_fired     text NOT NULL,
        policy_version text NOT NULL,
        created_at     timestamptz NOT NULL DEFAULT now(),
        updated_at     timestamptz NOT NULL DEFAULT now(),
        UNIQUE (tenant_id, account_id, run_date)
    )
    """,
]

# Every table with a tenant_id column. Only tenants and model_versions are left out.
TENANT_TABLES = (
    "csms",
    "accounts",
    "contracts",
    "crosswalk",
    "raw_events",
    "normalised_events",
    "quarantine",
    "snapshots",
    "job_runs",
    "scores",
    "tasks",
)

# NULLIF turns the empty string, which current_setting returns once a transaction-local setting
# has ended, into NULL. With no tenant context the comparison is NULL: no row is visible and no
# row may be written.
TENANT_PREDICATE = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"

# Neither runtime role gets DELETE or TRUNCATE anywhere, so stored snapshots and scores are
# immutable by privilege. UPDATE is granted only where a row has a lifecycle.
GRANTS = [
    "GRANT SELECT, INSERT ON tenants TO app",
    "GRANT SELECT ON model_versions TO app",
    "GRANT SELECT, INSERT ON csms, accounts, contracts, crosswalk, raw_events,"
    " normalised_events, quarantine, snapshots, scores TO app",
    "GRANT SELECT, INSERT, UPDATE ON job_runs, tasks TO app",
    # app_batch is the trainer: it reads every tenant and writes only model_versions.
    "GRANT SELECT ON tenants TO app_batch",
    "GRANT SELECT, INSERT, UPDATE ON model_versions TO app_batch",
    "GRANT SELECT ON " + ", ".join(TENANT_TABLES) + " TO app_batch",
]


def upgrade() -> None:
    for statement in CREATE_TABLES:
        op.execute(statement)
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # FORCE makes the policy bind the table owner (app_owner) as well.
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table}"
            f" USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})"
        )
    for statement in GRANTS:
        op.execute(statement)


def downgrade() -> None:
    # Reverse dependency order. Policies and grants go with their tables.
    for table in (
        "tasks",
        "scores",
        "job_runs",
        "snapshots",
        "quarantine",
        "normalised_events",
        "raw_events",
        "crosswalk",
        "contracts",
        "accounts",
        "csms",
        "model_versions",
        "tenants",
    ):
        op.execute(f"DROP TABLE {table}")
