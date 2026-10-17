"""Optional LLM narration of a night's briefings: two sentences a CSM can read at a glance.

Off unless ``LLM_API_KEY`` is set. The ranking never depends on it: the model ranks, the template
briefing states the reasons, and this only rephrases what the briefing already says.

Rules, each one checked by a test:

- **After the night, never inside it.** It runs once a tenant's night has committed, in its own
  transactions. A slow, failing or missing LLM cannot block, fail or roll back a night, and the
  night's ``job_runs`` row does not change.
- **Only the reason list leaves the system.** The input is the briefing's reasons (label and
  value), the probability, the ARR and the days to renewal. No account or person name, no email,
  no raw event. It is stored on the narration row as sent.
- **Every number is checked.** Each number in the text must appear in the input. A text that
  names any other number, or is empty or too long, is stored as ``rejected`` with the reason,
  and the CSM keeps seeing the template.
- **Capped and idempotent.** At most ``LLM_MAX_CALLS_PER_RUN`` calls per tenant per night, and a
  briefing that already has a narration is never sent again.

The call is one HTTPS request to the Anthropic Messages API, with the standard library, so the
feature adds no dependency. ``narrate_run`` takes any ``client`` (a function from the input to
text), which is how the tests run without a network.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from collections.abc import Callable
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from app.db import tenant_tx
from app.ids import stable_uuid
from app.jobs.actions import reason_value_text

log = logging.getLogger(__name__)

PROMPT_VERSION = "narrate-v1"
DEFAULT_MODEL = "claude-haiku-5-5"
API_URL = "https://api.anthropic.com/v1/messages"
MAX_CHARS = 600
DEFAULT_MAX_CALLS = 50

SYSTEM_PROMPT = (
    "You write a short internal note for a customer success manager (CSM) about one account "
    "whose renewal is at risk. Use only the facts in the JSON you are given. Write two sentences: "
    "what most raises the risk, and what the CSM should look at first. Do not add any number, "
    "name, date, cause or promise that is not in the facts. Do not address the customer. "
    "Plain text only, no lists, no markdown."
)

Client = Callable[[dict[str, Any]], str]

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def facts_of(briefing: Any) -> dict[str, Any]:
    """What the model is sent for one briefing: the reason list and three numbers, as text."""
    return {
        "renewal_risk": f"{float(briefing.probability):.0%}",
        "arr": f"{float(briefing.arr):,.0f}",
        "days_to_renewal": str(briefing.days_to_renewal),
        "reasons": [
            {"reason": r["label"], "value": reason_value_text(r)} for r in briefing.reasons
        ],
    }


def numbers_in(text: str) -> list[float]:
    """Every number written in ``text``, without commas, signs or percent signs."""
    return [float(m.replace(",", "")) for m in _NUMBER.findall(text)]


def check(text: str, facts: dict[str, Any]) -> str | None:
    """None if the narration may be shown, else why not."""
    if not text or not text.strip():
        return "empty"
    if len(text) > MAX_CHARS:
        return f"longer than {MAX_CHARS} characters"
    allowed = set(numbers_in(json.dumps(facts)))
    invented = [n for n in numbers_in(text) if n not in allowed]
    if invented:
        return "numbers not in the input: " + ", ".join(f"{n:g}" for n in invented[:5])
    return None


def anthropic_client(api_key: str, model: str, timeout: float = 20.0) -> Client:
    """A client that calls the Anthropic Messages API once per briefing."""

    def call(facts: dict[str, Any]) -> str:
        body = {
            "model": model,
            "max_tokens": 300,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": json.dumps(facts)}],
        }
        request = urllib.request.Request(  # noqa: S310 - a fixed https URL
            API_URL,
            data=json.dumps(body).encode(),
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            reply = json.loads(response.read())
        return "".join(part.get("text", "") for part in reply.get("content", [])).strip()

    return call


def configured_client() -> tuple[Client, str] | None:
    """The live client and model name from the environment, or None when narration is off."""
    key = os.environ.get("LLM_API_KEY", "").strip()
    if not key:
        return None
    model = os.environ.get("LLM_MODEL", "").strip() or DEFAULT_MODEL
    return anthropic_client(key, model), model


def narrate_run(
    tenant_id: UUID,
    run_id: UUID,
    client: Client | None = None,
    model: str | None = None,
    limit: int | None = None,
) -> dict[str, int]:
    """Narrate the night's briefings that have none yet. Returns counts by status.

    With no ``client``, the environment decides; with no key it does nothing. Never raises for a
    single briefing: a failed call is stored as ``error``.
    """
    if client is None:
        configured = configured_client()
        if configured is None:
            return {}
        client, model = configured
    model = model or "test-client"
    if limit is None:
        limit = int(os.environ.get("LLM_MAX_CALLS_PER_RUN", "") or DEFAULT_MAX_CALLS)

    with tenant_tx(tenant_id) as conn:
        briefings = conn.execute(
            sa.text(
                "SELECT b.briefing_id, b.reasons, s.probability, s.arr, s.days_to_renewal"
                " FROM briefings b JOIN scores s ON s.score_id = b.score_id"
                " LEFT JOIN narrations n ON n.briefing_id = b.briefing_id"
                " WHERE b.run_id = :r AND n.narration_id IS NULL"
                " ORDER BY s.rank_score DESC, b.briefing_id LIMIT :lim"
            ),
            {"r": run_id, "lim": limit},
        ).all()

    rows, counts = [], {"ok": 0, "rejected": 0, "error": 0}
    for b in briefings:  # outside any transaction: a slow call holds no lock
        facts = facts_of(b)
        try:
            text = client(facts)
            problem = check(text, facts)
            status, detail = ("ok", None) if problem is None else ("rejected", problem)
        except Exception as exc:  # the night is already safe; record why and move on
            log.warning("narration of briefing %s failed: %s", b.briefing_id, exc)
            text, status, detail = None, "error", f"{type(exc).__name__}: {exc}"[:500]
        counts[status] += 1
        rows.append(
            {
                "id": stable_uuid("narration", tenant_id, b.briefing_id),
                "tid": tenant_id,
                "b": b.briefing_id,
                "r": run_id,
                "model": model,
                "pv": PROMPT_VERSION,
                "input": json.dumps(facts),
                "status": status,
                "text": text if status == "ok" else None,
                "detail": detail,
            }
        )
    if rows:
        with tenant_tx(tenant_id) as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO narrations (narration_id, tenant_id, briefing_id, run_id, model,"
                    " prompt_version, input, status, text, detail) VALUES (:id, :tid, :b, :r,"
                    " :model, :pv, CAST(:input AS jsonb), :status, :text, :detail)"
                    " ON CONFLICT (tenant_id, briefing_id) DO NOTHING"
                ),
                rows,
            )
    return counts
