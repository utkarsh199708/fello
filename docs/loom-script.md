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

1. `/list`, with the switcher set to **Harbor Software** and **Avery Dubois**. The numbers below are read from that book on the live site after the night of 2026-10-20.
2. `/runs`.
3. The README's evaluation section on GitHub.
4. `docs/deck.pptx`.

**The demo account is Clear Foods Group**, rank 1 in Avery Dubois's book: 53.2%, top reason "Open tickets 12". If a later night changes any number, read it off the screen; the shape of each step stays the same.

**One question to be ready for.** Some rows give "Usage change +98%" or "+119%" as a reason, so a rise in usage counts as risk. Answer: "The reasons are what the model actually computed, not what I'd like it to say. I didn't add monotone constraints; with real data, I'd constrain features with a known direction, like usage." It is in the README as a deliberate skip.

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

Each step says which screen to show, what to do on it, and what to say. Every number is checked against the live site for Harbor Software · Avery Dubois, as of the night of 2026-10-20.

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
- Say: "This is what a CSM opens on Monday. The sidebar shows whose book this is, Harbor Software and Avery Dubois, a book of 111 accounts, and at the bottom, last night's run: succeeded, scores as of 2 a.m. UTC on the 20th. One sentence sums up the week: 8 accounts to work this week, with $144,309 of ARR at risk, and no task left waiting."
- Say: "The list is grouped by urgency: this week's top 8, then a watch list of the next 16, then the other accounts folded away. Each row shows when it renews, the ARR, why it's here in plain words, the probability, and the ARR at risk. Look at rows 1 and 7: Clear Foods at 53.2% on $69,110 is first, and Cedar Systems at 53.9% on $25,770 is seventh. Nearly the same probability, but ranking by ARR at risk puts the bigger renewal first."
- Say: "These tasks were raised on the 18th, so they're a day old. If a CSM leaves one untouched for 5 business days and the renewal is within 60 days, it's escalated and pinned above this group. That's what replaces the automatic email: nothing goes unseen."
- Do: open the "···" menu on row 8, Swift Analytics LLC, which renews in 1 day. Then click Done on it.
- Say: "One action per row. Behind this menu: snooze a week, dismiss with a reason, and the briefing explaining why the task was raised. Done closes the task. Every click is written to an audit trail."

**Step 5. Live site: Clear Foods Group's score page (1 min 15 s)**
- Do: click 53.2% on row 1, Clear Foods Group.
- Say: "Every number is traceable. This is why Clear Foods Group scores 53.2%: rank 1 in this book, $69,110 ARR, renewing in 159 days, built from 75 events across 3 sources in the last 90 days. Each factor shows how much it pushes the risk up or down, largest first. 12 open tickets push it up most, then seats in use at 70%. These come from the model itself, not a rule I wrote. The 30 small factors are folded away."
- Do: click Raw events on Open tickets. Scroll through the events, then back to the reconciliation line.
- Say: "Raw events shows the 46 Zendesk events that factor was computed from, each with when it happened, when it arrived, and how it was matched to this account. The requester's email is hashed. And this check proves the contributions add up exactly to the model's output. The rescore command reproduces any stored score from a terminal."

**Step 6. Live site: the account page, then another organisation (45 s)**
- Do: click "Account timeline". Point at the score history, the task, the contracts, the timeline.
- Say: "The account page has the score history: rank 1 at 53.2% for five nights. The task, open, raised on the 18th by the seed rule, with its audit trail. Both contract cycles, $70,000 then $69,110. And every raw event, newest first, with when it arrived. A late event changes the next night's score, never an old one."
- Do: copy the address bar, click the organisation box in the sidebar, pick Northwind Cloud and any CSM, then paste the copied link.
- Say: "Five separate customer organisations share this system. Here's Harbor Software's account opened while I'm in Northwind Cloud: not found. Postgres row-level security enforces that on every table, so a bug in a query can't leak another customer's data."
- Do: switch back to Harbor Software and Avery Dubois.
- End of Part 1: "In part 2: the job that runs itself, whether the model works, and what breaks at 10x."

### Part 2: proof and scale

**Step 7. Live site: Nightly runs (1 min)**
- Do: click Nightly runs.
- Say: "This is the agentic part. Every night at 2 a.m. UTC the worker runs the job by itself. Four unattended nights, the 17th to the 20th, each marked Scheduled: started by the clock, not by a person. The row on the 12th is the one-time bootstrap. Last night took 4.8 seconds for this organisation, and the next run is already scheduled for 2 a.m. on the 21st."
- Say: "Each night ingests new events, scores every account, runs a data-quality gate, and only then raises and escalates tasks. If the gate trips, the night rolls back and publishes nothing, and the list keeps the last good night with a warning."

**Step 8. Live site: the night of 18 October (45 s)**
- Do: click 2026-10-18.
- Say: "The 18th was the first night under the task policy. For Harbor Software it raised 88 tasks by itself, one for each account in a CSM's top 8, using the seed rule; across all five organisations it was 371. Here are the stages with their timings, in milliseconds, the transitions, and every task with the rule that raised it and its status now. Back on the runs page: 2 new tasks on the 19th and none on the 20th, because a task needs three nights outside the top 8 before it can open again, so scores near the line don't flap."

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
