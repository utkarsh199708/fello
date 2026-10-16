"""Deterministic ids.

Every row the pipeline creates gets an id derived from what the row is, not from a random
draw or the clock. Running the same step twice therefore produces the same keys, which is
what lets reruns use `ON CONFLICT DO NOTHING` (docs/decisions/step-1-design.md section 1).
"""

from __future__ import annotations

import uuid

# Fixed forever. Changing it would change every id in every database.
NAMESPACE = uuid.UUID("5f0c6f0e-3b5e-4c3e-9a57-0f31e1c0a7d2")


def stable_uuid(*parts: object) -> uuid.UUID:
    """uuid5 over the parts joined with '|'. The same parts always give the same UUID."""
    return uuid.uuid5(NAMESPACE, "|".join(str(p) for p in parts))
