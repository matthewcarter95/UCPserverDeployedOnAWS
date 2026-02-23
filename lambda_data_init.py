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

"""Lambda function to initialize DynamoDB tables with sample data."""

import json
import boto3
import config_lambda as config


def load_sample_data():
  """Load sample data into DynamoDB tables."""
  dynamodb = boto3.resource('dynamodb')

  # Sample products
  products_table = dynamodb.Table(config.PRODUCTS_TABLE)
  products = [
    {'id': 'bouquet_roses', 'title': 'Bouquet of Red Roses', 'price': 3500, 'image_url': None},
    {'id': 'bouquet_tulips', 'title': 'Bouquet of Tulips', 'price': 2500, 'image_url': None},
    {'id': 'bouquet_mixed', 'title': 'Mixed Flower Bouquet', 'price': 4000, 'image_url': None},
  ]
  for product in products:
    products_table.put_item(Item=product)

  # Sample inventory
  inventory_table = dynamodb.Table(config.INVENTORY_TABLE)
  inventory = [
    {'product_id': 'bouquet_roses', 'quantity': 100},
    {'product_id': 'bouquet_tulips', 'quantity': 50},
    {'product_id': 'bouquet_mixed', 'quantity': 75},
  ]
  for item in inventory:
    inventory_table.put_item(Item=item)

  # Sample discounts
  discounts_table = dynamodb.Table(config.DISCOUNTS_TABLE)
  discounts = [
    {'code': '10OFF', 'type': 'percentage', 'value': 10, 'description': '10% Off'},
    {'code': 'SAVE5', 'type': 'fixed_amount', 'value': 500, 'description': '$5 Off'},
  ]
  for discount in discounts:
    discounts_table.put_item(Item=discount)

  # Sample shipping rates
  shipping_table = dynamodb.Table(config.SHIPPING_RATES_TABLE)
  shipping_rates = [
    {'id': 'standard_us', 'country_code': 'US', 'service_level': 'standard', 'price': 500, 'title': 'Standard Shipping'},
    {'id': 'express_us', 'country_code': 'US', 'service_level': 'express', 'price': 1500, 'title': 'Express Shipping'},
    {'id': 'standard_default', 'country_code': 'default', 'service_level': 'standard', 'price': 1000, 'title': 'International Standard'},
  ]
  for rate in shipping_rates:
    shipping_table.put_item(Item=rate)

  # Sample promotions
  promotions_table = dynamodb.Table(config.PROMOTIONS_TABLE)
  promotions = [
    {
      'id': 'free_shipping_50',
      'type': 'free_shipping',
      'min_subtotal': 5000,
      'eligible_item_ids': None,
      'description': 'Free shipping on orders over $50'
    }
  ]
  for promotion in promotions:
    promotions_table.put_item(Item=promotion)

  return {
    'products': len(products),
    'inventory': len(inventory),
    'discounts': len(discounts),
    'shipping_rates': len(shipping_rates),
    'promotions': len(promotions)
  }


def handler(event, context):
  """Lambda handler to initialize sample data."""
  try:
    result = load_sample_data()
    return {
      'statusCode': 200,
      'body': json.dumps({
        'message': 'Sample data loaded successfully',
        'counts': result
      })
    }
  except Exception as e:
    return {
      'statusCode': 500,
      'body': json.dumps({
        'message': f'Error loading sample data: {str(e)}'
      })
    }
