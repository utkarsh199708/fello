# Loom script

Task 7.5. Recording and submitting are the owner's (7.6, 7.7).

Record against the live site, https://136-83-131-193.sslip.io. Say things in your own words. The lines below are a guide, not a teleprompter.

The script is two parts of under five minutes each, so it fits Loom's free plan as two videos. On a paid plan, record both parts in one take, for about ten minutes, and drop the hand-over lines.

## Before recording

**Record on or after 2026-10-20.** That gives three nights nobody triggered (17, 18 and 19 October) and three days since the final deploy on 2026-10-17. Do not redeploy before recording.

**Never show secrets.**

- Do not open `.env`, the VM's `/opt/monday-list/.env`, the GCP console's VM metadata, or a shell history that holds the password.
- Log in to the site before you start recording, so the password prompt is never on screen.
- Close any terminal that printed `DATABASE_URL`, `SESSION_SECRET`, `PII_HASH_SECRET`, `BASIC_AUTH_PASSWORD` or `LLM_API_KEY`.

**Pre-run the four headline tests** in a second terminal, so the output is ready to show. They take about 2 minutes.

```bash
uv run --env-file .env pytest -v tests/test_point_in_time.py::test_late_event_leaves_snapshot_d_unchanged_and_changes_the_next tests/test_rls_isolation.py::test_tenant_context_hides_the_other_tenant_on_every_table tests/test_rls_isolation.py::test_no_tenant_context_sees_zero_rows_on_every_table tests/test_agentic.py::test_a_crash_part_way_then_a_rerun_leaves_one_clean_night tests/test_agentic.py::test_identical_scores_make_zero_new_tasks tests/test_agentic.py::test_an_oscillating_score_raises_at_most_one_task_per_three_nights
```

These are point in time, isolation (two tests), crash and rerun, and no flapping (two tests).

**Open these tabs:**

1. `/list`, with the switcher set to Northwind Cloud and a CSM with an escalated or open task.
2. `/runs`.
3. The README's evaluation section on GitHub.
4. `docs/deck.pptx`.

**Pick the demo account the evening before.** On the list, choose a red account whose top reason chip is a ticket or usage signal, for example "Open tickets" or "Seats in use". Note its score page.

**The LLM summary (optional).** It shows only if an Anthropic API key is in the VM's `.env`. On the VM, put it on the `LLM_API_KEY=` line yourself (never paste it into a chat or a file in the repo), then run these two commands from `/opt/monday-list`. The first restarts the worker with the key; the second adds the summary to the latest night's briefings. Without a key, segment 5 says it is off, which is true and fine.

```bash
sudo docker compose -f compose.yaml -f deploy/compose.gcp.yaml up -d worker
```

```bash
sudo docker compose -f compose.yaml -f deploy/compose.gcp.yaml exec worker python -m app narrate
```

**The planted cases.** They cannot appear live: all ten renewed between 2025-11-25 and 2026-10-02, before the live clock. They are shown through the evaluation (segment 6).

**No live trigger.** The admin trigger would take a night the scheduler owns, and its no-op rule means a run for today does nothing. The script shows the scheduled nights instead.

## Part 1: the product (under 5 minutes)

### 1. The problem, and what I built instead (0:00 to 0:50)

Show: deck slide 2.

- "The ask was a red, yellow, green score per account, and an email to the customer when it goes red."
- "The real problem is prioritisation. A CSM has about 120 accounts and can work about 8 properly in a week. The signals for the two lost renewals were in Zendesk and in product usage. Nobody joined them."
- "So I built a ranked Monday list per CSM, with the reasons, and a nightly job that runs it with nobody pressing a button. I pushed back on three parts of the request, and I'll show what replaced each one."

### 2. The Monday list (0:50 to 2:20)

Show: `/list`.

- The four cards: "This CSM has 8 accounts to work this week, and this much ARR at risk across them."
- "Red is the top 8 by expected ARR at risk: probability times ARR. A $2k account at 60% should not outrank a $200k account at 50%." Re-sort once by probability to show the difference, then back.
- The reason chips: "These are the model's own top factors for this account, with their values. Not a rule I wrote."
- An escalated task at the top: "Untouched for 5 business days, close to renewal, so it is escalated and pinned. This replaced the automatic email. A task to a CSM is reversible; an email to a customer is not."
- Grey accounts: "Not enough signal. Grey is never green."

### 3. One account, down to the raw events (2:20 to 4:00)

Show: the demo account's score page, then one factor's raw events.

- "What moves this score, largest push towards risk first. Red bars raise the risk, green bars lower it; the small factors fold away below."
- The reconciliation box: "The contributions add up to the model's output, checked on the page. `rescore` with this id reproduces the score from a terminal."
- The snapshot line: "This score saw events received up to this time, from this model."
- Click "raw events" on the top factor: "These are the actual events behind that number, and how each one was matched to the account."
- If an "arrived late" marker is on the account page: "This arrived after the snapshot. It changes tonight's score, never last week's."

### 4. Five organisations, one database (4:00 to 4:40)

Show: "Switch organisation or CSM", then a pasted URL.

- Switch to another organisation: the list changes completely.
- Paste the first organisation's score URL: "404. Row-level security is forced on every table, and with no organisation set a query sees nothing."
- Hand-over: "Part 2 covers the nightly job, whether the model works, and what breaks at 10x."

## Part 2: proof and scale (under 5 minutes)

### 5. The night that runs itself (0:00 to 1:30)

Show: `/runs`, then the latest night.

- "Previously: a ranked list per CSM, with every number traceable. This is what keeps it fresh."
- "These rows say `schedule`. Nobody pressed anything. The first was 17 October at 02:00 UTC."
- On a night: the stage bars (ingest, snapshot, score, gate, transitions, policy and tasks, escalation), the transitions and the tasks it raised.
- "If the data-quality gate trips, the night rolls back and the list keeps the last good night, with a warning. Rerunning a succeeded night writes nothing. A task needs three nights outside the top 8 before it can open again, so scores near the line don't flap."
- "There is no LLM in the ranking. When a key is set, an LLM writes a two-sentence summary after the night, from the reason list only, and any number it invents gets the summary rejected." If the key is set, open the "···" menu on a row and show it.

### 6. Does the model work? (1:30 to 3:10)

Show: deck slide 8, then the README's evaluation table.

- "This is a temporal holdout: test renewals from October 2025 to October 2026."
- "Each CSM's weekly top 8 held 21.8% of the renewals that were then lost or contracted. Hand-written rules get 14.8%, and days to renewal 12.4%."
- "LightGBM and logistic regression tie: a +0.009 ROC-AUC difference, with a 95% interval from −0.009 to +0.028. I set the selection margin after seeing them this close, and the log says so. Either is defensible."
- "The oracle, which knows the generator's hidden health, reaches only 0.650 ROC-AUC. A fifth of churn has no signal, by design. So the bar is low, and the models are level with the oracle on recall."
- "No leakage: shuffled labels give 0.48. And it is not a calendar: days to renewal is under 1% of the model."
- "Planted cases: 7 of 10 reached their CSM's top 8 at least 4 weeks before the notice deadline."
- "All of this validates the pipeline on data I generated. It is not evidence about real churn."

### 7. The tests (3:10 to 3:40)

Show: the second terminal with the pre-run output.

- "Point in time, isolation on every table, a crash then a rerun giving one clean night, and no flapping. 240 tests in all."

### 8. What breaks first at 10x (3:40 to 4:50)

Show: deck slide 10, then slide 11.

- "I measured from the live stage timings: the whole night took 21 seconds."
- "At 10x events, the nightly run is still about 4 minutes. What breaks first is the bootstrap's weekly anchor build: it re-reads every event for every Monday, so one organisation takes about 40 minutes and peaks near a gigabyte. On this 2 GB host it runs out of memory before time."
- "The fix is incremental anchors from running aggregates. With real data, the first step is to refit on real renewals and report the same tables."
- Close: "The repo, the brief I wrote before any code, and a page of design questions with answers are linked in the README. Thanks."

## After recording

- Check that no frame shows a password, a `.env` or a connection string. Scrub the timeline at each terminal.
- Submission (7.7, owner): the repo link, the live URL with the password sent separately, `docs/deck.pptx` and the Loom link or links.
