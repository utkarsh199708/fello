"""Optional LLM narration of a briefing, and the check every narration must pass.

- ``narrations``: at most one per briefing. ``input`` is exactly what was sent to the model (the
  briefing's reason list and three numbers, no names or emails). ``status`` is ``ok`` (the text
  passed the check and is shown), ``rejected`` (it named a number that is not in the input, or was
  empty or too long; ``detail`` says which) or ``error`` (the call failed). Append-only, like
  everything the job writes.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

TENANT_PREDICATE = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE narrations (
            narration_id   uuid PRIMARY KEY,
            tenant_id      uuid NOT NULL REFERENCES tenants,
            briefing_id    uuid NOT NULL REFERENCES briefings,
            run_id         uuid NOT NULL REFERENCES job_runs,
            model          text NOT NULL,
            prompt_version text NOT NULL,
            input          jsonb NOT NULL,               -- exactly what the model was sent
            status         text NOT NULL CHECK (status IN ('ok', 'rejected', 'error')),
            text           text,                         -- NULL unless status is ok
            detail         text,                         -- why it was rejected or failed
            created_at     timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, briefing_id)
        )
        """
    )
    op.execute("CREATE INDEX narrations_tenant_run ON narrations (tenant_id, run_id)")
    op.execute("ALTER TABLE narrations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE narrations FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON narrations"
        f" USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})"
    )
    op.execute("GRANT SELECT, INSERT ON narrations TO app")
    op.execute("GRANT SELECT ON narrations TO app_batch")


def downgrade() -> None:
    op.execute("DROP TABLE narrations")
