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

import datetime
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


# Saved cards -----------------------------------------------------------------

SAVED_CARD_PREFIX = "pm_"
CARD_HANDLER_ID = "mock_payment_handler"

_BRANDS = {
  "american express": "amex",
  "amex": "amex",
  "mastercard": "mastercard",
  "master card": "mastercard",
  "visa": "visa",
  "discover": "discover",
  "diners club": "diners",
  "jcb": "jcb",
}


def card_id(sub: str, raw_id: str) -> str:
  """Opaque, buyer-scoped id for a saved card.

  Derived from the buyer's `sub`, so another buyer's token can never produce
  (or match) it, and the stored id is not exposed.
  """
  digest = hashlib.sha256(f"card|{sub}|{raw_id}".encode()).hexdigest()[:16]
  return f"{SAVED_CARD_PREFIX}{digest}"


def _brand(value: Any) -> str:
  text = str(value or "card").strip().lower()
  return _BRANDS.get(text, text.replace(" ", "_") or "card")


def saved_cards(
  identity: ucp_auth.Identity | None,
  today: datetime.date | None = None,
) -> dict[str, dict[str, Any]]:
  """Return the buyer's valid saved cards keyed by their opaque id.

  Malformed entries are skipped. Each value holds the display fields plus
  `expired` (a card is good through the end of its expiry month) and `default`.
  """
  if identity is None or not identity.payment_methods:
    return {}
  today = today or datetime.datetime.now(datetime.timezone.utc).date()
  cards: dict[str, dict[str, Any]] = {}
  for method in identity.payment_methods:
    raw_id = method.get("id")
    last4 = str(method.get("last4") or "")
    month, year = method.get("expMonth"), method.get("expYear")
    if (
      not isinstance(raw_id, str)
      or not raw_id
      or not (len(last4) == 4 and last4.isdigit())
      or not isinstance(month, int)
      or isinstance(month, bool)
      or not 1 <= month <= 12
      or not isinstance(year, int)
      or isinstance(year, bool)
    ):
      continue
    brand = _brand(method.get("brand"))
    label = str(method.get("brand") or "Card").strip()
    cards[card_id(identity.sub, raw_id)] = {
      "display": {
        "brand": brand,
        "last_digits": last4,
        "expiry_month": month,
        "expiry_year": year,
        "description": f"{label} ending in {last4}, expires {month:02d}/{year}",
        "default": bool(method.get("isDefault")),
      },
      "expired": (year, month) < (today.year, today.month),
    }
  return cards


def saved_card_instruments(
  identity: ucp_auth.Identity | None,
) -> list[dict[str, Any]]:
  """Saved cards as business-owned payment instruments (display only).

  Expired cards are not offered. No credential is returned: the business
  resolves it server-side when the buyer selects the instrument.
  """
  return [
    {
      "id": instrument_id,
      "handler_id": CARD_HANDLER_ID,
      "type": "card",
      "display": card["display"],
    }
    for instrument_id, card in saved_cards(identity).items()
    if not card["expired"]
  ]
