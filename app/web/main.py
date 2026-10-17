"""The web app: switcher, ranked Monday list, score drill-down, account page and ``/health``.

Run with ``python -m app web``. Importing this module loads FastAPI and the light
``feature_spec`` only, never pandas or scikit-learn.

Who is "logged in" is a signed session cookie (Starlette ``SessionMiddleware``, SameSite=Strict,
HttpOnly) holding ``tenant_id`` and ``csm_id``. The switcher page is the only place that reads a
tenant id from a request, and all it does is validate it and write the cookie. Every other route
takes the tenant from the cookie and reads through ``tenant_tx``, so row-level security means an
id belonging to another tenant finds nothing and answers 404.

Every form POST carries a CSRF token bound to the session (task 6.4), compared in constant
time. The one JSON endpoint, the admin trigger, is CSRF-safe by content type instead.
"""

from __future__ import annotations

import os
import secrets
import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.db import get_engine, tenant_tx
from app.ml.feature_spec import (
    FEATURE_EVENTS,
    FEATURE_LABELS,
    FEATURE_SOURCE,
    FEATURES,
    PERCENT_FEATURES,
    REASON_FEATURES,
    SHORT_LABELS,
)
from app.web import queries as q
from app.web.auth import make_basic_auth, required_password

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE.parent.parent / "static"


def _fmt_money(value: Any) -> str:
    return "" if value is None else f"{float(value):,.0f}"


def _fmt_pct(value: Any) -> str:
    return "" if value is None else f"{float(value) * 100:.1f}%"


def _fmt_num(value: Any) -> str:
    """Features may be NULL (source not connected), which is not the same as 0."""
    if value is None:
        return "n/a"
    return f"{float(value):,.3f}".rstrip("0").rstrip(".")


def missing_reason(feature: str, values: dict[str, Any]) -> str | None:
    """Why a feature is NULL, in words. None when it has a value.

    NULL never means zero (task 3.3): a source the tenant has not connected is named as such, so
    the page never reads "0 tickets" for an organisation without Zendesk.
    """
    if values.get(feature) is not None:
        return None
    source = FEATURE_SOURCE.get(feature)
    if source is not None and values.get(f"{source}_connected") == 0:
        return f"{source.title()} not connected"
    return "no data yet"


def _fmt_ts(value: Any) -> str:
    return "" if value is None else value.strftime("%Y-%m-%d %H:%M UTC")


def _short_value(feature: str, value: Any) -> str:
    """A reason's value as a chip shows it: percentages as such, flags as yes or no."""
    if feature in PERCENT_FEATURES:
        sign = "+" if feature.startswith("usage_change") else ""
        return f"{float(value) * 100:{sign}.0f}%"
    if feature in FLAG_FEATURES:
        return "yes" if value else "no"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.1f}"
    return f"{float(value):,.0f}"


# A chip must move the log-odds by at least this much. On the live model the signals a CSM can act
# on (open tickets, seats in use, active users, days since the customer wrote) push red accounts by
# 0.03 to 1.4, while the rest stay under 0.02. Smaller pushes are real but too small to be a reason
# to call; they stay visible on the score page, with every other contribution.
CHIP_MIN_CONTRIBUTION = 0.025
FLAG_FEATURES = frozenset({"unrecovered_failure_90d", "prior_contraction"})


def reason_chips(
    contributions: dict[str, float], values: dict[str, Any], n: int = 3
) -> list[dict[str, Any]]:
    """The ``n`` actionable features pushing this score up most, for the chips on the list.

    The same rule as the nightly briefing (``app.jobs.actions.reasons_of``): positive
    contributions among ``REASON_FEATURES``, largest first. Two display rules on top: a chip needs
    ``CHIP_MIN_CONTRIBUTION``, and a yes/no flag is a reason only when it is set. A NULL value says
    why it is NULL.
    """
    values = values or {}
    up = sorted(
        (
            (f, c)
            for f, c in (contributions or {}).items()
            if c >= CHIP_MIN_CONTRIBUTION
            and f in REASON_FEATURES
            and not (f in FLAG_FEATURES and not values.get(f))
        ),
        key=lambda fc: -fc[1],
    )
    chips = []
    for feature, _ in up[:n]:
        missing = missing_reason(feature, values)
        chips.append(
            {
                "label": SHORT_LABELS.get(feature, feature),
                "shown": missing or _short_value(feature, values[feature]),
                "title": FEATURE_LABELS.get(feature, feature),
                "missing": missing is not None,
            }
        )
    return chips


def book_summary(rows: list[dict[str, Any]], thin: list[dict[str, Any]]) -> dict[str, Any]:
    """The cards above the list: this week's work and the money behind it."""
    red = [r for r in rows if r["colour"] == "red"]
    book = rows + thin
    return {
        "book": len(rows) + len(thin),
        "max_probability": max((float(r["probability"] or 0) for r in rows), default=0.0) or 1.0,
        "red": len(red),
        "k": rows[0]["k"] if rows else 8,
        "red_at_risk": sum(float(r["rank_score"] or 0) for r in red),
        "book_at_risk": sum(float(r["rank_score"] or 0) for r in rows),
        "tasks": sum(1 for r in book if r["task_id"] is not None),
        "escalated": sum(1 for r in book if r["task_status"] == "escalated"),
        "renewing_60": sum(
            1 for r in book if r["days_to_renewal"] is not None and r["days_to_renewal"] <= 60
        ),
        "thin": len(thin),
    }


def sparkline(scores: list[dict[str, Any]], width: int = 320, height: int = 64) -> str | None:
    """SVG polyline points for an account's probability over its scored nights, oldest first."""
    values = [float(s["probability"]) for s in reversed(scores) if s["probability"] is not None]
    if len(values) < 2:
        return None
    lo, hi = min(values), max(values)
    step = width / (len(values) - 1)

    def y(v: float) -> float:  # a flat history is drawn across the middle
        return height / 2 if hi - lo < 1e-9 else height - 6 - (v - lo) / (hi - lo) * (height - 12)

    return " ".join(f"{i * step:.1f},{y(v):.1f}" for i, v in enumerate(values))


def now() -> datetime:
    """The clock the pages use: ``CLOCK_OVERRIDE`` when set, like the job."""
    override = os.environ.get("CLOCK_OVERRIDE", "").strip()
    if override:
        from app.config import parse_utc

        return parse_utc(override)
    return datetime.now(UTC)


def business_days(start: date, end: date) -> int:
    """Weekdays in ``[start, end)``: a task's age as the escalation rule counts it.

    Plain Python, so the web process never imports numpy (the job uses ``np.busday_count``, which
    gives the same count).
    """
    if end <= start:
        return 0
    weeks, rest = divmod((end - start).days, 7)
    extra = sum((start + timedelta(days=i)).weekday() < 5 for i in range(rest))
    return weeks * 5 + extra


def next_fire() -> datetime | None:
    """The next time the worker's ``NIGHTLY_CRON`` fires, in UTC."""
    from apscheduler.triggers.cron import CronTrigger

    cron = os.environ.get("NIGHTLY_CRON", "").strip() or "0 2 * * *"
    try:
        trigger = CronTrigger.from_crontab(cron, timezone=UTC)
    except ValueError:
        return None
    return trigger.get_next_fire_time(None, now())


def create_app() -> FastAPI:
    """Build the app. Fails at startup if ``BASIC_AUTH_PASSWORD`` or ``SESSION_SECRET`` is unset."""
    password = required_password()
    secret = os.environ.get("SESSION_SECRET", "")
    if not secret:
        raise RuntimeError("SESSION_SECRET is not set")

    app = FastAPI(title="Monday list", docs_url=None, redoc_url=None, openapi_url=None)
    # Added first, so it runs inside the session middleware (order is last-added = outermost).
    app.middleware("http")(make_basic_auth(password))
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        same_site="strict",
        https_only=os.environ.get("COOKIE_SECURE", "0") == "1",
        max_age=14 * 24 * 3600,
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters.update(money=_fmt_money, pct=_fmt_pct, num=_fmt_num, ts=_fmt_ts)

    def csrf_token(request: Request) -> str:
        """The session's CSRF token, created on first use."""
        token = request.session.get("csrf")
        if not token:
            token = secrets.token_urlsafe(32)
            request.session["csrf"] = token
        return token

    def require_csrf(request: Request, token: str | None) -> None:
        expected = request.session.get("csrf")
        if not expected or not token or not secrets.compare_digest(expected, token):
            raise HTTPException(403, "missing or wrong CSRF token")

    def page(request: Request, name: str, context: dict, tenant_id=None) -> Response:
        """Render with the CSRF token and, inside a tenant, the staleness header."""
        context = {"csrf": csrf_token(request), **context}
        if tenant_id is not None:
            with tenant_tx(tenant_id) as conn:
                context["staleness"] = q.staleness(conn, now())
        return templates.TemplateResponse(request, name, context)

    def selected(request: Request) -> tuple[uuid.UUID, uuid.UUID | str] | None:
        """The (tenant, csm) in the cookie, or None if there is none or it is malformed.

        The csm is a UUID, or the string ``unassigned`` for the virtual owner of ownerless accounts.
        """
        try:
            tenant_id = uuid.UUID(request.session["tenant_id"])
            raw = request.session["csm_id"]
            return tenant_id, (raw if raw == q.UNASSIGNED else uuid.UUID(raw))
        except (KeyError, ValueError, TypeError):
            return None

    def to_switcher() -> Response:
        return RedirectResponse("/switch", status_code=303)

    @app.get("/")
    def home() -> Response:
        return RedirectResponse("/list", status_code=303)

    @app.get("/switch", response_class=HTMLResponse)
    def switch_page(request: Request, tenant_id: str | None = None) -> Response:
        """Pick a tenant, then (reloading with ``?tenant_id=``) one of its CSMs."""
        with get_engine().connect() as conn:  # tenants has no RLS
            tenants = q.list_tenants(conn)
        csms: list[dict[str, Any]] = []
        chosen = None
        if tenant_id:
            try:
                chosen = uuid.UUID(tenant_id)
            except ValueError:
                raise HTTPException(404) from None
            if chosen not in {t["tenant_id"] for t in tenants}:
                raise HTTPException(404)
            with tenant_tx(chosen) as conn:
                csms = q.list_csms(conn)
        return page(request, "switch.html", {"tenants": tenants, "chosen": chosen, "csms": csms})

    @app.post("/switch")
    def switch_submit(
        request: Request,
        tenant_id: Annotated[uuid.UUID, Form()],
        csm_id: Annotated[str, Form()],
        csrf: Annotated[str | None, Form()] = None,
    ) -> Response:
        """Validate the pair inside the tenant's transaction, then write the cookie."""
        require_csrf(request, csrf)
        with get_engine().connect() as conn:
            known = q.tenant_exists(conn, tenant_id)
        if not known:
            raise HTTPException(404)
        if csm_id != q.UNASSIGNED:
            try:
                csm_uuid = uuid.UUID(csm_id)
            except ValueError:
                raise HTTPException(404) from None
            with tenant_tx(tenant_id) as conn:
                csm = q.get_csm(conn, csm_uuid)
            if csm is None:
                raise HTTPException(404)
        request.session["tenant_id"] = str(tenant_id)
        request.session["csm_id"] = str(csm_id)
        return RedirectResponse("/list", status_code=303)

    @app.get("/list", response_class=HTMLResponse)
    def ranked(request: Request, sort: str = "rank_score") -> Response:
        ids = selected(request)
        if ids is None:
            return to_switcher()
        tenant_id, csm_id = ids
        sort = sort if sort in q.SORTS else "rank_score"
        with tenant_tx(tenant_id) as conn:
            tenant = q.tenant_name(conn)
            if csm_id == q.UNASSIGNED:
                csm = {"csm_id": None, "name": "Unassigned", "email": ""}
            else:
                csm = q.get_csm(conn, csm_id)
            as_of = q.latest_as_of(conn)
            rows = q.ranked_list(conn, csm_id, as_of, sort) if as_of and csm else []
            quality = q.data_quality(conn)
        if csm is None:  # the cookie names a CSM this tenant does not have
            return to_switcher()
        today = now().date()
        for r in rows:
            r["marker"] = q.marker(r)
            r["reasons"] = reason_chips(r["contributions"], r["feature_values"])
            if r["task_run_date"] is not None:
                r["task_age"] = business_days(r["task_run_date"], today)
        ranked_rows = [r for r in rows if r["evidence_state"] != "insufficient"]
        thin = [r for r in rows if r["evidence_state"] == "insufficient"]
        return page(
            request,
            "list.html",
            {
                "tenant": tenant,
                "csm": csm,
                "as_of": as_of,
                "rows": ranked_rows,
                "thin": thin,
                "summary": book_summary(ranked_rows, thin),
                "quality": quality,
                "sort": sort,
                "sorts": list(q.SORTS),
            },
            tenant_id,
        )

    @app.post("/tasks/{task_id}/status")
    def task_status(
        request: Request,
        task_id: uuid.UUID,
        status: Annotated[str, Form()],
        csrf: Annotated[str | None, Form()] = None,
        reason: Annotated[str | None, Form()] = None,
    ) -> Response:
        """A CSM's button: done, snooze a week, or dismiss with a one-line reason."""
        require_csrf(request, csrf)
        ids = selected(request)
        if ids is None:
            return to_switcher()
        reason = (reason or "").strip()[:200] or None
        if status == "dismissed" and reason is None:
            raise HTTPException(400, "a dismissal needs a reason")
        if status not in q.TASK_ACTIONS:
            raise HTTPException(400, "unknown status")
        with tenant_tx(ids[0]) as conn:
            changed = q.set_task_status(conn, task_id, status, reason, str(ids[1]))
        if not changed:
            raise HTTPException(404)
        return RedirectResponse("/list", status_code=303)

    @app.get("/scores/{score_id}", response_class=HTMLResponse)
    def score_page(request: Request, score_id: uuid.UUID) -> Response:
        ids = selected(request)
        if ids is None:
            return to_switcher()
        with tenant_tx(ids[0]) as conn:
            score = q.get_score(conn, score_id)
        if score is None:
            raise HTTPException(404)
        contributions = score["contributions"]
        values = score["feature_values"]
        rows = [
            {
                "name": f,
                "label": FEATURE_LABELS.get(f, f.replace("_", " ")),
                "value": values.get(f),
                "missing": missing_reason(f, values),
                "contribution": contributions.get(f, 0.0),
                "has_events": FEATURE_EVENTS[f] is not None,
                "actionable": f in REASON_FEATURES,
            }
            for f in FEATURES
        ]
        total = score["expected_value"] + sum(r["contribution"] for r in rows)
        reconciles = abs(total - score["margin"]) < 1e-6
        # Largest push towards risk first, then what lowers it; the bar is relative to the largest.
        rows.sort(key=lambda r: -r["contribution"])
        widest = max((abs(r["contribution"]) for r in rows), default=0.0) or 1.0
        for r in rows:
            r["width"] = round(abs(r["contribution"]) / widest * 100, 1)
        return page(
            request,
            "score.html",
            {"score": score, "rows": rows, "total": total, "reconciles": reconciles},
        )

    @app.get("/scores/{score_id}/events/{feature}", response_class=HTMLResponse)
    def score_events(request: Request, score_id: uuid.UUID, feature: str) -> Response:
        """HTMX partial: the raw events that fed one feature of one score."""
        ids = selected(request)
        if ids is None:
            return to_switcher()
        with tenant_tx(ids[0]) as conn:
            score = q.get_score(conn, score_id)
            found = q.feature_events(conn, score, feature) if score else None
        if found is None:
            raise HTTPException(404)
        events, total = found
        return templates.TemplateResponse(
            request,
            "events.html",
            {
                "feature": feature,
                "events": events,
                "total": total,
                "limit": q.EVENT_LIMIT,
                "from_events": FEATURE_EVENTS[feature] is not None,
            },
        )

    @app.get("/accounts/{account_id}", response_class=HTMLResponse)
    def account_page(request: Request, account_id: uuid.UUID) -> Response:
        ids = selected(request)
        if ids is None:
            return to_switcher()
        with tenant_tx(ids[0]) as conn:
            account = q.get_account(conn, account_id)
            if account is None:
                raise HTTPException(404)
            scores = q.account_scores(conn, account_id)
            latest = q.latest_as_of(conn)
            contracts = q.account_contracts(conn, account_id, latest) if latest else []
            timeline = q.account_timeline(conn, account_id, latest) if latest else []
            tasks = q.account_tasks(conn, account_id)
        return page(
            request,
            "account.html",
            {
                "account": account,
                "contracts": contracts,
                "scores": scores,
                "timeline": timeline,
                "tasks": tasks,
                "as_of": latest,
                "spark": sparkline(scores),
            },
            ids[0],
        )

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request) -> Response:
        """The last 30 nights and the next scheduled fire (task 6.7)."""
        ids = selected(request)
        if ids is None:
            return to_switcher()
        with tenant_tx(ids[0]) as conn:
            runs = q.run_history(conn)
        return page(request, "runs.html", {"runs": runs, "next_fire": next_fire()}, ids[0])

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: uuid.UUID) -> Response:
        ids = selected(request)
        if ids is None:
            return to_switcher()
        with tenant_tx(ids[0]) as conn:
            run = q.get_run(conn, run_id)
        if run is None:
            raise HTTPException(404)
        return page(request, "run.html", {"run": run}, ids[0])

    @app.get("/quarantine", response_class=HTMLResponse)
    def quarantine_page(request: Request) -> Response:
        """What the pipeline could not match or read, by reason and source (task 6.8)."""
        ids = selected(request)
        if ids is None:
            return to_switcher()
        with tenant_tx(ids[0]) as conn:
            summary = q.quarantine_summary(conn)
            quality = q.data_quality(conn)
        return page(request, "quarantine.html", {"summary": summary, "quality": quality}, ids[0])

    @app.get("/misses", response_class=HTMLResponse)
    def misses_page(request: Request) -> Response:
        """Lost renewals that were scored but never reached their CSM's top k (task 6.8)."""
        ids = selected(request)
        if ids is None:
            return to_switcher()
        with tenant_tx(ids[0]) as conn:
            misses = q.worst_misses(conn)
        return page(request, "misses.html", {"misses": misses}, ids[0])

    @app.post("/admin/jobs/nightly")
    async def admin_nightly(request: Request) -> JSONResponse:
        """Run the nightly job now, for a demo or a missed night (task 5.7). Behind basic auth.

        Body: JSON ``{"as_of": "<ISO 8601>", "tenant": "<slug or uuid>"}``, both optional; the
        default is "now" (``CLOCK_OVERRIDE`` honoured) and every tenant. It calls the same
        ``run_nightly`` as the scheduler, with ``trigger="manual"``, so the run is in job_runs
        like any other. JSON only: an HTML form cannot send it cross-site without a CORS
        preflight, which this app never grants, so the endpoint is not open to CSRF.
        """
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise HTTPException(415, "send a JSON body")
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(400, "body is not JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "body must be a JSON object")
        # Imported here: the job loads pandas and the model, which ordinary pages never need.
        from starlette.concurrency import run_in_threadpool

        from app.clock import now_utc
        from app.config import parse_utc
        from app.jobs.nightly import run_nightly

        try:
            as_of = parse_utc(body["as_of"]) if body.get("as_of") else now_utc()
        except ValueError as exc:
            raise HTTPException(400, f"as_of: {exc}") from exc
        tenant = body.get("tenant")
        try:
            runs = await run_in_threadpool(run_nightly, as_of, tenant, "manual")
        except ValueError as exc:  # an unknown tenant, or an argument run_nightly refuses
            status = 404 if "unknown tenant" in str(exc) else 400
            raise HTTPException(status, str(exc)) from exc
        return JSONResponse(
            {
                "as_of": as_of.isoformat(),
                "runs": [
                    {
                        "tenant_id": str(r.tenant_id),
                        "run_id": str(r.run_id),
                        "status": r.status,
                        "error": r.error,
                        "counts": r.counts,
                    }
                    for r in runs
                ],
            }
        )

    @app.get("/health")
    def health() -> JSONResponse:
        """Counts and status only, so a platform check can call it without credentials."""
        try:
            with get_engine().connect() as conn:
                conn.execute(sa.text("SELECT 1"))
                tenants = [t["tenant_id"] for t in q.list_tenants(conn)]
                model = q.active_model_version(conn)
            accounts = 0
            last = None
            for tid in tenants:
                with tenant_tx(tid) as conn:
                    accounts += q.account_count(conn)
                    run = q.last_job_run(conn)
                if run and (
                    last is None
                    or (run["run_date"], run["as_of"]) > (last["run_date"], last["as_of"])
                ):
                    last = run
        except Exception as exc:  # any failure means "not healthy"; report the type only
            return JSONResponse({"status": "down", "db": type(exc).__name__}, status_code=503)
        if last:
            last = {k: (v if isinstance(v, str) else str(v)) for k, v in last.items()}
        return JSONResponse(
            {
                "status": "ok",
                "db": "ok",
                "tenants": len(tenants),
                "accounts": accounts,
                "model_version": model,
                "last_job_run": last,
            }
        )

    return app


def main(argv: list[str]) -> int:
    """``python -m app web``: serve on 0.0.0.0:$PORT (default 8000)."""
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))  # noqa: S104
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
