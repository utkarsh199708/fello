# Step 3 log: pipeline hardening

Date: 2026-10-17. Tasks 3.1 to 3.7 of [../TASKS.md](../TASKS.md). Every kind of mess Step 2 generates now has a handler and a test.

## What each kind of mess meets

| Mess | Handler | Where | Test |
|---|---|---|---|
| Exact replay (duplicate kind 1) | The earliest delivery (by `received_at`, then raw id) is normalised. Later deliveries go to `rejects` as `duplicate_event_id`, pointing at the first. A unique index on `normalised_events (tenant_id, source, source_event_id)` makes a second copy impossible even through a bug | `resolve`, migration 0002 | `test_kind_1_*` |
| Re-sent or late ticket snapshot (kind 2) | `ticket_states(conn, as_of)`: per ticket, the snapshot with the newest `updated_at` among those received by `as_of`. A late older snapshot never wins | `app/ingest/objects.py` | `test_kind_2_*` |
| One renewal in Salesforce and Stripe (kind 3) | Resolve ties each Stripe subscription event to the contract ending within 3 days and stamps `renewal_date`. The renewal is decided when the first source reports it (`renewal_decisions`), and labelled once (`reconcile_outcomes`): Salesforce wins, and Stripe is translated when it is the only report | `resolve`, `features.py`, `labels.py` | `test_kind_3_*`, `test_salesforce_and_stripe_reconcile_to_one_outcome` |
| Schema drift | `canonical_payload` maps Zendesk v1 integer priority to its name and both Salesforce ARR field names to `arr`. An unknown version, an unknown event type, or a value of the wrong type goes to `rejects` with its reason and detail | `app/ingest/schema.py` | `test_zendesk_versions_*`, `test_salesforce_arr_*`, `test_drift_*`, `test_unreadable_*` |
| Arrival order | Dedup and state are decided by timestamps, never by load order | `resolve` | `test_resolve_and_features_do_not_depend_on_arrival_order` (the same data loaded in order and shuffled gives identical features, quarantine and rejects) |
| Unconnected source | Already NULL with a `<source>_connected` flag of 0 (Step 1). The score page now says "Zendesk not connected" instead of a number, and the gate trips if an unconnected source's feature ever has a value | `web/main.py`, `jobs/gate.py` | `test_a_null_feature_says_why_*`, `test_each_check_trips_on_its_own` |
| Unmatched identity | Quarantine with its reason (Step 1). The list page now shows the tenant's quarantined and rejected counts by reason in plain words | `web/queries.data_quality`, `list.html` | `test_the_pipeline_quarantines_identity_breaks_and_loads_drift` |
| Email addresses (PII) | The loader replaces each address's local part with an HMAC-SHA256 keyed per tenant from `PII_HASH_SECRET`. The domain stays in clear for matching, and loading refuses to run without the secret | `app/ingest/pii.py`, `loader.py` | `test_email_local_parts_*`, `test_loading_without_the_secret_*`, `test_raw_events_keep_every_field_as_received_except_addresses` |
| Bad data on a live night | The data-quality gate runs after scoring, in the night's transaction. If a check trips, the night rolls back, `job_runs` says `blocked` with the reason and every measured value, and the list keeps the previous night. A blocked night can be re-run | `app/jobs/gate.py`, `nightly.py` | `test_a_tripped_gate_blocks_the_night_and_publishes_nothing` |

## The gate's thresholds

| Check | Limit | Why this value |
|---|---|---|
| Duplicates after dedup | 0 | An invariant, also enforced by the unique index |
| Quarantine rate of tonight's events | under 10% | The full config runs at 0.4 to 1.5%. 10% means a mapping broke |
| NULL rate of a connected source's feature | at most 50% | The worst measured is 20% (t5 usage change, a young tenant) |
| NULL rate of an unconnected source's feature | exactly 100% | NULL, never zero |
| NULL rate of account and contract features | 0 | These always have a value |
| Mean probability shift against the last night scored by the same model | under 0.10 | Skipped after a model change (a rebaseline) |

## Decisions

- **Rejects are a third outcome, not a quarantine reason.** Quarantine means "we could not tell whose this is". A replay or an unreadable payload is a different failure, with a different owner (the connector).
- **The schema check is narrow.** It looks only at the fields that drift. A missing customer key is quarantine's job (`no_identity`), so the same event cannot land in two buckets for one cause.
- **Salesforce wins reconciliation.** It is the system of record in the brief's defaults. Stripe is used only when Salesforce never reported, which the generator never produces, so that path is exercised by unit tests.
- **Feature set fs3.** A renewal now opens the next cycle when either source reports it. Snapshots are keyed by feature set version, so fs2 rows stay valid for the model that used them.
- **The gate rolls back the whole night**, including resolve. The raw events stay, and the next run (or a re-run of the blocked night) processes them. Nothing half-published is left.
- **Hashing happens at load, so `raw_events` is "as received" except for addresses.** Storing addresses in clear and hashing later would put personal data at rest for no gain.

## Verification

- 196 tests pass, of which 28 are new in `tests/test_hardening.py`, and ruff and the format check are clean.
- The redeploy that puts Step 2's data and Step 3's handlers live is recorded in [0002-host-on-gcp-vm.md](0002-host-on-gcp-vm.md) once done.
