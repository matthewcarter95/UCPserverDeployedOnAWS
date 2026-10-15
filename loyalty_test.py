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

"""Tests for token validation, loyalty points and split-payment completion.

Tokens are real RS256 JWTs signed with a throwaway key; only the JWKS fetch is
replaced. Reuses the checkout fixtures from integration_test.IntegrationTest.
"""

import asyncio
import os
import time
from unittest import mock

from absl.testing import absltest
from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
import config
import db
import integration_test as it
import loyalty
import ucp_auth

ISSUER_DOMAIN = "test-tenant.example.com"
AUDIENCE = "api://ucp.session.service"
NS = "https://demo.okta.com"
CLAIM = f"{NS}/loyaltyId"
LOYALTY_ID = "4015782993"
START_POINTS = 3000

SAVED_ADDRESS = {
  "id": "9c708820-f889-47c2-9ce5-299d7ef6212a",
  "label": "Home",
  "street": "25 Beacon St",
  "city": "Boston",
  "state": "Massachusetts",
  "zip": "02116",
  "country": "US",
}

CARDS = [
  {"id": "9ef2ff31-a577-48b7-b950-86ba4b67f539", "brand": "American Express",
   "last4": "4321", "expMonth": 12, "expYear": 2099, "isDefault": True,
   "name": "Pat Buyer"},
  {"id": "old-card", "brand": "Visa", "last4": "1111", "expMonth": 1,
   "expYear": 2020, "isDefault": False, "name": "Pat Buyer"},
  {"id": "bad-last4", "brand": "Visa", "last4": "12", "expMonth": 5, "expYear": 2099},
  {"id": "bad-month", "brand": "Visa", "last4": "2222", "expMonth": 13, "expYear": 2099},
  {"brand": "Visa", "last4": "3333", "expMonth": 5, "expYear": 2099},
  "not-a-dict",
]

MANAGE = ucp_auth.SCOPE_CHECKOUT_MANAGE
COMPLETE = ucp_auth.SCOPE_CHECKOUT_COMPLETE


def ucp_auth_card_id(raw_id: str, sub: str = "auth0|buyer1") -> str:
  """The opaque id the merchant derives for a stored card (test helper)."""
  import buyer_profile

  return buyer_profile.card_id(sub, raw_id)


class LoyaltyTest(it.IntegrationTest):
  """Auth + loyalty tests on top of the SQLite-backed app.

  Subclasses the integration fixtures; the inherited checkout tests are
  stripped below so only the tests in this class run.
  """

  @classmethod
  def setUpClass(cls) -> None:
    super().setUpClass()
    cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

  def setUp(self) -> None:
    self._env = mock.patch.dict(
      os.environ,
      {
        "AUTH0_DOMAIN": ISSUER_DOMAIN,
        "AUTH_AUDIENCE": AUDIENCE,
        "AUTH_MODE": "optional",
        "LOYALTY_CLAIM": CLAIM,
        "CLAIM_NAMESPACE": NS,
        "LOYALTY_CENTS_PER_POINT": "1",
        "LOYALTY_EARN_PER_DOLLAR": "1",
        "LOYALTY_MAX_REDEEM_PERCENT": "50",
      },
    )
    self._env.start()
    self._jwks = mock.patch.object(
      ucp_auth, "_signing_key", lambda token: self.key.public_key()
    )
    self._jwks.start()
    super().setUp()
    asyncio.run(self._seed_loyalty())

  def tearDown(self) -> None:
    self._jwks.stop()
    self._env.stop()
    super().tearDown()

  async def _seed_loyalty(self) -> None:
    async with self.transactions_session_factory() as session:
      session.add(
        db.LoyaltyAccount(loyalty_id=LOYALTY_ID, balance=START_POINTS)
      )
      await session.commit()

  async def _seed_shipping_rate(self) -> None:
    """Only the tests that price shipping add this; others expect none."""
    async with self.transactions_session_factory() as session:
      session.add(
        db.ShippingRate(
          id="std-ship",
          country_code="default",
          service_level="standard",
          price=500,
          title="Standard Shipping",
        )
      )
      await session.commit()

  def _balance(self) -> int:
    async def read() -> int | None:
      async with self.transactions_session_factory() as session:
        return await db.get_loyalty_balance(session, LOYALTY_ID)

    return asyncio.run(read())

  def _ledger(self, entry_id: str) -> dict | None:
    async def read() -> dict | None:
      async with self.transactions_session_factory() as session:
        return await db.get_loyalty_entry(session, entry_id)

    return asyncio.run(read())

  def _token(
    self,
    scopes: tuple[str, ...] = (MANAGE, COMPLETE),
    loyalty_id: str | None = LOYALTY_ID,
    profile: bool = False,
    cards: list | None = None,
    **overrides,
  ) -> str:
    now = int(time.time())
    claims = {
      "iss": f"https://{ISSUER_DOMAIN}/",
      "aud": AUDIENCE,
      "sub": "auth0|buyer1",
      "azp": "agent-client",
      "iat": now,
      "exp": now + 300,
      "scope": " ".join(scopes),
    }
    if loyalty_id is not None:
      claims[CLAIM] = loyalty_id
    if cards is not None:
      claims[f"{NS}/paymentMethods"] = cards
    if profile:
      claims[f"{NS}/name"] = "Pat Q Buyer"
      claims[f"{NS}/email"] = "pat@example.com"
      claims[f"{NS}/shippingAddress"] = dict(SAVED_ADDRESS)
    claims.update(overrides)
    return jwt.encode(claims, self.key, algorithm="RS256")

  def _headers(self, key: str, token: str | None = None) -> dict[str, str]:
    headers = self._get_headers(idempotency_key=key, request_id=key)
    if token:
      headers["authorization"] = f"Bearer {token}"
    return headers

  def _new_checkout(self, key: str, token: str | None, qty: int = 2) -> str:
    payload = self._create_checkout_payload(
      f"co_{key}", [("rose", "Red Rose", 1000, qty)]
    )
    response = self.client.post(
      "/checkout-sessions",
      headers=self._headers(key, token),
      json=payload.model_dump(mode="json", exclude_none=True),
    )
    self.assertEqual(response.status_code, 201, response.text)
    return self.get_resource_id(response.json()["id"]), response

  def _complete(self, checkout_id, key, token, instruments):
    return self.client.post(
      f"/checkout-sessions/{checkout_id}/complete",
      headers=self._headers(key, token),
      json={"payment": {"instruments": instruments}, "risk_signals": {}},
    )

  @staticmethod
  def _points(amount=None, instrument_id="pts_1", **extra) -> dict:
    instrument = {
      "id": instrument_id,
      "handler_id": loyalty.HANDLER_ID,
      "type": "loyalty",
      "credential": {"type": "loyalty", "token": "ignored"},
      **extra,
    }
    if amount is not None:
      instrument["amount"] = amount
    return instrument

  @staticmethod
  def _card(token="success_token") -> dict:
    return {
      "id": "card_1",
      "handler_id": "mock_payment_handler",
      "type": "card",
      "credential": {"type": "token", "token": token},
    }

  # ----- token validation -------------------------------------------------

  def test_valid_token_is_accepted_and_recognizes_member(self) -> None:
    with self.client:
      _, response = self._new_checkout("a1", self._token())
    membership = response.json()["loyalty"][loyalty.PROGRAM_KEY]
    self.assertEqual(membership["display_id"], "****2993")
    self.assertFalse(membership["provisional"])
    self.assertNotIn("tiers", membership)
    forecast = membership["rewards"][0]["earning_forecast"]["amount"]
    self.assertEqual(forecast, 20)  # $20.00 order * 1 point/$

  def test_no_loyalty_block_without_token(self) -> None:
    with self.client:
      _, response = self._new_checkout("a2", None)
    self.assertNotIn("loyalty", response.json())

  def test_expired_token_rejected_in_enforce_mode(self) -> None:
    os.environ["AUTH_MODE"] = "enforce"
    token = self._token(exp=int(time.time()) - 600)
    with self.client:
      payload = self._create_checkout_payload(
        "x", [("rose", "Red Rose", 1000, 1)]
      )
      response = self.client.post(
        "/checkout-sessions",
        headers=self._headers("e1", token),
        json=payload.model_dump(mode="json", exclude_none=True),
      )
    self.assertEqual(response.status_code, 401)
    self.assertIn('error="invalid_token"', response.headers["www-authenticate"])
    self.assertEqual(response.json()["messages"][0]["code"], "identity_required")

  def test_wrong_audience_and_issuer_rejected(self) -> None:
    os.environ["AUTH_MODE"] = "enforce"
    for overrides in ({"aud": "api://other"}, {"iss": "https://evil.example/"}):
      token = self._token(**overrides)
      with self.client:
        payload = self._create_checkout_payload(
          "x", [("rose", "Red Rose", 1000, 1)]
        )
        response = self.client.post(
          "/checkout-sessions",
          headers=self._headers(f"w{len(overrides)}{overrides}", token),
          json=payload.model_dump(mode="json", exclude_none=True),
        )
      self.assertEqual(response.status_code, 401, overrides)

  def test_token_signed_by_other_key_rejected(self) -> None:
    os.environ["AUTH_MODE"] = "enforce"
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    forged = jwt.encode(
      {
        "iss": f"https://{ISSUER_DOMAIN}/",
        "aud": AUDIENCE,
        "sub": "auth0|buyer1",
        "exp": now + 300,
        "scope": f"{MANAGE} {COMPLETE}",
      },
      other,
      algorithm="RS256",
    )
    with self.client:
      payload = self._create_checkout_payload(
        "x", [("rose", "Red Rose", 1000, 1)]
      )
      response = self.client.post(
        "/checkout-sessions",
        headers=self._headers("f1", forged),
        json=payload.model_dump(mode="json", exclude_none=True),
      )
    self.assertEqual(response.status_code, 401)

  def test_enforce_requires_token_and_scope(self) -> None:
    os.environ["AUTH_MODE"] = "enforce"
    with self.client:
      payload = self._create_checkout_payload(
        "x", [("rose", "Red Rose", 1000, 1)]
      ).model_dump(mode="json", exclude_none=True)
      anonymous = self.client.post(
        "/checkout-sessions", headers=self._headers("n1"), json=payload
      )
      self.assertEqual(anonymous.status_code, 401)
      self.assertNotIn("error=", anonymous.headers["www-authenticate"])
      self.assertIn("resource_metadata=", anonymous.headers["www-authenticate"])

      no_scope = self.client.post(
        "/checkout-sessions",
        headers=self._headers("n2", self._token(scopes=("openid",))),
        json=payload,
      )
      self.assertEqual(no_scope.status_code, 403)
      challenge = no_scope.headers["www-authenticate"]
      self.assertIn('error="insufficient_scope"', challenge)
      self.assertIn(f'scope="{MANAGE}"', challenge)
      self.assertEqual(
        no_scope.json()["messages"][0]["code"], "insufficient_scope"
      )

      ok = self.client.post(
        "/checkout-sessions",
        headers=self._headers("n3", self._token(scopes=(MANAGE,))),
        json=payload,
      )
      self.assertEqual(ok.status_code, 201, ok.text)

  def test_legacy_scope_manages_but_never_completes(self) -> None:
    os.environ["AUTH_MODE"] = "enforce"
    token = self._token(scopes=(ucp_auth.LEGACY_MANAGE_SCOPE,))
    with self.client:
      checkout_id, _ = self._new_checkout("l1", token)
      response = self._complete(checkout_id, "l2", token, [self._card()])
    self.assertEqual(response.status_code, 403)
    self.assertIn(f'scope="{COMPLETE}"', response.headers["www-authenticate"])

  def test_orders_are_scope_gated_in_enforce_mode(self) -> None:
    token = self._token()
    with self.client:
      checkout_id, _ = self._new_checkout("g1", token)
      done = self._complete(checkout_id, "g2", token, [self._card()])
      order_id = done.json()["order"]["id"]
      os.environ["AUTH_MODE"] = "enforce"
      anonymous = self.client.get(
        f"/orders/{order_id}", headers=self._headers("g3")
      )
      no_scope = self.client.get(
        f"/orders/{order_id}", headers=self._headers("g4", self._token())
      )
      read = self.client.get(
        f"/orders/{order_id}",
        headers=self._headers(
          "g5", self._token(scopes=(ucp_auth.SCOPE_ORDER_READ,))
        ),
      )
    self.assertEqual(anonymous.status_code, 401)
    self.assertEqual(no_scope.status_code, 403)
    self.assertIn(
      f'scope="{ucp_auth.SCOPE_ORDER_READ}"',
      no_scope.headers["www-authenticate"],
    )
    self.assertEqual(read.status_code, 200, read.text)

  def test_client_allowlist(self) -> None:
    os.environ["AUTH_MODE"] = "enforce"
    os.environ["AUTH_ALLOWED_CLIENT_IDS"] = "agent-client"
    payload = self._create_checkout_payload(
      "x", [("rose", "Red Rose", 1000, 1)]
    ).model_dump(mode="json", exclude_none=True)
    with self.client:
      allowed = self.client.post(
        "/checkout-sessions", headers=self._headers("al1", self._token()), json=payload
      )
      blocked = self.client.post(
        "/checkout-sessions",
        headers=self._headers("al2", self._token(azp="other-client")),
        json=payload,
      )
    self.assertEqual(allowed.status_code, 201, allowed.text)
    self.assertEqual(blocked.status_code, 401)

  # ----- stored buyer profile + spendable points -------------------------

  def _create_plain(self, key, token, **body):
    return self.client.post(
      "/checkout-sessions",
      headers=self._headers(key, token),
      json={"line_items": [{"item": {"id": "rose"}, "quantity": 2}], **body},
    )

  def test_profile_is_populated_from_the_token(self) -> None:
    with self.client:
      response = self._create_plain("pr1", self._token(profile=True))
    self.assertEqual(response.status_code, 201, response.text)
    body = response.json()
    self.assertEqual(
      body["buyer"],
      {"first_name": "Pat Q", "last_name": "Buyer", "email": "pat@example.com"},
    )
    methods = body["fulfillment"]["methods"]
    self.assertEqual(len(methods), 1)
    destination = methods[0]["destinations"][0]
    self.assertEqual(destination["type"], "shipping_address")
    self.assertEqual(destination["street_address"], "25 Beacon St")
    self.assertEqual(destination["address_locality"], "Boston")
    self.assertEqual(destination["address_region"], "Massachusetts")
    self.assertEqual(destination["postal_code"], "02116")
    self.assertEqual(destination["address_country"], "US")
    self.assertTrue(destination["id"].startswith("saved_"))
    # Offered, never selected for the buyer.
    self.assertIsNone(methods[0].get("selected_destination_id"))

  def test_platform_supplied_buyer_overrides_stored(self) -> None:
    with self.client:
      response = self._create_plain(
        "pr2",
        self._token(profile=True),
        buyer={"email": "other@example.com"},
      )
    buyer = response.json()["buyer"]
    self.assertEqual(buyer["email"], "other@example.com")
    self.assertEqual(buyer["first_name"], "Pat Q")

  def test_no_profile_without_claims_or_token(self) -> None:
    with self.client:
      no_claims = self._create_plain("pr3", self._token())
      anonymous = self._create_plain("pr4", None)
    for response in (no_claims, anonymous):
      self.assertEqual(response.status_code, 201, response.text)
      self.assertNotIn("buyer", response.json())
      self.assertNotIn("fulfillment", response.json())

  def test_incomplete_saved_address_is_not_offered(self) -> None:
    token = self._token(profile=True)
    claims = jwt.decode(token, options={"verify_signature": False})
    claims[f"{NS}/shippingAddress"] = {"label": "Home", "city": "Boston"}
    bad = jwt.encode(claims, self.key, algorithm="RS256")
    with self.client:
      response = self._create_plain("pr5", bad)
    self.assertNotIn("fulfillment", response.json())

  def test_saved_address_is_selectable_through_checkout(self) -> None:
    asyncio.run(self._seed_shipping_rate())
    token = self._token(profile=True)
    with self.client:
      created = self._create_plain("pr6", token).json()
      checkout_id = self.get_resource_id(created["id"])
      line_id = created["line_items"][0]["id"]
      method = created["fulfillment"]["methods"][0]
      saved_id = method["destinations"][0]["id"]
      line_items = [{"id": line_id, "item": {"id": "rose"}, "quantity": 2}]
      with_options = self.client.put(
        f"/checkout-sessions/{checkout_id}",
        headers=self._headers("pr7", token),
        json={
          "id": checkout_id,
          "line_items": line_items,
          "fulfillment": {
            "methods": [
              {
                "id": method["id"],
                "type": "shipping",
                "line_item_ids": [line_id],
                "selected_destination_id": saved_id,
                "groups": [{"id": "g", "line_item_ids": [line_id]}],
              }
            ]
          },
        },
      )
    self.assertEqual(with_options.status_code, 200, with_options.text)
    method_back = with_options.json()["fulfillment"]["methods"][0]
    self.assertEqual(
      method_back["selected_destination_id"], saved_id
    )
    group = method_back["groups"][0]
    self.assertTrue(group["options"], "options are priced for the saved address")

  def test_points_offered_as_a_saved_instrument(self) -> None:
    with self.client:
      response = self._create_plain("pt1", self._token())  # $20.00
    instruments = response.json()["payment"]["instruments"]
    self.assertEqual(len(instruments), 1)
    points = instruments[0]
    self.assertEqual(points["handler_id"], loyalty.HANDLER_ID)
    self.assertEqual(points["type"], "loyalty")
    display = points["display"]
    self.assertEqual(display["balance_points"], START_POINTS)
    self.assertEqual(display["max_redeemable_cents"], 1000)  # 50% of $20
    self.assertEqual(display["max_redeemable_points"], 1000)
    self.assertNotIn("credential", points)

  def test_offered_points_instrument_can_be_spent(self) -> None:
    """Saved address + offered points, end to end, with nothing typed in."""
    asyncio.run(self._seed_shipping_rate())
    token = self._token(profile=True)
    with self.client:
      created = self._create_plain("pt2", token).json()
      checkout_id = self.get_resource_id(created["id"])
      line_id = created["line_items"][0]["id"]
      method = created["fulfillment"]["methods"][0]
      saved_id = method["destinations"][0]["id"]
      offered = created["payment"]["instruments"][0]
      line_items = [{"id": line_id, "item": {"id": "rose"}, "quantity": 2}]

      def put(key, group):
        return self.client.put(
          f"/checkout-sessions/{checkout_id}",
          headers=self._headers(key, token),
          json={
            "id": checkout_id,
            "line_items": line_items,
            "fulfillment": {
              "methods": [
                {
                  "id": method["id"],
                  "type": "shipping",
                  "line_item_ids": [line_id],
                  "selected_destination_id": saved_id,
                  "groups": [group],
                }
              ]
            },
          },
        )

      priced = put("pt2a", {"id": "g", "line_item_ids": [line_id]}).json()
      group = priced["fulfillment"]["methods"][0]["groups"][0]
      chosen = put(
        "pt2b",
        {
          "id": group["id"],
          "line_item_ids": [line_id],
          "selected_option_id": group["options"][0]["id"],
        },
      )
      self.assertEqual(chosen.status_code, 200, chosen.text)
      # Totals now include shipping; the points offer tracks the new total.
      offered = chosen.json()["payment"]["instruments"][0]
      cap = offered["display"]["max_redeemable_cents"]
      total = [
        t["amount"] for t in chosen.json()["totals"] if t["type"] == "total"
      ][0]
      self.assertEqual(cap, total // 2)
      spend = {
        "id": offered["id"],
        "handler_id": offered["handler_id"],
        "type": offered["type"],
        "credential": {"type": "loyalty", "token": "n/a"},
      }  # open amount: the merchant decides, up to the cap
      response = self._complete(
        checkout_id, "pt3", token, [spend, self._card()]
      )
    self.assertEqual(response.status_code, 200, response.text)
    self.assertEqual(self._balance(), START_POINTS - cap + (total - cap) // 100)

  def test_no_points_offer_when_nothing_to_spend(self) -> None:
    asyncio.run(self._set_balance(0))
    with self.client:
      empty = self._create_plain("pt4", self._token())
      stranger = self._create_plain("pt5", self._token(loyalty_id="1111111111"))
    self.assertNotIn("payment", empty.json())
    self.assertNotIn("payment", stranger.json())

  def test_get_reflects_current_balance_and_buyer_only(self) -> None:
    token = self._token()
    with self.client:
      created = self._create_plain("pt6", token)
      checkout_id = self.get_resource_id(created.json()["id"])
      asyncio.run(self._set_balance(400))
      mine = self.client.get(
        f"/checkout-sessions/{checkout_id}", headers=self._headers("pt7", token)
      )
      anonymous = self.client.get(
        f"/checkout-sessions/{checkout_id}", headers=self._headers("pt8")
      )
    self.assertEqual(
      mine.json()["payment"]["instruments"][0]["display"]["balance_points"], 400
    )
    self.assertNotIn("payment", anonymous.json())
    self.assertNotIn("loyalty", anonymous.json())

  def test_idempotent_replay_does_not_leak_profile_to_another_buyer(self) -> None:
    with self.client:
      mine = self._create_plain("pl1", self._token(profile=True))
      other = self._create_plain(
        "pl1", self._token(profile=True, sub="auth0|someone-else")
      )
    self.assertEqual(mine.status_code, 201, mine.text)
    self.assertEqual(other.status_code, 409)

  # ----- saved payment methods -------------------------------------------

  def _offered_cards(self, response):
    return [
      i
      for i in response.json().get("payment", {}).get("instruments", [])
      if i["type"] == "card"
    ]

  def test_saved_cards_are_offered_display_only(self) -> None:
    with self.client:
      response = self._create_plain("sc1", self._token(cards=CARDS))
    cards = self._offered_cards(response)
    self.assertEqual(len(cards), 1, "expired and malformed entries are dropped")
    card = cards[0]
    self.assertTrue(card["id"].startswith("pm_"))
    self.assertNotIn("9ef2ff31", card["id"], "the stored id is not exposed")
    self.assertEqual(card["handler_id"], "mock_payment_handler")
    self.assertNotIn("credential", card)
    self.assertEqual(card["display"]["brand"], "amex")
    self.assertEqual(card["display"]["last_digits"], "4321")
    self.assertEqual(card["display"]["expiry_year"], 2099)
    self.assertIn("ending in 4321", card["display"]["description"])
    self.assertTrue(card["display"]["default"])
    self.assertNotIn("Pat Buyer", response.text, "cardholder name is not returned")

  def test_no_cards_offered_without_claim_or_token(self) -> None:
    with self.client:
      none = self._create_plain("sc2", self._token())
      anonymous = self._create_plain("sc3", None)
      empty = self._create_plain("sc4", self._token(cards=[]))
    for response in (none, anonymous, empty):
      self.assertEqual(self._offered_cards(response), [], response.text)

  def test_pay_with_saved_card_by_id_only(self) -> None:
    token = self._token(cards=CARDS)
    with self.client:
      checkout_id, created = self._new_checkout("sc5", token)
      card = self._offered_cards(created)[0]
      pay = {"id": card["id"], "handler_id": card["handler_id"], "type": "card"}
      response = self._complete(checkout_id, "sc6", token, [pay])
    self.assertEqual(response.status_code, 200, response.text)
    self.assertEqual(response.json()["status"], "completed")

  def test_saved_card_with_points_split(self) -> None:
    token = self._token(cards=CARDS)
    with self.client:
      checkout_id, created = self._new_checkout("sc7", token)  # $20.00
      card = self._offered_cards(created)[0]
      pay = {"id": card["id"], "handler_id": card["handler_id"], "type": "card"}
      response = self._complete(
        checkout_id, "sc8", token, [self._points(amount=500), pay]
      )
    self.assertEqual(response.status_code, 200, response.text)
    amounts = {
      i["id"]: i["amount"] for i in response.json()["payment"]["instruments"]
    }
    self.assertEqual(amounts[card["id"]], 1500)
    self.assertEqual(self._balance(), START_POINTS - 500 + 15)

  def test_another_buyers_card_id_is_refused(self) -> None:
    mine = self._token(cards=CARDS)
    other = self._other(cards=CARDS)  # same stored card id, different buyer
    with self.client:
      created = self._create_plain("sc9", mine)
      checkout_id = self.get_resource_id(created.json()["id"])
      theirs = self._offered_cards(
        self._create_plain("sc10", other)
      )[0]
      stolen = {"id": theirs["id"], "handler_id": theirs["handler_id"], "type": "card"}
      response = self._complete(checkout_id, "sc11", mine, [stolen])
    self.assertNotEqual(theirs["id"], self._offered_cards(created)[0]["id"])
    self.assertEqual(response.status_code, 402, response.text)
    self.assertEqual(
      response.json()["messages"][0]["path"], "$.payment.instruments[0]"
    )

  def test_invented_expired_and_unauthenticated_card_ids_are_refused(self) -> None:
    token = self._token(cards=CARDS)
    with self.client:
      created = self._create_plain("sc12", token)
      checkout_id = self.get_resource_id(created.json()["id"])
      expired_id = ucp_auth_card_id("old-card")
      cases = {
        "invented": "pm_0000000000000000",
        "expired": expired_id,
      }
      results = {
        name: self._complete(
          checkout_id,
          f"sc13{name}",
          token,
          [{"id": cid, "handler_id": "mock_payment_handler", "type": "card"}],
        )
        for name, cid in cases.items()
      }
      anonymous_card = self._complete(
        checkout_id,
        "sc14",
        self._token(),  # token without the cards claim
        [{"id": self._offered_cards(created)[0]["id"],
          "handler_id": "mock_payment_handler", "type": "card"}],
      )
    for name, response in results.items():
      self.assertEqual(response.status_code, 402, (name, response.text))
    self.assertEqual(anonymous_card.status_code, 402)

  def test_caller_cannot_choose_the_card_credential(self) -> None:
    """A saved-card id never lets the caller supply a failing/other token."""
    token = self._token(cards=CARDS)
    with self.client:
      checkout_id, created = self._new_checkout("sc15", token)
      card = self._offered_cards(created)[0]
      pay = {
        "id": card["id"],
        "handler_id": "mock_payment_handler",
        "type": "card",
        "credential": {"type": "token", "token": "fail_token"},
      }
      response = self._complete(checkout_id, "sc16", token, [pay])
    # The merchant supplies the credential for a saved card, so a caller-sent
    # token is ignored rather than honored.
    self.assertEqual(response.status_code, 200, response.text)

  def test_completed_checkout_no_longer_offers_cards(self) -> None:
    token = self._token(cards=CARDS)
    with self.client:
      checkout_id, created = self._new_checkout("sc17", token)
      card = self._offered_cards(created)[0]
      done = self._complete(
        checkout_id, "sc18", token,
        [{"id": card["id"], "handler_id": card["handler_id"], "type": "card"}],
      )
      after = self.client.get(
        f"/checkout-sessions/{checkout_id}", headers=self._headers("sc19", token)
      )
    self.assertEqual(done.status_code, 200)
    self.assertEqual(self._offered_cards(after), [])

  # ----- orders: owner recorded, owner-only reads, per-buyer list ---------

  def _place_order(self, key, token, qty=1):
    checkout_id, _ = self._new_checkout(key, token, qty=qty)
    done = self._complete(checkout_id, f"{key}c", token, [self._card()])
    self.assertEqual(done.status_code, 200, done.text)
    return done.json()["order"]["id"]

  def _order_row(self, order_id):
    async def read():
      async with self.transactions_session_factory() as session:
        row = await session.get(db.Order, order_id)
        return row.buyer_sub, row.buyer_email, row.created_at, row.data

    return asyncio.run(read())

  def test_order_records_the_verified_buyer(self) -> None:
    token = self._token(profile=True, email="pat@example.com")
    with self.client:
      order_id = self._place_order("or1", token)
    sub, email, created_at, data = self._order_row(order_id)
    self.assertEqual(sub, "auth0|buyer1")
    self.assertEqual(email, "pat@example.com")
    self.assertTrue(created_at)
    self.assertNotIn("buyer", data, "UCP order object stays schema-clean")

  def test_anonymous_order_has_no_owner(self) -> None:
    with self.client:
      order_id = self._place_order("or2", None)
    sub, email, _, _ = self._order_row(order_id)
    self.assertIsNone(sub)
    self.assertIsNone(email)

  def test_order_read_is_owner_only(self) -> None:
    os.environ["AUTH_MODE"] = "enforce"
    mine = self._token(scopes=(MANAGE, COMPLETE, ucp_auth.SCOPE_ORDER_READ))
    other = self._other(scopes=(ucp_auth.SCOPE_ORDER_READ,))
    with self.client:
      order_id = self._place_order("or3", mine)
      owner = self.client.get(f"/orders/{order_id}", headers=self._headers("or3a", mine))
      stranger = self.client.get(f"/orders/{order_id}", headers=self._headers("or3b", other))
      anonymous = self.client.get(f"/orders/{order_id}", headers=self._headers("or3c"))
    self.assertEqual(owner.status_code, 200, owner.text)
    self.assertEqual(owner.json()["id"], order_id)
    self.assertEqual(stranger.status_code, 404, stranger.text)
    self.assertEqual(anonymous.status_code, 401)

  def test_shipping_an_order_keeps_its_owner(self) -> None:
    mine = self._token()
    with self.client:
      order_id = self._place_order("or4", mine)
      secret = config.FLAGS.simulation_secret
      shipped = self.client.post(
        f"/testing/simulate-shipping/{order_id}",
        headers={**self._headers("or4a"), "Simulation-Secret": secret},
      )
    self.assertEqual(shipped.status_code, 200, shipped.text)
    sub, _, _, data = self._order_row(order_id)
    events = data["fulfillment"]["events"]
    self.assertEqual([e["type"] for e in events], ["shipped"], "update applied")
    self.assertEqual(sub, "auth0|buyer1", "an update must not strip the owner")

  def test_list_orders_returns_only_my_orders_newest_first(self) -> None:
    os.environ["AUTH_MODE"] = "enforce"
    scopes = (MANAGE, COMPLETE, ucp_auth.SCOPE_ORDER_READ)
    mine = self._token(scopes=scopes)
    other = self._other(scopes=scopes)
    with self.client:
      first = self._place_order("ls1", mine)
      second = self._place_order("ls2", mine)
      third = self._place_order("ls3", mine)
      theirs = self._place_order("ls4", other)
      listing = self.client.get("/orders", headers=self._headers("ls5", mine))
      page = self.client.get("/orders?limit=2&offset=0", headers=self._headers("ls6", mine))
      page2 = self.client.get("/orders?limit=2&offset=2", headers=self._headers("ls7", mine))
      other_listing = self.client.get("/orders", headers=self._headers("ls8", other))
    body = listing.json()
    ids = [o["id"] for o in body["orders"]]
    self.assertEqual(listing.status_code, 200, listing.text)
    self.assertEqual(body["pagination"]["total_count"], 3)
    self.assertEqual(set(ids), {first, second, third})
    self.assertNotIn(theirs, ids)
    self.assertEqual(ids, [third, second, first], "newest first")
    self.assertEqual(len(page.json()["orders"]), 2)
    self.assertTrue(page.json()["pagination"]["has_next_page"])
    self.assertEqual(len(page2.json()["orders"]), 1)
    self.assertFalse(page2.json()["pagination"]["has_next_page"])
    self.assertEqual([o["id"] for o in other_listing.json()["orders"]], [theirs])

  def test_list_orders_needs_identity_and_scope(self) -> None:
    os.environ["AUTH_MODE"] = "enforce"
    with self.client:
      anonymous = self.client.get("/orders", headers=self._headers("lq1"))
      no_scope = self.client.get("/orders", headers=self._headers("lq2", self._token()))
    self.assertEqual(anonymous.status_code, 401)
    self.assertIn("www-authenticate", anonymous.headers)
    self.assertEqual(no_scope.status_code, 403)
    self.assertIn(ucp_auth.SCOPE_ORDER_READ, no_scope.headers["www-authenticate"])

  def test_list_orders_is_never_anonymous_even_in_optional_mode(self) -> None:
    with self.client:
      response = self.client.get("/orders", headers=self._headers("lo1"))
    self.assertEqual(response.status_code, 401)

  # ----- ownership --------------------------------------------------------

  def _other(self, **kw) -> str:
    return self._token(sub="auth0|someone-else", loyalty_id="1111111111", **kw)

  def test_checkout_is_invisible_to_other_buyers(self) -> None:
    mine = self._token(profile=True)
    with self.client:
      created = self._create_plain("ow1", mine).json()
      cid = self.get_resource_id(created["id"])
      url = f"/checkout-sessions/{cid}"
      owner = self.client.get(url, headers=self._headers("ow2", mine))
      other_get = self.client.get(url, headers=self._headers("ow3", self._other()))
      anonymous = self.client.get(url, headers=self._headers("ow4"))
      other_complete = self._complete(cid, "ow5", self._other(), [self._card()])
      other_cancel = self.client.post(
        f"{url}/cancel", headers=self._headers("ow6", self._other())
      )
      still_there = self.client.get(url, headers=self._headers("ow7", mine))
    self.assertEqual(owner.status_code, 200)
    for denied in (other_get, anonymous, other_complete, other_cancel):
      self.assertEqual(denied.status_code, 404, denied.text)
      self.assertNotIn("pat@example.com", denied.text)
    self.assertEqual(still_there.status_code, 200)
    self.assertEqual(still_there.json()["status"], created["status"])

  def test_owner_field_never_appears_in_responses(self) -> None:
    mine = self._token(profile=True)
    with self.client:
      created = self._create_plain("ow8", mine)
      cid = self.get_resource_id(created.json()["id"])
      fetched = self.client.get(
        f"/checkout-sessions/{cid}", headers=self._headers("ow9", mine)
      )
    for response in (created, fetched):
      self.assertNotIn("_owner", response.text)
      self.assertNotIn("auth0|buyer1", response.text)

  def test_owner_survives_update_and_complete(self) -> None:
    mine = self._token()
    with self.client:
      checkout_id, _ = self._new_checkout("ow10", mine)
      done = self._complete(checkout_id, "ow11", mine, [self._card()])
      replay_other = self._complete(checkout_id, "ow12", self._other(), [self._card()])
    self.assertEqual(done.status_code, 200, done.text)
    self.assertEqual(replay_other.status_code, 404)

  def test_anonymous_checkout_stays_open(self) -> None:
    with self.client:
      created = self._create_plain("ow13", None)
      cid = self.get_resource_id(created.json()["id"])
      anyone = self.client.get(
        f"/checkout-sessions/{cid}", headers=self._headers("ow14", self._other())
      )
    self.assertEqual(anyone.status_code, 200)

  def test_cart_is_invisible_to_other_buyers_and_cannot_be_hijacked(self) -> None:
    mine = self._token()
    body = {"line_items": [{"item": {"id": "rose"}, "quantity": 1}]}
    with self.client:
      cart = self.client.post("/carts", headers=self._headers("cw1", mine), json=body)
      self.assertEqual(cart.status_code, 201, cart.text)
      cart_id = cart.json()["id"]
      owner = self.client.get(f"/carts/{cart_id}", headers=self._headers("cw2", mine))
      other = self.client.get(f"/carts/{cart_id}", headers=self._headers("cw3", self._other()))
      other_update = self.client.put(
        f"/carts/{cart_id}", headers=self._headers("cw4", self._other()),
        json={"id": cart_id, "line_items": [{"item": {"id": "rose"}, "quantity": 9}]},
      )
      other_cancel = self.client.post(
        f"/carts/{cart_id}/cancel", headers=self._headers("cw5", self._other())
      )
      other_checkout = self.client.post(
        "/checkout-sessions", headers=self._headers("cw6", self._other()),
        json={"cart_id": cart_id},
      )
      mine_checkout = self.client.post(
        "/checkout-sessions", headers=self._headers("cw7", mine),
        json={"cart_id": cart_id},
      )
      other_reuse = self.client.post(
        "/checkout-sessions", headers=self._headers("cw8", self._other()),
        json={"cart_id": cart_id},
      )
    self.assertEqual(owner.status_code, 200)
    self.assertNotIn("_owner", owner.text)
    for denied in (other, other_update, other_cancel, other_checkout, other_reuse):
      self.assertEqual(denied.status_code, 404, denied.text)
    self.assertEqual(mine_checkout.status_code, 201, mine_checkout.text)

  # ----- loyalty redemption ----------------------------------------------

  def test_split_payment_points_plus_card(self) -> None:
    token = self._token()
    with self.client:
      checkout_id, _ = self._new_checkout("s1", token)  # $20.00
      response = self._complete(
        checkout_id, "s2", token, [self._points(amount=600), self._card()]
      )
    self.assertEqual(response.status_code, 200, response.text)
    body = response.json()
    self.assertEqual(body["status"], "completed")
    amounts = {i["id"]: i["amount"] for i in body["payment"]["instruments"]}
    self.assertEqual(amounts, {"pts_1": 600, "card_1": 1400})
    # -600 redeemed, +14 earned on the $14.00 paid by card.
    self.assertEqual(self._balance(), START_POINTS - 600 + 14)
    self.assertEqual(self._ledger("redeem:s2:pts_1")["status"], "committed")

  def test_open_amount_uses_cap(self) -> None:
    token = self._token()
    with self.client:
      checkout_id, _ = self._new_checkout("o1", token)  # $20.00, cap $10.00
      response = self._complete(
        checkout_id, "o2", token, [self._points(), self._card()]
      )
    self.assertEqual(response.status_code, 200, response.text)
    amounts = {
      i["id"]: i["amount"] for i in response.json()["payment"]["instruments"]
    }
    self.assertEqual(amounts["pts_1"], 1000)
    self.assertEqual(amounts["card_1"], 1000)
    self.assertEqual(self._balance(), START_POINTS - 1000 + 10)

  def test_card_failure_releases_points(self) -> None:
    token = self._token()
    with self.client:
      checkout_id, _ = self._new_checkout("c1", token)
      response = self._complete(
        checkout_id,
        "c2",
        token,
        [self._points(amount=500), self._card("fail_token")],
      )
      self.assertEqual(response.status_code, 402, response.text)
      message = response.json()["messages"][0]
      self.assertEqual(message["path"], "$.payment.instruments[1]")
      self.assertEqual(self._balance(), START_POINTS)
      self.assertEqual(self._ledger("redeem:c2:pts_1")["status"], "released")
      # The checkout is still open and can be paid with a good card.
      retry = self._complete(
        checkout_id,
        "c3",
        token,
        [self._points(amount=500), self._card()],
      )
    self.assertEqual(retry.status_code, 200, retry.text)
    self.assertEqual(self._balance(), START_POINTS - 500 + 15)

  def test_insufficient_points(self) -> None:
    token = self._token()
    asyncio.run(self._set_balance(100))
    with self.client:
      checkout_id, _ = self._new_checkout("i1", token)
      response = self._complete(
        checkout_id, "i2", token, [self._points(amount=500), self._card()]
      )
    self.assertEqual(response.status_code, 402)
    self.assertEqual(
      response.json()["messages"][0]["path"], "$.payment.instruments[0]"
    )
    self.assertEqual(self._balance(), 100)

  async def _set_balance(self, balance: int) -> None:
    async with self.transactions_session_factory() as session:
      account = await session.get(db.LoyaltyAccount, LOYALTY_ID)
      account.balance = balance
      await session.commit()

  def test_amount_above_cap_or_total_rejected(self) -> None:
    token = self._token()
    with self.client:
      checkout_id, _ = self._new_checkout("m1", token)  # $20.00, cap $10.00
      over_cap = self._complete(
        checkout_id, "m2", token, [self._points(amount=1500), self._card()]
      )
      over_total = self._complete(
        checkout_id, "m3", token, [self._points(amount=2500), self._card()]
      )
      zero = self._complete(
        checkout_id, "m4", token, [self._points(amount=0), self._card()]
      )
      no_remainder_instrument = self._complete(
        checkout_id, "m5", token, [self._points(amount=500)]
      )
    for response in (over_cap, over_total, zero, no_remainder_instrument):
      self.assertEqual(response.status_code, 400, response.text)
    self.assertEqual(self._balance(), START_POINTS)

  def test_points_need_identity_scope_and_account(self) -> None:
    with self.client:
      # Anonymous checkout (no owner): spending points still needs a buyer.
      open_id, _ = self._new_checkout("p0", None)
      anonymous = self._complete(
        open_id, "p2", None, [self._points(amount=100), self._card()]
      )
      checkout_id, _ = self._new_checkout("p1", self._token())
      manage_only = self._complete(
        checkout_id,
        "p3",
        self._token(scopes=(MANAGE,)),
        [self._points(amount=100), self._card()],
      )
      no_account = self._complete(
        checkout_id,
        "p4",
        self._token(loyalty_id=None),
        [self._points(amount=100), self._card()],
      )
      stranger = self._complete(
        checkout_id,
        "p5",
        self._other(),
        [self._points(amount=100), self._card()],
      )
    self.assertEqual(anonymous.status_code, 401)
    self.assertEqual(manage_only.status_code, 403)
    self.assertEqual(no_account.status_code, 402)
    self.assertEqual(stranger.status_code, 404)
    self.assertEqual(self._balance(), START_POINTS)

  def test_account_comes_from_token_not_request(self) -> None:
    other_id = "9999999999"

    async def seed_other() -> None:
      async with self.transactions_session_factory() as session:
        session.add(db.LoyaltyAccount(loyalty_id=other_id, balance=5000))
        await session.commit()

    asyncio.run(seed_other())
    token = self._token()
    with self.client:
      checkout_id, _ = self._new_checkout("t1", token)
      instrument = self._points(
        amount=400, loyalty_id=other_id, account=other_id
      )
      instrument["credential"] = {"type": "loyalty", "loyalty_id": other_id}
      response = self._complete(checkout_id, "t2", token, [instrument, self._card()])
    self.assertEqual(response.status_code, 200, response.text)

    async def other_balance() -> int:
      async with self.transactions_session_factory() as session:
        return await db.get_loyalty_balance(session, other_id)

    self.assertEqual(asyncio.run(other_balance()), 5000)
    self.assertEqual(self._balance(), START_POINTS - 400 + 16)

  def test_replay_does_not_double_spend(self) -> None:
    token = self._token()
    with self.client:
      checkout_id, _ = self._new_checkout("r1", token)
      body = [self._points(amount=400), self._card()]
      first = self._complete(checkout_id, "r2", token, body)
      second = self._complete(checkout_id, "r2", token, body)
    self.assertEqual(first.status_code, 200, first.text)
    self.assertEqual(second.status_code, 200, second.text)
    self.assertEqual(first.json()["order"], second.json()["order"])
    self.assertEqual(self._balance(), START_POINTS - 400 + 16)

  def test_same_key_other_buyer_is_not_a_cached_success(self) -> None:
    with self.client:
      checkout_id, _ = self._new_checkout("b1", self._token())
      body = [self._points(amount=400), self._card()]
      first = self._complete(checkout_id, "b2", self._token(), body)
      stranger = self._complete(
        checkout_id, "b2", self._token(sub="auth0|someone-else"), body
      )
    self.assertEqual(first.status_code, 200, first.text)
    self.assertEqual(stranger.status_code, 409)

  def test_card_only_checkout_still_works_and_earns(self) -> None:
    token = self._token()
    with self.client:
      checkout_id, _ = self._new_checkout("k1", token)
      response = self._complete(checkout_id, "k2", token, [self._card()])
    self.assertEqual(response.status_code, 200, response.text)
    self.assertEqual(self._balance(), START_POINTS + 20)

  def test_anonymous_card_checkout_unchanged(self) -> None:
    with self.client:
      checkout_id, _ = self._new_checkout("u1", None)
      response = self._complete(checkout_id, "u2", None, [self._card()])
    self.assertEqual(response.status_code, 200, response.text)
    self.assertEqual(self._balance(), START_POINTS)

  def test_profile_advertises_loyalty_and_scopes(self) -> None:
    with self.client:
      profile = self.client.get("/.well-known/ucp").json()
      metadata = self.client.get("/.well-known/oauth-protected-resource").json()
    caps = profile["ucp"]["capabilities"]
    self.assertIn("dev.ucp.common.loyalty", caps)
    self.assertIn("dev.ucp.common.payment.split_payments", caps)
    scopes = caps["dev.ucp.common.identity_linking"][0]["config"]["scopes"]
    self.assertEqual(set(scopes), set(ucp_auth.ALL_SCOPES))
    self.assertIn("mock.loyalty_points", profile["ucp"]["payment_handlers"])
    self.assertEqual(metadata["resource"], AUDIENCE)
    self.assertEqual(
      metadata["authorization_servers"], [f"https://{ISSUER_DOMAIN}"]
    )


# Run only this module's tests, not the ones inherited from the fixture class.
for _name in [n for n in dir(it.IntegrationTest) if n.startswith("test_")]:
  if _name not in LoyaltyTest.__dict__:
    setattr(LoyaltyTest, _name, None)


if __name__ == "__main__":
  absltest.main()
