# Step 7: what breaks first at 10x, measured

Date: 2026-10-17. Task 7.2. Every number below is a measurement, followed by a linear extrapolation that states its assumptions.

## What was measured

**Live, on the e2-small VM (2 shared vCPU, 2 GB RAM).** These are the stage timings of the unattended night of 2026-10-17, from `job_runs.stage_ms`:

| Tenant | Accounts | Raw events | Ingest | Snapshot | Score | Gate | Night |
|---|---|---|---|---|---|---|---|
| t1 | 2,000 | 84,044 | 2.7 s | 4.4 s | 2.0 s | 0.3 s | 9.5 s |
| t2 | 1,200 | 45,753 | 1.8 s | 2.0 s | 1.2 s | 0.2 s | 5.1 s |
| t3 | 900 | 31,466 | 0.6 s | 1.5 s | 0.8 s | 0.1 s | 3.0 s |
| t4 | 600 | 28,930 | 0.6 s | 1.2 s | 0.6 s | 0.1 s | 2.4 s |
| t5 | 300 | 3,756 | 0.1 s | 0.2 s | 0.4 s | 0.0 s | 0.7 s |

The whole night took 21 s. That night ran on the Step 4 build, before the decision stages (transitions, policy and tasks, escalation) were deployed at 06:28 UTC the same day. On the local copy of the full dataset those stages add under a second for t1: 217 ms for transitions, 634 ms for policy and tasks and 3 ms for escalation.

- The snapshot stage scales with the tenant's events: about 0.05 ms per normalised event. It reads all of the tenant's events into pandas.
- Scoring scales with accounts: about 1.07 ms per account.
- Ingest scales with the raw-event table: its "not yet handled" scan runs per tenant.

The first bootstrap took about 14.5 minutes on this VM for feature set fs3: anchors 457 s and training 110 s. The fs4 bootstrap (32 features) took about 23 minutes from deploy to the first night, including the image build. Postgres holds 674 MB, of which `snapshots` is 456 MB for 321,000 anchor rows.

**Locally, the feature build per tenant.** Feature inputs were loaded once, and features computed for nine Mondays:

| Tenant | Normalised events | Load | Compute per Monday | Events frame | Python peak |
|---|---|---|---|---|---|
| t1 | 74,176 | 4.1 s | 926 ms | 40 MB | 92 MB |
| t2 | 40,450 | 1.7 s | 598 ms | 21 MB | 54 MB |
| t4 | 25,387 | 1.8 s | 543 ms | 14 MB | 31 MB |

That is about 0.4 s of fixed cost per Monday plus about 7 µs per event. Memory is about 540 bytes per event in the frame, and about 1.2 KB per event at peak. The weekly anchors (147 Mondays: 36 months less a 12-week warm-up) repeat that pass every week, so bootstrap is O(weeks × events). The VM runs about 2.8 times slower than the laptop.

## Extrapolation, one axis at a time

| Axis | Nightly run | Bootstrap (anchors and training) | Storage | What breaks |
|---|---|---|---|---|
| **10x events** (2M), same accounts | t1's snapshot reads 740,000 events: about 45 s, plus about 40 s to load. The whole night is about 4 minutes. **Fine** | t1 alone: 147 Mondays × (0.4 + 7 µs × 740,000) s, about 14 minutes locally and about 40 minutes on the VM. The events frame is about 400 MB, peaking near 0.9 GB in Python before pandas copies. **Breaks first:** a 2 GB VM that also runs Postgres and the web process swaps and then runs out of memory | `raw_events` and `normalised_events` grow to about 1.8 GB | **The bootstrap's anchor build, on memory then time** |
| **10x accounts** (50,000, events growing with them) | Scoring about 20 s per t1-sized tenant, and 50,000 score rows a night with full contribution vectors (about 100 MB a night) | As above, plus 3.2M anchor rows written | `snapshots` about 4.5 GB, and `scores` about 3 GB a month | **Disk within weeks** on a 20 GB disk, then training memory: 3.2M JSON feature rows read into pandas |
| **10x tenants** (50) | The per-tenant loop is sequential: about 210 s. One failure is still isolated to its tenant. **Fine** | 10x the anchors work, about 4 hours on the VM. Training reads every tenant's anchors at once as `app_batch` | As 10x accounts | **Training memory**, and a bootstrap too long for a deploy |
| LLM narration | Scales with tasks created (at most k per CSM a night), not with accounts. It is off today | | | Not a first failure |

## Was the hypothesis right?

Mostly yes. PLAN.md section 11 predicted that the nightly feature build, a full pandas pass per tenant, breaks first, at bootstrap time on the small host. The measurements put it slightly differently:

- The **nightly** run is comfortable at 10x on every axis, at about 4 minutes.
- The **bootstrap's weekly anchors** break first, on **memory** before time, at 10x events on this VM.

The other prediction holds too: storage (snapshots and scores) is the binding limit at 10x accounts, which is why retention and a top-10-plus-residual contribution vector were named.

## Mitigations, named, not built

1. **Anchors incrementally.** Build each Monday from the events received since the previous one, with per-account running aggregates, instead of re-reading every event every week. This turns O(weeks × events) into O(events).
2. **Partition** `normalised_events` and `snapshots` by tenant and month, and read events per account partition in the feature build.
3. **Retention** of nightly snapshots and scores older than 90 days. Store the top 10 contributions plus a residual instead of all 32, so the reconciliation still adds up.
4. **Training from columns, not JSON.** Store anchor features as a typed table (or Parquet) for the trainer, and sample negatives by account.
5. **More tenants:** one queue job per tenant with `pg_try_advisory_lock`, then a second worker.
6. **The host:** an e2-standard-2 (8 GB) costs about $35 a month more and moves the anchor-build ceiling roughly 4 times. It is the cheap answer until mitigation 1 is built.
