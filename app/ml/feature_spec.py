"""What the features are, without computing any of them.

This module imports nothing heavy (no numpy, pandas or scikit-learn), so the web process can use
it to show which raw events fed a feature. ``app/ml/features.py`` computes the values.

Rules every feature follows (docs/decisions/step-1-design.md section 7):

- **Visibility.** An event counts only if ``received_at <= as_of``. What had not arrived by
  ``as_of`` did not exist at ``as_of``.
- **Windows** are on ``occurred_at`` and are half-open: ``(as_of - window, as_of]``.
- **Outcome events never count.** Anything in ``OUTCOME_EVENT_TYPES`` states how a renewal ended,
  so a feature built from it would leak the label. The filters below never list those types.
- **Not connected means NULL.** A feature that needs a source is NULL when the tenant had not
  connected that source by ``as_of``. "We could not see it" is different from "it was zero".
"""

from __future__ import annotations

from dataclasses import dataclass

from app.vocab import EVENT_TYPES, OUTCOME_EVENT_TYPES, SOURCES

# Bump when a feature's definition changes. Snapshots are keyed by it, so old rows stay valid
# for the model that was trained on them.
# fs2: the active contract (arr, cycle_number, days_to_renewal, and the population) moves to the
# next cycle only once that renewal's Salesforce event has arrived, not at 00:00 on its date.
# fs3: a renewal counts as decided once either Salesforce or Stripe has reported it (the
# two are reconciled into one outcome, task 3.1), so the cycle can move a few hours sooner.
# fs4: the full feature set of PLAN.md section 6 (task 4.1).
FEATURE_SET_VERSION = "fs4"

# A "days since" feature is capped here, and takes the cap when the event never happened. That
# keeps it defined (never NULL) for a connected source; NULL stays reserved for "not connected".
DAYS_SINCE_CAP = 365
MONTHS_SINCE_CAP = 12

# Order matters: it is the column order of the feature matrix and of the model's coefficients.
FEATURES = (
    # From the account and its active contract.
    "account_tenure_days",
    "arr",
    "days_to_renewal",
    "cycle_number",
    "prior_contraction",  # 1 if the active contract's ARR is below 70% of the previous one's
    # Coverage.
    "total_event_count",
    "events_90d",
    "n_sources_with_events_90d",
    # Zendesk.
    "tickets_30d",
    "tickets_90d",
    "ticket_rate_30d_vs_prior_90d",
    "open_tickets",
    "high_priority_30d",
    "mean_csat_90d",
    "days_since_last_ticket",
    # Product.
    "active_users_last_month",
    "sessions_last_month",
    "usage_change_last_month_vs_prior_3",
    "seat_utilisation",
    "months_since_last_activity",
    # Stripe.
    "failed_payments_90d",
    "unrecovered_failure_90d",
    "days_since_last_failure",
    # Gmail.
    "threads_90d",
    "outbound_unanswered_30d",
    "bounce_or_autoreply_90d",
    "days_since_last_inbound",
    # Salesforce.
    "missing_field_count",
    # Which sources the tenant had connected at as_of: 0 or 1, never NULL.
    "zendesk_connected",
    "product_connected",
    "stripe_connected",
    "gmail_connected",
)

# The source each source-specific feature needs. Such a feature is NULL when that source is not
# connected at as_of.
FEATURE_SOURCE = {
    "tickets_30d": "zendesk",
    "tickets_90d": "zendesk",
    "ticket_rate_30d_vs_prior_90d": "zendesk",
    "open_tickets": "zendesk",
    "high_priority_30d": "zendesk",
    "mean_csat_90d": "zendesk",
    "days_since_last_ticket": "zendesk",
    "active_users_last_month": "product",
    "sessions_last_month": "product",
    "usage_change_last_month_vs_prior_3": "product",
    "seat_utilisation": "product",
    "months_since_last_activity": "product",
    "failed_payments_90d": "stripe",
    "unrecovered_failure_90d": "stripe",
    "days_since_last_failure": "stripe",
    "threads_90d": "gmail",
    "outbound_unanswered_30d": "gmail",
    "bounce_or_autoreply_90d": "gmail",
    "days_since_last_inbound": "gmail",
}

# Features where NULL is normal even with the source connected: most accounts have no rated
# ticket in 90 days. The data-quality gate does not cap their NULL rate.
SPARSE_FEATURES = frozenset({"mean_csat_90d"})

CONNECTED_FLAGS = {
    f"{source}_connected": source for source in ("zendesk", "product", "stripe", "gmail")
}

# Window lengths in days, named so features.py and the filters below cannot disagree.
TICKET_WINDOW = 30
TICKET_PRIOR_WINDOW = 90  # the 90 days before the last 30
PAYMENT_WINDOW = 90
THREAD_WINDOW = 90
# Product reports arrive once a month, stamped on the first instant of the next month. "Last
# month" is the newest report if it is at most 45 days old (a month plus a late report); the
# three reports before it, within 125 days, are the baseline.
USAGE_RECENT_WINDOW = 45
USAGE_HISTORY_WINDOW = 125


@dataclass(frozen=True)
class EventFilter:
    """Which events fed a feature: these sources, these event types, within the window.

    ``window_days`` None means all history up to ``as_of``. Visibility (``received_at <= as_of``)
    always applies on top. ``event_types`` never contains an outcome type.
    """

    sources: tuple[str, ...]
    event_types: tuple[str, ...]
    window_days: int | None

    def matches(self, source: str, event_type: str) -> bool:
        return source in self.sources and event_type in self.event_types


# Every event type that may feed a feature: all of them except the outcome types.
NON_OUTCOME_TYPES = tuple(
    event_type
    for source in SOURCES
    for event_type in EVENT_TYPES[source]
    if (source, event_type) not in OUTCOME_EVENT_TYPES
)

# For each feature, the events behind it. None means the value comes from the account, contract
# or tenant row, not from events.
TICKET_TYPES = ("ticket_created", "ticket_updated")
STRIPE_INVOICE_TYPES = ("invoice.paid", "invoice.payment_failed")

FEATURE_EVENTS: dict[str, EventFilter | None] = {
    "account_tenure_days": None,
    "arr": None,
    "days_to_renewal": None,
    "cycle_number": None,
    "prior_contraction": None,
    "total_event_count": EventFilter(SOURCES, NON_OUTCOME_TYPES, None),
    "events_90d": EventFilter(SOURCES, NON_OUTCOME_TYPES, 90),
    "n_sources_with_events_90d": EventFilter(SOURCES, NON_OUTCOME_TYPES, 90),
    "tickets_30d": EventFilter(("zendesk",), ("ticket_created",), TICKET_WINDOW),
    "tickets_90d": EventFilter(("zendesk",), ("ticket_created",), 90),
    "ticket_rate_30d_vs_prior_90d": EventFilter(
        ("zendesk",), ("ticket_created",), TICKET_WINDOW + TICKET_PRIOR_WINDOW
    ),
    "open_tickets": EventFilter(("zendesk",), TICKET_TYPES, None),
    "high_priority_30d": EventFilter(("zendesk",), ("ticket_created",), TICKET_WINDOW),
    "mean_csat_90d": EventFilter(("zendesk",), TICKET_TYPES, 90),
    "days_since_last_ticket": EventFilter(("zendesk",), ("ticket_created",), DAYS_SINCE_CAP),
    "active_users_last_month": EventFilter(
        ("product",), ("usage_rollup_monthly",), USAGE_RECENT_WINDOW
    ),
    "sessions_last_month": EventFilter(
        ("product",), ("usage_rollup_monthly",), USAGE_RECENT_WINDOW
    ),
    "usage_change_last_month_vs_prior_3": EventFilter(
        ("product",), ("usage_rollup_monthly",), USAGE_HISTORY_WINDOW
    ),
    "seat_utilisation": EventFilter(("product",), ("usage_rollup_monthly",), USAGE_RECENT_WINDOW),
    "months_since_last_activity": EventFilter(
        ("product",), ("usage_rollup_monthly",), MONTHS_SINCE_CAP * 31
    ),
    "failed_payments_90d": EventFilter(("stripe",), ("invoice.payment_failed",), PAYMENT_WINDOW),
    "unrecovered_failure_90d": EventFilter(("stripe",), STRIPE_INVOICE_TYPES, PAYMENT_WINDOW),
    "days_since_last_failure": EventFilter(
        ("stripe",), ("invoice.payment_failed",), DAYS_SINCE_CAP
    ),
    "threads_90d": EventFilter(("gmail",), ("message",), THREAD_WINDOW),
    "outbound_unanswered_30d": EventFilter(("gmail",), ("message",), 30),
    "bounce_or_autoreply_90d": EventFilter(("gmail",), ("message",), 90),
    "days_since_last_inbound": EventFilter(("gmail",), ("message",), DAYS_SINCE_CAP),
    "missing_field_count": EventFilter(("salesforce",), ("account_upserted",), None),
    "zendesk_connected": None,
    "product_connected": None,
    "stripe_connected": None,
    "gmail_connected": None,
}

assert tuple(FEATURE_EVENTS) == FEATURES, "every feature needs an entry, in FEATURES order"
assert not any(
    (s, t) in OUTCOME_EVENT_TYPES
    for f in FEATURE_EVENTS.values()
    if f is not None
    for s in f.sources
    for t in f.event_types
), "no feature may read an outcome event"
