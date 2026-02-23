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

"""Catalog routes for product discovery (Lambda/DynamoDB version)."""

from fastapi import APIRouter
from fastapi import Request

import db_dynamodb as db
from models import CatalogProduct
from models import ProductListResponse
from models import ProductPrice

router = APIRouter()


@router.get(
  "/api/ucp/products",
  response_model=ProductListResponse,
  summary="List all products",
)
async def list_products(request: Request):
  """Return all products with availability information.

  This endpoint allows AI agents to discover available products before
  creating checkout sessions. No authentication is required for public
  catalog browsing.

  Returns:
    ProductListResponse with all products and their availability status.

  """
  # Fetch all products with inventory
  products_with_inventory = await db.list_products_with_inventory()

  # Transform to catalog format
  catalog_products = []
  base_url = str(request.base_url).rstrip("/")

  for product_data in products_with_inventory:
    # Determine availability
    quantity = product_data.get('inventory_quantity', 0)
    availability = "in_stock" if quantity > 0 else "out_of_stock"

    # Convert price from cents to dollars
    price_cents = product_data.get('price', 0)
    price_dollars = price_cents / 100.0

    # Get timestamps (may be None for existing data)
    created_at = product_data.get('created_at')
    updated_at = product_data.get('updated_at')

    # Build product link
    link = product_data.get('link') or f"{base_url}/products/{product_data['id']}"

    catalog_product = CatalogProduct(
      id=product_data['id'],
      title=product_data['title'],
      description=product_data.get('description'),
      price=ProductPrice(value=price_dollars, currency="USD"),
      availability=availability,
      link=link,
      image_link=product_data.get('image_url'),
      brand=product_data.get('brand'),
      category=product_data.get('category'),
      created_at=created_at,
      updated_at=updated_at,
    )
    catalog_products.append(catalog_product)

  return ProductListResponse(
    success=True,
    count=len(catalog_products),
    products=catalog_products,
  )
