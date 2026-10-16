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
FEATURE_SET_VERSION = "fs2"

# Order matters: it is the column order of the feature matrix and of the model's coefficients.
FEATURES = (
    # From the account and its active contract.
    "account_tenure_days",
    "arr",
    "days_to_renewal",
    "cycle_number",
    # Coverage.
    "total_event_count",
    # Zendesk.
    "tickets_30d",
    "ticket_rate_30d_vs_prior_90d",
    # Product.
    "active_users_last_month",
    "usage_change_last_month_vs_prior_3",
    # Stripe.
    "failed_payments_90d",
    # Gmail.
    "threads_90d",
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
    "ticket_rate_30d_vs_prior_90d": "zendesk",
    "active_users_last_month": "product",
    "usage_change_last_month_vs_prior_3": "product",
    "failed_payments_90d": "stripe",
    "threads_90d": "gmail",
}

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
FEATURE_EVENTS: dict[str, EventFilter | None] = {
    "account_tenure_days": None,
    "arr": None,
    "days_to_renewal": None,
    "cycle_number": None,
    "total_event_count": EventFilter(SOURCES, NON_OUTCOME_TYPES, None),
    "tickets_30d": EventFilter(("zendesk",), ("ticket_created",), TICKET_WINDOW),
    "ticket_rate_30d_vs_prior_90d": EventFilter(
        ("zendesk",), ("ticket_created",), TICKET_WINDOW + TICKET_PRIOR_WINDOW
    ),
    "active_users_last_month": EventFilter(
        ("product",), ("usage_rollup_monthly",), USAGE_RECENT_WINDOW
    ),
    "usage_change_last_month_vs_prior_3": EventFilter(
        ("product",), ("usage_rollup_monthly",), USAGE_HISTORY_WINDOW
    ),
    "failed_payments_90d": EventFilter(("stripe",), ("invoice.payment_failed",), PAYMENT_WINDOW),
    "threads_90d": EventFilter(("gmail",), ("message",), THREAD_WINDOW),
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
