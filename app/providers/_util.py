"""Small helpers shared by provider implementations."""

from __future__ import annotations

from typing import Any

import httpx

from app.providers.base import ProviderError


def json_body(resp: httpx.Response, service: str) -> Any:
    """Parse JSON or raise a friendly ProviderError (never echoing the body)."""
    try:
        return resp.json()
    except ValueError as exc:
        raise ProviderError(
            f"{service} answered, but not with JSON. Is the URL pointing at {service}?"
        ) from exc


def status_message(service: str, code: int) -> str:
    if code in (401, 403):
        return f"{service} rejected the credentials (HTTP {code})."
    if code == 404:
        return f"{service} answered with 404 — check the URL (and any URL base / sub-path)."
    if code >= 500:
        return f"{service} had an internal error (HTTP {code}). Check its logs."
    return f"{service} responded with HTTP {code}."
