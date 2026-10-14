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

"""Lambda-specific configuration using DynamoDB instead of SQLite."""

import contextlib
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import boto3

# Environment variables for DynamoDB table names
PRODUCTS_TABLE = os.environ.get('PRODUCTS_TABLE', 'ucp-products')
TRANSACTIONS_TABLE = os.environ.get('TRANSACTIONS_TABLE', 'ucp-transactions')
INVENTORY_TABLE = os.environ.get('INVENTORY_TABLE', 'ucp-inventory')
CHECKOUTS_TABLE = os.environ.get('CHECKOUTS_TABLE', 'ucp-checkouts')
ORDERS_TABLE = os.environ.get('ORDERS_TABLE', 'ucp-orders')
CUSTOMERS_TABLE = os.environ.get('CUSTOMERS_TABLE', 'ucp-customers')
CUSTOMER_ADDRESSES_TABLE = os.environ.get('CUSTOMER_ADDRESSES_TABLE', 'ucp-customer-addresses')
DISCOUNTS_TABLE = os.environ.get('DISCOUNTS_TABLE', 'ucp-discounts')
SHIPPING_RATES_TABLE = os.environ.get('SHIPPING_RATES_TABLE', 'ucp-shipping-rates')
PROMOTIONS_TABLE = os.environ.get('PROMOTIONS_TABLE', 'ucp-promotions')
IDEMPOTENCY_TABLE = os.environ.get('IDEMPOTENCY_TABLE', 'ucp-idempotency')
REQUEST_LOGS_TABLE = os.environ.get('REQUEST_LOGS_TABLE', 'ucp-request-logs')
PAYMENT_INSTRUMENTS_TABLE = os.environ.get('PAYMENT_INSTRUMENTS_TABLE', 'ucp-payment-instruments')
CARTS_TABLE = os.environ.get('CARTS_TABLE', 'ucp-carts')
# Existing points balance table (shared with other apps) and our ledger.
LOYALTY_POINTS_TABLE = os.environ.get('LOYALTY_POINTS_TABLE', 'RetailLoyaltyPointsTracker')
LOYALTY_LEDGER_TABLE = os.environ.get('LOYALTY_LEDGER_TABLE', 'ucp-loyalty-ledger')

SIMULATION_SECRET = os.environ.get('SIMULATION_SECRET', str(uuid.uuid4()))

# DynamoDB client
dynamodb = None


async def init_dynamodb():
  """Initialize DynamoDB client."""
  global dynamodb
  if dynamodb is None:
    dynamodb = boto3.resource('dynamodb')


async def cleanup_dynamodb():
  """Cleanup DynamoDB connections."""
  # boto3 resources don't require explicit cleanup
  pass


_PROFILE_CACHE = None

# Shared with routes/discovery.py so the profile and checkout agree.
SHOP_ID = str(uuid.uuid4())

DEFAULT_CURRENCY = "USD"

# Stand-in for the absl FLAGS the shared (SQLite) code reads. In Lambda these
# come from environment variables.
FLAGS = SimpleNamespace(
  simulation_secret=SIMULATION_SECRET,
  require_signatures=os.environ.get('REQUIRE_SIGNATURES', '').lower() == 'true',
  allow_insecure_profile_urls=False,
  webhook_signing_key=os.environ.get('WEBHOOK_SIGNING_KEY') or None,
  webhook_delivery_attempts=int(os.environ.get('WEBHOOK_DELIVERY_ATTEMPTS', '3')),
  webhook_retry_backoff_seconds=float(
    os.environ.get('WEBHOOK_RETRY_BACKOFF_SECONDS', '0.5')
  ),
  products_db_path=None,
  transactions_db_path=None,
  port=None,
)


def get_default_currency() -> str:
  """Return the currency this business trades in."""
  return DEFAULT_CURRENCY


def _get_profile() -> dict:
  global _PROFILE_CACHE
  if not _PROFILE_CACHE:
    path = Path(__file__).resolve().parent / "routes" / "discovery_profile.json"
    with path.open(encoding="utf-8") as f:
      _PROFILE_CACHE = json.load(f)
  return _PROFILE_CACHE


def get_server_version() -> str:
  """Return the UCP version declared in the discovery profile."""
  return _get_profile()["ucp"]["version"]


def get_payment_handlers() -> dict:
  """Return the payment handlers declared in the discovery profile."""
  profile = json.loads(
    json.dumps(_get_profile()).replace("{{SHOP_ID}}", SHOP_ID)
  )
  return profile["ucp"].get("payment_handlers", {})


_lowlevel = None


def get_dynamodb_lowlevel_client():
  """Get a low-level DynamoDB client (explicit attribute types).

  The resource's own client auto-converts Python values, so hand-typed
  {'N': '1'} items passed through it are converted twice and rejected.
  """
  global _lowlevel
  if _lowlevel is None:
    _lowlevel = boto3.client('dynamodb')
  return _lowlevel


def get_dynamodb_client():
  """Get DynamoDB resource."""
  global dynamodb
  if dynamodb is None:
    dynamodb = boto3.resource('dynamodb')
  return dynamodb


@contextlib.asynccontextmanager
async def lifespan(app):
  """Lifespan for Lambda: DynamoDB needs no startup beyond the client."""
  del app
  import webhook_signer

  webhook_signer.signing_key()
  await init_dynamodb()
  yield
  await cleanup_dynamodb()
