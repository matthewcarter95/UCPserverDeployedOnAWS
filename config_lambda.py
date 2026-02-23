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

import os
import uuid
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


def get_server_version() -> str:
  """Return the UCP version."""
  return "2026-01-11"


def get_dynamodb_client():
  """Get DynamoDB resource."""
  global dynamodb
  if dynamodb is None:
    dynamodb = boto3.resource('dynamodb')
  return dynamodb
