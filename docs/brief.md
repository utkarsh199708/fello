# Solution brief: a Monday list for CSMs

## The actual problem

The request is for a health score. The problem is prioritisation. Each CSM owns about 120 accounts and can properly work perhaps 8 a week. The warning signs already sit in Zendesk, product usage, Stripe, Gmail and Salesforce, but nobody joins them. The two lost renewals had the data. Nobody read it. Part of the mess is identity: five systems, five different keys for one customer.

So the Monday question is not "how healthy is each account?" It is "which accounts do I work this week, and why?"

## What I will build, and what comes first

A ranked Monday list for each CSM: the accounts most worth working this week, in order, each with its reasons. Every reason clicks through to the raw events behind it.

- A model fitted on past renewals ranks the accounts. An LLM, if used, only words the model's reasons.
- A nightly job runs unattended. It rescores every account, opens a task for each new top-8 entry, and logs every run.
- Several customer organisations share the system, and the database keeps each one to its own accounts.

First, a thin version of the whole chain goes live by day two, on full-scale generated data. Each later step deepens it.

The test of success: replay past weeks. Did each lost renewal reach its CSM's top 8 before the notice deadline, and how early?

## Where I disagree with the request

**1. No auto-email to customers.**

- Wrong recipient. A departing champion is one of the signals, so the contact on file is often the person who left.
- Wrong message. A ticket backlog, falling usage and a failed payment need different conversations. A generic check-in is tone-deaf.
- Wrong trigger. Red is a line on a noisy score, so accounts near it flip week to week. A task can be undone. An email cannot.

Instead, every account entering a CSM's top 8 gets a task. An untouched task on an account renewing within 60 days escalates after 5 business days and is pinned to the top. Nothing goes unseen, and nothing is dropped silently. No system can make a CSM act. Neither could an email. Stripe keeps its payment reminders, and this system sends no email.

**2. Red, yellow and green are a display, derived from rank.** In each CSM's book, red is the top 8, yellow the next 16 and green the rest. Accounts with almost no history are grey, marked "not enough signal", never green. So every CSM always has 8 red: a week's work, by design. Probability and dollars at risk carry the absolute picture, because fixed probability bands drift with every retrain.

**3. Rank by ARR at risk, not by probability.** Lost renewals are lost revenue. By probability alone, a $2k account at 60% outranks a $200k account at 50%. The default sort is probability times contract ARR, with one-click re-sort.

**The label, stated up front.** The model predicts the outcome of the next renewal, whenever it falls. A "churn within 90 days" label would mark every account further out as safe, which kills early warning. The cost is that predictions ten months out sit near the base rate. The evaluation shows this by days to renewal.

## What I need from the business

Each default is one setting.

| Question | Default until answered |
|---|---|
| What counts as churn: logo loss, contraction, downgrade? | Renewal lost, or renewed below 70% of prior ARR |
| Which two renewals were lost, and when did signals first show? | Ten planted look-alikes. The backtest reports how early each surfaced |
| Contract terms and notice period? | Annual terms, 30-day notice, 90-day urgency window |
| How many accounts can a CSM work per week? | 8 |
| How soon must a flagged account near renewal be touched? | 5 business days, then escalated |
| Which organisations, with which sources connected? | Five. Three each lack one source |
| Which system owns account identity? | Salesforce. Stripe and Zendesk carry its Account Id. Gmail matches by email domain |
| Rank by dollars at risk or by probability? | Dollars at risk, with re-sort |
| What legal basis covers reading mailboxes and requester emails? | Metadata only, no bodies. Addresses hashed on arrival |
| Does each organisation set its own list size? | Yes, in configuration |
