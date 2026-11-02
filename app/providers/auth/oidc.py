"""OpenID Connect sign-in (Authentik, Keycloak, Authelia, Pocket ID, …).

This is an *additional* way to sign in, offered next to the normal login
provider (Kavita) rather than replacing it, so an outage at the identity
provider never locks the admin out.

Flow: authorization code + PKCE (S256), confidential client. The code is
exchanged server-to-server at the provider's token endpoint and the user's
claims are read from its userinfo endpoint with the access token. Because both
calls go directly from WishPanel to the issuer the admin configured (over the
shared, TLS-verifying HTTP client), the ID token's payload is decoded only to
check ``nonce``, ``aud`` and ``iss`` — the same trust model the Kavita provider
uses for Kavita's JWT. Tokens are never stored or logged.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.http import ExternalServiceError, request_with_retry
from app.domain import ConnectionTestResult
from app.providers.base import ProviderError

log = logging.getLogger(__name__)

SERVICE = "SSO provider"
SCOPES = "openid email profile"


@dataclass(frozen=True)
class OIDCEndpoints:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    userinfo_endpoint: str


@dataclass(frozen=True)
class OIDCClaims:
    """The identity a successful SSO sign-in produced."""

    subject: str
    username: str
    email: str | None
    email_verified: bool | None
    groups: tuple[str, ...]


@dataclass(frozen=True)
class AuthorizationRequest:
    url: str
    state: str
    nonce: str
    code_verifier: str


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def pkce_challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def _jwt_claims(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _norm_issuer(url: str) -> str:
    return url.strip().rstrip("/")


def _groups(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(g for g in value.replace(",", " ").split() if g)
    if isinstance(value, list):
        return tuple(str(g) for g in value if str(g).strip())
    return ()


class OIDCClient:
    name = "oidc"

    def __init__(
        self, client: httpx.AsyncClient, issuer_url: str, client_id: str, client_secret: str
    ) -> None:
        self.client = client
        self.issuer_url = _norm_issuer(issuer_url or "")
        self.client_id = (client_id or "").strip()
        self.client_secret = client_secret or ""

    @property
    def configured(self) -> bool:
        return bool(self.issuer_url and self.client_id and self.client_secret)

    async def discover(self) -> OIDCEndpoints:
        if not self.issuer_url:
            raise ProviderError("SSO is not configured: the issuer URL is empty.")
        url = f"{self.issuer_url}/.well-known/openid-configuration"
        try:
            resp = await request_with_retry(self.client, SERVICE, "GET", url, retries=1)
        except ExternalServiceError as exc:
            raise ProviderError(
                f"Could not reach the SSO provider: {exc.message}.", retryable=True
            ) from exc
        if resp.status_code != 200:
            raise ProviderError(
                f"The SSO provider has no discovery document at that issuer URL "
                f"(HTTP {resp.status_code}). Use the provider's issuer URL, e.g. "
                "https://sso.example.com/application/o/wishpanel/"
            )
        try:
            doc = resp.json()
            endpoints = OIDCEndpoints(
                issuer=_norm_issuer(str(doc["issuer"])),
                authorization_endpoint=str(doc["authorization_endpoint"]),
                token_endpoint=str(doc["token_endpoint"]),
                userinfo_endpoint=str(doc["userinfo_endpoint"]),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError("The SSO provider's discovery document is incomplete.") from exc
        return endpoints

    async def test_connection(self) -> ConnectionTestResult:
        try:
            ep = await self.discover()
        except ProviderError as exc:
            return ConnectionTestResult(False, exc.message)
        if not self.client_id or not self.client_secret:
            return ConnectionTestResult(
                False, "Found the SSO provider, but the client ID or secret is empty."
            )
        return ConnectionTestResult(True, f"Found the SSO provider ({ep.issuer}).")

    async def authorization_request(self, redirect_uri: str) -> AuthorizationRequest:
        if not self.configured:
            raise ProviderError("SSO is not fully configured.")
        ep = await self.discover()
        state, nonce, verifier = (
            secrets.token_urlsafe(24),
            secrets.token_urlsafe(24),
            secrets.token_urlsafe(48),
        )
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "scope": SCOPES,
                "state": state,
                "nonce": nonce,
                "code_challenge": pkce_challenge(verifier),
                "code_challenge_method": "S256",
            }
        )
        sep = "&" if "?" in ep.authorization_endpoint else "?"
        return AuthorizationRequest(
            url=f"{ep.authorization_endpoint}{sep}{query}",
            state=state,
            nonce=nonce,
            code_verifier=verifier,
        )

    async def finish(
        self, code: str, redirect_uri: str, *, nonce: str, code_verifier: str
    ) -> OIDCClaims:
        """Exchange the code and return the user's claims, or raise ProviderError."""
        ep = await self.discover()
        try:
            resp = await request_with_retry(
                self.client,
                SERVICE,
                "POST",
                ep.token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code_verifier": code_verifier,
                },
                headers={"Accept": "application/json"},
                retries=0,
            )
        except ExternalServiceError as exc:
            raise ProviderError(f"The SSO provider is unreachable: {exc.message}.") from exc
        if resp.status_code != 200:
            reason = ""
            with contextlib.suppress(ValueError, AttributeError):
                reason = str(resp.json().get("error") or "")
            log.warning(
                "SSO token exchange failed", extra={"status": resp.status_code, "error": reason}
            )
            raise ProviderError("The SSO provider rejected the sign-in. Please try again.")
        try:
            tokens = resp.json()
            access_token = str(tokens["access_token"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError("The SSO provider returned an unexpected response.") from exc

        id_claims = _jwt_claims(str(tokens.get("id_token") or ""))
        if id_claims:
            aud = id_claims.get("aud")
            auds = aud if isinstance(aud, list) else [aud]
            if self.client_id not in auds:
                raise ProviderError("The SSO sign-in was issued for a different application.")
            if _norm_issuer(str(id_claims.get("iss") or "")) != ep.issuer:
                raise ProviderError("The SSO sign-in came from an unexpected issuer.")
            if id_claims.get("nonce") != nonce:
                raise ProviderError("The SSO sign-in could not be verified. Please try again.")

        try:
            info_resp = await request_with_retry(
                self.client,
                SERVICE,
                "GET",
                ep.userinfo_endpoint,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
                retries=1,
            )
        except ExternalServiceError as exc:
            raise ProviderError(f"The SSO provider is unreachable: {exc.message}.") from exc
        if info_resp.status_code != 200:
            raise ProviderError("Could not read your profile from the SSO provider.")
        try:
            info = info_resp.json()
        except ValueError as exc:
            raise ProviderError("The SSO provider returned an unexpected profile.") from exc
        if not isinstance(info, dict):
            raise ProviderError("The SSO provider returned an unexpected profile.")

        subject = str(info.get("sub") or id_claims.get("sub") or "").strip()
        if not subject:
            raise ProviderError("The SSO provider did not identify the user.")
        if id_claims.get("sub") and str(id_claims["sub"]) != subject:
            raise ProviderError("The SSO sign-in could not be verified. Please try again.")
        email = str(info.get("email") or "").strip() or None
        verified = info.get("email_verified")
        username = (
            str(info.get("preferred_username") or "").strip()
            or (email.split("@")[0] if email else "")
            or str(info.get("name") or "").strip()
            or f"user-{subject[:8]}"
        )
        return OIDCClaims(
            subject=subject,
            username=username[:255],
            email=email[:255] if email else None,
            email_verified=verified if isinstance(verified, bool) else None,
            groups=_groups(info.get("groups", id_claims.get("groups"))),
        )
