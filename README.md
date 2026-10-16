# Monday list

A ranked Monday list for customer success managers: the accounts most worth working this week, in order, with reasons that trace back to raw events. Built for the Fello GTM AI take-home.

Status: Steps 0 to 4 of [docs/TASKS.md](docs/TASKS.md) are built and tested. Step 1 is the walking skeleton: generate, load, resolve, features, train, score, nightly job, worker and web. It is live on one GCP VM at https://136-83-131-193.sslip.io, behind the shared password ([docs/decisions/0002-host-on-gcp-vm.md](docs/decisions/0002-host-on-gcp-vm.md)). Step 2 makes the generator emit every kind of mess in PLAN.md section 5. Step 3 gives each kind a handler and a test: dedup, schema dispatch, email pseudonymisation and a data-quality gate ([docs/decisions/step-3-log.md](docs/decisions/step-3-log.md)). Step 4 replaces the Step 1 logistic regression with LightGBM, adds the full feature set and stores an evaluation fit beside the live one. The holdout evaluation is below. The solution brief, written before any code, is [docs/brief.md](docs/brief.md). The design is [docs/PLAN.md](docs/PLAN.md).

## What I decided

To be written in Step 7. Until then the decisions and their reasons are in [docs/PLAN.md](docs/PLAN.md) sections 1 and 4 to 8, and each decision taken during the build is logged in [docs/decisions/](docs/decisions/).

## What I deliberately skipped

To be written in Step 7. The starting list is [docs/PLAN.md](docs/PLAN.md) section 10.

## What breaks first at 10x

To be written in Step 7, from measured stage timings. The hypothesis it will confirm or contradict is [docs/PLAN.md](docs/PLAN.md) section 11.

## Local development

Needs uv 0.12.17 or newer and Docker.

```bash
uv sync                          # .venv with Python 3.11 and the locked dependencies
cp .env.example .env             # dev-only values; .env is gitignored
docker compose up -d db          # Postgres 16 on 127.0.0.1:${DB_PORT:-5432}, data in a named volume
uv run ruff check .
uv run --env-file .env pytest    # database tests create and drop their own fello_test database
```

If another program already holds port 5432, set `DB_PORT` in `.env` to a free port (for example `DB_PORT=55433`) and use the same port in `DATABASE_URL` and `TEST_DATABASE_URL`. Check the binding with `docker compose port db 5432`: Compose can report the db healthy even when the host port was taken, and then host-side tools silently reach the other program.

The app reads only process environment variables and never opens `.env` itself, so every command that needs it goes through `uv run --env-file .env`.

Run the whole system on the full dataset (5 tenants, 5,000 accounts, about 192,000 events):

```bash
uv run --env-file .env python -m app bootstrap --config config/full.yaml   # about 2 minutes: migrate, generate, load, resolve, anchors, train, first night
uv run --env-file .env python -m app nightly --as-of 2026-10-13T02:00:00Z  # one more night, a few seconds; run again and it is a no-op
uv run --env-file .env python -m app web                                   # http://127.0.0.1:8000, password BASIC_AUTH_PASSWORD from .env, username ignored
```

Or run everything in containers: `docker compose up -d` starts db, web and worker. The worker bootstraps on first start (`BOOTSTRAP_CONFIG`) and then fires the nightly job on `NIGHTLY_CRON` (UTC); set `NIGHTLY_CRON="*/2 * * * *"` in `.env` to watch it fire. `curl localhost:8000/health` shows the model version and the newest job run.

Tests that need Postgres read `TEST_DATABASE_URL` (default `.../fello_test`), refuse any database not named `fello_test*`, and drop and recreate it each session. Give each parallel worker its own, for example `.../fello_test_qa`. Without a reachable Postgres they skip.

Secret scanning runs as a gitleaks pre-commit hook. Set it up once per clone with `uv tool install pre-commit`, then `pre-commit install`.

## Data report

All data is synthetic. `python -m app data --config config/full.yaml` writes it, and `python -m app data-report` prints the report below. The report reads only what the pipeline would see, never `data/truth/`. It uses the resolve step's own matching rules, so the quarantine line is what ingest will produce. Regenerating gives identical bytes on Windows and in the Linux image. What each kind of mess is, and which step handles it, is in [docs/decisions/step-2-log.md](docs/decisions/step-2-log.md).

Notes for reading it:

- Totals count distinct events. "Replays" are the extra deliveries of an event that already arrived (duplicate kind 1).
- Versions `v1` and `v2` are the two payload schemas of a drifting source.
- "Re-sent unchanged" counts ticket snapshots identical to the ticket's previous one (duplicate kind 2).
- A renewal counts as contracted when it was renewed below 70% of the prior ARR.

From truth, checked by `tests/test_generator_mess.py`:

- 20.8% of churn shows no signal beforehand (target 20 to 25%).
- 7.4% of accounts have fewer than 5 events.
- 10 planted "signals were sitting right there" cases.

```text
== t1 Northwind Cloud: 2,000 accounts, 83,246 events
   connected: gmail, product, salesforce, stripe, zendesk
   events per account p5/p50/p95: 2/36/95; fewer than 5 events: 7.1%
   source       events  replays  versions        lag hours p50/p95/p99
   salesforce    6,780   1.09%  v1:2764 v2:4016 4.3/19.9/68.9
   zendesk      20,082   0.92%  v1:2053 v2:18029 2.9/33.2/144.1
   product      32,061   0.94%  v1:32061        26.8/146.8/241.8
   stripe       10,916   1.06%  v1:10916        1.4/6.7/49.0
   gmail        13,407   0.92%  v1:13407        9.0/53.7/111.0
   zendesk snapshots re-sent unchanged: 2,967
   quarantine: 0.85% (ambiguous_domain 42, unknown_domain 251, unknown_native_key 418)
   renewals 2024: 122, lost 14 (11.5%), contracted 7, renewed 101
   renewals 2025: 666, lost 49 (7.4%), contracted 63, renewed 554
   renewals 2026: 1,144, lost 78 (6.8%), contracted 109, renewed 957

== t2 Harbor Software: 1,200 accounts, 45,307 events
   connected: product, salesforce, stripe, zendesk
   events per account p5/p50/p95: 2/33/83; fewer than 5 events: 7.8%
   source       events  replays  versions        lag hours p50/p95/p99
   salesforce    4,041   1.11%  v1:1687 v2:2354 4.2/19.9/53.8
   zendesk      11,873   0.86%  v1:1404 v2:10469 2.9/33.5/140.2
   product      19,216   0.95%  v1:19216        27.4/145.1/237.4
   stripe       10,177   1.14%  v1:10177        1.4/6.9/47.5
   zendesk snapshots re-sent unchanged: 1,781
   quarantine: 0.87% (unknown_native_key 396)
   renewals 2024: 78, lost 5 (6.4%), contracted 10, renewed 63
   renewals 2025: 417, lost 54 (12.9%), contracted 33, renewed 330
   renewals 2026: 682, lost 65 (9.5%), contracted 47, renewed 570

== t3 Lumen Retail Systems: 900 accounts, 31,181 events
   connected: gmail, product, salesforce, zendesk
   events per account p5/p50/p95: 2/28/83; fewer than 5 events: 7.6%
   source       events  replays  versions        lag hours p50/p95/p99
   salesforce    3,051   1.25%  v1:1256 v2:1795 4.2/19.7/50.2
   zendesk       8,253   0.81%  v1:990 v2:7263  2.9/23.1/143.3
   product      14,260   0.95%  v1:14260        27.2/147.9/247.9
   gmail         5,617   0.78%  v1:5617         8.9/54.7/111.5
   zendesk snapshots re-sent unchanged: 1,168
   quarantine: 0.42% (ambiguous_domain 47, unknown_domain 78, unknown_native_key 6)
   renewals 2024: 64, lost 8 (12.5%), contracted 6, renewed 50
   renewals 2025: 319, lost 36 (11.3%), contracted 30, renewed 253
   renewals 2026: 503, lost 57 (11.3%), contracted 33, renewed 413

== t4 Copper Desk: 600 accounts, 28,634 events
   connected: gmail, product, salesforce, stripe, zendesk
   events per account p5/p50/p95: 2/41/98; fewer than 5 events: 7.0%
   source       events  replays  versions        lag hours p50/p95/p99
   salesforce    2,015   0.79%  v1:886 v2:1129  4.2/21.3/60.3
   zendesk       3,726   1.05%  v2:3726         3.0/34.8/141.5
   product       9,218   0.92%  v1:9218         26.7/143.2/236.3
   stripe        9,920   1.19%  v1:9920         1.4/7.1/51.4
   gmail         3,755   1.01%  v1:3755         9.0/51.9/109.6
   zendesk snapshots re-sent unchanged: 542
   quarantine: 1.13% (ambiguous_domain 14, unknown_domain 83, unknown_native_key 226)
   renewals 2024: 45, lost 8 (17.8%), contracted 1, renewed 36
   renewals 2025: 206, lost 40 (19.4%), contracted 10, renewed 156
   renewals 2026: 322, lost 57 (17.7%), contracted 23, renewed 242

== t5 Pine Launch: 300 accounts, 3,712 events
   connected: gmail, product, salesforce, stripe
   events per account p5/p50/p95: 1/12/20; fewer than 5 events: 8.3%
   source       events  replays  versions        lag hours p50/p95/p99
   salesforce      434   0.46%  v2:434          4.6/22.1/69.3
   product       1,366   1.32%  v1:1366         26.1/148.8/254.9
   stripe        1,324   1.21%  v1:1324         1.4/7.1/47.9
   gmail           588   1.36%  v1:588          9.6/59.0/117.2
   zendesk snapshots re-sent unchanged: 0
   quarantine: 1.51% (ambiguous_domain 4, unknown_domain 4, unknown_native_key 48)
   renewals: none observed

== all tenants: 5,000 accounts, 192,080 events
   salesforce   16,321    8.5%  (target 8%, off by 6%)
   zendesk      43,934   22.9%  (target 22%, off by 4%)
   product      76,121   39.6%  (target 38%, off by 4%)
   stripe       32,337   16.8%  (target 18%, off by 6%)
   gmail        23,367   12.2%  (target 12%, off by 1%)
```

## Evaluation

`python -m app evaluate --config config/full.yaml` prints the evaluation below. It runs on the test cycles of a cycle-based temporal split: the training cycles renew on or before 2025-10-12, and the test cycles renew after it, up to 2026-10-12. Every weekly row of one account's cycle sits on one side. The model under test is the evaluation fit, trained on the training cycles only. The live model is a refit on every cycle, with the same settings. How it is built is in [docs/decisions/step-4-log.md](docs/decisions/step-4-log.md).

What it says, plainly:

- **Both models beat the baselines that matter.** LightGBM's ROC-AUC is 0.600 and its PR-AUC 0.248. The hand-written rules score 0.547 and 0.201. Days to renewal alone, the "is this a calendar" row, scores 0.500 and 0.180. Days to renewal carries under 1% of the model's contributions and is never an account's top reason.
- **LightGBM and logistic regression are within noise of each other.** The ROC-AUC difference is +0.009, with a 95% interval of -0.009 to +0.028, from a paired bootstrap over accounts. Logistic regression is slightly ahead on PR-AUC and recall@8. LightGBM is live because of a rule fixed in `app/ml/train.py`, not a choice made from this table: LightGBM is kept unless logistic regression wins the validation tail clearly, by at least 1% lower log loss over at least 100 renewal cycles. Here the two are 0.4% apart.
- **The ceiling is low on purpose.** The oracle, which ranks by the generator's latent health, reaches only 0.650. About 21% of churn shows no signal, and health moves between the anchor and the renewal. Recall@8 is 21.8% against the oracle's 23.6%, so most of what is learnable is learned.
- **Planted cases.** 7 of the 10 planted "signals were sitting right there" cases enter their CSM's top 8 at least 4 weeks before the notice deadline. For 9 of them the top reason is a ticket or usage feature.
- **Calibration** is good up to a predicted probability of 0.3, which covers 89% of rows. Above that the model is too pessimistic.
- **Leakage probes pass.** AUC rises gently toward the renewal date, with no jump toward 1.0. A model trained on shuffled labels scores 0.475.

These metrics validate the point-in-time labelling, the pipeline and the ranking on data whose generating process is known. They are not evidence about real-world churn. The first real-data step is refitting and re-baselining on actual renewal outcomes.

Model under test: lightgbm-694a83671c18, the evaluation fit of the kind chosen on the validation tail (lightgbm). The live version is its refit on every cycle.
Training cycles renew on or before 2025-10-12; test cycles renew after it, up to 2026-10-12.
68,989 training rows; 135,613 test rows with 24,344 positives; 40 holdout Mondays; 466 renewals lost or contracted.

| Method | PR-AUC | ROC-AUC | Brier | recall@8 | ARR recall@8 | precision@8 | caught 30d before notice | caught 60d | median lead (days) |
|---|---|---|---|---|---|---|---|---|---|
| oracle | 0.296 | 0.650 | 0.141 | 23.6% | 35.6% | 8.3% | 36.1% | 35.0% | 287 |
| lightgbm (live kind) | 0.248 | 0.600 | 0.146 | 21.8% | 32.2% | 7.6% | 38.4% | 37.3% | 272 |
| logistic regression | 0.256 | 0.591 | 0.145 | 23.7% | 34.5% | 8.2% | 39.5% | 39.1% | 270 |
| rules | 0.201 | 0.547 | 0.147 | 14.8% | 26.7% | 5.0% | 30.3% | 30.0% | 294 |
| days to renewal | 0.180 | 0.500 | 0.148 | 12.4% | 25.1% | 4.2% | 18.0% | 18.0% | 304 |

precision@8 ceiling (if every due loss were in the top 8): 37.5%.
The live kind ranked by probability alone instead of probability x ARR: recall@8 24.7%, ARR recall@8 20.0%.
LightGBM minus logistic regression, ROC-AUC: +0.009, 95% interval -0.009 to +0.028 (paired bootstrap over accounts): within noise.

Reliability (lightgbm, test rows):

| Predicted bin | Rows | Mean predicted | Observed |
|---|---|---|---|
| 0.0-0.1 | 18,221 | 0.080 | 0.109 |
| 0.1-0.2 | 61,414 | 0.150 | 0.153 |
| 0.2-0.3 | 41,739 | 0.239 | 0.217 |
| 0.3-0.4 | 9,397 | 0.340 | 0.245 |
| 0.4-0.5 | 3,016 | 0.443 | 0.268 |
| 0.5-0.6 | 1,433 | 0.543 | 0.380 |
| 0.6-0.7 | 350 | 0.638 | 0.580 |
| 0.7-0.8 | 43 | 0.726 | 0.605 |

Slices (lightgbm, test rows):

| Slice | Rows | Positives | PR-AUC | ROC-AUC |
|---|---|---|---|---|
| tenant t1 | 57,963 | 9,874 | 0.214 | 0.585 |
| tenant t2 | 35,090 | 5,958 | 0.244 | 0.606 |
| tenant t3 | 25,802 | 4,490 | 0.248 | 0.613 |
| tenant t4 | 16,758 | 4,022 | 0.345 | 0.605 |
| tenant t5 | - | - | no observed renewals | |
| days_to_renewal 0-90 | 33,786 | 6,064 | 0.291 | 0.646 |
| days_to_renewal 91-180 | 33,389 | 5,994 | 0.255 | 0.606 |
| days_to_renewal over 180 | 68,438 | 12,286 | 0.222 | 0.572 |
| event_count under 10 | 25,794 | 4,583 | 0.212 | 0.571 |
| event_count 10-100 | 109,282 | 19,525 | 0.254 | 0.607 |
| event_count over 100 | 537 | 236 | 0.467 | 0.515 |

Is it a calendar? days_to_renewal carries 0.8% of the absolute contributions and is the top reason on 0.0% of test rows.
Planted cases: 7 of 10 entered their CSM's top 8 at least 4 weeks before the notice deadline; the top reason was a ticket or usage feature for 9 of 10.
Worst misses: 277 lost renewals never reached their CSM's top 8. Largest:
- t1 Lumen Supply LLC, ARR 207,240, renewal 2026-02-03
- t1 Clear Media Ltd, ARR 188,800, renewal 2026-08-04
- t1 Blue Analytics Group, ARR 183,600, renewal 2026-06-26
- t1 Vertex Foods Co, ARR 178,200, renewal 2026-09-10
- t1 Silver Energy GmbH, ARR 174,800, renewal 2026-02-02

Leakage probes:

- AUC by days before renewal: 90 days 0.629, 60 days 0.637, 30 days 0.655, 7 days 0.667 (no jump toward 1.0: ok)
- Shuffled-label AUC: 0.475 (near 0.5: ok)
- Random row split AUC: 0.690, not reported: rows of one account would sit on both sides.

## Dependencies and pins

| What | Pin | Where |
|---|---|---|
| Python packages | Exact versions and hashes in `uv.lock`, written by uv 0.12.17. CI and the image install with `uv sync --locked`, which fails if the lock is stale | `pyproject.toml`, `uv.lock` |
| Base image | `python:3.11-slim` by digest (Python 3.11.17, Debian trixie) | `Dockerfile` |
| uv in the image | `ghcr.io/astral-sh/uv:0.12.17` by digest | `Dockerfile` |
| GitHub Actions | Commit SHAs, release named in a comment | `.github/workflows/ci.yml` |
| gitleaks | v8.30.1 by commit SHA | `.pre-commit-config.yaml` |

## Vendored files

| File | Upstream | Version | Licence | SHA-256 |
|---|---|---|---|---|
| `static/htmx.min.js` | npm package `htmx.org`, file `dist/htmx.min.js` | 2.0.11 | 0BSD | `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717` |

The npm tarball was checked against the registry's sha512 integrity before extraction, and the file matches the jsDelivr copy of the same release. Git stores it as binary so its bytes never change. Check it with `sha256sum static/htmx.min.js`.

## Licence

MIT. See [LICENSE](LICENSE).
