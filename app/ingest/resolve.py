"""Resolve step: turn raw events into account-level events, or quarantine them with a reason.

Raw events never carry our account id, only each source's own key (docs/PLAN.md section 5). For
every raw event received by ``as_of`` that has not been handled yet:

1. **Crosswalk.** Look up ``payload[NATIVE_KEY[source]]`` in the tenant's crosswalk.
2. **Email domain.** Failing that, take the domain of ``payload[EMAIL_FIELD[source]]`` and match
   it against ``accounts.website_domain``. Exactly one account must own the domain.
3. **Quarantine.** Otherwise the event goes to ``quarantine`` with the reason, so the mess is
   counted and never silently dropped.

Each normalised row records which rule matched (``match_method``) and points back to its raw row.
A raw event ends up in exactly one of ``normalised_events`` and ``quarantine``, which is how a
rerun finds the unprocessed ones, and why running twice with the same ``as_of`` changes nothing.

Functions here take ``conn`` and never open or commit a transaction: the caller wraps them in
``tenant_tx`` (design section 1).
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, NamedTuple
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.db import copy_insert
from app.vocab import EMAIL_FIELD, NATIVE_KEY

CHUNK_SIZE = 10_000  # raw rows fetched, resolved and written per round trip

NORMALISED_COLUMNS = (
    "tenant_id",
    "raw_event_id",
    "account_id",
    "source",
    "event_type",
    "occurred_at",
    "received_at",
    "match_method",
    "attrs",
    "resolved_as_of",
)
QUARANTINE_COLUMNS = ("tenant_id", "raw_event_id", "source", "reason", "detail", "resolved_as_of")

# Payload fields that only identify the customer. The account_id column replaces them, so they
# are not copied into attrs. Everything else in the payload is kept.
IDENTITY_FIELDS = frozenset(NATIVE_KEY.values()) | frozenset(EMAIL_FIELD.values())


@dataclass(frozen=True)
class ResolveCounts:
    normalised: int
    quarantined: int
    by_method: dict[str, int]  # {"crosswalk": n, "email_domain": n}
    by_reason: dict[str, int] = field(default_factory=dict)  # quarantine reason -> n


class Decision(NamedTuple):
    """Either (account_id, match_method) is set, or (reason, detail) is, never both."""

    account_id: UUID | None
    match_method: str | None
    reason: str | None
    detail: dict[str, Any]


def email_domain(address: Any) -> str | None:
    """The lower-cased part after the last '@', or None when there is no usable address."""
    if not isinstance(address, str) or "@" not in address:
        return None
    domain = address.rpartition("@")[2].strip().lower()
    return domain or None


def decide(
    source: str,
    payload: dict[str, Any],
    crosswalk: dict[tuple[str, str], UUID],
    domains: dict[str, list[UUID]],
) -> Decision:
    """Pick the account for one event. Pure: all lookups are passed in, nothing is read here.

    ``crosswalk`` maps (source, external_id) to account_id. ``domains`` maps a lower-cased
    website domain to every account that owns it.

    When nothing matches, the reason names the most telling failure: a domain shared by several
    accounts first (``ambiguous_domain``), then a native key we did not know
    (``unknown_native_key``), then an email whose domain we did not know (``unknown_domain``),
    and ``no_identity`` when the event carried neither a key nor an email.
    """
    key_field = NATIVE_KEY.get(source)
    raw_key = payload.get(key_field) if key_field else None
    key = str(raw_key) if raw_key not in (None, "") else None
    if key is not None and (source, key) in crosswalk:
        return Decision(crosswalk[(source, key)], "crosswalk", None, {})

    email_field = EMAIL_FIELD.get(source)
    domain = email_domain(payload.get(email_field)) if email_field else None
    detail: dict[str, Any] = {}
    if key is not None:
        detail["native_key"] = key
    if domain is not None:
        detail["domain"] = domain
        owners = domains.get(domain, [])
        if len(owners) == 1:
            return Decision(owners[0], "email_domain", None, {})
        if len(owners) > 1:
            detail["accounts"] = len(owners)
            return Decision(None, None, "ambiguous_domain", detail)

    if key is not None:
        return Decision(None, None, "unknown_native_key", detail)
    if domain is not None:
        return Decision(None, None, "unknown_domain", detail)
    return Decision(None, None, "no_identity", detail)


def _load_lookups(
    conn: Connection, tenant_id: UUID
) -> tuple[dict[tuple[str, str], UUID], dict[str, list[UUID]]]:
    """Read the tenant's crosswalk and account domains once. Both are small."""
    params = {"tid": tenant_id}
    crosswalk = {
        (r.source, r.external_id): r.account_id
        for r in conn.execute(
            sa.text("SELECT source, external_id, account_id FROM crosswalk WHERE tenant_id = :tid"),
            params,
        )
    }
    domains: dict[str, list[UUID]] = {}
    for r in conn.execute(
        sa.text(
            "SELECT account_id, lower(website_domain) AS domain FROM accounts"
            " WHERE tenant_id = :tid AND website_domain IS NOT NULL ORDER BY account_id"
        ),
        params,
    ):
        domains.setdefault(r.domain, []).append(r.account_id)
    return crosswalk, domains


# Raw events received by :as_of that are neither normalised nor quarantined, oldest id first.
# Keyset paging on raw_event_id (not OFFSET) stays correct while rows are being written.
UNPROCESSED_SQL = sa.text(
    """
    SELECT r.raw_event_id, r.source, r.event_type, r.occurred_at, r.received_at, r.payload
    FROM raw_events r
    WHERE r.tenant_id = :tid
      AND r.received_at <= :as_of
      AND r.raw_event_id > :after
      AND NOT EXISTS (SELECT 1 FROM normalised_events n WHERE n.raw_event_id = r.raw_event_id)
      AND NOT EXISTS (SELECT 1 FROM quarantine q WHERE q.raw_event_id = r.raw_event_id)
    ORDER BY r.raw_event_id
    LIMIT :limit
    """
)


def resolve(
    conn: Connection, tenant_id: UUID | str, as_of: datetime, chunk_size: int = CHUNK_SIZE
) -> ResolveCounts:
    """Resolve every unhandled raw event with ``received_at <= as_of``.

    An event that arrives after ``as_of`` is invisible here: it is picked up by a later call with
    a later ``as_of``. That is the point-in-time rule, applied at the door.
    """
    tid = UUID(str(tenant_id))
    crosswalk, domains = _load_lookups(conn, tid)
    by_method: Counter[str] = Counter()
    by_reason: Counter[str] = Counter()
    after = 0
    while True:
        chunk = conn.execute(
            UNPROCESSED_SQL, {"tid": tid, "as_of": as_of, "after": after, "limit": chunk_size}
        ).all()
        if not chunk:
            break
        after = chunk[-1].raw_event_id
        normalised, quarantined = [], []
        for r in chunk:
            d = decide(r.source, r.payload, crosswalk, domains)
            if d.account_id is not None:
                attrs = {k: v for k, v in r.payload.items() if k not in IDENTITY_FIELDS}
                normalised.append(
                    (
                        tid,
                        r.raw_event_id,
                        d.account_id,
                        r.source,
                        r.event_type,
                        r.occurred_at,
                        r.received_at,
                        d.match_method,
                        json.dumps(attrs, separators=(",", ":")),
                        as_of,
                    )
                )
                by_method[d.match_method] += 1
            else:
                quarantined.append(
                    (tid, r.raw_event_id, r.source, d.reason, json.dumps(d.detail), as_of)
                )
                by_reason[d.reason] += 1
        copy_insert(conn, "normalised_events", NORMALISED_COLUMNS, normalised)
        copy_insert(conn, "quarantine", QUARANTINE_COLUMNS, quarantined)
    return ResolveCounts(
        normalised=sum(by_method.values()),
        quarantined=sum(by_reason.values()),
        by_method=dict(by_method),
        by_reason=dict(by_reason),
    )


def ingest_watermark(conn: Connection, as_of: datetime) -> datetime | None:
    """The newest ``received_at`` among the tenant's normalised events received by ``as_of``.

    Stored on snapshots and job runs to say "this is everything we had seen". The tenant comes
    from the transaction's context: row-level security hides every other tenant's rows.
    """
    return conn.execute(
        sa.text("SELECT max(received_at) FROM normalised_events WHERE received_at <= :as_of"),
        {"as_of": as_of},
    ).scalar_one()
