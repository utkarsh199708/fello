# 0002. Host on one GCP VM instead of Railway

Date: 2026-10-17. Status: accepted. Supersedes the Railway choice in PLAN.md section 4 and decisions D2 and D3 in TASKS.md.

## Decision

The live system runs on one Compute Engine VM in GCP project `fello-monday-list`:

- e2-small (2 shared vCPU, 2 GB RAM, plus 2 GB swap), Debian 12, zone `asia-south1-a`, 20 GB pd-balanced disk.
- Static external IP `136.83.131.193`. The site is `https://136-83-131-193.sslip.io`. sslip.io resolves that name to the IP, so Caddy gets a Let's Encrypt certificate without a domain of our own.
- The same `compose.yaml` that runs locally, plus `deploy/compose.gcp.yaml`: db, web, worker and Caddy. Only ports 80 and 443 are open for the app. Postgres listens on 127.0.0.1 inside the VM.
- `deploy/vm-setup.sh` installs Docker, unpacks a `git archive` of a local commit into `/opt/monday-list`, writes `.env` with secrets generated on the VM, and runs `docker compose up -d --build`. Redeploying means copying a new archive and running the script again. `.env` is kept.

## Why

- The worker must stay awake overnight, which is the "runs without a human" non-negotiable. An always-on VM does that for about $21 a month. On Cloud Run, an always-on worker would cost $40 or more a month. A Cloud Scheduler job would mean redesigning the worker.
- It runs exactly the stack that was tested locally, so the first deploy has the fewest new moving parts.
- Secrets never pass through the repo, chat or a CI system. They are generated on the VM, and the shared password is read there with `sudo grep BASIC_AUTH_PASSWORD /opt/monday-list/.env`.

## Costs and trade-offs

- About $15 for the VM, $2.40 for the disk and $3.65 for the static IP, each per month.
- Postgres is self-managed on the VM's disk. There are no automated backups yet. The data is regenerable from the seed, so this is acceptable for the exercise.
- Google's default `default-allow-ssh` rule leaves port 22 open to the internet. Access still needs the SSH key gcloud created. Restricting it is a follow-up.
- Deploys come from local commits, not from GitHub, so nothing has to be pushed to deploy.

## First deploy, 2026-10-16 (UTC)

The first deploy was commit `9a4d20b`. The first bootstrap on the e2-small took about 14.5 minutes. Locally it takes about 2 minutes.

| Stage | Time |
|---|---|
| generate | 45 s |
| load | 39 s |
| resolve | 120 s |
| anchors | 499 s |
| train | 113 s |
| first night | 36 s |

Weekly anchor snapshots are the slowest stage by far, which supports the first hypothesis in PLAN.md section 11. Memory peaked at about 1.2 GB used, with under 200 MB of swap. No container restarted or was killed for running out of memory. Afterwards the scheduler armed the nightly job at `0 2 * * *` UTC.

## Redeploy with Step 2 data and Step 3 handlers, 2026-10-16 (UTC)

Commit `0f94757`. The database and generated-data volumes were dropped and re-created, because the worker bootstraps only into an empty database. The new feature set (fs3) and generator need a fresh bootstrap anyway. The only rows lost were the five bootstrap `job_runs` rows of the first deploy, before any unattended night had run. `vm-setup.sh` added the keys new to `.env.example` and generated a fresh `PII_HASH_SECRET`.

The bootstrap finished at 21:52 UTC. Anchors took 457 s and training 110 s. Afterwards:

- 193,963 raw rows were loaded.
- 170,348 events were normalised.
- 1,426 events were quarantined: 948 `unknown_native_key`, 387 `unknown_domain` and 91 `ambiguous_domain`.
- 1,668 replays were rejected as `duplicate_event_id`.
- The model is `logreg-73c27dc9e5c3`.
- The bootstrap night succeeded for all five tenants, and the gate passed.

The scheduler is armed at `0 2 * * *` UTC, so the first unattended night is 2026-10-17 02:00 UTC.

## Affects

TASKS.md items 1.15, 2.15 and 7.1 now mean "redeploy to the VM" rather than Railway. D2 (spend) is approved at about $21 a month, and D3 (project and Postgres) is done.
