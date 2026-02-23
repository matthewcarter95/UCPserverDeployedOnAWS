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

"""Catalog routes for product discovery (SQLite version)."""

from typing import Annotated

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

import db
from dependencies import get_products_db
from dependencies import get_transactions_db
from models import CatalogProduct
from models import ProductListResponse
from models import ProductPrice

router = APIRouter()


@router.get(
  "/api/ucp/products",
  response_model=ProductListResponse,
  summary="List all products",
)
async def list_products(
  request: Request,
  products_session: Annotated[AsyncSession, Depends(get_products_db)],
  transactions_session: Annotated[AsyncSession, Depends(get_transactions_db)],
):
  """Return all products with availability information.

  This endpoint allows AI agents to discover available products before
  creating checkout sessions. No authentication is required for public
  catalog browsing.

  Returns:
    ProductListResponse with all products and their availability status.

  """
  # Fetch all products with inventory
  products_with_inventory = await db.list_products_with_inventory(
    products_session, transactions_session
  )

  # Transform to catalog format
  catalog_products = []
  base_url = str(request.base_url).rstrip("/")

  for product, quantity in products_with_inventory:
    # Determine availability
    availability = "in_stock" if quantity > 0 else "out_of_stock"

    # Convert price from cents to dollars
    price_dollars = product.price / 100.0

    # Format timestamps
    created_at = (
      product.created_at.isoformat()
      if product.created_at
      else None
    )
    updated_at = (
      product.updated_at.isoformat()
      if product.updated_at
      else None
    )

    # Build product link
    link = product.link or f"{base_url}/products/{product.id}"

    catalog_product = CatalogProduct(
      id=product.id,
      title=product.title,
      description=product.description,
      price=ProductPrice(value=price_dollars, currency="USD"),
      availability=availability,
      link=link,
      image_link=product.image_url,
      brand=product.brand,
      category=product.category,
      created_at=created_at,
      updated_at=updated_at,
    )
    catalog_products.append(catalog_product)

  return ProductListResponse(
    success=True,
    count=len(catalog_products),
    products=catalog_products,
  )
