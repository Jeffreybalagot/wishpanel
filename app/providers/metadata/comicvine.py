"""ComicVine metadata — API key test (M2). Search and details arrive in M4.

ComicVine wraps every response in ``{"status_code": int, "error": str, ...}``:
1 = OK, 100 = invalid API key, 107 = rate limited. It also requires a
descriptive User-Agent, which the shared client sets.
"""

from __future__ import annotations

import httpx

from app.core.http import ExternalServiceError, request_with_retry
from app.domain import ConnectionTestResult
from app.providers._util import json_body

SERVICE = "ComicVine"
API_BASE = "https://comicvine.gamespot.com/api"


class ComicVineMetadataProvider:
    name = "comicvine"

    def __init__(self, client: httpx.AsyncClient, api_key: str, base_url: str = API_BASE) -> None:
        self.client = client
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    async def test_connection(self) -> ConnectionTestResult:
        if not self.api_key:
            return ConnectionTestResult(
                False, "ComicVine API key is not set. Get one at comicvine.gamespot.com/api."
            )
        try:
            resp = await request_with_retry(
                self.client,
                SERVICE,
                "GET",
                f"{self.base_url}/types/",
                params={"api_key": self.api_key, "format": "json"},
                retries=1,
            )
        except ExternalServiceError as exc:
            return ConnectionTestResult(False, f"Could not reach ComicVine: {exc.message}.")
        try:
            body = json_body(resp, SERVICE)
        except Exception:
            return ConnectionTestResult(False, f"ComicVine responded with HTTP {resp.status_code}.")
        code = body.get("status_code") if isinstance(body, dict) else None
        if code == 1:
            return ConnectionTestResult(True, "ComicVine accepted the API key.")
        if code == 100:
            return ConnectionTestResult(False, "ComicVine says the API key is invalid.")
        if code == 107:
            return ConnectionTestResult(
                False, "ComicVine is rate-limiting this key right now. Try again in a few minutes."
            )
        err = body.get("error") if isinstance(body, dict) else None
        return ConnectionTestResult(
            False, f"ComicVine responded with an error: {err or f'HTTP {resp.status_code}'}."
        )
