"""Step 4: two linked model versions, and the full score row.

- ``model_versions.role``: ``evaluation`` (fit on the training cycles, scored on the holdout; its
  metrics are what the README reports) or ``live`` (refit on every observed cycle with the same
  settings, and the one that scores). ``validated_by`` links a live version to the evaluation
  version it was checked with. Only a live version may be active.
- ``scores.colour``: red, yellow, green, or grey for not enough signal. It is derived from the
  rank in the CSM's book, a display rather than a model (docs/brief.md).
- ``scores.coverage``: how much the score rests on (events, sources seen in 90 days, tenure).
- ``scores.rank_in_book`` may be NULL: an account with not enough signal is listed separately
  and takes no place in the ranking.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE model_versions ADD COLUMN role text NOT NULL DEFAULT 'live'"
        " CHECK (role IN ('evaluation', 'live'))"
    )
    op.execute("ALTER TABLE model_versions ADD COLUMN validated_by text REFERENCES model_versions")
    op.execute(
        "ALTER TABLE model_versions ADD CONSTRAINT model_versions_only_live_active"
        " CHECK (NOT is_active OR role = 'live')"
    )
    op.execute(
        "ALTER TABLE scores ADD COLUMN colour text NOT NULL DEFAULT 'green'"
        " CHECK (colour IN ('red', 'yellow', 'green', 'grey'))"
    )
    op.execute("ALTER TABLE scores ADD COLUMN coverage jsonb NOT NULL DEFAULT '{}'::jsonb")
    op.execute("ALTER TABLE scores ALTER COLUMN rank_in_book DROP NOT NULL")
    op.execute(
        "ALTER TABLE scores ADD CONSTRAINT scores_evidence_state"
        " CHECK (evidence_state IN ('scored', 'insufficient'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE scores DROP CONSTRAINT scores_evidence_state")
    op.execute("ALTER TABLE scores ALTER COLUMN rank_in_book SET NOT NULL")
    op.execute("ALTER TABLE scores DROP COLUMN coverage")
    op.execute("ALTER TABLE scores DROP COLUMN colour")
    op.execute("ALTER TABLE model_versions DROP CONSTRAINT model_versions_only_live_active")
    op.execute("ALTER TABLE model_versions DROP COLUMN validated_by")
    op.execute("ALTER TABLE model_versions DROP COLUMN role")
