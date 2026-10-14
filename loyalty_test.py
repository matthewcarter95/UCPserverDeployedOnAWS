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
import db
import integration_test as it
import loyalty
import ucp_auth

ISSUER_DOMAIN = "test-tenant.example.com"
AUDIENCE = "api://ucp.session.service"
CLAIM = "https://demo.okta.com/loyaltyId"
LOYALTY_ID = "4015782993"
START_POINTS = 3000

MANAGE = ucp_auth.SCOPE_CHECKOUT_MANAGE
COMPLETE = ucp_auth.SCOPE_CHECKOUT_COMPLETE


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
      checkout_id, _ = self._new_checkout("p1", self._token())
      anonymous = self._complete(
        checkout_id, "p2", None, [self._points(amount=100), self._card()]
      )
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
    self.assertEqual(anonymous.status_code, 401)
    self.assertEqual(manage_only.status_code, 403)
    self.assertEqual(no_account.status_code, 402)
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
