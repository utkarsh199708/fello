"""Step 5: the agentic job's lineage. Transitions, a task lifecycle, briefings and an audit trail.

- ``transitions``: one row per scored account per night, comparing tonight's rank with the
  previous completed night's (from and to score ids and ranks). It is how "went red" is traced.
- ``tasks`` gains its lifecycle. Status is open, snoozed, done, dismissed or escalated. A task
  belongs to a CSM's queue, or to the CS-lead queue when the account has no owner. It records
  the probability it was raised at, the reasons appended to it night by night, and links to the
  transition and briefing behind it.
- ``briefings``: the deterministic reason list behind a task, and the text rendered from it.
- ``task_events``: every lifecycle change and every policy decision that wrote no task (a
  budget breach, a rebaseline). Append-only, like everything the job writes.
- ``job_runs.policy_version``: the policy that made the night's decisions.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

TENANT_PREDICATE = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
NEW_TABLES = ("transitions", "briefings", "task_events")


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE transitions (
            transition_id  uuid PRIMARY KEY,
            tenant_id      uuid NOT NULL REFERENCES tenants,
            account_id     uuid NOT NULL REFERENCES accounts,
            run_id         uuid NOT NULL REFERENCES job_runs,
            run_date       date NOT NULL,
            from_score_id  uuid REFERENCES scores,       -- NULL: not scored on the previous night
            to_score_id    uuid NOT NULL REFERENCES scores,
            from_rank      int,
            to_rank        int,                          -- NULL: not enough signal tonight
            k              int NOT NULL,
            entered_top_k  boolean NOT NULL,
            left_top_k     boolean NOT NULL,
            policy_version text NOT NULL,
            created_at     timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, account_id, run_id)
        )
        """
    )
    op.execute("CREATE INDEX transitions_tenant_run ON transitions (tenant_id, run_id)")
    op.execute(
        """
        CREATE TABLE briefings (
            briefing_id    uuid PRIMARY KEY,
            tenant_id      uuid NOT NULL REFERENCES tenants,
            account_id     uuid NOT NULL REFERENCES accounts,
            score_id       uuid NOT NULL REFERENCES scores,
            run_id         uuid NOT NULL REFERENCES job_runs,
            reasons        jsonb NOT NULL,               -- the deterministic reason list
            text           text NOT NULL,                -- rendered from reasons by a template
            template       text NOT NULL,                -- which template, and its version
            created_at     timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, score_id)
        )
        """
    )
    for statement in (
        "ALTER TABLE tasks ADD COLUMN csm_id uuid REFERENCES csms",  # NULL: CS-lead queue
        "ALTER TABLE tasks ADD COLUMN queue text NOT NULL DEFAULT 'csm'"
        " CHECK (queue IN ('csm', 'cs_lead'))",
        "ALTER TABLE tasks ADD COLUMN owner_missing boolean NOT NULL DEFAULT false",
        "ALTER TABLE tasks ADD COLUMN transition_id uuid REFERENCES transitions",
        "ALTER TABLE tasks ADD COLUMN briefing_id uuid REFERENCES briefings",
        "ALTER TABLE tasks ADD COLUMN probability double precision",
        "ALTER TABLE tasks ADD COLUMN reasons jsonb NOT NULL DEFAULT '[]'::jsonb",
        "ALTER TABLE tasks ADD COLUMN snoozed_until date",
        "ALTER TABLE tasks ADD COLUMN dismissed_reason text",
        "ALTER TABLE tasks ADD COLUMN dismissed_probability double precision",
        "ALTER TABLE tasks ADD COLUMN escalated_at timestamptz",
        "ALTER TABLE tasks ADD CONSTRAINT tasks_status CHECK"
        " (status IN ('open', 'snoozed', 'done', 'dismissed', 'escalated'))",
        "ALTER TABLE job_runs ADD COLUMN policy_version text",
    ):
        op.execute(statement)
    op.execute(
        """
        CREATE TABLE task_events (
            task_event_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            tenant_id      uuid NOT NULL REFERENCES tenants,
            task_id        uuid REFERENCES tasks,        -- NULL: a decision that wrote no task
            run_id         uuid REFERENCES job_runs,     -- NULL: a CSM's action in the web app
            kind           text NOT NULL CHECK (kind IN (
                               'created', 'reasons_appended', 'escalated', 'snooze_ended',
                               'status_changed', 'budget_exceeded', 'rebaselined'
                           )),
            detail         jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at     timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX task_events_tenant_task ON task_events (tenant_id, task_id)")
    for table in NEW_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table}"
            f" USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})"
        )
        op.execute(f"GRANT SELECT, INSERT ON {table} TO app")
        op.execute(f"GRANT SELECT ON {table} TO app_batch")


def downgrade() -> None:
    op.execute("DROP TABLE task_events")
    op.execute("ALTER TABLE job_runs DROP COLUMN policy_version")
    op.execute("ALTER TABLE tasks DROP CONSTRAINT tasks_status")
    for column in (
        "escalated_at",
        "dismissed_probability",
        "dismissed_reason",
        "snoozed_until",
        "reasons",
        "probability",
        "briefing_id",
        "transition_id",
        "owner_missing",
        "queue",
        "csm_id",
    ):
        op.execute(f"ALTER TABLE tasks DROP COLUMN {column}")
    op.execute("DROP TABLE briefings")
    op.execute("DROP TABLE transitions")
