"""The web app: switcher, ranked Monday list, score drill-down, account page and ``/health``.

Run with ``python -m app web``. Importing this module loads FastAPI and the light
``feature_spec`` only, never pandas or scikit-learn.

Who is "logged in" is a signed session cookie (Starlette ``SessionMiddleware``, SameSite=Strict,
HttpOnly) holding ``tenant_id`` and ``csm_id``. The switcher page is the only place that reads a
tenant id from a request, and all it does is validate it and write the cookie. Every other route
takes the tenant from the cookie and reads through ``tenant_tx``, so row-level security means an
id belonging to another tenant finds nothing and answers 404.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.db import get_engine, tenant_tx
from app.ml.feature_spec import FEATURE_EVENTS, FEATURE_SOURCE, FEATURES
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


def create_app() -> FastAPI:
    """Build the app. Fails at startup if ``BASIC_AUTH_PASSWORD`` or ``SESSION_SECRET`` is unset."""
    password = required_password()
    secret = os.environ.get("SESSION_SECRET", "")
    if not secret:
        raise RuntimeError("SESSION_SECRET is not set")

    app = FastAPI(title="Renewal risk", docs_url=None, redoc_url=None, openapi_url=None)
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
        return templates.TemplateResponse(
            request, "switch.html", {"tenants": tenants, "chosen": chosen, "csms": csms}
        )

    @app.post("/switch")
    def switch_submit(
        request: Request,
        tenant_id: Annotated[uuid.UUID, Form()],
        csm_id: Annotated[str, Form()],
    ) -> Response:
        """Validate the pair inside the tenant's transaction, then write the cookie."""
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
    def ranked(request: Request) -> Response:
        ids = selected(request)
        if ids is None:
            return to_switcher()
        tenant_id, csm_id = ids
        with tenant_tx(tenant_id) as conn:
            tenant = q.tenant_name(conn)
            if csm_id == q.UNASSIGNED:
                csm = {"csm_id": None, "name": "Unassigned", "email": ""}
            else:
                csm = q.get_csm(conn, csm_id)
            as_of = q.latest_as_of(conn)
            rows = q.ranked_list(conn, csm_id, as_of) if as_of and csm else []
            quality = q.data_quality(conn)
        if csm is None:  # the cookie names a CSM this tenant does not have
            return to_switcher()
        return templates.TemplateResponse(
            request,
            "list.html",
            {"tenant": tenant, "csm": csm, "as_of": as_of, "rows": rows, "quality": quality},
        )

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
                "value": values.get(f),
                "missing": missing_reason(f, values),
                "contribution": contributions.get(f, 0.0),
                "has_events": FEATURE_EVENTS[f] is not None,
            }
            for f in FEATURES
        ]
        total = score["expected_value"] + sum(r["contribution"] for r in rows)
        return templates.TemplateResponse(
            request,
            "score.html",
            {"score": score, "rows": rows, "total": total},
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
        return templates.TemplateResponse(
            request,
            "account.html",
            {
                "account": account,
                "contracts": contracts,
                "scores": scores,
                "timeline": timeline,
                "as_of": latest,
            },
        )

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
