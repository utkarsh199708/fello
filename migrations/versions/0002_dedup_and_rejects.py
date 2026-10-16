"""Step 3: one normalised row per source event, and a rejects table.

- ``normalised_events.source_event_id`` with a unique index on (tenant_id, source,
  source_event_id). Resolve drops exact replays (duplicate kind 1), and the index makes a
  duplicate impossible even if that logic had a bug: the night would fail loudly instead.
- An index on raw_events for the "was this event id delivered before?" lookup.
- ``rejects``: a raw event that is a replay, or that no schema version can parse. With
  normalised_events and quarantine it makes three outcomes; every raw event ends in exactly one,
  which is how a rerun finds the unprocessed ones.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TENANT_PREDICATE = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"

REJECT_REASONS = (
    "duplicate_event_id",  # the same source_event_id was delivered earlier
    "unknown_event_type",  # the source has no such event type
    "unknown_schema_version",  # no dispatcher for this source_schema_version
    "unparseable_payload",  # a required field is missing or has the wrong type
)


def upgrade() -> None:
    op.execute("ALTER TABLE normalised_events ADD COLUMN source_event_id text")
    # Backfill rows written before this revision. FORCE row-level security binds the owner too,
    # and a migration has no tenant context, so it is lifted for the one statement.
    op.execute("ALTER TABLE normalised_events NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE raw_events NO FORCE ROW LEVEL SECURITY")
    op.execute(
        "UPDATE normalised_events n SET source_event_id = r.source_event_id"
        " FROM raw_events r WHERE r.raw_event_id = n.raw_event_id"
    )
    op.execute("ALTER TABLE raw_events FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE normalised_events FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE normalised_events ALTER COLUMN source_event_id SET NOT NULL")
    op.execute(
        "CREATE UNIQUE INDEX normalised_events_one_per_source_event"
        " ON normalised_events (tenant_id, source, source_event_id)"
    )
    op.execute(
        "CREATE INDEX raw_events_tenant_source_event"
        " ON raw_events (tenant_id, source, source_event_id)"
    )

    reasons = ", ".join(f"'{r}'" for r in REJECT_REASONS)
    op.execute(
        f"""
        CREATE TABLE rejects (
            reject_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            tenant_id      uuid NOT NULL REFERENCES tenants,
            raw_event_id   bigint NOT NULL UNIQUE REFERENCES raw_events,
            source         text NOT NULL,
            reason         text NOT NULL CHECK (reason IN ({reasons})),
            detail         jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            resolved_as_of timestamptz NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX rejects_tenant ON rejects (tenant_id)")
    op.execute("ALTER TABLE rejects ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE rejects FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON rejects"
        f" USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})"
    )
    op.execute("GRANT SELECT, INSERT ON rejects TO app")
    op.execute("GRANT SELECT ON rejects TO app_batch")


def downgrade() -> None:
    op.execute("DROP TABLE rejects")
    op.execute("DROP INDEX raw_events_tenant_source_event")
    op.execute("DROP INDEX normalised_events_one_per_source_event")
    op.execute("ALTER TABLE normalised_events DROP COLUMN source_event_id")
