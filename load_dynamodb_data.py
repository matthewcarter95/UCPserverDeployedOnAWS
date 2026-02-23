#!/usr/bin/env python3
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

"""Load test data from CSV files into DynamoDB tables.

This script reads CSV files from the test_data directory and loads them
into the DynamoDB tables created by the CloudFormation stack.

Usage:
  python load_dynamodb_data.py --stack_name=ucp-merchant-server \
      --data_dir=../test_data/flower_shop
"""

import csv
import json
import logging
from pathlib import Path
from absl import app as absl_app
from absl import flags
import boto3
from botocore.exceptions import ClientError

FLAGS = flags.FLAGS
flags.DEFINE_string(
  "stack_name",
  "ucp-merchant-server",
  "CloudFormation stack name"
)
flags.DEFINE_string(
  "data_dir",
  "../test_data/flower_shop",
  "Directory containing CSV files"
)
flags.DEFINE_string(
  "region",
  "us-east-1",
  "AWS region"
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def get_table_name(stack_name: str, suffix: str) -> str:
  """Construct table name from stack name and suffix."""
  return f"{stack_name}-{suffix}"


def load_csv_to_table(
  table,
  csv_path: Path,
  transform_fn=None
) -> int:
  """Load CSV data into a DynamoDB table."""
  if not csv_path.exists():
    logger.warning(f"CSV file not found: {csv_path}")
    return 0

  count = 0
  with csv_path.open() as f:
    reader = csv.DictReader(f)
    for row in reader:
      item = transform_fn(row) if transform_fn else row
      try:
        table.put_item(Item=item)
        count += 1
      except ClientError as e:
        logger.error(f"Error loading item {item}: {e}")

  return count


def load_data(stack_name: str, data_dir: Path, region: str) -> None:
  """Load all test data from CSV files into DynamoDB."""
  dynamodb = boto3.resource('dynamodb', region_name=region)

  # Products
  logger.info("Loading products...")
  products_table = dynamodb.Table(get_table_name(stack_name, 'products'))
  count = load_csv_to_table(
    products_table,
    data_dir / 'products.csv',
    lambda row: {
      'id': row['id'],
      'title': row['title'],
      'price': int(row['price']),
      'image_url': row['image_url'] if row['image_url'] else None
    }
  )
  logger.info(f"Loaded {count} products")

  # Inventory
  logger.info("Loading inventory...")
  inventory_table = dynamodb.Table(get_table_name(stack_name, 'inventory'))
  count = load_csv_to_table(
    inventory_table,
    data_dir / 'inventory.csv',
    lambda row: {
      'product_id': row['product_id'],
      'quantity': int(row['quantity'])
    }
  )
  logger.info(f"Loaded {count} inventory items")

  # Discounts
  logger.info("Loading discounts...")
  discounts_table = dynamodb.Table(get_table_name(stack_name, 'discounts'))
  count = load_csv_to_table(
    discounts_table,
    data_dir / 'discounts.csv',
    lambda row: {
      'code': row['code'],
      'type': row['type'],
      'value': int(row['value']),
      'description': row['description']
    }
  )
  logger.info(f"Loaded {count} discounts")

  # Shipping Rates
  logger.info("Loading shipping rates...")
  shipping_table = dynamodb.Table(get_table_name(stack_name, 'shipping-rates'))
  count = load_csv_to_table(
    shipping_table,
    data_dir / 'shipping_rates.csv',
    lambda row: {
      'id': row['id'],
      'country_code': row['country_code'],
      'service_level': row['service_level'],
      'price': int(row['price']),
      'title': row['title']
    }
  )
  logger.info(f"Loaded {count} shipping rates")

  # Promotions
  logger.info("Loading promotions...")
  promotions_table = dynamodb.Table(get_table_name(stack_name, 'promotions'))
  count = load_csv_to_table(
    promotions_table,
    data_dir / 'promotions.csv',
    lambda row: {
      'id': row['id'],
      'type': row['type'],
      'min_subtotal': int(row['min_subtotal']) if row.get('min_subtotal') else None,
      'eligible_item_ids': json.loads(row['eligible_item_ids']) if row.get('eligible_item_ids') else None,
      'description': row['description']
    }
  )
  logger.info(f"Loaded {count} promotions")

  # Customers
  logger.info("Loading customers...")
  customers_table = dynamodb.Table(get_table_name(stack_name, 'customers'))
  count = load_csv_to_table(
    customers_table,
    data_dir / 'customers.csv',
    lambda row: {
      'id': row['id'],
      'name': row['name'],
      'email': row['email']
    }
  )
  logger.info(f"Loaded {count} customers")

  # Customer Addresses
  logger.info("Loading customer addresses...")
  addresses_table = dynamodb.Table(get_table_name(stack_name, 'customer-addresses'))
  count = load_csv_to_table(
    addresses_table,
    data_dir / 'addresses.csv',
    lambda row: {
      'id': row['id'],
      'customer_id': row['customer_id'],
      'street_address': row['street_address'],
      'city': row['city'],
      'state': row['state'],
      'postal_code': row['postal_code'],
      'country': row['country']
    }
  )
  logger.info(f"Loaded {count} customer addresses")

  # Payment Instruments
  logger.info("Loading payment instruments...")
  payment_table = dynamodb.Table(get_table_name(stack_name, 'payment-instruments'))
  count = load_csv_to_table(
    payment_table,
    data_dir / 'payment_instruments.csv',
    lambda row: {
      'id': row['id'],
      'type': row['type'],
      'brand': row['brand'],
      'last_digits': row['last_digits'],
      'token': row['token'],
      'handler_id': row['handler_id']
    }
  )
  logger.info(f"Loaded {count} payment instruments")

  logger.info("All data loaded successfully!")


def main(argv) -> None:
  """Run the data loading script."""
  del argv
  data_dir = Path(FLAGS.data_dir).resolve()

  if not data_dir.exists():
    logger.error(f"Data directory not found: {data_dir}")
    return

  logger.info(f"Loading data from: {data_dir}")
  logger.info(f"Target stack: {FLAGS.stack_name} in region {FLAGS.region}")

  try:
    load_data(FLAGS.stack_name, data_dir, FLAGS.region)
  except Exception as e:
    logger.error(f"Error loading data: {e}")
    raise


if __name__ == "__main__":
  absl_app.run(main)
