# Step 7: review fixes before submission

Date: 2026-10-17. A review of the finished submission, read as a reviewer would, found the problems below. Each line says what was wrong and what changed.

## What the review found

- **The dashboard looked like a prototype.** It had about 20 lines of CSS and plain tables, and the list showed no reasons on the rows.
- **Several README claims were wrong or unbacked**, checked one by one against the code:
  - "The superuser is never used by the app": `migrate` uses it on every worker start.
  - "No code reads the clock except the scheduler": the web reads it for display and to date a snooze.
  - "The tenant never comes from a URL": the switcher reads one from the query string.
  - "Email addresses are HMAC-hashed": only customer emails in event payloads are; CSM emails are in clear.
  - The licence line missed PSF-2.0, Zlib, CC0, MPL-2.0 (test-only) and psycopg-binary.
  - "Today a night takes 21 s" was measured before the decision stages were deployed.
  - "157 Mondays" is 147, and "277 lost renewals" counts lost or contracted.
  - "Bootstrap takes about 2 minutes" is 4 for the full feature set.
- **The evaluation text overclaimed twice.**
  - It called the oracle a ceiling, but logistic regression's recall@8 (23.7%) beats the oracle's (23.6%). The oracle ignores the organisation-level churn rate the models learn, so it is a reference, not a ceiling.
  - It said LightGBM was live "because of a rule fixed in advance". The step 4 log says the margin was set after seeing the first validation run.
- **The days-to-renewal baseline was misdescribed.** Its ROC-AUC is 0.500 and its PR-AUC is the base rate, so its calibrated probability is flat. Multiplied by ARR, the row ranks by ARR. The text now says so.
- **No language model anywhere**, in a GTM AI take-home, with no explanation of why or where one would go.
- Seven of the 18 walkthrough questions in PLAN.md section 12 had no answer in the README.

## What changed

- **Dashboard.** The pages were rewritten on one stylesheet in `base.html`: a top bar, summary cards, risk pills, probability bars and reason chips on the list. The score page shows contribution bars. The account page has a score sparkline, and run pages show stage bars. Every string and id the tests check was kept.
- **Reason chips** use the briefing's rule: the largest positive contributions among the actionable features. Two display rules sit on top:
  - A chip needs a contribution of at least 0.025 log-odds. On the live model the signals a CSM can act on (open tickets, seats in use, active users, days since the customer wrote) push red accounts by 0.03 to 1.4. The rest (bounces, prior contraction, ticket pace) stay under 0.02.
  - A yes/no flag is a reason only when it is set.
  - Every contribution is still on the score page.
- **LLM summary** (`app/jobs/narrate.py`, migration 0005, `narrations` table). Off unless `LLM_API_KEY` is set.
  - It runs after a night has committed and been recorded, in its own transactions, so it cannot fail, delay or change a night.
  - It sends only the reason list and three numbers, with no names, emails or raw events.
  - Every number in the reply must appear in that input. A reply that fails is stored as rejected, with the reason, and is not shown.
  - Calls are capped per tenant per night, and a briefing is narrated once.
  - It uses the standard library for the one HTTPS call, so it adds no dependency. The default model is `claude-haiku-5-5`; `LLM_MODEL` changes it.
  - `python -m app narrate` narrates the latest night, for a night that ran before the key was set.
  - Tests use a fake client, and the test session clears `LLM_API_KEY`, so no test can call the API.
- **README.** It now starts with a two-minute summary for reviewers and screenshots. Every claim above was corrected, and the missing walkthrough answers were added.
- **[design-questions.md](../design-questions.md)** answers each walkthrough question, with evidence.
- **Deck.** Two slides with real screenshots were added. The oracle and model-choice wording was corrected, and the LLM row was updated.
- **Loom script.** It is now two parts of under five minutes each, for the free plan's limit, with the corrected lines.
- Stale "Railway" comments in `.env.example`, the Dockerfile and `app/db.py` were fixed.

## Not changed, on purpose

- **No monotone constraints.** The model sometimes treats a rise in usage as a small risk factor. Retraining now would change the evaluation and the live model three days before the demo. It is listed as a deliberate skip.
- **The data was not regenerated** to move the planted renewals after the demo date. That would need a new bootstrap and restart the three-night soak.
