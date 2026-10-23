"""Shared outbound HTTP client with timeouts, retry/backoff and friendly errors.

Every provider talks to the outside world through ``request_with_retry`` so
behaviour (timeouts, retries, error wording) is consistent and the admin
always gets a human-readable message rather than a stack trace.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import httpx

from app import __version__

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
USER_AGENT = f"WishPanel/{__version__} (+https://github.com/Jeffreybalagot/wishpanel)"

_RETRY_STATUS = {429, 502, 503, 504}


class ExternalServiceError(Exception):
    """A remote service failed in a way worth showing to the admin."""

    def __init__(self, service: str, message: str, *, status_code: int | None = None) -> None:
        super().__init__(f"{service}: {message}")
        self.service = service
        self.message = message
        self.status_code = status_code


def build_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )


async def request_with_retry(
    client: httpx.AsyncClient,
    service: str,
    method: str,
    url: str,
    *,
    retries: int = 2,
    backoff: float = 0.5,
    **kwargs: Any,
) -> httpx.Response:
    """Perform a request, retrying transient failures with jittered backoff.

    Only network errors and 429/502/503/504 are retried. Other HTTP statuses are
    returned to the caller, which knows what they mean for that API.
    """
    attempt = 0
    while True:
        try:
            resp = await client.request(method, url, **kwargs)
            if resp.status_code not in _RETRY_STATUS or attempt >= retries:
                return resp
            reason = f"HTTP {resp.status_code}"
        except httpx.TimeoutException as exc:
            if attempt >= retries:
                raise ExternalServiceError(service, "timed out waiting for a response") from exc
            reason = "timeout"
        except httpx.ConnectError as exc:
            if attempt >= retries:
                raise ExternalServiceError(
                    service, "could not connect — check the URL and that the service is running"
                ) from exc
            reason = "connect error"
        except httpx.InvalidURL as exc:
            raise ExternalServiceError(service, "the URL is not valid") from exc
        except httpx.HTTPError as exc:
            if attempt >= retries:
                raise ExternalServiceError(
                    service, f"request failed ({type(exc).__name__})"
                ) from exc
            reason = type(exc).__name__

        attempt += 1
        delay = backoff * (2 ** (attempt - 1)) * (1 + random.random() / 2)  # noqa: S311
        log.debug("Retrying %s request", service, extra={"attempt": attempt, "reason": reason})
        await asyncio.sleep(delay)
