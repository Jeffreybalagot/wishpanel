"""Kavita as the login provider.

Login uses Kavita's own account endpoint, so WishPanel never stores user
passwords. Roles come from the JWT Kavita issues; we read its claims to learn
whether the user holds the configured admin role. The token is used only for
that and is discarded — it is never stored or logged.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import httpx

from app.core.http import ExternalServiceError, request_with_retry
from app.domain import AuthenticatedUser, ConnectionTestResult
from app.providers.base import InvalidCredentials, ProviderError

log = logging.getLogger(__name__)

SERVICE = "Kavita"
_ROLE_CLAIMS = ("role", "roles", "http://schemas.microsoft.com/ws/2008/06/identity/claims/role")


def _jwt_claims(token: str) -> dict[str, Any]:
    """Decode a JWT payload without verifying it.

    Safe here because the token arrived directly from the Kavita server the
    admin configured, over our own outbound request — we are not trusting a
    client-supplied token.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return {}


def _extract_roles(body: dict[str, Any]) -> tuple[str, ...]:
    roles: list[str] = []
    if isinstance(body.get("roles"), list):
        roles.extend(str(r) for r in body["roles"])
    claims = _jwt_claims(str(body.get("token") or ""))
    for claim in _ROLE_CLAIMS:
        value = claims.get(claim)
        if isinstance(value, str):
            roles.append(value)
        elif isinstance(value, list):
            roles.extend(str(v) for v in value)
    # de-dupe, keep order
    return tuple(dict.fromkeys(roles))


class KavitaAuthProvider:
    name = "kavita"

    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self.client = client
        self.base_url = base_url.rstrip("/")

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    async def test_connection(self) -> ConnectionTestResult:
        if not self.base_url:
            return ConnectionTestResult(False, "Kavita URL is not set.")
        try:
            resp = await request_with_retry(
                self.client, SERVICE, "GET", self._url("/api/health"), retries=0
            )
        except ExternalServiceError as exc:
            return ConnectionTestResult(False, f"Could not reach Kavita: {exc.message}.")
        if resp.status_code == 200:
            return ConnectionTestResult(True, "Connected to Kavita.")
        if resp.status_code == 404:
            return ConnectionTestResult(
                False,
                "Something answered at that URL, but it doesn't look like Kavita "
                "(no /api/health). Check the address and port.",
            )
        return ConnectionTestResult(False, f"Kavita responded with HTTP {resp.status_code}.")

    async def login(self, username: str, password: str) -> AuthenticatedUser:
        if not self.base_url:
            raise ProviderError("Login is unavailable: Kavita has not been configured yet.")
        try:
            resp = await request_with_retry(
                self.client,
                SERVICE,
                "POST",
                self._url("/api/Account/login"),
                json={"username": username, "password": password, "apiKey": ""},
                retries=1,
            )
        except ExternalServiceError as exc:
            raise ProviderError(f"Kavita is unreachable: {exc.message}.", retryable=True) from exc

        if resp.status_code in (400, 401, 403):
            raise InvalidCredentials("Invalid username or password.")
        if resp.status_code != 200:
            raise ProviderError(f"Kavita login failed (HTTP {resp.status_code}).", retryable=True)

        try:
            body = resp.json()
        except ValueError as exc:
            raise ProviderError("Kavita returned an unexpected response.") from exc
        if not isinstance(body, dict) or not body.get("username"):
            raise ProviderError("Kavita returned an unexpected response.")

        username_out = str(body["username"])
        return AuthenticatedUser(
            provider=self.name,
            # Kavita usernames are unique and stable; the login DTO has no numeric id.
            external_id=username_out.lower(),
            username=username_out,
            email=body.get("email") or None,
            roles=_extract_roles(body),
        )

    def is_admin(self, user: AuthenticatedUser, admin_role: str) -> bool:
        wanted = admin_role.strip().lower()
        return bool(wanted) and any(r.lower() == wanted for r in user.roles)
