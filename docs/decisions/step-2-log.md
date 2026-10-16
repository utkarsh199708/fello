# Step 2 log: generator to spec

Date: 2026-10-17. Tasks 2.1 to 2.14 of [../TASKS.md](../TASKS.md). Task 2.15, the redeploy, is deferred (see the end of this log).

The Step 1 generator already had the five tenant profiles (2.1), the AR(1) health model with drift, noise and shocks, the separate contraction draw (2.2), per-source lag with a late tail running to `horizon_end` (2.10), and `connected_at` per tenant and source (2.7). Step 2 adds the rest of PLAN.md section 5.

## How the mess is switched on

Every Step 2 knob lives in the `generator:` block of `config/full.yaml` and `config/small.yaml`, and every one is optional. Without them the generator emits clean data, which is what `tests/fixtures/tiny.yaml` and the Step 1 tests rely on. Each new kind of randomness has its own stream (`identity`, `planted`, `duplicates`, `thin`, `resend`), so adding one never shifts the draws of another.

## What was added, and how the pipeline meets it

| Mess | Generator | Rate in the full config | Who consumes it |
|---|---|---|---|
| Thin accounts (2.4) | 8% of accounts, chosen among those started in the last 150 days: no product workspace yet, billed once upfront, a fifth of the usual ticket and mail activity | 7.4% have fewer than 5 events | Step 6 "not enough signal" bucket |
| Onboarding ticket burst (2.3) | 3x the base ticket rate in an account's first 60 days, whatever its health | always on | The model must not read it as risk |
| Usage drives tickets (2.3) | Product sessions scale with the same activity multiplier as tickets | always on | Decoy for a ticket-count rule |
| Failed payments that recover (2.3) | Already in Step 1: 70% of mid-term failures are paid on retry within 6 days | as before | Feature `failed_payments_90d` |
| Unobserved churn (2.2) | `unobserved_sd` raised from 1.0 to 1.5 | 21.0% of churn looks healthy beforehand (target 20 to 25%) | The ceiling below the oracle |
| Mutable records (2.5) | Ticket snapshots and Salesforce records carry `updated_at` and `last_modified_date` | every record | 3.1 upserts on the newest |
| Missing back-references (2.6) | 3.5% of Stripe customers and Zendesk organisations have no crosswalk row | 295 accounts | Stripe goes to quarantine; Zendesk falls back to the requester's email domain |
| Personal-domain contacts (2.6) | 5% of accounts have one contact on `freemail.example` | 260 accounts | Their mail goes to quarantine (`unknown_domain`) |
| Shared domains (2.6) | 0.4% of accounts share a website domain with another account | 20 accounts | Mail goes to quarantine (`ambiguous_domain`) |
| Missing Salesforce fields (2.7) | 10% of account records lack `industry`, and 10% lack the ARR field | per record | 3.3 NULL is not zero |
| Schema drift (2.8) | Zendesk priority is an integer code before 2024-10-07 (v1) and a name after (v2). The Salesforce ARR field is `arr__c` before 2025-10-06 (v1) and `annual_contract_value__c` after (v2) | both versions in every tenant connected early enough | 3.2 dispatches on `source_schema_version` |
| Exact replays (2.9, kind 1) | 1% of events delivered a second time, 1 to 72 hours later, with the same `source_event_id` | about 1% per source | 3.1 drops on id |
| Re-sent snapshots (2.9, kind 2) | 3% of ticket snapshots re-sent unchanged with a newer `updated_at` | per snapshot | 3.1 upserts, not a new ticket |
| One renewal, two sources (2.9, kind 3) | Already in Step 1: Salesforce and Stripe both report each renewal | every renewal where Stripe is connected | 3.1 reconciles to one outcome |
| Planted cases (2.11) | 10 accounts across t1 to t4, each with a first holdout renewal above its CSM's median ARR. Health falls from 10 to 12 weeks before that renewal, and the renewal is lost. Clean identities only | 10 | Step 4 test: each enters its CSM's top 8 in time |
| Safe addresses (2.12) | Everything under `.example` and `.test`, plus `freemail.example` | all | Test `test_only_reserved_domains_appear` |

New truth files, which only evaluation may read: `truth/account_mess.csv` (each account's mess flags, `thin_history` and its distinct event count) and `truth/planted_cases.csv`.

## Decisions

- **Industry name.** "healthcare" became "life_sciences", because the truth-isolation test bans the word "health" outside `data/truth/`. The test stays strict.
- **The version follows occurred_at.** A real connector changes format by delivery time. Keying the switch to `occurred_at` keeps a payload's shape a function of the event alone. A late v1 event can arrive after the cut, which is realistic enough.
- **Without a drift date, a source is on its latest version.** That keeps the clean fixture on one format, and Step 3 needs only one dispatch table.
- **Ticket rate cut from 0.125 to 0.11 a month.** This offsets the onboarding burst, so Zendesk stays within 10% of its 22% share.
- **owner_changed is still not generated.** No step consumes CSM reassignment yet. A mess with no handler is the thing the plan review warned about.
- **`data-report` reads only what the pipeline sees** (reference CSVs and events), never truth. It reuses the resolve step's `decide`, so its quarantine rate is the one ingest produces.

## Verification

- 163 tests pass, including 10 new ones in `tests/test_generator_mess.py`. They check:
  - every mess kind is present;
  - drift matches the vocabulary;
  - identity breaks match their truth;
  - the planted-case spec;
  - byte-identical regeneration;
  - the clean fixture stays clean;
  - the full-config acceptance targets;
  - truth isolation;
  - the pipeline quarantines the identity breaks;
  - no feature correlates 0.99 or more with latent health.
- Generating `config/full.yaml` inside the Linux image and on Windows gives identical bytes in all 15 files.
- A full bootstrap of the messy data into a fresh local database took 252 s. In order:
  - It loaded 194,000 raw rows (including the replays).
  - It quarantined 1,439 events: t1 648, t2 354, t3 120, t4 290, t5 27.
  - It trained `logreg-cae5953e43cf`.
  - The first night succeeded for all five tenants.

## 2.15 deferred, then done with Step 3

Redeploying means wiping the live database, because the worker bootstraps only when `model_versions` is empty. That would erase the first unattended `job_runs` row due at 02:00 UTC on 2026-10-17, which is milestone M1's evidence. It would also put 1% replayed events on the live list before Step 3.1 can drop them. So the redeploy waits until after that run has been seen and Step 3 has landed, and the database is wiped once.

Done on 2026-10-16 at 21:52 UTC, together with Step 3, before the first unattended night ran. See [0002-host-on-gcp-vm.md](0002-host-on-gcp-vm.md).
