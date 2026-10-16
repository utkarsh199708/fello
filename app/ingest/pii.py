"""Pseudonymise email addresses on the way in (PLAN.md section 5, task 3.4).

Email addresses in Gmail and Zendesk metadata are personal data even without message bodies. At
load time the local part of every address in ``PII_EMAIL_FIELDS`` is replaced by an HMAC-SHA256
of it, keyed per tenant, and the domain stays in clear:

    jordan.rivera@t1-17.example  ->  3f9c0d...e1@t1-17.example

- The same person always hashes to the same value within a tenant, so the hash still works as a
  join key, for example to count distinct requesters.
- The domain is what resolve matches accounts on, so identity resolution is unchanged.
- The key differs per tenant, so one tenant's hashes cannot be joined to another's.

The per-tenant key is derived from ``PII_HASH_SECRET``, which lives only in the environment
(Railway or VM variables, ``.env`` locally) and never in the database or the repo. Without it,
loading refuses to run rather than store addresses in clear.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any
from uuid import UUID

# Every payload field that can hold a personal email address.
PII_EMAIL_FIELDS = ("requester_email", "counterpart_email", "owner_email")
HASH_CHARS = 20  # 80 bits of the HMAC: collision-free at this scale, short enough to read
SECRET_ENV = "PII_HASH_SECRET"


def tenant_key(tenant_id: UUID | str, secret: str | None = None) -> bytes:
    """The tenant's HMAC key: HMAC-SHA256(secret, tenant_id)."""
    secret = os.environ.get(SECRET_ENV, "") if secret is None else secret
    if not secret:
        raise RuntimeError(
            f"{SECRET_ENV} is not set: refusing to load email addresses in clear."
            " Set it in .env (local) or the deployment's variables."
        )
    return hmac.new(secret.encode(), str(UUID(str(tenant_id))).encode(), hashlib.sha256).digest()


def hash_email(address: Any, key: bytes) -> Any:
    """``<hmac of the local part>@<domain>``. Anything that is not an address is returned as is."""
    if not isinstance(address, str) or "@" not in address:
        return address
    local, _, domain = address.strip().rpartition("@")
    digest = hmac.new(key, local.lower().encode(), hashlib.sha256).hexdigest()[:HASH_CHARS]
    return f"{digest}@{domain.lower()}"


def scrub(payload: dict[str, Any], key: bytes) -> dict[str, Any]:
    """A copy of ``payload`` with every address in ``PII_EMAIL_FIELDS`` pseudonymised."""
    out = dict(payload)
    for name in PII_EMAIL_FIELDS:
        if name in out:
            out[name] = hash_email(out[name], key)
    return out
