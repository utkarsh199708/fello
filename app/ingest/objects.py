"""Current state of mutable records, point in time (task 3.1, duplicate kind 2).

A Zendesk ticket is a mutable object. Every change arrives as a full snapshot, and a connector
re-sync can re-send an unchanged snapshot with a newer ``updated_at``. Neither is a new ticket.
The state of a ticket at ``as_of`` is its snapshot with the newest ``updated_at`` among those
received by ``as_of``. Arrival order does not matter: an older snapshot that arrives late never
overwrites a newer one.

Keyed by (account_id, ticket_id). Both timestamps are kept: ``updated_at`` orders the versions,
``received_at`` decides what was visible.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

TICKET_TYPES = ("ticket_created", "ticket_updated")

TICKET_STATES_SQL = sa.text(
    """
    SELECT DISTINCT ON (account_id, (attrs->>'ticket_id'))
           account_id, (attrs->>'ticket_id')::bigint AS ticket_id,
           attrs->>'status' AS status, attrs->>'priority' AS priority,
           (attrs->>'csat')::int AS csat,
           (attrs->>'updated_at')::timestamptz AS updated_at, received_at
    FROM normalised_events
    WHERE source = 'zendesk' AND event_type IN ('ticket_created', 'ticket_updated')
      AND received_at <= :as_of
    ORDER BY account_id, (attrs->>'ticket_id'),
             (attrs->>'updated_at')::timestamptz DESC NULLS LAST, received_at DESC
    """
)


def ticket_states(conn: Connection, as_of: datetime) -> list[dict[str, Any]]:
    """One row per ticket of the tenant in context: its state as known at ``as_of``."""
    return [dict(r._mapping) for r in conn.execute(TICKET_STATES_SQL, {"as_of": as_of})]
