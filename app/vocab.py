"""Data definitions shared by the generator, the loader and the featurizer.

Data only, no behaviour, and no heavy imports. The generator writes exactly these fields and
the pipeline reads exactly these fields, so a change here is a change to the contract between
them (docs/decisions/step-1-design.md section 4).
"""

from __future__ import annotations

SOURCES = ("salesforce", "zendesk", "product", "stripe", "gmail")

# The payload field that holds each source's own key for the customer. Gmail has none:
# its mail is matched by the domain of the counterpart address instead.
NATIVE_KEY = {
    "salesforce": "account_id",
    "zendesk": "organization_id",
    "product": "workspace_id",
    "stripe": "customer",
}

# Payload fields that carry an email address, used for the domain fallback in resolve.
EMAIL_FIELD = {"zendesk": "requester_email", "gmail": "counterpart_email"}

# Event types per source, with the payload fields each type must carry.
EVENT_TYPES = {
    "salesforce": {
        # Also carries the ARR custom field, whose name depends on the schema version
        # (SALESFORCE_ARR_FIELD). industry and that field are null when the CRM has no value.
        "account_upserted": (
            "account_id",
            "name",
            "website_domain",
            "owner_email",
            "industry",
            "last_modified_date",
        ),
        "opportunity_stage_changed": (
            "account_id",
            "opportunity_id",
            "stage",  # closed_won | closed_lost
            "renewal_date",  # YYYY-MM-DD, equals the contract end_date
            "prior_arr",
            "renewed_arr",
        ),
    },
    "zendesk": {
        # A ticket is a mutable object: every change is a full snapshot of its state, and the
        # newest updated_at wins. A connector re-sync can re-send an unchanged snapshot.
        "ticket_created": (
            "organization_id",
            "ticket_id",
            "requester_email",
            "status",
            "priority",  # see ZENDESK_PRIORITY_CODES for how it changes type
            "csat",  # null until the ticket is rated
            "updated_at",
        ),
        "ticket_updated": (
            "organization_id",
            "ticket_id",
            "requester_email",
            "status",
            "priority",
            "csat",
            "updated_at",
        ),
    },
    "product": {
        # occurred_at is the first instant of the month after `month`.
        "usage_rollup_monthly": (
            "workspace_id",
            "month",
            "active_users",
            "sessions",
            "key_actions",
            "seats_used",
            "seats_purchased",
        ),
    },
    "stripe": {
        # Mid-term billing only. The renewal itself is the subscription events below.
        "invoice.paid": ("customer", "invoice_id", "amount_usd", "attempt"),
        "invoice.payment_failed": ("customer", "invoice_id", "amount_usd", "attempt"),
        "subscription.updated": ("customer", "subscription_id", "arr_usd"),
        "subscription.deleted": ("customer", "subscription_id", "arr_usd"),
    },
    "gmail": {
        "message": (
            "thread_id",
            "direction",  # inbound | outbound
            "counterpart_email",
            "bounce_or_autoreply",
        ),
    },
}

# Schema drift (PLAN.md section 5). Every raw row carries source_schema_version, and these are
# the differences between versions. A source not listed has only version 1.
LATEST_SCHEMA_VERSION = {"salesforce": 2, "zendesk": 2, "product": 1, "stripe": 1, "gmail": 1}
# Zendesk version 1 sent priority as an integer code; version 2 sends the name.
ZENDESK_PRIORITY_CODES = {"low": 1, "normal": 2, "high": 3, "urgent": 4}
# Salesforce renamed the ARR custom field on the account record.
SALESFORCE_ARR_FIELD = {1: "arr__c", 2: "annual_contract_value__c"}

# Payload fields whose name depends on the schema version, on top of EVENT_TYPES.
VERSIONED_FIELDS = {
    ("salesforce", "account_upserted"): {v: (f,) for v, f in SALESFORCE_ARR_FIELD.items()},
}


def payload_fields(source: str, event_type: str, version: int) -> tuple[str, ...]:
    """Every field a payload of this type and schema version carries."""
    extra = VERSIONED_FIELDS.get((source, event_type), {}).get(version, ())
    return EVENT_TYPES[source][event_type] + extra


# Events that state how a renewal ended. The labeler reads them; the featurizer drops them
# before computing anything, so a feature can never see the answer.
OUTCOME_EVENT_TYPES = frozenset(
    {
        ("salesforce", "opportunity_stage_changed"),
        ("stripe", "subscription.updated"),
        ("stripe", "subscription.deleted"),
    }
)
