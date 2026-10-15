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

"""Catalog capability routes (search, lookup, get_product).

Implements dev.ucp.shopping.catalog.search and .lookup per the UCP
2026-08-25 REST binding:

  POST /catalog/search   - search_catalog
  POST /catalog/lookup   - lookup_catalog (batch)
  POST /catalog/product  - get_product

Every product in this shop has exactly one variant, whose id and sku equal the
product id. That keeps checkout (which resolves line items by product id)
working with ids taken from catalog responses.
"""

import base64
import json
import os
from typing import Any

import config
from fastapi import APIRouter
from fastapi import Request
from fastapi.responses import JSONResponse

router = APIRouter()

MAX_LOOKUP_IDS = 50
DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 50
DEFAULT_CATEGORY = "Home & Garden > Plants > Flowers"


async def _load_products() -> list[dict[str, Any]]:
  """Return all products with an `inventory_quantity`, sorted by id."""
  if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    import db_dynamodb  # noqa: PLC0415

    products = await db_dynamodb.list_products_with_inventory()
  else:
    import db  # noqa: PLC0415
    from sqlalchemy import select  # noqa: PLC0415

    async with db.manager.products_session_factory() as ps:
      rows = (await ps.execute(select(db.Product))).scalars().all()
    products = [
      {"id": r.id, "title": r.title, "price": r.price, "image_url": r.image_url}
      for r in rows
    ]
    async with db.manager.transactions_session_factory() as ts:
      for p in products:
        p["inventory_quantity"] = await db.get_inventory(ts, p["id"]) or 0
  return sorted(products, key=lambda p: p["id"])


def _to_product(p: dict[str, Any], base_url: str) -> dict[str, Any]:
  """Map a stored product row to a spec Product with a single Variant."""
  title = p["title"]
  desc = {"plain": p.get("description") or title}
  price = {"amount": int(p["price"]), "currency": config.get_default_currency()}
  url = p.get("link") or f"{base_url}/products/{p['id']}"
  media = (
    [{"type": "image", "url": p["image_url"], "alt_text": title}]
    if p.get("image_url")
    else None
  )
  category = p.get("category") or DEFAULT_CATEGORY
  variant: dict[str, Any] = {
    "id": p["id"],
    "sku": p["id"],
    "title": title,
    "description": desc,
    "price": price,
    "availability": {"available": p.get("inventory_quantity", 0) > 0},
  }
  product: dict[str, Any] = {
    "id": p["id"],
    "title": title,
    "description": desc,
    "url": url,
    "categories": [{"value": category, "taxonomy": "merchant"}],
    "price_range": {"min": price, "max": price},
    "variants": [variant],
  }
  if media:
    product["media"] = media
  return product


def _envelope(capability: str) -> dict[str, Any]:
  return {
    "version": config.get_server_version(),
    "capabilities": {
      capability: [{"version": config.get_server_version()}]
    },
  }


def _matches_filters(product: dict[str, Any], filters: dict | None) -> bool:
  if not filters:
    return True
  cats = filters.get("categories")
  if cats:
    have = {c["value"].lower() for c in product.get("categories", [])}
    have |= {
      part.strip().lower()
      for c in product.get("categories", [])
      for part in c["value"].split(">")
    }
    if not have & {c.lower() for c in cats}:
      return False
  price = filters.get("price") or {}
  amount = product["price_range"]["min"]["amount"]
  if price.get("min") is not None and amount < price["min"]:
    return False
  if price.get("max") is not None and amount > price["max"]:
    return False
  return True


def _error(status: int, code: str, content: str, capability: str) -> JSONResponse:
  return JSONResponse(
    status_code=status,
    content={
      "ucp": {**_envelope(capability), "status": "error"},
      "messages": [
        {
          "type": "error",
          "code": code,
          "content": content,
          "severity": "unrecoverable",
        }
      ],
    },
  )


@router.post("/catalog/search")
async def search_catalog(request: Request, body: dict[str, Any]):
  """Search the catalog by free text and/or filters, with cursor paging."""
  cap = "dev.ucp.shopping.catalog.search"
  query = (body.get("query") or "").strip()
  filters = body.get("filters")
  if not query and not filters:
    return _error(
      400, "invalid_request", "Provide a `query` or at least one filter.", cap
    )

  base = str(request.base_url).rstrip("/")
  products = [_to_product(p, base) for p in await _load_products()]
  if query:
    terms = query.lower().split()
    products = [
      p
      for p in products
      if all(
        t in f"{p['title']} {p['description']['plain']}".lower() for t in terms
      )
    ]
  products = [p for p in products if _matches_filters(p, filters)]

  page = body.get("pagination") or {}
  limit = min(int(page.get("limit") or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE)
  offset = 0
  if page.get("cursor"):
    try:
      offset = int(json.loads(base64.urlsafe_b64decode(page["cursor"]))["o"])
    except (ValueError, KeyError, TypeError):
      return _error(400, "invalid_cursor", "Unrecognized pagination cursor.", cap)
  window = products[offset : offset + limit]
  has_next = offset + limit < len(products)
  pagination: dict[str, Any] = {
    "has_next_page": has_next,
    "total_count": len(products),
  }
  if has_next:
    pagination["cursor"] = base64.urlsafe_b64encode(
      json.dumps({"o": offset + limit}).encode()
    ).decode()
  return {
    "ucp": _envelope(cap),
    "products": window,
    "pagination": pagination,
  }


def _resolve(products: list[dict[str, Any]], ident: str):
  """Return (product, match) for a product id, variant id or sku."""
  for p in products:
    for v in p["variants"]:
      if ident in (v["id"], v.get("sku")):
        return p, "exact"
  return None, None


@router.post("/catalog/lookup")
async def lookup_catalog(request: Request, body: dict[str, Any]):
  """Batch lookup by product or variant id."""
  cap = "dev.ucp.shopping.catalog.lookup"
  ids = body.get("ids")
  if not isinstance(ids, list) or not ids:
    return _error(400, "invalid_request", "`ids` must be a non-empty array.", cap)
  if len(ids) > MAX_LOOKUP_IDS:
    return _error(
      400,
      "request_too_large",
      f"At most {MAX_LOOKUP_IDS} ids per request.",
      cap,
    )
  base = str(request.base_url).rstrip("/")
  products = [_to_product(p, base) for p in await _load_products()]
  found: dict[str, dict[str, Any]] = {}
  messages = []
  for i, ident in enumerate(dict.fromkeys(ids)):
    product, match = _resolve(products, ident)
    if not product or not _matches_filters(product, body.get("filters")):
      messages.append(
        {
          "type": "info",
          "code": "not_found",
          "path": f"$.ids[{i}]",
          "content": f"No product found for id '{ident}'.",
        }
      )
      continue
    entry = found.setdefault(product["id"], json.loads(json.dumps(product)))
    entry["variants"][0].setdefault("inputs", []).append(
      {"id": ident, "match": match}
    )
  resp: dict[str, Any] = {"ucp": _envelope(cap), "products": list(found.values())}
  if messages:
    resp["messages"] = messages
  return resp


@router.post("/catalog/product")
async def get_product(request: Request, body: dict[str, Any]):
  """Full detail for one product or variant id."""
  cap = "dev.ucp.shopping.catalog.lookup"
  ident = body.get("id")
  base = str(request.base_url).rstrip("/")
  products = [_to_product(p, base) for p in await _load_products()]
  product, _ = _resolve(products, ident) if ident else (None, None)
  if not product or not _matches_filters(product, body.get("filters")):
    return JSONResponse(
      status_code=200,
      content={
        "ucp": {**_envelope(cap), "status": "error"},
        "messages": [
          {
            "type": "error",
            "code": "not_found",
            "content": f"No product found for id '{ident}'.",
            "severity": "unrecoverable",
          }
        ],
      },
    )
  # Single-variant products have no configurable options.
  return {"ucp": _envelope(cap), "product": product}
