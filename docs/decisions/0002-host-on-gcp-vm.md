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

## Step 4 redeploy and the first unattended night, 2026-10-17 (UTC)

Commit `44e031b` was redeployed with fresh volumes at 23:12 UTC on 2026-10-16. Feature set fs4 and the new model roles need a fresh bootstrap. It finished at 23:35, and the live model is `lightgbm-73976df6c317`. That is the same version hash the local Windows run produced from the same config, so training is reproducible across Windows and the Linux image.

At 02:00 UTC on 2026-10-17 the scheduler fired the nightly job with nobody involved (`trigger: schedule`). All five tenants succeeded in 21 s. The night took in the events received since the bootstrap night: 422, 205, 139, 161 and 72 for t1 to t5. It quarantined 11, 3, 1, 1 and 2, and rejected 12 replays. The gate's mean-probability shift against the bootstrap night was at most 0.0015. Public `/health` shows that night as the last run. This is milestone M1's "a row nobody triggered".

## Step 5 redeploy, 2026-10-17 (UTC)

Commit `c9a9ae4` was deployed at 06:28 UTC, with the volumes kept. Migration 0004 only adds tables and columns, so the bootstrap night and the unattended 02:00 night are untouched, and the worker found both already succeeded. The first night under policy version 1 is 2026-10-18 at 02:00 UTC. It seeds a task for every top-8 account and writes the first transitions and briefings, with nobody involved. The admin trigger is not used on the live system for a night the scheduler owns: a forced re-run would replace that night's `trigger: schedule`.

## Step 6 redeploy, 2026-10-17 (UTC)

Commit `dd1188d` was deployed at 07:32 UTC, with the volumes kept and no migration. The live dashboard has the re-sortable list, the task buttons behind CSRF, the run history, quarantine and worst-misses pages, and the staleness header. With the shared password the switcher answers 200, and every tenant page redirects to it until a tenant and CSM are chosen. Without the password every page except `/health` answers 401.

## Affects

TASKS.md items 1.15, 2.15 and 7.1 now mean "redeploy to the VM" rather than Railway. D2 (spend) is approved at about $21 a month, and D3 (project and Postgres) is done.

## Review-fixes redeploy, 2026-10-17 (UTC)

Commit `4d10c56` was deployed at 10:02 UTC, with the volumes kept. The worker ran migration 0005 (the `narrations` table, with forced row-level security), found the bootstrap night already succeeded, and armed the 02:00 UTC schedule. The unattended night of 2026-10-17 is still recorded with `trigger: schedule`. The new dashboard is live. The LLM summary stays off: the VM's `.env` has no `LLM_API_KEY`.

## Penpot restyle redeploy, 2026-10-17 (UTC)

Commit `7575550` was deployed at 10:24 UTC, with the volumes kept. It changes only the templates and adds the vendored Work Sans fonts. The scheduler is armed for 02:00 UTC, and the night history is unchanged.
