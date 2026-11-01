"""OAuth 2.1 sign-in for the OpenFPL MCP server.

Claude's custom connectors cannot send a fixed API key; they sign in with
OAuth. OpenFPL is therefore its own authorization server: connecting opens a
page on this site where the person pastes an OpenFPL API key for every tool,
or continues with the free tools only.

Nothing is stored. Client registrations, authorization requests, codes, and
tokens are HS256-signed JWTs, so any Cloud Run instance can verify any of
them as long as all share ``OPENFPL_OAUTH_SECRET``. A token for the full tier
carries a keyed fingerprint of the API key (never the key itself) and stops
working as soon as that key leaves ``VALID_API_KEYS``.

Being stateless has two costs: an authorization code can be replayed until it
expires (five minutes, and PKCE still binds it to the client that started the
flow), and a refresh token keeps working until it expires even after rotation.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import logging
import secrets
import time
from typing import Any, Literal, Optional
from urllib.parse import urlparse

import jwt
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import (
    build_resource_metadata_url,
    create_auth_routes,
    create_protected_resource_routes,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp

from src.auth import bearer_token, is_valid_api_key, valid_api_keys

logger = logging.getLogger(__name__)

Tier = Literal["free", "full"]

ACCESS_TOKEN_SECONDS = 60 * 60
REFRESH_TOKEN_SECONDS = 30 * 24 * 60 * 60
AUTHORIZATION_CODE_SECONDS = 5 * 60
AUTHORIZATION_REQUEST_SECONDS = 15 * 60
CONNECT_PATH = "/oauth/connect"
DEFAULT_SCOPE = "openfpl"
API_KEY_CLIENT_ID = "openfpl-api-key"


class OpenFPLAuthorizationCode(AuthorizationCode):
    tier: Tier
    key_fingerprint: Optional[str] = None


class OpenFPLRefreshToken(RefreshToken):
    tier: Tier
    key_fingerprint: Optional[str] = None


class OpenFPLOAuth(
    OAuthAuthorizationServerProvider[
        OpenFPLAuthorizationCode, OpenFPLRefreshToken, AccessToken
    ]
):
    """Stateless OAuth provider whose tokens grant the free or the full tier."""

    def __init__(self, site_url: str, secret: Optional[str] = None):
        self.issuer_url = site_url.rstrip("/")
        self.resource_url = f"{self.issuer_url}/mcp"
        if not secret:
            logger.warning(
                "OPENFPL_OAUTH_SECRET is not set; using a random per-process "
                "secret. MCP sign-ins will not survive a restart or work "
                "across instances."
            )
            secret = secrets.token_urlsafe(48)
        self._secret = secret.encode()

    # Signed values --------------------------------------------------------

    def _sign(self, kind: str, claims: dict[str, Any], lifetime: Optional[int]) -> str:
        now = int(time.time())
        payload = {"iss": self.issuer_url, "typ": kind, "iat": now, **claims}
        if lifetime is not None:
            payload["exp"] = now + lifetime
        return jwt.encode(payload, self._secret, algorithm="HS256")

    def _verify(self, kind: str, token: str) -> Optional[dict[str, Any]]:
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],
                issuer=self.issuer_url,
                options={"require": ["iss", "typ", "iat"]},
            )
        except jwt.PyJWTError:
            return None
        return claims if claims.get("typ") == kind else None

    def _mac(self, purpose: str, value: str) -> str:
        message = f"{purpose}:{value}".encode()
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()

    def key_fingerprint(self, api_key: str) -> str:
        return self._mac("api-key", api_key)[:32]

    def _key_still_valid(self, fingerprint: Optional[str]) -> bool:
        return fingerprint is not None and any(
            hmac.compare_digest(fingerprint, self.key_fingerprint(key))
            for key in valid_api_keys()
        )

    def _tier(self, tier: str, fingerprint: Optional[str]) -> Optional[Tier]:
        """The tier a grant still carries, or None once its key is revoked."""
        if tier == "full":
            return "full" if self._key_still_valid(fingerprint) else None
        return "free"

    # Clients: dynamic registration with a signed client_id ----------------

    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        claims = self._verify("client", client_id)
        if claims is None:
            return None
        metadata = claims["metadata"]
        secret = None
        if metadata.get("token_endpoint_auth_method") != "none":
            secret = self._mac("client-secret", client_id)
        return OAuthClientInformationFull.model_validate(
            {
                **metadata,
                "client_id": client_id,
                "client_secret": secret,
                "client_id_issued_at": claims["iat"],
                "client_secret_expires_at": 0 if secret else None,
            }
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # The registration handler returns this same object, so replacing the
        # random id and secret here is what the client receives.
        metadata = client_info.model_dump(
            mode="json",
            exclude_none=True,
            exclude={
                "client_id",
                "client_secret",
                "client_id_issued_at",
                "client_secret_expires_at",
            },
        )
        # The nonce keeps two registrations with identical metadata distinct.
        client_info.client_id = self._sign(
            "client", {"metadata": metadata, "nonce": secrets.token_urlsafe(12)}, None
        )
        if client_info.client_secret is not None:
            client_info.client_secret = self._mac("client-secret", client_info.client_id)
            client_info.client_secret_expires_at = 0

    # Authorization: hand off to the connect page --------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if params.resource and params.resource.rstrip("/") != self.resource_url:
            raise AuthorizeError(
                "invalid_target", f"This server only issues tokens for {self.resource_url}"
            )
        request = self._sign(
            "authorization-request",
            {
                "client_id": client.client_id,
                "client_name": client.client_name,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "code_challenge": params.code_challenge,
                "state": params.state,
                "scopes": params.scopes or (client.scope or DEFAULT_SCOPE).split(),
            },
            AUTHORIZATION_REQUEST_SECONDS,
        )
        return construct_redirect_uri(f"{self.issuer_url}{CONNECT_PATH}", request=request)

    def _authorization_code(self, request: dict[str, Any], tier: Tier, api_key: Optional[str]) -> str:
        return self._sign(
            "authorization-code",
            {
                "client_id": request["client_id"],
                "redirect_uri": request["redirect_uri"],
                "redirect_uri_provided_explicitly": request["redirect_uri_provided_explicitly"],
                "code_challenge": request["code_challenge"],
                "scopes": request["scopes"],
                "tier": tier,
                "key_fingerprint": self.key_fingerprint(api_key) if api_key else None,
                "nonce": secrets.token_urlsafe(16),
            },
            AUTHORIZATION_CODE_SECONDS,
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> Optional[OpenFPLAuthorizationCode]:
        claims = self._verify("authorization-code", authorization_code)
        if claims is None or claims["client_id"] != client.client_id:
            return None
        return OpenFPLAuthorizationCode(
            code=authorization_code,
            scopes=claims["scopes"],
            expires_at=claims["exp"],
            client_id=claims["client_id"],
            code_challenge=claims["code_challenge"],
            redirect_uri=claims["redirect_uri"],
            redirect_uri_provided_explicitly=claims["redirect_uri_provided_explicitly"],
            resource=self.resource_url,
            tier=claims["tier"],
            key_fingerprint=claims["key_fingerprint"],
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: OpenFPLAuthorizationCode
    ) -> OAuthToken:
        return self._issue_tokens(
            client.client_id,
            authorization_code.scopes,
            authorization_code.tier,
            authorization_code.key_fingerprint,
        )

    # Tokens ---------------------------------------------------------------

    def _issue_tokens(
        self, client_id: str, scopes: list[str], tier: Tier, fingerprint: Optional[str]
    ) -> OAuthToken:
        if self._tier(tier, fingerprint) is None:
            raise TokenError("invalid_grant", "The API key for this sign-in was revoked")
        claims = {
            "client_id": client_id,
            "scopes": scopes,
            "tier": tier,
            "key_fingerprint": fingerprint,
        }
        return OAuthToken(
            access_token=self._sign("access", claims, ACCESS_TOKEN_SECONDS),
            expires_in=ACCESS_TOKEN_SECONDS,
            refresh_token=self._sign(
                "refresh", {**claims, "nonce": secrets.token_urlsafe(16)}, REFRESH_TOKEN_SECONDS
            ),
            scope=" ".join(scopes),
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> Optional[OpenFPLRefreshToken]:
        claims = self._verify("refresh", refresh_token)
        if claims is None or claims["client_id"] != client.client_id:
            return None
        return OpenFPLRefreshToken(
            token=refresh_token,
            client_id=claims["client_id"],
            scopes=claims["scopes"],
            expires_at=claims["exp"],
            resource=self.resource_url,
            tier=claims["tier"],
            key_fingerprint=claims["key_fingerprint"],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: OpenFPLRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        return self._issue_tokens(
            client.client_id, scopes, refresh_token.tier, refresh_token.key_fingerprint
        )

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        if is_valid_api_key(token):
            # A raw API key still works as a bearer token, for Claude Code
            # headers and the Messages API's authorization_token.
            return AccessToken(
                token=token,
                client_id=API_KEY_CLIENT_ID,
                scopes=[DEFAULT_SCOPE],
                resource=self.resource_url,
                claims={"tier": "full"},
            )
        claims = self._verify("access", token)
        if claims is None:
            return None
        tier = self._tier(claims["tier"], claims["key_fingerprint"])
        if tier is None:
            return None
        return AccessToken(
            token=token,
            client_id=claims["client_id"],
            scopes=claims["scopes"],
            expires_at=claims["exp"],
            resource=self.resource_url,
            claims={"tier": tier},
        )

    async def revoke_token(self, token: AccessToken | OpenFPLRefreshToken) -> None:
        """Stateless tokens cannot be revoked one by one; revoke the API key."""

    async def tier_for(self, authorization: Optional[str]) -> Optional[Tier]:
        """The tier an ``Authorization`` header grants, if any."""
        token = bearer_token(authorization)
        access = await self.load_access_token(token) if token else None
        return (access.claims or {}).get("tier") if access else None

    # The connect page ------------------------------------------------------

    async def connect_page(self, request: Request) -> Response:
        if request.method == "GET":
            signed = request.query_params.get("request", "")
            form = None
        else:
            form = await request.form()
            signed = str(form.get("request", ""))
        authorization = self._verify("authorization-request", signed)
        if authorization is None:
            return _page(
                "This sign-in link has expired or is invalid. Go back to Claude "
                "and connect OpenFPL again.",
                status_code=400,
            )
        if form is None:
            return _connect_form(signed, authorization)

        action = form.get("action")
        if action == "cancel":
            return _redirect(
                authorization,
                error="access_denied",
                error_description="The person cancelled the sign-in",
            )
        if action == "full":
            api_key = str(form.get("api_key", "")).strip()
            if not is_valid_api_key(api_key):
                return _connect_form(
                    signed, authorization, error="That API key is not valid.", status_code=401
                )
            code = self._authorization_code(authorization, "full", api_key)
        elif action == "free":
            code = self._authorization_code(authorization, "free", None)
        else:
            return _connect_form(signed, authorization, status_code=400)
        return _redirect(authorization, code=code)

    # Wiring -----------------------------------------------------------------

    def routes(self) -> list[Route]:
        """OAuth metadata, registration, authorize, token, and connect routes."""
        settings = self._settings()
        return [
            *create_auth_routes(
                provider=self,
                issuer_url=settings.issuer_url,
                client_registration_options=ClientRegistrationOptions(
                    enabled=True, default_scopes=[DEFAULT_SCOPE]
                ),
            ),
            *create_protected_resource_routes(
                resource_url=settings.resource_server_url,
                authorization_servers=[settings.issuer_url],
                scopes_supported=[DEFAULT_SCOPE],
                resource_name="OpenFPL Scout AI",
            ),
            Route(CONNECT_PATH, self.connect_page, methods=["GET", "POST"]),
        ]

    def protect(self, app: ASGIApp) -> ASGIApp:
        """Require a valid bearer token, answering 401 with OAuth discovery."""
        resource = self._settings().resource_server_url
        required = RequireAuthMiddleware(
            app,
            required_scopes=[],
            resource_metadata_url=build_resource_metadata_url(resource),
        )
        return AuthenticationMiddleware(
            AuthContextMiddleware(required),
            backend=BearerAuthBackend(self, resource_server_url=resource),
        )

    def _settings(self) -> AuthSettings:
        # AuthSettings keeps a path-less issuer without a trailing slash, as
        # RFC 8414 compares issuers as exact strings.
        return AuthSettings(
            issuer_url=self.issuer_url, resource_server_url=self.resource_url
        )

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        """``TokenVerifier`` for the bearer middleware."""
        return await self.load_access_token(token)


def _redirect(authorization: dict[str, Any], **params: Optional[str]) -> Response:
    location = construct_redirect_uri(
        authorization["redirect_uri"], state=authorization.get("state"), **params
    )
    return RedirectResponse(location, status_code=302, headers=_PAGE_HEADERS)


_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    # No form-action: browsers apply it to the redirect after the form
    # posts, which goes to the client's redirect URI (possibly a custom scheme).
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
    "img-src 'self'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def _connect_form(
    signed: str,
    authorization: dict[str, Any],
    error: Optional[str] = None,
    status_code: int = 200,
) -> Response:
    client = html.escape(authorization.get("client_name") or "An MCP client")
    host = html.escape(urlparse(authorization["redirect_uri"]).netloc or "an app")
    alert = f'<p class="error" role="alert">{html.escape(error)}</p>' if error else ""
    body = f"""
      <p class="lead"><strong>{client}</strong> wants to use OpenFPL Scout AI.
      After you choose, you go back to <strong>{host}</strong>.</p>
      {alert}
      <form method="post">
        <input type="hidden" name="request" value="{html.escape(signed)}">
        <label for="api_key">OpenFPL API key</label>
        <input id="api_key" name="api_key" type="password" autocomplete="off"
               spellcheck="false" placeholder="Paste your key">
        <button class="primary" name="action" value="full">Connect with my key</button>
        <p class="hint">Unlocks all 8 tools: player projections, live points,
        set-piece notes, and manager picks.</p>
        <button name="action" value="free">Continue with free tools</button>
        <p class="hint">AI scout squad, team ratings, gameweeks, and fixtures.</p>
        <button class="link" name="action" value="cancel">Cancel</button>
      </form>"""
    return _page(body, status_code=status_code, raw=True)


def _page(content: str, status_code: int = 200, raw: bool = False) -> HTMLResponse:
    body = content if raw else f'<p class="lead">{html.escape(content)}</p>'
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Connect OpenFPL</title>
<style>
  :root {{ color-scheme: dark; --ink: #13071c; --plum: #21062f; --lime: #c8ff32;
           --paper: #f8f7fb; --muted: #b8adc3; --line: rgba(255,255,255,.12);
           --pink: #ff4fa7; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center;
          padding: 24px 16px; background: radial-gradient(circle at 20% 0%, #350849, var(--ink) 60%);
          color: var(--paper); font: 16px/1.5 Inter, "Segoe UI", Helvetica, Arial, sans-serif; }}
  main {{ width: 100%; max-width: 420px; padding: 28px; border: 1px solid var(--line);
          border-radius: 24px; background: rgba(25, 8, 36, .8); }}
  .brand {{ display: flex; align-items: center; gap: 12px; margin-bottom: 20px; }}
  .brand img {{ width: 44px; height: 44px; }}
  h1 {{ margin: 0; font-size: 1.25rem; }}
  .brand small {{ color: var(--muted); font-size: .7rem; font-weight: 800;
                  letter-spacing: .16em; text-transform: uppercase; }}
  .lead {{ margin: 0 0 20px; color: var(--muted); }}
  .lead strong {{ color: var(--paper); }}
  label {{ display: block; margin-bottom: 6px; font-size: .8rem; font-weight: 700; }}
  input[type=password] {{ width: 100%; padding: 12px 14px; border: 1px solid var(--line);
          border-radius: 12px; background: var(--ink); color: var(--paper); font: inherit; }}
  input:focus-visible, button:focus-visible {{ outline: 2px solid var(--lime); outline-offset: 2px; }}
  button {{ width: 100%; margin-top: 16px; padding: 12px; border: 1px solid var(--line);
            border-radius: 999px; background: rgba(255,255,255,.06); color: var(--paper);
            font: inherit; font-weight: 800; cursor: pointer; }}
  button.primary {{ border-color: var(--lime); background: var(--lime); color: var(--ink); }}
  button.link {{ border: 0; background: none; color: var(--muted); font-weight: 600; }}
  .hint {{ margin: 6px 0 0; color: var(--muted); font-size: .8rem; text-align: center; }}
  .error {{ margin: 0 0 16px; padding: 10px 12px; border-radius: 12px;
            background: rgba(255,79,167,.14); color: var(--pink); font-weight: 700; }}
</style>
</head>
<body>
<main>
  <div class="brand">
    <img src="/assets/openfpl-predictive-lion-frameless-2026-512.png" alt="">
    <div><h1>Connect OpenFPL</h1><small>Fantasy Premier League AI scout</small></div>
  </div>
  {body}
</main>
</body>
</html>""",
        status_code=status_code,
        headers=_PAGE_HEADERS,
    )
