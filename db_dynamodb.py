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
from types import SimpleNamespace
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


class _Session:
  """No-op stand-in for an SQLAlchemy session.

  The shared service code threads a session through every db call and calls
  commit()/rollback(). DynamoDB writes are immediate, so these do nothing.
  """

  async def commit(self) -> None:
    """No-op: DynamoDB writes are applied immediately."""

  async def rollback(self) -> None:
    """No-op: there is no transaction to roll back."""

  async def __aenter__(self) -> "_Session":
    return self

  async def __aexit__(self, *exc) -> None:
    return None


class _Manager:
  """Mirrors db.DatabaseManager's session-factory surface."""

  def products_session_factory(self) -> _Session:
    return _Session()

  def transactions_session_factory(self) -> _Session:
    return _Session()


manager = _Manager()


class Promotion(SimpleNamespace):
  """Row type used for annotations in the shared fulfillment service."""


def _row(item: dict | None, cls: type = SimpleNamespace):
  """Wrap a DynamoDB item so shared code can use attribute access."""
  return cls(**_convert_decimals(item)) if item else None


# Product operations
async def get_product(session: Any, product_id: str):
  """Retrieve a product by ID."""
  table = _get_table(config.PRODUCTS_TABLE)
  return _row(table.get_item(Key={'id': product_id}).get('Item'))


# Inventory operations
async def get_inventory(session: Any, product_id: str) -> int | None:
  """Retrieve the inventory quantity for a product."""
  table = _get_table(config.INVENTORY_TABLE)
  item = table.get_item(Key={'product_id': product_id}).get('Item')
  return int(item['quantity']) if item else None


async def reserve_stock(session: Any, product_id: str, quantity: int) -> bool:
  """Atomically decrement inventory if sufficient stock exists."""
  table = _get_table(config.INVENTORY_TABLE)
  try:
    table.update_item(
      Key={'product_id': product_id},
      UpdateExpression='SET quantity = quantity - :qty',
      ConditionExpression='quantity >= :qty',
      ExpressionAttributeValues={':qty': quantity},
    )
    return True
  except Exception:
    return False


def _save_blob(table_name: str, key: str, extra: dict, data: dict) -> None:
  _get_table(table_name).put_item(
    Item={'id': key, **extra, 'data': json.loads(json.dumps(data, cls=DecimalEncoder))}
  )


def _get_blob(table_name: str, key: str) -> dict[str, Any] | None:
  item = _get_table(table_name).get_item(Key={'id': key}).get('Item')
  return _convert_decimals(item['data']) if item else None


# Checkout operations
async def save_checkout(
  session: Any, checkout_id: str, status: str, checkout_obj: dict[str, Any]
) -> None:
  """Save or update a checkout session."""
  _save_blob(config.CHECKOUTS_TABLE, checkout_id, {'status': status}, checkout_obj)


async def get_checkout_session(session: Any, checkout_id: str):
  """Retrieve a checkout session by ID."""
  return _get_blob(config.CHECKOUTS_TABLE, checkout_id)


async def get_checkouts_by_cart_id(session: Any, cart_id: str) -> list[dict]:
  """Retrieve all checkout sessions created from a cart."""
  table = _get_table(config.CHECKOUTS_TABLE)
  items, kwargs = [], {}
  while True:
    resp = table.scan(**kwargs)
    items.extend(resp.get('Items', []))
    if 'LastEvaluatedKey' not in resp:
      break
    kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']
  return [
    _convert_decimals(i['data'])
    for i in items
    if (i.get('data') or {}).get('cart_id') == cart_id
  ]


# Cart operations
async def save_cart(session: Any, cart_id: str, cart_obj: dict[str, Any]) -> None:
  """Save or update a cart session."""
  _save_blob(config.CARTS_TABLE, cart_id, {}, cart_obj)


async def get_cart_session(session: Any, cart_id: str):
  """Retrieve a cart session by ID."""
  return _get_blob(config.CARTS_TABLE, cart_id)


async def delete_cart_session(session: Any, cart_id: str) -> None:
  """Delete a cart session by ID."""
  _get_table(config.CARTS_TABLE).delete_item(Key={'id': cart_id})


# Order operations
async def save_order(
  session: Any,
  order_id: str,
  order_obj: dict[str, Any],
  buyer_sub: str | None = None,
  buyer_email: str | None = None,
) -> None:
  """Save or update an order.

  buyer_sub / buyer_email are stored beside `data` (the UCP Order schema has no
  buyer field) and only on creation: a later save passes no buyer and must not
  strip it, so those attributes are written only when given.
  """
  table = _get_table(config.ORDERS_TABLE)
  data = json.loads(json.dumps(order_obj, cls=DecimalEncoder))
  if buyer_sub is None:
    # An update keeps whatever buyer attributes the row already has.
    table.update_item(
      Key={'id': order_id},
      UpdateExpression='SET #d = :d',
      ExpressionAttributeNames={'#d': 'data'},
      ExpressionAttributeValues={':d': data},
    )
    return
  now = datetime.datetime.now(datetime.timezone.utc).isoformat()
  names = {'#d': 'data'}
  values = {':d': data, ':s': str(buyer_sub), ':c': now}
  expr = 'SET #d = :d, buyer_sub = :s, created_at = if_not_exists(created_at, :c)'
  if buyer_email:
    expr += ', buyer_email = :e'
    values[':e'] = buyer_email
  table.update_item(
    Key={'id': order_id},
    UpdateExpression=expr,
    ExpressionAttributeNames=names,
    ExpressionAttributeValues=values,
  )


async def get_order(session: Any, order_id: str):
  """Retrieve an order by ID."""
  return _get_blob(config.ORDERS_TABLE, order_id)


async def get_order_owner(session: Any, order_id: str) -> str | None:
  """Return the order's buyer_sub (None when unowned or missing)."""
  item = _get_table(config.ORDERS_TABLE).get_item(
    Key={'id': order_id}, ProjectionExpression='buyer_sub'
  ).get('Item')
  return item.get('buyer_sub') if item else None


async def list_orders_for_buyer(
  session: Any, buyer_sub: str, limit: int, offset: int
) -> tuple[list[dict[str, Any]], int]:
  """Return (a page of this buyer's orders newest first, total count).

  Reads the buyer-sub-index GSI; sorting and paging happen in memory because a
  buyer has few orders and the index sorts by created_at only within the buyer.
  """
  table = _get_table(config.ORDERS_TABLE)
  items, kwargs = [], {}
  while True:
    resp = table.query(
      IndexName='buyer-sub-index',
      KeyConditionExpression=Key('buyer_sub').eq(str(buyer_sub)),
      ScanIndexForward=False,
      **kwargs,
    )
    items.extend(resp.get('Items', []))
    if 'LastEvaluatedKey' not in resp:
      break
    kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']
  items.sort(key=lambda i: (i.get('created_at') or '', i['id']), reverse=True)
  page = items[offset : offset + limit]
  return [_convert_decimals(i['data']) for i in page], len(items)


# Customer operations
async def get_customer(session: Any, email: str):
  """Retrieve a customer by email."""
  table = _get_table(config.CUSTOMERS_TABLE)
  items = table.query(
    IndexName='email-index', KeyConditionExpression=Key('email').eq(email)
  ).get('Items', [])
  return _row(items[0]) if items else None


async def get_customer_addresses(session: Any, email: str) -> list:
  """Retrieve addresses for a customer by email."""
  customer = await get_customer(session, email)
  if not customer:
    return []
  table = _get_table(config.CUSTOMER_ADDRESSES_TABLE)
  items = table.query(
    IndexName='customer-id-index',
    KeyConditionExpression=Key('customer_id').eq(customer.id),
  ).get('Items', [])
  return [_row(i) for i in items]


async def save_customer_address(
  session: Any, email: str, address: dict[str, Any]
) -> str:
  """Save a customer address, reusing the existing ID if content matches."""
  customer = await get_customer(session, email)
  if not customer:
    customer = SimpleNamespace(id=str(uuid.uuid4()))
    _get_table(config.CUSTOMERS_TABLE).put_item(
      Item={
        'id': customer.id,
        'email': email,
        'name': address.get('full_name', 'Unknown'),
      }
    )

  for addr in await get_customer_addresses(session, email):
    if (
      getattr(addr, 'street_address', None) == address.get('street_address')
      and getattr(addr, 'city', None) == address.get('address_locality')
      and getattr(addr, 'state', None) == address.get('address_region')
      and getattr(addr, 'postal_code', None) == address.get('postal_code')
      and getattr(addr, 'country', None) == address.get('address_country')
    ):
      return addr.id

  address_id = address.get('id') or str(uuid.uuid4())
  _get_table(config.CUSTOMER_ADDRESSES_TABLE).put_item(
    Item={
      'id': address_id,
      'customer_id': customer.id,
      'street_address': address.get('street_address'),
      'city': address.get('address_locality'),
      'state': address.get('address_region'),
      'postal_code': address.get('postal_code'),
      'country': address.get('address_country'),
    }
  )
  return address_id


# Discount operations
async def get_discount(session: Any, code: str):
  """Retrieve a discount by code (case-insensitive)."""
  table = _get_table(config.DISCOUNTS_TABLE)
  for candidate in dict.fromkeys([code, code.upper(), code.lower()]):
    item = table.get_item(Key={'code': candidate}).get('Item')
    if item:
      return _row(item)
  return None


async def get_discounts_by_codes(session: Any, codes: list[str]) -> list:
  """Retrieve multiple discounts by their codes."""
  found = [await get_discount(session, c) for c in codes]
  return [d for d in found if d]


# Shipping operations
async def get_shipping_rates(session: Any, country_code: str) -> list:
  """Retrieve shipping rates for a country plus the default rates."""
  table = _get_table(config.SHIPPING_RATES_TABLE)
  items = []
  for cc in dict.fromkeys([country_code, 'default']):
    items.extend(
      table.query(
        IndexName='country-code-index',
        KeyConditionExpression=Key('country_code').eq(cc),
      ).get('Items', [])
    )
  return [_row(i) for i in items]


# Promotion operations
async def get_active_promotions(session: Any) -> list:
  """Retrieve all active promotions."""
  items = _get_table(config.PROMOTIONS_TABLE).scan().get('Items', [])
  return [_row(i, Promotion) for i in items]


# Idempotency operations
async def get_idempotency_record(session: Any, key: str):
  """Retrieve an idempotency record by key."""
  item = _get_table(config.IDEMPOTENCY_TABLE).get_item(Key={'key': key}).get('Item')
  return _row(item)


async def save_idempotency_record(
  session: Any,
  key: str,
  request_hash: str,
  response_status: int,
  response_body: dict[str, Any],
) -> None:
  """Save a new idempotency record (24h TTL)."""
  now = datetime.datetime.now(datetime.timezone.utc)
  _get_table(config.IDEMPOTENCY_TABLE).put_item(
    Item={
      'key': key,
      'request_hash': request_hash,
      'response_status': response_status,
      'response_body': json.loads(json.dumps(response_body, cls=DecimalEncoder)),
      'created_at': now.isoformat(),
      'ttl': int((now + datetime.timedelta(days=1)).timestamp()),
    }
  )


# Request logging
async def log_request(
  session: Any,
  method: str,
  url: str,
  checkout_id: str | None = None,
  payload: dict[str, Any] | None = None,
) -> None:
  """Log an HTTP request."""
  _get_table(config.REQUEST_LOGS_TABLE).put_item(
    Item={
      'id': str(uuid.uuid4()),
      'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(),
      'method': method,
      'url': url,
      'checkout_id': checkout_id,
      'payload': json.loads(json.dumps(payload, cls=DecimalEncoder)) if payload else None,
    }
  )


# Catalog operations
async def list_all_products() -> list[dict]:
  """Retrieve all products (paginated scan)."""
  table = _get_table(config.PRODUCTS_TABLE)
  items, kwargs = [], {}
  while True:
    resp = table.scan(**kwargs)
    items.extend(resp.get('Items', []))
    if 'LastEvaluatedKey' not in resp:
      break
    kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']
  return _convert_decimals(items)


async def list_products_with_inventory() -> list[dict]:
  """Retrieve all products with an 'inventory_quantity' field added."""
  products = await list_all_products()
  inv = _get_table(config.INVENTORY_TABLE).scan().get('Items', [])
  inventory_map = {i['product_id']: int(i['quantity']) for i in inv}
  for p in products:
    p['inventory_quantity'] = inventory_map.get(p['id'], 0)
  return products


# Loyalty points
#
# The balance lives in an existing table (config.LOYALTY_POINTS_TABLE, keyed by
# a Number `loyaltyId`, attribute `totalPoints`) that other apps also write. All
# of our writes are conditional and paired with a ledger row in a table we own
# (config.LOYALTY_LEDGER_TABLE, keyed by `entry_id`) inside one transaction, so
# a retry can never deduct or credit twice.


def _ddb_client():
  return config.get_dynamodb_lowlevel_client()


def _tracker_key(loyalty_id: str) -> dict:
  return {'loyaltyId': {'N': str(int(loyalty_id))}}


def _is_cancelled(exc: Exception) -> list[str]:
  """Return per-item cancellation codes for a TransactionCanceledException.

  Re-raises anything else, so only failures we understand are translated.
  """
  response = getattr(exc, 'response', None) or {}
  if response.get('Error', {}).get('Code') != 'TransactionCanceledException':
    raise exc
  return [r.get('Code', 'None') for r in response.get('CancellationReasons', [])]


def _failed(codes: list[str], index: int) -> bool:
  """Whether transaction item `index` failed its condition."""
  return len(codes) > index and codes[index] == 'ConditionalCheckFailed'


async def get_loyalty_balance(session: Any, loyalty_id: str) -> int | None:
  """Return the points balance, or None if the account does not exist."""
  table = _get_table(config.LOYALTY_POINTS_TABLE)
  item = table.get_item(Key={'loyaltyId': int(loyalty_id)}).get('Item')
  return int(item['totalPoints']) if item and 'totalPoints' in item else None


async def reserve_loyalty_points(
  session: Any, loyalty_id: str, points: int, entry_id: str
) -> str:
  """Atomically deduct points and record the reservation.

  Idempotent on `entry_id`: a reserved or committed entry is left alone, and a
  released one (an earlier attempt that failed) is reserved again.

  Returns 'reserved', 'exists' or 'insufficient'.
  """
  now = datetime.datetime.now(datetime.timezone.utc).isoformat()
  entry = _get_table(config.LOYALTY_LEDGER_TABLE).get_item(
    Key={'entry_id': entry_id}
  ).get('Item')
  if entry and entry.get('status') != 'released':
    return 'exists'
  deduct = {
    'Update': {
      'TableName': config.LOYALTY_POINTS_TABLE,
      'Key': _tracker_key(loyalty_id),
      'UpdateExpression': 'SET totalPoints = totalPoints - :p',
      'ConditionExpression': 'totalPoints >= :p',
      'ExpressionAttributeValues': {':p': {'N': str(points)}},
    }
  }
  if entry:
    record = {
      'Update': {
        'TableName': config.LOYALTY_LEDGER_TABLE,
        'Key': {'entry_id': {'S': entry_id}},
        'UpdateExpression': 'SET #s = :reserved, points = :p, created_at = :now',
        'ConditionExpression': '#s = :released',
        'ExpressionAttributeNames': {'#s': 'status'},
        'ExpressionAttributeValues': {
          ':reserved': {'S': 'reserved'},
          ':released': {'S': 'released'},
          ':p': {'N': str(points)},
          ':now': {'S': now},
        },
      }
    }
  else:
    record = {
      'Put': {
        'TableName': config.LOYALTY_LEDGER_TABLE,
        'Item': {
          'entry_id': {'S': entry_id},
          'loyalty_id': {'S': str(loyalty_id)},
          'kind': {'S': 'redeem'},
          'points': {'N': str(points)},
          'status': {'S': 'reserved'},
          'created_at': {'S': now},
        },
        'ConditionExpression': 'attribute_not_exists(entry_id)',
      }
    }
  try:
    _ddb_client().transact_write_items(TransactItems=[deduct, record])
    return 'reserved'
  except Exception as exc:
    codes = _is_cancelled(exc)
    # Item 0 is the balance check; item 1 is a concurrent writer of this entry.
    if _failed(codes, 0):
      return 'insufficient'
    if _failed(codes, 1):
      return 'exists'
    raise exc


async def get_loyalty_entry(session: Any, entry_id: str) -> dict | None:
  """Return {'points', 'status'} for a ledger entry, or None."""
  item = _get_table(config.LOYALTY_LEDGER_TABLE).get_item(
    Key={'entry_id': entry_id}
  ).get('Item')
  if not item:
    return None
  return {'points': int(item['points']), 'status': item['status']}


async def release_loyalty_points(
  session: Any, loyalty_id: str, entry_id: str
) -> bool:
  """Return a reservation's points to the account. No-op unless 'reserved'."""
  ledger = _get_table(config.LOYALTY_LEDGER_TABLE)
  entry = ledger.get_item(Key={'entry_id': entry_id}).get('Item')
  if not entry or entry.get('status') != 'reserved':
    return False
  try:
    _ddb_client().transact_write_items(
      TransactItems=[
        {
          'Update': {
            'TableName': config.LOYALTY_POINTS_TABLE,
            'Key': _tracker_key(loyalty_id),
            'UpdateExpression': 'SET totalPoints = totalPoints + :p',
            'ExpressionAttributeValues': {':p': {'N': str(int(entry['points']))}},
          }
        },
        {
          'Update': {
            'TableName': config.LOYALTY_LEDGER_TABLE,
            'Key': {'entry_id': {'S': entry_id}},
            'UpdateExpression': 'SET #s = :released',
            'ConditionExpression': '#s = :reserved',
            'ExpressionAttributeNames': {'#s': 'status'},
            'ExpressionAttributeValues': {
              ':released': {'S': 'released'},
              ':reserved': {'S': 'reserved'},
            },
          }
        },
      ]
    )
    return True
  except Exception as exc:
    if _failed(_is_cancelled(exc), 1):
      return False  # someone else already released/committed it
    raise


async def commit_loyalty_reservation(session: Any, entry_id: str) -> None:
  """Mark a reservation as spent."""
  try:
    _get_table(config.LOYALTY_LEDGER_TABLE).update_item(
      Key={'entry_id': entry_id},
      UpdateExpression='SET #s = :c',
      ConditionExpression='#s = :r',
      ExpressionAttributeNames={'#s': 'status'},
      ExpressionAttributeValues={':c': 'committed', ':r': 'reserved'},
    )
  except Exception as exc:
    if getattr(exc, 'response', {}).get('Error', {}).get('Code') != (
      'ConditionalCheckFailedException'
    ):
      raise


async def credit_loyalty_points(
  session: Any, loyalty_id: str, points: int, entry_id: str
) -> bool:
  """Add points once per entry_id. Returns False if already credited."""
  now = datetime.datetime.now(datetime.timezone.utc).isoformat()
  try:
    _ddb_client().transact_write_items(
      TransactItems=[
        {
          'Update': {
            'TableName': config.LOYALTY_POINTS_TABLE,
            'Key': _tracker_key(loyalty_id),
            'UpdateExpression': (
              'SET totalPoints = if_not_exists(totalPoints, :zero) + :p'
            ),
            'ExpressionAttributeValues': {
              ':p': {'N': str(points)},
              ':zero': {'N': '0'},
            },
          }
        },
        {
          'Put': {
            'TableName': config.LOYALTY_LEDGER_TABLE,
            'Item': {
              'entry_id': {'S': entry_id},
              'loyalty_id': {'S': str(loyalty_id)},
              'kind': {'S': 'earn'},
              'points': {'N': str(points)},
              'status': {'S': 'credited'},
              'created_at': {'S': now},
            },
            'ConditionExpression': 'attribute_not_exists(entry_id)',
          }
        },
      ]
    )
    return True
  except Exception as exc:
    if _failed(_is_cancelled(exc), 1):
      return False  # already credited for this entry
    raise
