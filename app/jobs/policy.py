"""The nightly job's decisions: which accounts get a task tonight (docs/PLAN.md section 7).

Pure. Inputs are plain values read by ``app.jobs.actions``, and the output is a list of decisions
it applies, so every rule here is tested without a database.

Rules (``POLICY_VERSION = "1"``):

- **Trigger.** An account in its book's top ``k`` tonight that was outside the top ``k`` on each
  of the previous ``ANTI_FLAP_RUNS`` completed nights is a ``top_k_entry``. Fewer prior nights
  count as outside. A "hot" account dropping out for one night and coming back is not a new
  entry, so a score oscillating around the boundary raises at most one task per three nights.
  Separately, a probability that has risen by more than ``RISE`` since the account's last task
  is a ``probability_rise``.
- **Seed.** On the bootstrap night, on a rebaseline and on the first night run under this
  policy version, every top-``k`` account without an active task gets one (``seed``). Without
  this, accounts already red at launch, or after a model change, would never be tasked and never
  escalate.
- **Dedup.** No new task while one is open, snoozed or escalated for the account. Tonight's
  reasons are appended to that task instead.
- **Dismissal.** A dismissed task suppresses re-tasking until the probability rises by more
  than ``RISE`` over its value at dismissal.
- **Budget.** At most ``k`` new tasks per queue per night, highest ``rank_score`` first. Each
  CSM is a queue, and accounts with no owner go to the CS-lead queue. Anything over budget is
  recorded as ``budget_exceeded``, not tasked.
- **Rebaseline.** On the first night after the model version or the feature set changes, ranks
  are not comparable with the night before. No transition-based task is raised, and the seed
  rule runs instead.
- **Not enough signal.** Accounts with too little evidence are never tasked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

POLICY_VERSION = "1"
ANTI_FLAP_RUNS = 3
RISE = 0.15
ACTIVE = frozenset({"open", "snoozed", "escalated"})


@dataclass(frozen=True)
class Candidate:
    """One scored account tonight."""

    account_id: UUID
    csm_id: UUID | None  # None: no owner, the CS-lead queue
    score_id: UUID
    rank: int | None  # None: not enough signal
    probability: float
    rank_score: float
    k: int


@dataclass(frozen=True)
class History:
    """What earlier nights and tasks say about one account."""

    top_k_previous: tuple[bool, ...] = ()  # newest first, one per completed night
    active_task_id: UUID | None = None  # an open, snoozed or escalated task
    last_task_probability: float | None = None  # probability when its latest task was raised
    dismissed_probability: float | None = None  # set when its latest task was dismissed


@dataclass(frozen=True)
class Decision:
    kind: str  # create | append | budget_exceeded
    account_id: UUID
    rule: str  # seed | top_k_entry | probability_rise
    queue: str  # csm | cs_lead
    task_id: UUID | None = None  # for append: the task the reasons go to


@dataclass
class Plan:
    decisions: list[Decision] = field(default_factory=list)

    def of_kind(self, kind: str) -> list[Decision]:
        return [d for d in self.decisions if d.kind == kind]


def rule_for(c: Candidate, h: History, seed: bool) -> str | None:
    """The rule that fires for one account tonight, or None."""
    if c.rank is None:
        return None
    in_top = c.rank <= c.k
    if seed:
        return "seed" if in_top else None
    previous = (h.top_k_previous + (False,) * ANTI_FLAP_RUNS)[:ANTI_FLAP_RUNS]
    if in_top and not any(previous):
        return "top_k_entry"
    if h.last_task_probability is not None and c.probability > h.last_task_probability + RISE:
        return "probability_rise"
    return None


def decide(
    candidates: list[Candidate], history: dict[UUID, History], *, seed: bool = False
) -> Plan:
    """Tonight's decisions for one tenant.

    ``seed`` is true on the bootstrap night and on a rebaseline.
    """
    plan = Plan()
    wanted: list[tuple[Candidate, str]] = []
    for c in candidates:
        h = history.get(c.account_id, History())
        rule = rule_for(c, h, seed)
        if rule is None:
            continue
        if h.active_task_id is not None:  # dedup: tonight's reasons go to the task it has
            queue = "cs_lead" if c.csm_id is None else "csm"
            plan.decisions.append(Decision("append", c.account_id, rule, queue, h.active_task_id))
            continue
        if h.dismissed_probability is not None and c.probability <= h.dismissed_probability + RISE:
            continue  # dismissed, and not much worse since
        wanted.append((c, rule))

    # Budget: at most k new tasks per queue tonight, the most ARR at risk first.
    used: dict[UUID | None, int] = {}
    for c, rule in sorted(wanted, key=lambda w: (-w[0].rank_score, str(w[0].account_id))):
        queue = "cs_lead" if c.csm_id is None else "csm"
        n = used.get(c.csm_id, 0)
        kind = "create" if n < c.k else "budget_exceeded"
        used[c.csm_id] = n + 1
        plan.decisions.append(Decision(kind, c.account_id, rule, queue))
    return plan
