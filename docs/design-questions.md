# Design questions, answered

The questions a reviewer is likely to ask in the walkthrough, each with a short answer and where the evidence is. The list is from [PLAN.md](PLAN.md) section 12, written before the build. The answers are from what was built.

## The model

**1. Is the model just a renewal calendar?**
No. On its own, days to renewal scores a ROC-AUC of 0.500: it says nothing about whether a renewal will be lost. Inside the model it carries 0.8% of the contributions and is the top reason on under 0.05% of test rows. The model's largest gain feature is ARR, which stands in for segment, because smaller accounts churn more in this data. On a red account the reasons that move the score most are open tickets and seats in use.
Evidence: README "Evaluation"; [step-4-log.md](decisions/step-4-log.md).

**2. Why does the model beat your rules baseline, given you wrote both?**
Because the model learns combinations and thresholds the four hand-written signals do not have. I wrote the generator and the rules, so this shows the pipeline learns what is there. It does not show the model would beat a CSM's judgement. The stronger checks are the oracle reference (ROC-AUC 0.650 from the generator's hidden health), the planted cases (7 of 10 in time), and the leakage probes.
Evidence: README "Evaluation".

**3. LightGBM and logistic regression tie. Why ship LightGBM?**
They are within noise: the ROC-AUC difference is +0.009, with a 95% interval of -0.009 to +0.028. I set the selection margin after the first validation run showed them this close, and the log says so. Either model is defensible. LightGBM keeps a missing source as missing, where logistic regression fills it with the average.
Evidence: [step-4-log.md](decisions/step-4-log.md), "Model choice, honestly".

**4. Why one model across organisations, and why is `tenant_id` not a feature?**
The small organisations cannot support a model of their own: one has 300 accounts and no renewal outcome yet. With `tenant_id` out, an account is scored by what its users and tickets do, not by whose account it is, and no CSM ever reads "your organisation" as a reason. Per-organisation metrics are reported as slices, not corrected.
Evidence: PLAN.md section 6; README "Evaluation", slices.

**5. How do you know the model is not reading the generator's answer?**
The generator's truth sits in `data/truth/`, and a test fails if any pipeline code reads it. A model trained on shuffled labels scores 0.475. AUC rises gently toward the renewal date, with no jump toward 1.0. And the oracle itself only reaches 0.650.
Evidence: `tests/test_generator_mess.py`; README "Leakage probes".

**6. Why is a Stripe payment failure a signal and not the label?**
A failed mid-term payment is something a CSM can act on before the renewal, so it is a feature. The renewal invoice failing for good is how a renewal is lost, so it is part of the label. Every outcome event type is listed once in `OUTCOME_EVENT_TYPES`, which the labeler reads and every feature deny-lists. A test feeds all three outcome types to the feature code and checks nothing changes.
Evidence: `app/vocab.py`; `tests/test_features.py`.

## Time and data

**7. An event dated 30 days ago arrives tonight. Which snapshots change, and does the training set change?**
Tonight's snapshot counts it; last week's never does, because a snapshot at `as_of` only sees events received by `as_of`. Training anchors follow the same rule, so no past training row changes either. The event enters training only through anchors built after it arrived. The account page marks it "arrived late".
Evidence: `tests/test_point_in_time.py`.

**8. Why no daily snapshot table?**
Features are computed for any `as_of` by one function, the same code at training and serving. With about 40 events per account over three years, a daily table would be millions of mostly identical rows. Snapshots are stored per night and per weekly training anchor.
Evidence: PLAN.md section 4.

**9. Why monthly product-usage rollups?**
5,000 accounts over three years is about 5.5 million account-days, so daily usage rows cannot fit a budget of about 200,000 events. A monthly rollup per active account does. At 10x events the next step is weekly rollups.
Evidence: PLAN.md section 5.

**10. What does the pipeline do with a Zendesk ticket it cannot map to an account?**
It quarantines it with a reason (`unknown_native_key`, `unknown_domain` or `ambiguous_domain`) and what was tried. The ticket never reaches a score, and the count is on the list page and the data-quality page.
Evidence: README "What the pipeline does with the mess"; `/quarantine`.

**11. What happens to the live system when the synthetic data runs out?**
The generated events keep arriving until 2026-12-14. After that the nights keep running and rescoring, on no new events. A renewal that passes its date without an outcome event keeps the account on its current cycle, and nothing flags it; that is listed as a deliberate skip.
Evidence: `config/full.yaml` (`horizon_end`); README "What I deliberately skipped".

## The unattended job

**12. The job crashed halfway on Sunday. What does the CSM see on Monday, and what do you run?**
The night's work is one transaction, so nothing of it is published. The list keeps the last good night's scores under a visible "may be out of date" banner. The next scheduled night runs as usual. To recover the failed night, run `python -m app nightly --as-of <that night>`, which re-claims its row. A test crashes the job part-way and checks the rerun gives one clean night and no duplicate tasks.
Evidence: `tests/test_agentic.py`; `tests/test_dashboard.py`.

**13. What does "transition" mean, if thresholds drift?**
It is rank-based: an account entering its CSM's top 8, compared with the night before. A task needs the account to have been outside the top 8 on each of the previous three nights, so a score oscillating at the boundary raises at most one task per three nights. Every task stores the rule that fired.
Evidence: `app/jobs/policy.py`; `tests/test_agentic.py` (identical scores give zero tasks; an oscillating score at most one per three nights).

**14. What guarantees nothing slips now?**
Every account is scored every night. Every entry into a top 8 becomes a task, at most 8 new tasks per CSM per night. An open task untouched for 5 business days within 60 days of renewal is escalated and pinned to the top. Everything is in `job_runs`, `transitions` and `task_events`. Three gaps, stated: accounts with not enough signal are never tasked, escalation applies only within 60 days of renewal, and nothing guarantees a person acts. An automatic email would not either.
Evidence: README "What I decided"; `tests/test_agentic.py`.

**15. Is a nightly cron job agentic?**
The exercise defines the agentic part as running without a human. Beyond that, it decides with a written policy (budget, dedup, dismissal suppression, escalation), acts, and leaves an audit trail of every decision, including the ones that wrote no task.
Evidence: `app/jobs/policy.py`; the run pages.

**16. Where is the LLM in the ranking path?**
Nowhere. The ranking is the fitted model, and the briefing is a template over its reason list. An optional LLM writes a two-sentence summary on top, after the night has committed. It is sent only the reason list and three numbers, every number in its reply must appear in that input, and what it was sent is stored on the narration row. It is off until an API key is set.
Evidence: `app/jobs/narrate.py`; `tests/test_narrate.py`.

## Multi-tenancy and the request

**17. How does the organisation reach Postgres, and what happens if it is unset?**
The organisation comes from a signed session cookie. `tenant_tx` sets `app.tenant_id` for one transaction with `set_config(..., true)`, and every tenant table has a forced row-level security policy on it. With the setting unset or empty, a query sees zero rows and an insert is refused. Another organisation's id answers 404.
Evidence: `app/db.py`; `tests/test_rls_isolation.py`.

**18. Why not the automatic email?**
Wrong recipient (the contact on file is often the champion who left), wrong message (a ticket backlog and a failed payment need different conversations), and wrong trigger (scores near a line flip week to week). A task to a CSM is reversible; an email to a customer is not.
Evidence: [brief.md](brief.md); README "What I decided".

**19. What would you build next with real data?**
Refit and re-baseline on actual renewal outcomes and report the same holdout tables. Use CSM dismissal reasons as labels and as a precision signal for the size of the list. Add identity match confidence. Then build training anchors incrementally, which is what breaks first at 10x.
Evidence: README "What breaks first at 10x"; deck slide 9.
