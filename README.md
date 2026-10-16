# Monday list

A ranked Monday list for customer success managers: the accounts most worth working this week, in order, with reasons that trace back to raw events. Built for the Fello GTM AI take-home.

Status: Step 1 of [docs/TASKS.md](docs/TASKS.md) is built and tested locally (walking skeleton: generate, load, resolve, features, train, score, nightly job, worker, web). Deploy is next. The solution brief, written before any code, is [docs/brief.md](docs/brief.md). The design is [docs/PLAN.md](docs/PLAN.md).

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

Run the whole system on the full dataset (5 tenants, 5,000 accounts, about 194,000 events):

```bash
uv run --env-file .env python -m app bootstrap --config config/full.yaml   # about 2 minutes: migrate, generate, load, resolve, anchors, train, first night
uv run --env-file .env python -m app nightly --as-of 2026-10-13T02:00:00Z  # one more night, a few seconds; run again and it is a no-op
uv run --env-file .env python -m app web                                   # http://127.0.0.1:8000, password BASIC_AUTH_PASSWORD from .env, username ignored
```

Or run everything in containers: `docker compose up -d` starts db, web and worker. The worker bootstraps on first start (`BOOTSTRAP_CONFIG`) and then fires the nightly job on `NIGHTLY_CRON` (UTC); set `NIGHTLY_CRON="*/2 * * * *"` in `.env` to watch it fire. `curl localhost:8000/health` shows the model version and the newest job run.

Tests that need Postgres read `TEST_DATABASE_URL` (default `.../fello_test`), refuse any database not named `fello_test*`, and drop and recreate it each session. Give each parallel worker its own, for example `.../fello_test_qa`. Without a reachable Postgres they skip.

Secret scanning runs as a gitleaks pre-commit hook. Set it up once per clone with `uv tool install pre-commit`, then `pre-commit install`.

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
