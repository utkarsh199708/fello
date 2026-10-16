# Monday list

A ranked Monday list for customer success managers: the accounts most worth working this week, in order, with reasons that trace back to raw events. Built for the Fello GTM AI take-home.

Status: Step 0 of [docs/TASKS.md](docs/TASKS.md). The repo is scaffolded and nothing runs yet. The solution brief, written before any code, is [docs/brief.md](docs/brief.md). The design is [docs/PLAN.md](docs/PLAN.md).

## What I decided

To be written in Step 7. Until then the decisions and their reasons are in [docs/PLAN.md](docs/PLAN.md) sections 1 and 4 to 8, and each decision taken during the build is logged in [docs/decisions/](docs/decisions/).

## What I deliberately skipped

To be written in Step 7. The starting list is [docs/PLAN.md](docs/PLAN.md) section 10.

## What breaks first at 10x

To be written in Step 7, from measured stage timings. The hypothesis it will confirm or contradict is [docs/PLAN.md](docs/PLAN.md) section 11.

## Local development

Needs uv 0.12.17 or newer and Docker.

```bash
uv sync                    # .venv with Python 3.11 and the locked dependencies
docker compose up -d db    # Postgres 16 on 127.0.0.1:5432, data in a named volume
uv run ruff check .
uv run pytest
```

Copy `.env.example` to `.env` for local settings. `.env` is gitignored.

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
