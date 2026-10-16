"""Turn the world into raw events, one function per source.

Each source observes the same latent health through its own noise, and then shapes it into
its own event types. Three signals carry churn information:

- Zendesk: ticket volume rises when health falls (the "ticket spike").
- Product: active users and seat use fall with health (the "usage decline").
- Stripe: payment failures become more likely as health falls (the "failed payments").

Gmail volume and bounces carry a weaker signal. Salesforce reports account records and the
renewal outcome itself.

An emitter returns tuples `(occurred_epoch, event_type, payload)`. Ids and `received_at`
are added later in generate.py, so an emitter never has to think about lag.
"""

from __future__ import annotations

import numpy as np

from app.generator import timeutil as tu
from app.generator.world import BASE62, World, random_tokens, salesforce_id, sigmoid, stream

HOUR, DAY = 3600, 86400

Raw = tuple[int, str, dict]  # occurred_epoch, event_type, payload


def _window(w: World, source: str):
    """Per account and month: the span the account is live and the source is connected.

    Returns (lo, hi, frac) matrices of shape (n, M). `frac` is the live share of the month, so
    an account that started or churned mid-month produces proportionally fewer events.
    """
    conn = tu.to_epoch(w.tenant.connected_at[source])
    horizon = tu.to_epoch(w.cfg.horizon_end)
    lo = np.maximum(w.month_epoch[None, :-1], np.maximum(w.start_epoch, conn)[:, None])
    hi = np.minimum(w.month_epoch[None, 1:], np.minimum(w.end_epoch, horizon)[:, None])
    length = (w.month_epoch[1:] - w.month_epoch[:-1])[None, :]
    frac = np.clip(hi - lo, 0, None) / length
    return lo, hi, frac


def _times(rng, lo: int, hi: int, k: int) -> list[int]:
    return sorted(int(lo + t) for t in (rng.random(k) * (hi - lo)).astype(np.int64))


def _month_of(w: World, epoch: int) -> int:
    return int(np.searchsorted(w.month_epoch, epoch, side="right")) - 1


def salesforce_events(w: World) -> list[Raw]:
    rng = stream(w.cfg, w.tindex, "salesforce")
    knobs = w.cfg.generator["salesforce"]
    conn = tu.to_epoch(w.tenant.connected_at["salesforce"])
    horizon = tu.to_epoch(w.cfg.horizon_end)
    out: list[Raw] = []

    def upsert(a, t):
        owner = w.csms[a.csm].email if a.csm is not None else ""
        payload = {
            "account_id": a.salesforce_id,
            "name": a.name,
            "website_domain": a.domain,
            "owner_email": owner,
        }
        out.append((t, "account_upserted", payload))

    for a in w.accounts:  # the record appears when the account is created
        t = w.start_epoch[a.index] + int(rng.integers(8 * HOUR, 18 * HOUR))
        if conn <= t <= horizon:
            upsert(a, int(t))

    lo, hi, frac = _window(w, "salesforce")  # later edits to the record, same content
    counts = rng.poisson(knobs["upserts_per_month"] * frac)
    for i, m in zip(*np.nonzero(counts), strict=True):
        for t in _times(rng, lo[i, m], hi[i, m], counts[i, m]):
            upsert(w.accounts[i], t)

    bodies = iter(random_tokens(rng, BASE62, 12, sum(len(a.renewals) for a in w.accounts)))
    for a in w.accounts:  # the renewal outcome, closed on the renewal date
        for r in a.renewals:
            t = min(tu.to_epoch(r.date) + 9 * HOUR + int(rng.integers(0, 8 * HOUR)), horizon)
            out.append(
                (
                    t,
                    "opportunity_stage_changed",
                    {
                        "account_id": a.salesforce_id,
                        "opportunity_id": salesforce_id("006", next(bodies)),
                        "stage": "closed_lost" if r.outcome == "churned" else "closed_won",
                        "renewal_date": r.date.strftime("%Y-%m-%d"),
                        "prior_arr": r.prior_arr,
                        "renewed_arr": r.renewed_arr,
                    },
                )
            )
    return out


def zendesk_events(w: World) -> list[Raw]:
    """Tickets per account-month ~ Poisson, rising as health falls and as the account grows.

    Account size is a decoy on purpose: big healthy accounts file many tickets too, so a raw
    ticket count is not the same as a ticket spike.
    """
    rng = stream(w.cfg, w.tindex, "zendesk")
    k = w.cfg.generator["zendesk"]
    segment = w.cfg.generator["segments"][w.tenant.segment]
    horizon = tu.to_epoch(w.cfg.horizon_end)
    lo, hi, frac = _window(w, "zendesk")

    seen = w.health + rng.normal(0.0, k["noise_sd"], w.health.shape)
    size = np.power(np.maximum(w.arr_month, 1.0) / segment["arr_median"], k["size_exponent"])
    lam = k["tickets_per_month"] * w.activity[:, None] * size * np.exp(-k["health_coef"] * seen)
    counts = rng.poisson(lam * frac)

    out: list[Raw] = []
    ticket_id = 10_000
    priorities = ["low", "normal", "high", "urgent"]
    for i, m in zip(*np.nonzero(counts), strict=True):
        a = w.accounts[i]
        stop = min(int(w.end_epoch[i]), horizon)
        for t0 in _times(rng, lo[i, m], hi[i, m], counts[i, m]):
            ticket_id += 1
            email = a.contacts[int(rng.integers(len(a.contacts)))]
            priority = priorities[int(rng.choice(4, p=[0.2, 0.55, 0.2, 0.05]))]
            n_updates = int(rng.choice(4, p=[0.25, 0.35, 0.30, 0.10]))
            solved = n_updates > 0 and rng.random() < 0.85
            statuses = ["open"] + [
                "pending" if rng.random() < 0.5 else "open" for _ in range(n_updates)
            ]
            if solved:
                statuses[-1] = "solved"
            csat = None
            t = t0
            for step, status in enumerate(statuses):
                if step > 0:
                    t += HOUR + int(rng.exponential(1.5 * DAY))
                if t > stop:
                    break
                if status == "solved" and rng.random() < 0.4:
                    csat = int(np.clip(round(3.6 + 0.6 * w.health[i, m] + rng.normal()), 1, 5))
                out.append(
                    (
                        t,
                        "ticket_created" if step == 0 else "ticket_updated",
                        {
                            "organization_id": a.zendesk_org,
                            "ticket_id": ticket_id,
                            "requester_email": email,
                            "status": status,
                            "priority": priority,
                            "csat": csat,
                        },
                    )
                )
    return out


def product_events(w: World) -> list[Raw]:
    """One usage rollup per live account-month, stamped at the start of the next month."""
    rng = stream(w.cfg, w.tindex, "product")
    k = w.cfg.generator["product"]
    segment = w.cfg.generator["segments"][w.tenant.segment]
    conn = tu.to_epoch(w.tenant.connected_at["product"])
    horizon = tu.to_epoch(w.cfg.horizon_end)
    shape = w.health.shape

    seen = w.health + rng.normal(0.0, k["noise_sd"], shape)
    purchased = np.maximum(3, np.round(w.arr_month / segment["seat_price"])).astype(int)
    used = np.maximum(1, np.round(purchased * np.clip(sigmoid(1.2 + 0.8 * seen), 0.05, 1.0)))
    share = np.clip(sigmoid(0.3 + 1.0 * seen + rng.normal(0.0, 0.3, shape)), 0.02, 1.0)
    active = np.round(used * share).astype(int)
    sessions = np.round(active * rng.uniform(6, 16, shape)).astype(int)
    actions = np.round(sessions * rng.uniform(1.5, 4.5, shape)).astype(int)
    keep = rng.random(shape) >= k["dropout"]  # the connector sometimes misses a month

    stamp = w.month_epoch[None, 1:]  # a rollup describes the month that just ended
    live = (
        (w.start_epoch[:, None] < stamp)
        & (w.end_epoch[:, None] >= stamp)
        & (stamp <= horizon)
        & (stamp >= conn)
        & (w.arr_month > 0)
        & keep
    )
    out: list[Raw] = []
    for i, m in zip(*np.nonzero(live), strict=True):
        out.append(
            (
                int(stamp[0, m]),
                "usage_rollup_monthly",
                {
                    "workspace_id": w.accounts[i].workspace_id,
                    "month": w.month_label(m),
                    "active_users": int(active[i, m]),
                    "sessions": int(sessions[i, m]),
                    "key_actions": int(actions[i, m]),
                    "seats_used": int(used[i, m]),
                    "seats_purchased": int(purchased[i, m]),
                },
            )
        )
    return out


def stripe_events(w: World) -> list[Raw]:
    """Mid-term invoices (some fail more often when health is low) and the renewal events."""
    rng = stream(w.cfg, w.tindex, "stripe")
    k = w.cfg.generator["stripe"]
    segment = w.cfg.generator["segments"][w.tenant.segment]
    interval = segment["invoice_months"]
    conn = tu.to_epoch(w.tenant.connected_at["stripe"])
    horizon = tu.to_epoch(w.cfg.horizon_end)
    out: list[Raw] = []

    n_invoices = sum(len(a.contracts) * (11 // interval) for a in w.accounts)
    invoice_tokens = iter(random_tokens(rng, BASE62, 14, n_invoices))
    sub_tokens = iter(random_tokens(rng, BASE62, 14, len(w.accounts)))

    for a in w.accounts:
        subscription = "sub_" + next(sub_tokens)
        stop = min(int(w.end_epoch[a.index]), horizon)
        for c in a.contracts:
            for j in range(1, 12 // interval + 1):
                if j * interval >= 12:
                    break  # the invoice at term end belongs to the renewal, not mid-term
                t = (
                    tu.to_epoch(tu.add_months(c.start, j * interval))
                    + 10 * HOUR
                    + int(rng.integers(0, 6 * HOUR))
                )
                invoice = "in_" + next(invoice_tokens)
                if t < conn or t > stop:
                    continue
                m = _month_of(w, t)
                amount = round(float(w.arr_month[a.index, m]) * interval / 12, 2)
                seen = w.health[a.index, m] + rng.normal(0.0, k["noise_sd"])
                failed = rng.random() < sigmoid(k["fail_intercept"] + k["fail_health_coef"] * seen)

                def invoice_event(
                    when, kind, attempt, a=a, invoice=invoice, amount=amount, stop=stop
                ):
                    if when <= stop:
                        out.append(
                            (
                                when,
                                kind,
                                {
                                    "customer": a.stripe_id,
                                    "invoice_id": invoice,
                                    "amount_usd": amount,
                                    "attempt": attempt,
                                },
                            )
                        )

                if not failed:
                    invoice_event(t, "invoice.paid", 1)
                    continue
                invoice_event(t, "invoice.payment_failed", 1)
                if rng.random() < k["recover_prob"]:  # a retry succeeds within a week
                    invoice_event(t + int(rng.uniform(1, 6) * DAY), "invoice.paid", 2)
                else:
                    invoice_event(t + 3 * DAY, "invoice.payment_failed", 2)
                    invoice_event(t + 7 * DAY, "invoice.payment_failed", 3)

        for r in a.renewals:  # the renewal itself
            t = min(tu.to_epoch(r.date) + 9 * HOUR + int(rng.integers(0, 8 * HOUR)), horizon)
            if t < conn:
                continue
            kind = "subscription.deleted" if r.outcome == "churned" else "subscription.updated"
            arr = r.prior_arr if r.outcome == "churned" else r.renewed_arr
            out.append(
                (
                    t,
                    kind,
                    {"customer": a.stripe_id, "subscription_id": subscription, "arr_usd": arr},
                )
            )
    return out


def gmail_events(w: World) -> list[Raw]:
    """Message metadata. Quiet accounts (low health) send fewer messages and bounce more."""
    rng = stream(w.cfg, w.tindex, "gmail")
    k = w.cfg.generator["gmail"]
    lo, hi, frac = _window(w, "gmail")

    seen = w.health + rng.normal(0.0, k["noise_sd"], w.health.shape)
    lam = k["messages_per_month"] * w.activity[:, None] * np.exp(k["health_coef"] * seen) * frac
    counts = rng.poisson(lam)

    out: list[Raw] = []
    last_thread: dict[int, str] = {}
    for i, m in zip(*np.nonzero(counts), strict=True):
        a = w.accounts[i]
        p_bounce = 0.03 + (0.12 if seen[i, m] < -1.0 else 0.0)
        for t in _times(rng, lo[i, m], hi[i, m], counts[i, m]):
            new_thread = i not in last_thread or rng.random() < 0.4
            if new_thread:
                last_thread[i] = "th_" + random_tokens(rng, "0123456789abcdef", 10, 1)[0]
            outbound = rng.random() < (0.7 if new_thread else 0.5)
            out.append(
                (
                    t,
                    "message",
                    {
                        "thread_id": last_thread[i],
                        "direction": "outbound" if outbound else "inbound",
                        "counterpart_email": a.contacts[int(rng.integers(len(a.contacts)))],
                        "bounce_or_autoreply": bool(rng.random() < p_bounce),
                    },
                )
            )
    return out


EMITTERS = {
    "salesforce": salesforce_events,
    "zendesk": zendesk_events,
    "product": product_events,
    "stripe": stripe_events,
    "gmail": gmail_events,
}
