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

"""Merchant loyalty program: recognition, redemption (split payment) and earn.

Points are redeemed as one instrument of a split payment
(`dev.ucp.common.payment.split_payments`). The handler below is a mock: it
reserves points from the buyer's balance, and the remainder is paid by whatever
other instrument the platform supplies.

Program parameters can be overridden with environment variables so the demo can
be tuned without a deploy.
"""

import dataclasses
import hashlib
import os

# Reverse-domain key for this merchant's program in the `loyalty` map and in
# `context.eligibility`.
PROGRAM_KEY = os.environ.get("LOYALTY_PROGRAM_KEY", "mock.flowershop.rewards")
PROGRAM_NAME = os.environ.get("LOYALTY_PROGRAM_NAME", "Flower Shop Rewards")
CURRENCY_NAME = os.environ.get("LOYALTY_CURRENCY_NAME", "Petals")
CURRENCY_CODE = os.environ.get("LOYALTY_CURRENCY_CODE", "PTL")

HANDLER_ID = "mock_loyalty_points"
HANDLER_KEY = "mock.loyalty_points"
INSTRUMENT_TYPE = "loyalty"


@dataclasses.dataclass(frozen=True)
class Rules:
  """Program economics. All money is in minor units (cents)."""

  # Points are worth `cents_per_point` cents when redeemed. 1 = 100 points/$1.
  cents_per_point: int
  # Points earned per whole dollar of the order total (after discounts).
  earn_points_per_dollar: int
  # Maximum share of the order total points may cover, in percent.
  max_redeem_percent: int


def rules() -> Rules:
  """Return the current program rules (read at call time)."""
  return Rules(
    cents_per_point=int(os.environ.get("LOYALTY_CENTS_PER_POINT", "1")),
    earn_points_per_dollar=int(os.environ.get("LOYALTY_EARN_PER_DOLLAR", "1")),
    max_redeem_percent=int(os.environ.get("LOYALTY_MAX_REDEEM_PERCENT", "50")),
  )


def points_to_cents(points: int) -> int:
  """Convert points to checkout-currency minor units."""
  return points * rules().cents_per_point


def cents_to_points(cents: int) -> int:
  """Convert minor units to whole points, rounding down."""
  return cents // rules().cents_per_point


def max_redeemable_cents(order_total: int) -> int:
  """Most the order total can be paid with points."""
  return order_total * rules().max_redeem_percent // 100


def earn_for_total(order_total: int) -> int:
  """Points earned for an order total (whole dollars only)."""
  return (order_total // 100) * rules().earn_points_per_dollar


def reservation_id(idempotency_key: str, instrument_id: str) -> str:
  """Ledger id for a redemption: stable across retries of one request."""
  return f"redeem:{idempotency_key}:{instrument_id}"


def earn_id(checkout_id: str) -> str:
  """Ledger id for an order's earn credit: one per checkout."""
  return f"earn:{checkout_id}"


def spendable_instrument(
  sub: str, balance_points: int, order_total: int
) -> dict | None:
  """The buyer's points as a business-owned, saved payment instrument.

  Identity Linking lets a business return saved instruments in
  `payment.instruments[]` on a user-authenticated response. The platform
  treats `id` as opaque and submits this instrument at completion (with or
  without an `amount`) to spend points as part of a split payment.

  Returns None when there is nothing to spend or no order to apply it to.
  """
  if balance_points <= 0 or order_total <= 0:
    return None
  r = rules()
  balance_cents = points_to_cents(balance_points)
  max_cents = min(balance_cents, max_redeemable_cents(order_total))
  # Whole points only.
  max_cents -= max_cents % r.cents_per_point
  digest = hashlib.sha256(f"loyalty:{sub}".encode()).hexdigest()[:12]
  return {
    "id": f"loyalty_{digest}",
    "handler_id": HANDLER_ID,
    "type": INSTRUMENT_TYPE,
    "display": {
      "program": PROGRAM_NAME,
      "currency": {"name": CURRENCY_NAME, "code": CURRENCY_CODE},
      "balance_points": balance_points,
      "balance_cents": balance_cents,
      "max_redeemable_points": cents_to_points(max_cents),
      "max_redeemable_cents": max_cents,
      "cents_per_point": r.cents_per_point,
      "max_redeem_percent": r.max_redeem_percent,
    },
  }


def membership(loyalty_id: str | None, order_total: int | None = None) -> dict | None:
  """Build the `loyalty` map for an authenticated buyer, or None.

  Recognition comes from the validated token, never from the request. The
  account is returned as verified (`provisional: false`). The program has no
  tiers, so `tiers` is omitted, which the spec allows for flat programs.
  """
  if not loyalty_id:
    return None
  digits = str(loyalty_id)
  reward: dict = {"currency": {"name": CURRENCY_NAME, "code": CURRENCY_CODE}}
  if order_total is not None:
    reward["earning_forecast"] = {
      "amount": earn_for_total(order_total),
      "breakdown": [
        {
          "id": "base",
          "amount": earn_for_total(order_total),
          "description": f"{rules().earn_points_per_dollar} {CURRENCY_NAME}"
          " per $1 spent",
        }
      ],
    }
  return {
    PROGRAM_KEY: {
      "id": digits,
      "display_id": f"****{digits[-4:]}",
      "name": PROGRAM_NAME,
      "provisional": False,
      "rewards": [reward],
    }
  }
