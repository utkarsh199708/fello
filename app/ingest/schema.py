"""Schema-version dispatch: turn every payload version into one canonical shape (PLAN.md section 5).

Sources change their payload formats over time, and every raw row says which format it used in
``source_schema_version``. ``canonical_payload`` maps each known version to the latest shape, so
code downstream never branches on versions:

- Zendesk v1 sent ``priority`` as an integer code; v2 sends its name. Canonical: the name.
- Salesforce v1 held ARR in ``arr__c``, v2 in ``annual_contract_value__c``. Canonical: ``arr``.

Only the fields that drift are checked. A field that is simply absent is left to the step that
needs it (an event with no customer key reaches quarantine as ``no_identity``, not here).

A payload no dispatcher can read raises ``SchemaError`` with a reason. Resolve writes such rows to
``rejects``, so a format change nobody planned for is counted, never silently mis-read.

Pure, and no heavy imports.
"""

from __future__ import annotations

from typing import Any

from app.vocab import (
    EVENT_TYPES,
    LATEST_SCHEMA_VERSION,
    SALESFORCE_ARR_FIELD,
    ZENDESK_PRIORITY_CODES,
)

PRIORITY_NAMES = {code: name for name, code in ZENDESK_PRIORITY_CODES.items()}


class SchemaError(ValueError):
    """A payload that cannot be read. ``reason`` is one of the rejects reasons."""

    def __init__(self, reason: str, detail: dict[str, Any]):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def known_versions(source: str) -> range:
    return range(1, LATEST_SCHEMA_VERSION[source] + 1)


def canonical_payload(
    source: str, event_type: str, version: int, payload: dict[str, Any]
) -> dict[str, Any]:
    """The payload in the latest shape. Raises ``SchemaError`` when it cannot be read."""
    if source not in EVENT_TYPES or event_type not in EVENT_TYPES[source]:
        raise SchemaError("unknown_event_type", {"event_type": event_type})
    if version not in known_versions(source):
        raise SchemaError("unknown_schema_version", {"version": version})
    out = dict(payload)
    if source == "zendesk" and "priority" in out:
        priority = out["priority"]
        if version == 1:
            # bool is an int subclass in Python; True must not read as priority 1.
            if isinstance(priority, bool) or priority not in PRIORITY_NAMES:
                raise SchemaError("unparseable_payload", {"priority": priority, "version": 1})
            out["priority"] = PRIORITY_NAMES[priority]
        elif priority not in ZENDESK_PRIORITY_CODES:
            raise SchemaError("unparseable_payload", {"priority": priority, "version": version})
    if (source, event_type) == ("salesforce", "account_upserted"):
        others = [f for v, f in SALESFORCE_ARR_FIELD.items() if v != version and f in out]
        if others:  # the row says one version but carries another version's field
            raise SchemaError("unparseable_payload", {"field": others[0], "version": version})
        arr = out.pop(SALESFORCE_ARR_FIELD[version], None)
        if arr is not None and (isinstance(arr, bool) or not isinstance(arr, int | float)):
            raise SchemaError("unparseable_payload", {"arr": arr, "version": version})
        out["arr"] = arr  # None when the CRM has no ARR on file: NULL, not zero
    return out
