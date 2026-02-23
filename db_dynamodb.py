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

"""DynamoDB adapter for UCP Merchant Server.

This module provides DynamoDB implementations of the data access layer,
replacing the SQLite-based persistence in db.py.
"""

import datetime
import json
import uuid
from typing import Any
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
import config_lambda as config


class DecimalEncoder(json.JSONEncoder):
  """JSON encoder that converts Decimal to int/float."""

  def default(self, obj):
    if isinstance(obj, Decimal):
      return int(obj) if obj % 1 == 0 else float(obj)
    return super().default(obj)


def _get_table(table_name: str):
  """Get DynamoDB table resource."""
  dynamodb = config.get_dynamodb_client()
  return dynamodb.Table(table_name)


def _convert_decimals(obj: Any) -> Any:
  """Recursively convert Decimal objects to int/float."""
  if isinstance(obj, list):
    return [_convert_decimals(item) for item in obj]
  elif isinstance(obj, dict):
    return {key: _convert_decimals(value) for key, value in obj.items()}
  elif isinstance(obj, Decimal):
    return int(obj) if obj % 1 == 0 else float(obj)
  return obj


# Product operations
async def get_product(product_id: str) -> dict | None:
  """Retrieve a product by ID."""
  table = _get_table(config.PRODUCTS_TABLE)
  response = table.get_item(Key={'id': product_id})
  item = response.get('Item')
  return _convert_decimals(item) if item else None


# Inventory operations
async def get_inventory(product_id: str) -> int | None:
  """Retrieve the inventory quantity for a product."""
  table = _get_table(config.INVENTORY_TABLE)
  response = table.get_item(Key={'product_id': product_id})
  item = response.get('Item')
  return int(item['quantity']) if item else None


async def reserve_stock(product_id: str, quantity: int) -> bool:
  """Atomically decrements inventory if sufficient stock exists."""
  table = _get_table(config.INVENTORY_TABLE)
  try:
    table.update_item(
      Key={'product_id': product_id},
      UpdateExpression='SET quantity = quantity - :qty',
      ConditionExpression='quantity >= :qty',
      ExpressionAttributeValues={':qty': quantity}
    )
    return True
  except Exception:
    return False


# Checkout operations
async def save_checkout(checkout_id: str, status: str, checkout_obj: dict[str, Any]) -> None:
  """Save or update a checkout session."""
  table = _get_table(config.CHECKOUTS_TABLE)
  table.put_item(
    Item={
      'id': checkout_id,
      'status': status,
      'data': json.loads(json.dumps(checkout_obj, cls=DecimalEncoder))
    }
  )


async def get_checkout_session(checkout_id: str) -> dict[str, Any] | None:
  """Retrieve a checkout session by ID."""
  table = _get_table(config.CHECKOUTS_TABLE)
  response = table.get_item(Key={'id': checkout_id})
  item = response.get('Item')
  return _convert_decimals(item['data']) if item else None


# Order operations
async def save_order(order_id: str, order_obj: dict[str, Any]) -> None:
  """Save or update an order."""
  table = _get_table(config.ORDERS_TABLE)
  table.put_item(
    Item={
      'id': order_id,
      'data': json.loads(json.dumps(order_obj, cls=DecimalEncoder))
    }
  )


async def get_order(order_id: str) -> dict[str, Any] | None:
  """Retrieve an order by ID."""
  table = _get_table(config.ORDERS_TABLE)
  response = table.get_item(Key={'id': order_id})
  item = response.get('Item')
  return _convert_decimals(item['data']) if item else None


# Customer operations
async def get_customer(email: str) -> dict | None:
  """Retrieve a customer by email."""
  table = _get_table(config.CUSTOMERS_TABLE)
  response = table.query(
    IndexName='email-index',
    KeyConditionExpression=Key('email').eq(email)
  )
  items = response.get('Items', [])
  return _convert_decimals(items[0]) if items else None


async def get_customer_addresses(email: str) -> list[dict]:
  """Retrieve addresses for a customer by email."""
  customer = await get_customer(email)
  if not customer:
    return []

  table = _get_table(config.CUSTOMER_ADDRESSES_TABLE)
  response = table.query(
    IndexName='customer-id-index',
    KeyConditionExpression=Key('customer_id').eq(customer['id'])
  )
  return _convert_decimals(response.get('Items', []))


async def save_customer_address(email: str, address: dict[str, Any]) -> str:
  """Save a customer address, reusing existing ID if content matches."""
  customer = await get_customer(email)

  if not customer:
    # Create customer
    customer_id = str(uuid.uuid4())
    table = _get_table(config.CUSTOMERS_TABLE)
    table.put_item(
      Item={
        'id': customer_id,
        'email': email,
        'name': address.get('full_name', 'Unknown')
      }
    )
    customer = {'id': customer_id}

  # Check for existing matching address
  addresses = await get_customer_addresses(email)
  for addr in addresses:
    if (addr.get('street_address') == address.get('street_address') and
        addr.get('city') == address.get('address_locality') and
        addr.get('state') == address.get('address_region') and
        addr.get('postal_code') == address.get('postal_code') and
        addr.get('country') == address.get('address_country')):
      return addr['id']

  # Create new address
  address_id = address.get('id') or str(uuid.uuid4())
  table = _get_table(config.CUSTOMER_ADDRESSES_TABLE)
  table.put_item(
    Item={
      'id': address_id,
      'customer_id': customer['id'],
      'street_address': address.get('street_address'),
      'city': address.get('address_locality'),
      'state': address.get('address_region'),
      'postal_code': address.get('postal_code'),
      'country': address.get('address_country')
    }
  )
  return address_id


# Discount operations
async def get_discount(code: str) -> dict | None:
  """Retrieve a discount by code."""
  table = _get_table(config.DISCOUNTS_TABLE)
  response = table.get_item(Key={'code': code})
  item = response.get('Item')
  return _convert_decimals(item) if item else None


async def get_discounts_by_codes(codes: list[str]) -> list[dict]:
  """Retrieve multiple discounts by their codes."""
  results = []
  for code in codes:
    discount = await get_discount(code)
    if discount:
      results.append(discount)
  return results


# Shipping operations
async def get_shipping_rates(country_code: str) -> list[dict]:
  """Retrieve shipping rates for a specific country and default rates."""
  table = _get_table(config.SHIPPING_RATES_TABLE)
  results = []

  # Query by country code
  response = table.query(
    IndexName='country-code-index',
    KeyConditionExpression=Key('country_code').eq(country_code)
  )
  results.extend(response.get('Items', []))

  # Query default rates
  response = table.query(
    IndexName='country-code-index',
    KeyConditionExpression=Key('country_code').eq('default')
  )
  results.extend(response.get('Items', []))

  return _convert_decimals(results)


# Promotion operations
async def get_active_promotions() -> list[dict]:
  """Retrieve all active promotions."""
  table = _get_table(config.PROMOTIONS_TABLE)
  response = table.scan()
  return _convert_decimals(response.get('Items', []))


# Idempotency operations
async def get_idempotency_record(key: str) -> dict | None:
  """Retrieve an idempotency record by key."""
  table = _get_table(config.IDEMPOTENCY_TABLE)
  response = table.get_item(Key={'key': key})
  item = response.get('Item')
  return _convert_decimals(item) if item else None


async def save_idempotency_record(
  key: str,
  request_hash: str,
  response_status: int,
  response_body: dict[str, Any]
) -> None:
  """Save a new idempotency record."""
  table = _get_table(config.IDEMPOTENCY_TABLE)
  # Set TTL to 24 hours from now
  ttl = int((datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)).timestamp())
  table.put_item(
    Item={
      'key': key,
      'request_hash': request_hash,
      'response_status': response_status,
      'response_body': json.loads(json.dumps(response_body, cls=DecimalEncoder)),
      'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
      'ttl': ttl
    }
  )


# Request logging
async def log_request(
  method: str,
  url: str,
  checkout_id: str | None = None,
  payload: dict[str, Any] | None = None
) -> None:
  """Log an HTTP request to the database."""
  table = _get_table(config.REQUEST_LOGS_TABLE)
  timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
  table.put_item(
    Item={
      'id': str(uuid.uuid4()),
      'timestamp': timestamp,
      'method': method,
      'url': url,
      'checkout_id': checkout_id,
      'payload': json.loads(json.dumps(payload, cls=DecimalEncoder)) if payload else None
    }
  )
