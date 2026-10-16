"""The world behind the events: customers, contracts, renewal outcomes and monthly health.

Everything an event later reports is decided here first. Each account has a latent health that
moves month to month. Health decides whether the account renews, contracts or churns at each
annual renewal, and (in events.py) how loud each source is. The pipeline never sees health: it
is written to data/truth/ and only evaluation code may read it.

One tenant is built from random streams keyed by (seed, tenant index, purpose), so adding a
tenant or changing one source's knobs never shifts the random numbers of another.
"""

from __future__ import annotations

import math
import string
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

import numpy as np

from app.config import Config, TenantConfig
from app.generator import timeutil as tu
from app.ids import stable_uuid

PURPOSE = {
    "structure": 0,
    "health": 1,
    "renewals": 2,
    "salesforce": 3,
    "zendesk": 4,
    "product": 5,
    "stripe": 6,
    "gmail": 7,
    "lag": 8,
}

FIRST = (
    "alex sam jordan taylor morgan casey riley jamie avery quinn drew blake rowan "
    "harper dana robin kai noel reese skyler emery parker devon sasha tatum lee"
).split()
LAST = (
    "rivera chen patel nguyen kim garcia okafor silva novak larsen haddad ito "
    "moreau costa singh weber ortiz lund bianchi petrov mensah dubois tanaka"
).split()
ADJECTIVE = (
    "bright north blue summit iron clear river silver maple cedar swift prime "
    "granite harbor lumen vertex orbit pine copper amber delta"
).split()
NOUN = (
    "works labs systems logistics analytics media devices foods energy robotics "
    "capital studios networks supply partners dynamics freight materials"
).split()
SUFFIX = ("Inc", "Ltd", "Group", "Co", "GmbH", "LLC")

BASE62 = string.digits + string.ascii_letters


def sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def stream(cfg: Config, tindex: int, purpose: str) -> np.random.Generator:
    return np.random.default_rng([cfg.seed, tindex, PURPOSE[purpose]])


def random_tokens(rng: np.random.Generator, alphabet: str, length: int, n: int) -> list[str]:
    chars = np.array(list(alphabet))
    picks = rng.integers(0, len(alphabet), size=(n, length))
    return ["".join(row) for row in chars[picks]]


def salesforce_id(prefix: str, body12: str) -> str:
    """An 18-character Salesforce id: 3-char object prefix, 12 random chars, 3-char checksum.

    The checksum is Salesforce's own: for each block of five characters, bit i is set when
    character i is upper case, and the 5-bit number picks a letter or digit.
    """
    id15 = prefix + body12
    suffix = ""
    for start in (0, 5, 10):
        bits = sum(1 << i for i, ch in enumerate(id15[start : start + 5]) if ch.isupper())
        suffix += "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"[bits]
    return id15 + suffix


@dataclass
class Csm:
    csm_id: UUID
    name: str
    email: str


@dataclass
class Contract:
    cycle: int
    start: datetime
    end: datetime  # the renewal date; the next cycle starts here
    arr: float


@dataclass
class Renewal:
    cycle: int
    date: datetime
    outcome: str  # renewed | contracted | churned
    prior_arr: float
    renewed_arr: float  # 0 when churned


@dataclass
class Account:
    index: int
    account_id: UUID
    name: str
    domain: str
    csm: int | None
    start: datetime
    salesforce_id: str
    stripe_id: str
    zendesk_org: str
    workspace_id: str
    contacts: list[str]
    contracts: list[Contract] = field(default_factory=list)
    renewals: list[Renewal] = field(default_factory=list)
    end_life: datetime | None = None  # churn date, or the horizon end for a live account


@dataclass
class World:
    cfg: Config
    tenant: TenantConfig
    tindex: int
    start: datetime  # first day this tenant has any history
    csms: list[Csm]
    accounts: list[Account]
    month_epoch: np.ndarray  # (M+1,) first second of each month, plus one past the last
    health: np.ndarray  # (n, M)
    unobserved: np.ndarray  # (n,) the per-account factor that no source shows
    activity: np.ndarray  # (n,) heavy-tailed multiplier on tickets and mail
    start_epoch: np.ndarray  # (n,)
    end_epoch: np.ndarray  # (n,)
    arr_month: np.ndarray  # (n, M) ARR of the contract in force that month

    @property
    def months(self) -> int:
        return len(self.month_epoch) - 1

    def month_label(self, m: int) -> str:
        return tu.from_epoch(int(self.month_epoch[m])).strftime("%Y-%m")


def _build_people(cfg, tcfg, tindex, rng, n_accounts):
    """CSMs and the structural identity of every account."""
    slug = tcfg.slug
    n_csm = max(1, math.ceil(n_accounts / tcfg.accounts_per_csm))
    csms, seen = [], set()
    for k in range(n_csm):
        first, last = FIRST[int(rng.integers(len(FIRST)))], LAST[int(rng.integers(len(LAST)))]
        email = f"{first}.{last}@{slug}.test"
        if email in seen:
            email = f"{first}.{last}{k + 1}@{slug}.test"
        seen.add(email)
        csms.append(
            Csm(
                stable_uuid(cfg.name, cfg.seed, "csm", slug, k),
                f"{first.title()} {last.title()}",
                email,
            )
        )
    return csms


def build_world(cfg: Config, tindex: int) -> World:
    tcfg = cfg.tenants[tindex]
    knobs = cfg.generator
    rng = stream(cfg, tindex, "structure")
    n = tcfg.accounts
    seg = knobs["segments"][tcfg.segment]

    tenant_start = max(cfg.horizon_start, tu.add_months(cfg.train_as_of, -tcfg.history_months))
    first_month = tu.month_floor(tenant_start)
    last_month = tu.month_floor(cfg.horizon_end)
    n_months = (last_month.year - first_month.year) * 12 + last_month.month - first_month.month + 1
    month_epoch = np.array(
        [tu.to_epoch(tu.add_months(first_month, m)) for m in range(n_months + 1)], dtype=np.int64
    )

    def month_index(dt: datetime) -> int:
        return (dt.year - first_month.year) * 12 + dt.month - first_month.month

    # ---- who: CSMs and accounts -------------------------------------------------------
    csms = _build_people(cfg, tcfg, tindex, rng, n)
    window_days = (cfg.train_as_of - timedelta(days=30) - tenant_start).days
    recent = rng.random(n) < 0.15  # a recent-cohort tail on top of an even spread
    offsets = np.where(
        recent,
        rng.integers(max(0, window_days - 120), window_days + 1, n),
        rng.integers(0, window_days + 1, n),
    )
    offsets.sort()  # account index follows start date, so ids read chronologically

    csm_of = np.arange(n) % len(csms)
    rng.shuffle(csm_of)
    unassigned = rng.random(n) < knobs["unassigned_rate"]

    adjectives = rng.integers(len(ADJECTIVE), size=n)
    nouns = rng.integers(len(NOUN), size=n)
    suffixes = rng.integers(len(SUFFIX), size=n)
    sf_bodies = random_tokens(rng, BASE62, 12, n)
    stripe_bodies = random_tokens(rng, BASE62, 14, n)
    workspace_bodies = random_tokens(rng, string.ascii_lowercase + string.digits, 12, n)
    zendesk_orgs = 360_000_000_000 + np.cumsum(rng.integers(1, 5000, n))  # strictly increasing
    first_names = np.stack([rng.permutation(len(FIRST))[:3] for _ in range(n)])
    last_names = rng.integers(len(LAST), size=(n, 3))

    accounts = []
    for i in range(n):
        domain = f"{tcfg.slug}-{i + 1}.example"
        company = (
            f"{ADJECTIVE[adjectives[i]].title()} {NOUN[nouns[i]].title()} {SUFFIX[suffixes[i]]}"
        )
        contacts = [
            f"{FIRST[first_names[i, j]]}.{LAST[last_names[i, j]]}@{domain}" for j in range(3)
        ]
        accounts.append(
            Account(
                index=i,
                account_id=stable_uuid(cfg.name, cfg.seed, "account", tcfg.slug, i),
                name=company,
                domain=domain,
                csm=None if unassigned[i] else int(csm_of[i]),
                start=tenant_start + timedelta(days=int(offsets[i])),
                salesforce_id=salesforce_id("001", sf_bodies[i]),
                stripe_id="cus_" + stripe_bodies[i],
                zendesk_org=str(zendesk_orgs[i]),
                workspace_id="ws_" + workspace_bodies[i],
                contacts=contacts,
            )
        )

    # ---- latent health: an AR(1) walk with a per-account drift and rare shocks --------
    hk = knobs["health"]
    hrng = stream(cfg, tindex, "health")
    health = np.empty((n, n_months))
    health[:, 0] = hrng.normal(0.0, hk["initial_sd"], n)
    drift = hrng.normal(0.0, hk["drift_sd"], n)
    for m in range(1, n_months):
        shock = (hrng.random(n) < hk["shock_prob"]) * -hrng.uniform(
            hk["shock_min"], hk["shock_max"], n
        )
        health[:, m] = (
            hk["phi"] * health[:, m - 1] + drift + hrng.normal(0.0, hk["noise_sd"], n) + shock
        )
    activity = hrng.lognormal(0.0, knobs["activity_sigma"], n)

    # ---- renewals: churn depends on health at renewal plus an unobserved factor -------
    rrng = stream(cfg, tindex, "renewals")
    ck = knobs["churn"]
    unobserved = rrng.normal(0.0, ck["unobserved_sd"], n)
    max_cycles = 6
    draws = rrng.random((n, max_cycles, 3))  # churn, contraction, size of the change
    arr0 = rrng.lognormal(math.log(seg["arr_median"]), seg["arr_sigma"], n)

    # Choose the churn intercept so the average churn probability over every renewal that
    # could happen equals the tenant's base annual churn. Bisection on the logit scale.
    pool_h, pool_u = [], []
    for acct in accounts:
        for k in range(1, max_cycles + 1):
            due = tu.add_months(acct.start, 12 * k)
            if due >= cfg.horizon_end:
                break
            pool_h.append(health[acct.index, max(0, min(n_months - 1, month_index(due) - 1))])
            pool_u.append(unobserved[acct.index])
    if pool_h:
        h_arr, u_arr = np.array(pool_h), np.array(pool_u)
        lo, hi = -20.0, 20.0
        for _ in range(60):
            mid = (lo + hi) / 2
            if sigmoid(mid + ck["health_coef"] * h_arr + u_arr).mean() < tcfg.base_churn:
                lo = mid
            else:
                hi = mid
        intercept = (lo + hi) / 2
    else:  # no observable renewals (a five-month tenant): fall back to the plain logit
        intercept = math.log(tcfg.base_churn / (1 - tcfg.base_churn))

    con, exp_ = knobs["contraction"], knobs["expansion"]
    for acct in accounts:
        i, arr, start, cycle = acct.index, round(float(arr0[acct.index]), -2), acct.start, 1
        while True:
            end = tu.add_months(start, 12)
            acct.contracts.append(Contract(cycle, start, end, arr))
            if end >= cfg.horizon_end or cycle >= max_cycles:
                acct.end_life = cfg.horizon_end  # the next decision is beyond the horizon
                break
            h = health[i, max(0, min(n_months - 1, month_index(end) - 1))]
            u_churn, u_contract, u_size = draws[i, cycle - 1]
            if u_churn < sigmoid(intercept + ck["health_coef"] * h + unobserved[i]):
                acct.renewals.append(Renewal(cycle, end, "churned", arr, 0.0))
                acct.end_life = end
                break
            if u_contract < sigmoid(con["intercept"] + con["health_coef"] * h):
                scale = con["scale_min"] + u_size * (con["scale_max"] - con["scale_min"])
                outcome = "contracted"
            else:
                scale = exp_["scale_min"] + u_size * (exp_["scale_max"] - exp_["scale_min"])
                outcome = "renewed"
            new_arr = round(arr * scale, -1)
            acct.renewals.append(Renewal(cycle, end, outcome, arr, new_arr))
            start, arr, cycle = end, new_arr, cycle + 1

    start_epoch = np.array([tu.to_epoch(a.start) for a in accounts], dtype=np.int64)
    end_epoch = np.array([tu.to_epoch(a.end_life) for a in accounts], dtype=np.int64)

    arr_month = np.zeros((n, n_months))
    for acct in accounts:
        for c in acct.contracts:
            s, e = tu.to_epoch(c.start), tu.to_epoch(c.end)
            in_force = (month_epoch[1:] > s) & (month_epoch[:-1] < e)
            arr_month[acct.index, in_force] = c.arr

    return World(
        cfg, tcfg, tindex, tenant_start, csms, accounts, month_epoch, health, unobserved,
        activity, start_epoch, end_epoch, arr_month,
    )  # fmt: skip
