# Monday list

A ranked Monday list for customer success managers: the accounts most worth working this week, in order, with reasons that trace back to raw events. Built for the Fello GTM AI take-home.

Status: Steps 0 to 3 of [docs/TASKS.md](docs/TASKS.md) are built and tested. Step 1 is the walking skeleton: generate, load, resolve, features, train, score, nightly job, worker and web. It is live on one GCP VM at https://136-83-131-193.sslip.io, behind the shared password ([docs/decisions/0002-host-on-gcp-vm.md](docs/decisions/0002-host-on-gcp-vm.md)). Step 2 makes the generator emit every kind of mess in PLAN.md section 5. Step 3 gives each kind a handler and a test: dedup, schema dispatch, email pseudonymisation and a data-quality gate ([docs/decisions/step-3-log.md](docs/decisions/step-3-log.md)). The solution brief, written before any code, is [docs/brief.md](docs/brief.md). The design is [docs/PLAN.md](docs/PLAN.md).

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

- 21.0% of churn shows no signal beforehand (target 20 to 25%).
- 7.4% of accounts have fewer than 5 events.
- 10 planted "signals were sitting right there" cases.

```text
== t1 Northwind Cloud: 2,000 accounts, 83,264 events
   connected: gmail, product, salesforce, stripe, zendesk
   events per account p5/p50/p95: 2/36/95; fewer than 5 events: 7.1%
   source       events  replays  versions        lag hours p50/p95/p99
   salesforce    6,780   1.09%  v1:2764 v2:4016 4.3/19.9/68.9
   zendesk      20,097   0.92%  v1:2065 v2:18032 2.9/33.1/147.6
   product      32,061   0.94%  v1:32061        26.8/146.8/241.8
   stripe       10,919   1.06%  v1:10919        1.4/6.7/48.6
   gmail        13,407   0.92%  v1:13407        9.1/54.3/112.3
   zendesk snapshots re-sent unchanged: 2,982
   quarantine: 0.88% (ambiguous_domain 42, unknown_domain 263, unknown_native_key 427)
   renewals 2024: 122, lost 14 (11.5%), contracted 7, renewed 101
   renewals 2025: 666, lost 49 (7.4%), contracted 63, renewed 554
   renewals 2026: 1,144, lost 78 (6.8%), contracted 109, renewed 957

== t2 Harbor Software: 1,200 accounts, 45,220 events
   connected: product, salesforce, stripe, zendesk
   events per account p5/p50/p95: 2/33/84; fewer than 5 events: 7.7%
   source       events  replays  versions        lag hours p50/p95/p99
   salesforce    4,041   1.11%  v1:1687 v2:2354 4.2/19.9/53.8
   zendesk      11,786   0.87%  v1:1387 v2:10399 2.9/32.5/145.0
   product      19,216   0.95%  v1:19216        27.3/145.3/240.4
   stripe       10,177   1.14%  v1:10177        1.4/6.9/49.3
   zendesk snapshots re-sent unchanged: 1,683
   quarantine: 0.86% (unknown_native_key 391)
   renewals 2024: 78, lost 5 (6.4%), contracted 10, renewed 63
   renewals 2025: 417, lost 54 (12.9%), contracted 33, renewed 330
   renewals 2026: 682, lost 65 (9.5%), contracted 47, renewed 570

== t3 Lumen Retail Systems: 900 accounts, 31,243 events
   connected: gmail, product, salesforce, zendesk
   events per account p5/p50/p95: 2/29/83; fewer than 5 events: 7.2%
   source       events  replays  versions        lag hours p50/p95/p99
   salesforce    3,051   1.25%  v1:1256 v2:1795 4.2/19.7/50.2
   zendesk       8,315   0.82%  v1:990 v2:7325  2.9/21.8/142.8
   product      14,260   0.95%  v1:14260        26.9/148.3/247.7
   gmail         5,617   0.80%  v1:5617         8.9/53.2/115.4
   zendesk snapshots re-sent unchanged: 1,174
   quarantine: 0.42% (ambiguous_domain 47, unknown_domain 78, unknown_native_key 6)
   renewals 2024: 64, lost 8 (12.5%), contracted 6, renewed 50
   renewals 2025: 319, lost 36 (11.3%), contracted 30, renewed 253
   renewals 2026: 503, lost 57 (11.3%), contracted 33, renewed 413

== t4 Copper Desk: 600 accounts, 28,653 events
   connected: gmail, product, salesforce, stripe, zendesk
   events per account p5/p50/p95: 2/41/98; fewer than 5 events: 7.7%
   source       events  replays  versions        lag hours p50/p95/p99
   salesforce    2,015   0.79%  v1:886 v2:1129  4.2/21.3/60.3
   zendesk       3,746   1.04%  v2:3746         3.0/32.7/141.6
   product       9,218   0.93%  v1:9218         26.7/141.8/239.8
   stripe        9,919   1.18%  v1:9919         1.4/7.2/50.9
   gmail         3,755   1.04%  v1:3755         9.1/51.1/111.4
   zendesk snapshots re-sent unchanged: 560
   quarantine: 1.12% (ambiguous_domain 14, unknown_domain 83, unknown_native_key 225)
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

== all tenants: 5,000 accounts, 192,092 events
   salesforce   16,321    8.5%  (target 8%, off by 6%)
   zendesk      43,944   22.9%  (target 22%, off by 4%)
   product      76,121   39.6%  (target 38%, off by 4%)
   stripe       32,339   16.8%  (target 18%, off by 6%)
   gmail        23,367   12.2%  (target 12%, off by 1%)
```

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
