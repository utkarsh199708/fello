"""One shared basic-auth password for the whole app, except ``/health``.

WHY one password: Step 1 is a demo for a small team, and a user table would be code nobody asked
for yet. The username is ignored; only the password is compared, in constant time.
``/health`` stays open because a platform health check cannot send credentials, and it shows
counts only.
"""

from __future__ import annotations

import base64
import binascii
import os
import secrets

from fastapi import Request
from fastapi.responses import Response

OPEN_PATHS = ("/health",)


def required_password() -> str:
    """Read ``BASIC_AUTH_PASSWORD``; refuse to run without it rather than serve an open app."""
    password = os.environ.get("BASIC_AUTH_PASSWORD", "")
    if not password:
        raise RuntimeError("BASIC_AUTH_PASSWORD is not set")
    return password


def password_from_header(header: str | None) -> str | None:
    """Return the password in an ``Authorization: Basic ...`` header, or None if unusable."""
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    return decoded.partition(":")[2]


def make_basic_auth(password: str):
    """Build the HTTP middleware that answers 401 unless the password matches."""

    async def basic_auth(request: Request, call_next):
        if request.url.path in OPEN_PATHS:
            return await call_next(request)
        given = password_from_header(request.headers.get("authorization"))
        # compare_digest needs equal types; encode so non-ASCII input cannot raise.
        if given is None or not secrets.compare_digest(given.encode(), password.encode()):
            return Response(
                "Authentication required",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="renewals"'},
            )
        return await call_next(request)

    return basic_auth
