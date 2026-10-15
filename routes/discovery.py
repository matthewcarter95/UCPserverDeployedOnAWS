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

"""Discovery routes for the UCP server."""

import json
import pathlib
from fastapi import APIRouter
from fastapi import Request
from fastapi import Response
import config
import ucp_auth
import webhook_signer

router = APIRouter()

PROFILE_TEMPLATE_PATH = pathlib.Path(__file__).parent / "discovery_profile.json"

# Profiles are stable, non-sensitive documents; the spec requires a cacheable
# response (overview.md, Discovery: profile caching): `public` with `max-age`
# of at least 60 seconds, and never `private`/`no-store`/`no-cache`.
PROFILE_CACHE_CONTROL = "public, max-age=3600"

# Shop ID shared with checkout responses (config.get_payment_handlers)
SHOP_ID = config.SHOP_ID


@router.get(
  "/.well-known/ucp",
  response_model=dict,
  summary="Get Merchant Profile",
)
async def get_merchant_profile(request: Request, response: Response):
  """Return the merchant profile and capabilities."""
  response.headers["Cache-Control"] = PROFILE_CACHE_CONTROL
  # Read template and perform simple substitution
  with PROFILE_TEMPLATE_PATH.open(encoding="utf-8") as f:
    template = f.read()

  # Replace placeholders
  profile_json = template.replace(
    "{{ENDPOINT}}", str(request.base_url).rstrip("/")
  ).replace("{{SHOP_ID}}", SHOP_ID)

  profile_data = json.loads(profile_json)

  # Preserve the spec-required top-level `ucp` wrapper. The UCP discovery
  # profile schema (discovery/profile.json) defines `$defs.base.required =
  # ["ucp"]`, so the served body MUST be `{"ucp": {...}}`. Default
  # payment_handlers INSIDE the ucp object rather than at the document root.
  ucp = profile_data.setdefault("ucp", {})
  ucp.setdefault("payment_handlers", {})

  # Publish the webhook-signing public key as a JWK Set member so platforms
  # can verify our order-event deliveries (order.md, Webhook Signature
  # Verification / signatures.md, Key Discovery). At 2026-08-25 the canonical
  # field is the top-level `keys[]` (profile.json $defs/base), which replaced
  # the earlier `signing_keys[]`.
  jwk = webhook_signer.public_jwk()
  profile_data.setdefault("keys", []).append(jwk)

  return profile_data


@router.get(
  "/.well-known/oauth-protected-resource",
  response_model=dict,
  summary="OAuth protected resource metadata (RFC 9728)",
)
async def get_protected_resource_metadata(request: Request, response: Response):
  """Tell platforms which authorization server protects this business."""
  response.headers["Cache-Control"] = PROFILE_CACHE_CONTROL
  base = str(request.base_url).rstrip("/")
  doc = {
    "resource": ucp_auth.audience() or base,
    "scopes_supported": list(ucp_auth.ALL_SCOPES),
    "bearer_methods_supported": ["header"],
  }
  if ucp_auth.issuer() != "https:///":
    doc["authorization_servers"] = [ucp_auth.issuer().rstrip("/")]
  return doc
