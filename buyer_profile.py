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

"""Turn the authenticated buyer's token claims into UCP buyer and address data.

Identity Linking lets a business populate `buyer` and saved fulfillment
destinations from its own stored state for a user-authenticated request. The
merchant never accepts these from the caller as identity: they come only from
the validated access token.
"""

import hashlib
from typing import Any

import ucp_auth

_ADDRESS_KEYS = (
  "street_address",
  "address_locality",
  "address_region",
  "postal_code",
  "address_country",
)


def split_name(full_name: str | None) -> tuple[str | None, str | None]:
  """Split a display name into (first, last) on the last space."""
  if not full_name:
    return None, None
  parts = full_name.strip().split()
  if len(parts) == 1:
    return parts[0], None
  return " ".join(parts[:-1]), parts[-1]


def buyer_from(identity: ucp_auth.Identity | None) -> dict[str, Any] | None:
  """Return a UCP `buyer` object for the token's user, or None."""
  if identity is None:
    return None
  first, last = split_name(identity.name)
  buyer = {
    k: v
    for k, v in {
      "first_name": first,
      "last_name": last,
      "email": identity.email,
    }.items()
    if v
  }
  return buyer or None


def address_from(identity: ucp_auth.Identity | None) -> dict[str, Any] | None:
  """Return the saved address as flat UCP postal-address fields, or None.

  Accepts the Auth0 user_metadata shape ({street, city, state, zip, country})
  and tolerates UCP-style keys. Returns None unless there is enough to ship
  to: a street, a city and a country.
  """
  raw = identity.shipping_address if identity else None
  if not raw:
    return None

  def pick(*keys: str) -> str | None:
    for key in keys:
      value = raw.get(key)
      if isinstance(value, str) and value.strip():
        return value.strip()
    return None

  fields = {
    "street_address": pick("street_address", "street", "address_line1"),
    "address_locality": pick("address_locality", "city", "locality"),
    "address_region": pick("address_region", "state", "region"),
    "postal_code": pick("postal_code", "zip", "postcode"),
    "address_country": pick("address_country", "country"),
  }
  if not (
    fields["street_address"]
    and fields["address_locality"]
    and fields["address_country"]
  ):
    return None
  fields = {k: v for k, v in fields.items() if v}
  # Stable, opaque, buyer-scoped id: the same address always gets the same id.
  basis = "|".join(str(fields.get(k, "")) for k in _ADDRESS_KEYS)
  stable = raw.get("id") or hashlib.sha256(basis.encode()).hexdigest()[:16]
  fields["id"] = f"saved_{stable}"
  if raw.get("label"):
    fields["label"] = str(raw["label"])
  return fields
