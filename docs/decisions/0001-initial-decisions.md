# 0001. Initial decisions (Step 0)

Date: 2026-10-16. Status: accepted.

This folder is the running decision log. Each file is numbered and records one decision or one batch. Entries are not rewritten. A later entry supersedes an earlier one and says which. The design lives in [../PLAN.md](../PLAN.md). This log records what the build actually chose, including the small choices the plan does not mention.

## Taken from the plan

- The product decisions argued in [../brief.md](../brief.md): a ranked list per CSM instead of a health score, no auto-email, colour derived from rank, ranking by expected ARR at risk, and the next renewal outcome as the label.
- The stack in PLAN.md section 4: Python 3.11, FastAPI with Jinja2 and vendored HTMX, Postgres 16 with row-level security, SQLAlchemy 2 Core and Alembic, LightGBM, APScheduler 3 in a separate worker process, Railway Hobby.

## Made while scaffolding

| Decision | Why |
|---|---|
| uv, with `uv.lock` committed. Python is limited to 3.11 (`>=3.11,<3.12`). | One resolution for Windows, CI and the Linux image. The lock covers every platform, and the image runs 3.11. |
| The project is not built as a wheel (`tool.uv.package = false`). Code runs from the checkout. pytest gets the repo root on its path. | It is an application, not a library. No build backend is fetched, unpinned, at image build time. |
| Every process goes through one CLI: `python -m app web`, `python -m app worker`, and the commands named in PLAN.md. Step 1 implements them. | Compose and Railway then differ only by command. The web command reads `PORT`, which Railway sets. |
| The base image is pinned by digest. The uv binary is copied from a digest-pinned uv image of the version that wrote the lock. | Rebuilds are reproducible, and a base image update is a deliberate commit. |
| Compose and CI use the `postgres:16` tag, not a digest. | Patch releases flow in. Production uses Railway's managed Postgres, so a digest would not match production anyway. |
| The image runs as a non-root user. Only `/srv/data` is writable. | The generator writes `data/` at run time. Nothing else needs write access. |
| `/data/` is anchored to the repo root in `.gitignore`. | An unanchored `data/` would also hide any future package directory named `data`. |
| `.gitattributes` forces LF. `static/htmx.min.js` is stored as binary and `.editorconfig` leaves it alone. | Regeneration must be byte-identical on Windows and Linux (PLAN.md section 5). The vendored file must keep its published hash. |
| Compose binds Postgres and web to 127.0.0.1. | The local password is trivial. Nothing on the network should reach it. |
| `DATABASE_URL` uses the `postgresql+psycopg://` scheme. | SQLAlchemy would otherwise choose psycopg2, which is not installed. Railway issues `postgresql://` URLs, so Step 1 must normalise the scheme at startup. |
| GitHub Actions are pinned by commit SHA. CI runs `uv sync --locked`, `ruff check`, `ruff format --check` and pytest against a Postgres 16 service. | Supply chain, and a stale lock fails the build instead of drifting. |
| A smoke test imports the package and checks the database answers as Postgres 16. It skips when `DATABASE_URL` is unset. | pytest fails when it collects no tests, and the CI database wiring should be proven before Step 1 depends on it. |
| gitleaks runs as a pre-commit hook, pinned by commit SHA. CI does not repeat the scan yet. | It stops a key before the commit exists. A CI scan is a few lines once the GitHub repository exists (D4). |
| htmx 2.0.11, the latest 2.x release, is vendored in `static/`. | The live page loads no third-party script. Version, licence and hash are in the README. |
