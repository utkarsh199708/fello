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
        "account_upserted": ("account_id", "name", "website_domain", "owner_email"),
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
        "ticket_created": (
            "organization_id",
            "ticket_id",
            "requester_email",
            "status",
            "priority",
            "csat",  # null until the ticket is rated
        ),
        "ticket_updated": (
            "organization_id",
            "ticket_id",
            "requester_email",
            "status",
            "priority",
            "csat",
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

# Events that state how a renewal ended. The labeler reads them; the featurizer drops them
# before computing anything, so a feature can never see the answer.
OUTCOME_EVENT_TYPES = frozenset(
    {
        ("salesforce", "opportunity_stage_changed"),
        ("stripe", "subscription.updated"),
        ("stripe", "subscription.deleted"),
    }
)
