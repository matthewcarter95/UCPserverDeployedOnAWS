#   Copyright 2026 UCP Authors
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

"""OAuth access-token validation for user-authenticated UCP operations.

Implements the business side of the Identity Linking capability: verify the
bearer token's signature, `iss`, `aud` and `exp` on every request, map scopes to
operations, and emit RFC 6750 `WWW-Authenticate` challenges.

Configuration (environment variables, so it works locally and in Lambda):

  AUTH_MODE         off | optional | enforce
                    off       - never look at the Authorization header.
                    optional  - tokens are used when valid and ignored (logged)
                                when not; scopes are not enforced. Spending
                                loyalty points always needs a valid token with
                                the complete scope.
                    enforce   - operations listed in OPERATION_SCOPES need a
                                valid token carrying the required scope.
                    Defaults to `off` when AUTH0_DOMAIN is unset, else
                    `optional`.
  AUTH0_DOMAIN      Tenant domain; the issuer is https://<domain>/.
  AUTH_AUDIENCE     Resource server identifier expected in `aud`.
  LOYALTY_CLAIM     Access-token claim carrying the buyer's loyaltyId.
  AUTH_ALLOWED_CLIENT_IDS  Optional comma-separated `azp`/`client_id` allowlist.
"""

import asyncio
from dataclasses import dataclass
from dataclasses import field
import logging
import os
from typing import Any

from enums import ErrorSeverity
from exceptions import UcpError
from fastapi import Request
import jwt
from jwt import PyJWKClient

logger = logging.getLogger(__name__)

DEFAULT_LOYALTY_CLAIM = "https://demo.okta.com/loyaltyId"

# Scope names. The `mock.` prefix is used because Auth0 reserves `dev.ucp`.
SCOPE_CHECKOUT_MANAGE = "mock.ucp.shopping.checkout:manage"
SCOPE_CHECKOUT_COMPLETE = "mock.ucp.shopping.checkout:complete"
SCOPE_ORDER_READ = "mock.ucp.shopping.order:read"
SCOPE_ORDER_MANAGE = "mock.ucp.shopping.order:manage"
ALL_SCOPES = (
  SCOPE_CHECKOUT_MANAGE,
  SCOPE_CHECKOUT_COMPLETE,
  SCOPE_ORDER_READ,
  SCOPE_ORDER_MANAGE,
)

# The pre-UCP scope. It predates the split between managing and completing a
# checkout, so it is honoured as `manage` only: it never authorizes a purchase.
LEGACY_MANAGE_SCOPE = "ucp:scopes:checkout_session"

# operation_id (from generated_routes) -> scopes the token must carry (all of).
OPERATION_SCOPES: dict[str, tuple[str, ...]] = {
  "create_checkout": (SCOPE_CHECKOUT_MANAGE,),
  "get_checkout": (SCOPE_CHECKOUT_MANAGE,),
  "update_checkout": (SCOPE_CHECKOUT_MANAGE,),
  "cancel_checkout": (SCOPE_CHECKOUT_MANAGE,),
  "complete_checkout": (SCOPE_CHECKOUT_COMPLETE,),
  "create_cart": (SCOPE_CHECKOUT_MANAGE,),
  "get_cart": (SCOPE_CHECKOUT_MANAGE,),
  "update_cart": (SCOPE_CHECKOUT_MANAGE,),
  "cancel_cart": (SCOPE_CHECKOUT_MANAGE,),
  "get_order": (SCOPE_ORDER_READ,),
  "update_order": (SCOPE_ORDER_MANAGE,),
}


@dataclass
class Identity:
  """The authenticated buyer behind a request."""

  sub: str
  scopes: frozenset[str]
  client_id: str | None = None
  loyalty_id: str | None = None
  claims: dict[str, Any] = field(default_factory=dict, repr=False)

  def has_scope(self, scope: str) -> bool:
    """Return whether the token carries `scope` (legacy scope = manage)."""
    if scope in self.scopes:
      return True
    return scope == SCOPE_CHECKOUT_MANAGE and LEGACY_MANAGE_SCOPE in self.scopes


def mode() -> str:
  """Return the configured auth mode."""
  configured = os.environ.get("AUTH_MODE", "").strip().lower()
  if configured in ("off", "optional", "enforce"):
    return configured
  return "optional" if os.environ.get("AUTH0_DOMAIN") else "off"


def issuer() -> str:
  """Return the expected token issuer."""
  return f"https://{os.environ.get('AUTH0_DOMAIN', '')}/"


def audience() -> str:
  """Return the expected token audience."""
  return os.environ.get("AUTH_AUDIENCE", "")


def loyalty_claim() -> str:
  """Return the access-token claim that carries the loyaltyId."""
  return os.environ.get("LOYALTY_CLAIM", DEFAULT_LOYALTY_CLAIM)


def _challenge(
  base_url: str,
  error: str | None = None,
  description: str | None = None,
  scope: str | None = None,
) -> dict[str, str]:
  """Build the RFC 6750 §3 WWW-Authenticate header."""
  parts = [f'realm="{issuer()}"']
  if error:
    parts.append(f'error="{error}"')
  if description:
    parts.append(f'error_description="{description}"')
  if scope:
    parts.append(f'scope="{scope}"')
  parts.append(
    f'resource_metadata="{base_url}/.well-known/oauth-protected-resource"'
  )
  return {"WWW-Authenticate": "Bearer " + ", ".join(parts)}


def unauthorized(
  base_url: str, content: str, error: str | None = None
) -> UcpError:
  return UcpError(
    content,
    code="identity_required",
    status_code=401,
    severity=ErrorSeverity.REQUIRES_BUYER_REVIEW,
    headers=_challenge(
      base_url, error=error, description=content if error else None
    ),
  )


def forbidden(base_url: str, required: tuple[str, ...]) -> UcpError:
  scope = " ".join(required)
  return UcpError(
    f"This operation requires scopes: {', '.join(required)}",
    code="insufficient_scope",
    status_code=403,
    severity=ErrorSeverity.REQUIRES_BUYER_REVIEW,
    headers=_challenge(base_url, error="insufficient_scope", scope=scope),
  )


_jwk_client: PyJWKClient | None = None


def _signing_key(token: str) -> Any:
  """Fetch the JWKS signing key for `token` (blocking; cached by PyJWKClient)."""
  global _jwk_client
  if _jwk_client is None:
    _jwk_client = PyJWKClient(
      f"{issuer()}.well-known/jwks.json", cache_keys=True, lifespan=3600
    )
  return _jwk_client.get_signing_key_from_jwt(token).key


def _decode(token: str) -> dict[str, Any]:
  """Verify signature, iss, aud and exp; return the claims."""
  key = _signing_key(token)
  return jwt.decode(
    token,
    key,
    algorithms=["RS256"],
    audience=audience(),
    issuer=issuer(),
    options={"require": ["exp", "iss", "aud", "sub"]},
    leeway=30,
  )


def _identity_from_claims(claims: dict[str, Any]) -> Identity:
  raw_scope = claims.get("scope", "")
  scopes = frozenset(
    raw_scope.split() if isinstance(raw_scope, str) else list(raw_scope)
  )
  raw_loyalty = claims.get(loyalty_claim())
  return Identity(
    sub=str(claims["sub"]),
    scopes=scopes,
    client_id=claims.get("azp") or claims.get("client_id"),
    loyalty_id=str(raw_loyalty) if raw_loyalty not in (None, "") else None,
    claims=claims,
  )


async def authenticate(request: Request) -> Identity | None:
  """Validate the bearer token, if any, and return the buyer identity.

  Returns None when auth is off or no token was presented. When a token is
  presented but invalid, raises a 401 UcpError in `enforce` mode and falls back
  to anonymous in `optional` mode (so an unexpected token never breaks a
  public operation).
  """
  if mode() == "off":
    return None
  header = request.headers.get("authorization")
  if not header:
    return None
  try:
    return await _authenticate(request, header)
  except UcpError as exc:
    if mode() == "enforce":
      raise
    logger.warning("Ignoring invalid token in optional mode: %s", exc.message)
    return None


async def _authenticate(request: Request, header: str) -> Identity:
  """Strictly validate `header`; raise a 401 UcpError on any failure."""
  base_url = str(request.base_url).rstrip("/")
  scheme, _, token = header.partition(" ")
  if scheme.lower() != "bearer" or not token.strip():
    raise unauthorized(
      base_url, "Authorization must be a Bearer token", "invalid_token"
    )
  try:
    claims = await asyncio.to_thread(_decode, token.strip())
  except jwt.ExpiredSignatureError as exc:
    raise unauthorized(
      base_url, "The access token expired", "invalid_token"
    ) from exc
  except jwt.PyJWTError as exc:
    logger.warning("Rejected access token: %s", exc)
    raise unauthorized(
      base_url, "The access token is invalid", "invalid_token"
    ) from exc

  identity = _identity_from_claims(claims)
  allowed = {
    c.strip()
    for c in os.environ.get("AUTH_ALLOWED_CLIENT_IDS", "").split(",")
    if c.strip()
  }
  if allowed and identity.client_id not in allowed:
    raise unauthorized(
      base_url, "The token was not issued to a permitted client", "invalid_token"
    )
  return identity


def require(operation_id: str):
  """Return a FastAPI dependency enforcing the scopes for `operation_id`.

  The resolved identity (or None) is stored on `request.state.identity` so
  handlers can read it without re-validating.
  """
  required = OPERATION_SCOPES.get(operation_id, ())

  async def dependency(request: Request) -> Identity | None:
    identity = await authenticate(request)
    request.state.identity = identity
    if mode() == "enforce" and required:
      base_url = str(request.base_url).rstrip("/")
      if identity is None:
        raise unauthorized(base_url, "User identity is required")
      if not all(identity.has_scope(s) for s in required):
        raise forbidden(base_url, required)
    return identity

  return dependency


def identity_of(request: Request) -> Identity | None:
  """Return the identity resolved for this request, validating if needed."""
  return getattr(request.state, "identity", None)
