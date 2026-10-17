# Loom script

Task 7.5. Target length: 10 to 12 minutes. Recording and submitting are the owner's (7.6, 7.7).

Record against the live site, https://136-83-131-193.sslip.io. Say things in your own words. The lines below are a guide, not a teleprompter.

## Before recording

**Check the Loom length limit first.** The free plan caps a video at 5 minutes. If the plan you use still has that cap, either upgrade for the month or cut to the four "must" segments marked below and link the deck for the rest.

**Wait for the soak (7.1).** Record only after `/runs` shows three nights nobody triggered. The worker started its schedule on 2026-10-17, so the earliest is after 02:00 UTC on 2026-10-19. No redeploy is needed for Step 7: it changed only docs.

**Never show secrets.**

- Do not open `.env`, the VM's `/opt/monday-list/.env`, the GCP console's VM metadata, or a shell history that holds the password.
- Log in to the site before you start recording, so the password prompt is never on screen.
- Close any terminal that printed `DATABASE_URL`, `SESSION_SECRET`, `PII_HASH_SECRET` or `BASIC_AUTH_PASSWORD`.

**Pre-run the four headline tests** in a second terminal, so the output is ready to show. They take about 2 minutes, and they all passed on 2026-10-17.

```bash
uv run --env-file .env pytest -v tests/test_point_in_time.py::test_late_event_leaves_snapshot_d_unchanged_and_changes_the_next tests/test_rls_isolation.py::test_tenant_context_hides_the_other_tenant_on_every_table tests/test_rls_isolation.py::test_no_tenant_context_sees_zero_rows_on_every_table tests/test_agentic.py::test_a_crash_part_way_then_a_rerun_leaves_one_clean_night tests/test_agentic.py::test_identical_scores_make_zero_new_tasks tests/test_agentic.py::test_an_oscillating_score_raises_at_most_one_task_per_three_nights
```

These are point in time, isolation (two tests), crash and rerun, and no flapping (two tests).

**Open these tabs:**

1. `/list`, with the switcher set to t1 and a CSM who has open tasks.
2. `/runs`.
3. `/health`.
4. The README's evaluation section on GitHub (or in an editor).
5. `docs/deck.pptx`, on slide 8.

**Pick the demo account the evening before.** Open the latest night's run page (`/runs/<id>`) for each tenant, and note an account that entered its CSM's top 8 with a task and a briefing. The first night under the policy (2026-10-18) seeds tasks for each CSM's current top 8, so there will be several. Prefer one whose top reason is a ticket or usage feature.

**A finding about the planted cases.** The plan said to check that a planted case enters a top 8 on demo night. It cannot. All ten planted cases renew between 2025-11-25 and 2026-10-02, inside the holdout year, so every one has already renewed (and been lost) before the live clock. The script shows them through the evaluation instead (segment 6), where 7 of 10 entered their CSM's top 8 at least 4 weeks before the notice deadline. Moving planted renewals after the demo date would mean regenerating the data and re-bootstrapping, which restarts the 3-day soak. That is the owner's call. The script does not need it.

**The live trigger (optional, segment 5b).** `POST /admin/jobs/nightly` runs a night as `manual`. Use it only for a night the scheduler has not run yet, and never with `--force` on a scheduled night: that overwrites `trigger: schedule`, which is the record that nobody pressed a button. A run's date is `as_of`'s date, so a manual run for today is a no-op (02:00 UTC already ran it). The only useful demo is t5 with `as_of` set to tomorrow, which means t5's scheduled run tomorrow becomes a no-op and t5 loses one unattended row. If you would rather keep every scheduled row, skip 5b and show the latest scheduled night instead. The script works either way.

## The script

Times are cumulative. "Must" marks the segments to keep if you have to cut to 5 minutes.

### 1. The problem, and what I built instead (0:00 to 1:15) · must

Show: deck slide 2, then the list.

- "The ask was a red, yellow, green score per account, and an email to the customer when it goes red."
- "The real problem is prioritisation. A CSM has about 120 accounts and can work about 8 properly in a week. The signals for the two lost renewals were in Zendesk and in product usage. Nobody joined them."
- "So I built a ranked Monday list per CSM, with reasons. Every number on it clicks back to the raw events. A nightly job runs it with nobody pressing a button."
- "I disagreed with three parts of the request: no auto-email, colour from rank instead of fixed bands, and rank by dollars at risk. I'll show each one."

### 2. The Monday list (1:15 to 3:00) · must

Show: `/list` for one CSM.

- The staleness header: "This list is from last night's run at 02:00 UTC."
- Red is the top 8 of this CSM's book, yellow the next 16, and grey means there is not enough signal. "Grey is never green."
- The columns: probability, ARR and expected ARR at risk. Re-sort once by probability, to show that a small account at high risk does not outrank a large account at moderate risk.
- The reason chips on one row: "These are the model's own contributions, not a hand-written rule."
- An escalated task pinned to the top, if there is one. "Untouched for 5 business days within 60 days of renewal. This replaces the auto-email: nothing goes unseen."
- The data-quality line: quarantined and rejected counts by reason.

### 3. One reason, down to the raw events (3:00 to 5:00) · must

Show: the demo account, then its score page, then one reason's events.

- On the account page: the score history and the tasks.
- Open the score page (`/scores/<id>`). Point at the reconciliation: "Expected value plus the sum of contributions equals the margin, and it's checked on every page."
- Point at the model version and the snapshot's ingest watermark: "This score was made by this model, from this snapshot, which saw events received up to this time."
- Click one reason, such as the ticket count, down to `/scores/<id>/events/<feature>`. "These are the actual events behind that number, with how each was matched to the account."
- If one is visible, point at a late-arrival marker: "This event arrived after the snapshot. It changes tonight's snapshot, never last week's."
- In a terminal, `python -m app rescore <score_id>` on the VM reproduces the stored score. Show it only if the terminal shows no secrets. Otherwise, say it and move on.

### 4. Multi-tenant (5:00 to 5:45)

Show: the switcher, then a URL edit.

- Switch to another organisation. The list changes completely.
- Paste the first tenant's account URL: "Another tenant's id answers 404. The database enforces it with forced row-level security. With no tenant set, a query sees zero rows."

### 5. The night that runs itself (5:45 to 7:30) · must

Show: `/runs`, then one run, then `/health`.

- "These rows say `trigger: schedule`. Nobody pressed anything. The first was 17 October at 02:00 UTC: all five tenants in 21 seconds."
- Open one run: the stages with their times (ingest, snapshot, score, gate, transitions, policy and tasks, escalation), the counts, the transitions, the tasks it opened and the briefing for the demo account.
- "If the data-quality gate trips, the night rolls back and publishes nothing, and the list keeps the last good night with a warning."
- "Rerunning a succeeded night writes nothing. A task needs three nights outside the top 8 before it can open again, so scores near the line don't flap."
- `/health`: the last run and its trigger.

**5b, optional: one night live on t5.** Only if you chose this the evening before. Run it from the VM, so the password never appears on screen, and point at the new `manual` row on `/runs`. "Same function as the scheduler, marked manual."

### 6. Does the model work? (7:30 to 9:30) · must

Show: the README's evaluation section, then deck slide 6.

- "This is a cycle-based temporal holdout: test renewals from October 2025 to October 2026, scored as of a year before."
- "LightGBM gets 0.600 ROC-AUC. It beats hand-written rules (0.547) and the calendar (0.500). It ties logistic regression: the difference is +0.009, with a 95% interval from −0.009 to +0.028. I kept LightGBM by a rule set in advance: switch only for a clear win."
- "The oracle, which knows the generator's hidden health, gets only 0.650. About a fifth of churn has no visible signal, by design. Recall@8 is 21.8% against the oracle's 23.6%, so most of what is learnable is learned."
- "It is not a calendar: days to renewal is 0.8% of the model."
- "There is no leakage: shuffled labels give 0.48."
- "Planted cases: 7 of 10 entered their CSM's top 8 at least 4 weeks before the notice deadline. For 9 of 10 the top reason was a ticket or usage signal. I reported that rather than tuning it away."
- "This validates the pipeline on data whose generator I know. It is not evidence about real churn."

### 7. The tests (9:30 to 10:15)

Show: the second terminal with the pre-run output.

- "Point in time: a late event leaves the old snapshot unchanged."
- "Isolation: every table, and no tenant context means zero rows."
- "A crash part-way, then a rerun, gives one clean night and no duplicate tasks."
- "No flapping: identical scores make zero new tasks, and an oscillating score makes at most one per three nights."
- "There are 231 tests in all."

### 8. What breaks first at 10x, and what's next (10:15 to 11:30) · must

Show: deck slide 8, then slide 9.

- "I measured from the live stage timings. Today a night takes 21 seconds."
- "At 10x events, the nightly run is still about 4 minutes. What breaks first is the bootstrap's weekly anchor build. It re-reads every event for every Monday, so one tenant takes about 40 minutes and peaks near a gigabyte. On a 2 GB host, it runs out of memory before it runs out of time."
- "The fix is incremental anchors from running aggregates. Then retention, and a per-tenant queue."
- "With real data, the first step is to refit on real renewals and report the same holdout tables."
- Close: "The repo, the brief I wrote before any code, and the decision log are linked in the README. Thanks."

## After recording

- Check that no frame shows a password, a `.env` or a connection string. Scrub the timeline at each terminal.
- Submission (7.7, owner): the repo link, the live URL with the password sent separately, `docs/deck.pptx` and the Loom link.
