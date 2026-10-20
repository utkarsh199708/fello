# Loom script

Task 7.5. Recording and submitting are the owner's (7.6, 7.7).

Record against the live site, https://136-83-131-193.sslip.io. Say things in your own words. The lines below are a guide, not a teleprompter.

The script is two parts of under five minutes each, so it fits Loom's free plan as two videos. On a paid plan, record both parts in one take, for about ten minutes, and drop the hand-over lines.

## Before recording

**Ready to record.** Four nights ran unattended (17 to 20 October), and the final deploy was on 2026-10-17. Do not redeploy before recording.

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

## The script, screen by screen

Each step says which screen to show, what to do on it, and what to say. Numbers in [square brackets] differ between organisations and nights: read them off the screen. Everything else is fixed and checked.

Part 1 is steps 1 to 6 (about 5 minutes). Part 2 is steps 7 to 12 (about 5 minutes). On a paid plan, record both in one take.

### Part 1: the product

**Step 1. Deck slide 1, the title (15 s)**
- Do: nothing; this is the opening frame.
- Say: "Hi, I'm Utkarsh. This is my take-home for Fello's GTM AI team. I built Monday list: every Monday, each customer success manager gets a short, ranked list of the accounts most worth working this week, with the reasons, and a nightly job keeps it current with nobody pressing a button."

**Step 2. Deck slide 2, "The ask was a health score; the problem is prioritisation" (45 s)**
- Say: "The request was a red, yellow, green score per account, and an email to the customer when one goes red. But the real problem is prioritisation. A CSM owns about 120 accounts and can properly work about 8 a week. The two renewals that were lost had their signs in Zendesk and product usage, and nobody joined them up. So I built a ranked list per CSM, with reasons, not a score per account."

**Step 3. Deck slide 6, "Where I disagreed with the request" (45 s)**
- Say: "I disagreed with three things, and wrote them in my brief before any code. No automatic email: it can reach the wrong person, because the contact on file is often the champion who left; it can say the wrong thing, because a support backlog and a failed payment need different conversations; and it fires on a noisy trigger. Instead, an account entering the top 8 gets a task, and a task left untouched near renewal is escalated. Second, colour comes from rank: the top 8 in each book is red. Third, the list is ranked by ARR at risk, probability times ARR, so a $2,000 account at 60% doesn't outrank a $200,000 account at 50%."

**Step 4. Live site: My list (1 min 30 s)**
- Do: switch to the browser on My list. Point at the sidebar, then the summary sentence, then the groups.
- Say: "This is what a CSM opens on Monday. The sidebar shows whose book this is, [organisation] and [CSM], and at the bottom, last night's run. One sentence sums up the week: [N] accounts to work, and [amount] of ARR at risk."
- Say: "The list is grouped by urgency: this week's top 8, then a watch list of the next 16, then everyone else folded away. Each row shows when it renews, the ARR, why it's here in plain words, the probability, and the ARR at risk."
- Say: "These tasks were raised on the 18th, so they're a day or two old. If a CSM leaves one untouched for 5 business days and the renewal is within 60 days, it's escalated and pinned to the top, in an Escalated group above this one. That's what replaces the automatic email: nothing goes unseen."
- Do: click Done on one row in "This week". Then open the "···" menu on another row.
- Say: "One action per row. Done closes the task. Behind this menu: snooze a week, dismiss with a reason, and the briefing explaining why the task was raised. Every click is written to an audit trail."

**Step 5. Live site: one score page (1 min 15 s)**
- Do: click the probability on a red row, preferably one whose reason is "Seats in use" or "Open tickets".
- Say: "Every number is traceable. This is why [account] scores [probability]. Each factor shows how much it pushes the risk up or down, largest first. These come from the model itself, not a rule I wrote."
- Do: click Raw events on the top factor. Scroll down to the events and the green reconciliation box.
- Say: "Raw events shows exactly which events that factor was computed from, and how each was matched to this account. And this check proves the contributions add up exactly to the model's output. The rescore command reproduces any stored score from a terminal."

**Step 6. Live site: the account page, then another organisation (45 s)**
- Do: click "Account timeline". Point at the score history, the task, the timeline.
- Say: "The account page has the score history, the task with its full audit trail, the contracts, and every raw event, including events that arrived late. A late event changes the next night's score, never an old one."
- Do: copy the address bar, click the organisation box in the sidebar, pick a different organisation and CSM, then paste the copied link.
- Say: "Five separate customer organisations share this system. If I open the first organisation's account while in another one, it answers not found. Postgres row-level security enforces that on every table, so a bug in a query can't leak another customer's data."
- End of Part 1: "In part 2: the job that runs itself, whether the model works, and what breaks at 10x."

### Part 2: proof and scale

**Step 7. Live site: Nightly runs (1 min)**
- Do: click Nightly runs.
- Say: "This is the agentic part. Every night at 2 a.m. UTC the worker runs the job by itself. Each of these rows says Scheduled: started by the clock, not by a person. The 17th, 18th, 19th and 20th of October all ran unattended for all five organisations."
- Say: "Each night ingests new events, scores every account, runs a data-quality gate, and only then raises and escalates tasks. If the gate trips, the night rolls back and publishes nothing, and the list keeps the last good night with a warning."

**Step 8. Live site: the night of 18 October (45 s)**
- Do: click 2026-10-18.
- Say: "The 18th was the first night under the task policy. For this organisation it raised [144] tasks by itself, one for each account in a CSM's top 8, using the seed rule; across all five organisations it was 371. Here are the stages with their timings, the accounts that moved in or out of a top 8, and every task with the rule that raised it. On the nights since, only one or two new tasks appear, because a task needs three nights outside the top 8 before it can open again, so scores near the line don't flap."

**Step 9. Deck slide 8, "Beats rules and the calendar; ties logistic regression" (1 min 15 s)**
- Say: "Does the model work? I held out a year of renewals. Each CSM's weekly top 8 caught 21.8% of the renewals that were then lost or contracted, against 14.8% for hand-written rules and 12.4% for a baseline from days to renewal."
- Say: "To be honest about it: LightGBM ties logistic regression, a difference of plus 0.009 with a confidence interval that crosses zero. The oracle, which knows the generator's hidden health, only reaches 0.65, because a fifth of churn has no visible signal by design. Shuffled labels score 0.48, so there's no leakage, and days to renewal is under 1% of the model, so it isn't just a calendar. All of this validates the pipeline on synthetic data, not real churn."

**Step 10. Terminal: the pre-run tests (30 s, optional)**
- Do: show the terminal with the test output already run. Check that no secret is on screen.
- Say: "These are the headline tests: a late event never changes an old snapshot; another organisation's rows are invisible on every table; a crash part-way, then a rerun, gives one clean night and no duplicate tasks; and identical or oscillating scores don't create a flood of tasks. There are 240 tests in all, and CI runs them on every push."

**Step 11. Deck slide 10, "What breaks first at 10x, measured" (1 min)**
- Say: "What breaks first at ten times the scale? I measured it from the live timings. The first unattended night took 21 seconds for all five organisations; now, with the task stages, the largest one takes about 14 seconds. At ten times the events, the nightly run is still about four minutes. What breaks first is the one-time bootstrap: it rebuilds training snapshots for every Monday by re-reading every event, so one organisation takes about 40 minutes and peaks near a gigabyte of memory. On this 2 GB server it runs out of memory before it runs out of time. The fix is to build those snapshots incrementally."

**Step 12. Deck slide 11, "With real data, the first four steps" (30 s)**
- Say: "With real data, the first step is to refit on actual renewals and report the same tables. Then use the CSMs' dismissal reasons as feedback. The brief I wrote before any code, the decision log, and the README are all in the repository. Thank you."

### Slides not to show in the Loom

Slides 3, 4, 5, 7 and 9 stay in the deck you send. Slides 4 and 5 are screenshots of pages you show live, and 3, 7 and 9 are for reading.

## After recording

- Check that no frame shows a password, a `.env` or a connection string. Scrub the timeline at each terminal.
- Submission (7.7, owner): the repo link, the live URL with the password sent separately, `docs/deck.pptx` and the Loom link or links.
