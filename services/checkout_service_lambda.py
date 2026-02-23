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

"""Lambda-compatible CheckoutService using DynamoDB.

This is a simplified adapter that uses db_dynamodb instead of SQLAlchemy.
For a full implementation, you would need to port all the logic from
checkout_service.py to work with DynamoDB's API.
"""

import datetime
import hashlib
import json
import logging
import uuid
from typing import Any

import config_lambda as config
import db_dynamodb as db
import httpx
from enums import CheckoutStatus
from exceptions import (
  CheckoutNotModifiableError,
  IdempotencyConflictError,
  InvalidRequestError,
  OutOfStockError,
  PaymentFailedError,
  ResourceNotFoundError,
)
from models import UnifiedCheckout as Checkout
from models import UnifiedCheckoutCreateRequest, UnifiedCheckoutUpdateRequest
from pydantic import BaseModel
from services.fulfillment_service_lambda import FulfillmentService
from ucp_sdk.models._internal import Response, ResponseCheckout, ResponseOrder, Version
from ucp_sdk.models.schemas.shopping.ap2_mandate import Ap2CompleteRequest
from ucp_sdk.models.schemas.shopping.order import Order, PlatformConfig
from ucp_sdk.models.schemas.shopping.payment_create_req import (
  PaymentCreateRequest,
)


logger = logging.getLogger(__name__)


class CheckoutServiceLambda:
  """Simplified CheckoutService for Lambda/DynamoDB.

  Note: This is a minimal implementation for demonstration.
  A production version would need full feature parity with checkout_service.py.
  """

  def __init__(
    self,
    fulfillment_service: FulfillmentService,
    base_url: str,
  ):
    self.fulfillment_service = fulfillment_service
    self.base_url = base_url

  async def create_checkout(
    self,
    request: UnifiedCheckoutCreateRequest,
    idempotency_key: str,
    platform_config: PlatformConfig | None = None,
  ) -> Checkout:
    """Create a new checkout session."""
    # Check idempotency
    existing_record = await db.get_idempotency_record(idempotency_key)
    if existing_record:
      request_hash = hashlib.sha256(
        json.dumps(request.model_dump(), sort_keys=True).encode()
      ).hexdigest()
      if existing_record["request_hash"] != request_hash:
        raise IdempotencyConflictError(
          "Idempotency key reused with different request"
        )
      # Return cached response
      checkout = Checkout(**existing_record["response_body"])
      return checkout

    # Create checkout
    checkout_id = str(uuid.uuid4())
    checkout_data = {
      "id": checkout_id,
      "status": CheckoutStatus.READY_FOR_COMPLETE.value,
      "line_items": [],
      "buyer": request.buyer.model_dump() if request.buyer else None,
      "currency": request.currency,
      "totals": [],
      "payment": {
        "handlers": [],
        "selected_instrument_id": request.payment.selected_instrument_id if request.payment else None,
        "instruments": request.payment.instruments if request.payment else None,
      },
      "platform": platform_config.model_dump() if platform_config else None,
      "ucp": {
        "version": config.get_server_version(),
        "capabilities": [
          {
            "name": "dev.ucp.shopping.checkout",
            "version": config.get_server_version(),
          }
        ],
      },
      "links": [],
    }

    # Process line items
    total_amount = 0
    line_items = []
    for item in request.line_items or []:
      product = await db.get_product(item.item.id)
      if not product:
        raise InvalidRequestError(f"Product {item.item.id} not found")

      line_item = {
        "id": str(uuid.uuid4()),
        "item": {
          "id": product["id"],
          "title": product["title"],
          "price": product["price"],
        },
        "quantity": item.quantity,
        "totals": [
          {"type": "subtotal", "amount": product["price"] * item.quantity},
          {"type": "total", "amount": product["price"] * item.quantity},
        ],
      }
      line_items.append(line_item)
      total_amount += product["price"] * item.quantity

    checkout_data["line_items"] = line_items
    checkout_data["totals"] = [
      {"type": "subtotal", "amount": total_amount},
      {"type": "total", "amount": total_amount},
    ]

    # Save checkout
    await db.save_checkout(
      checkout_id, CheckoutStatus.READY_FOR_COMPLETE.value, checkout_data
    )

    # Save idempotency record
    request_hash = hashlib.sha256(
      json.dumps(request.model_dump(), sort_keys=True).encode()
    ).hexdigest()
    await db.save_idempotency_record(
      idempotency_key, request_hash, 200, checkout_data
    )

    checkout = Checkout(**checkout_data)
    return checkout

  async def get_checkout(self, checkout_id: str) -> Checkout:
    """Retrieve a checkout session."""
    checkout_data = await db.get_checkout_session(checkout_id)
    if not checkout_data:
      raise ResourceNotFoundError(f"Checkout {checkout_id} not found")

    # Ensure ucp and links fields exist
    if "ucp" not in checkout_data:
      checkout_data["ucp"] = {
        "version": config.get_server_version(),
        "capabilities": [
          {
            "name": "dev.ucp.shopping.checkout",
            "version": config.get_server_version(),
          }
        ],
      }
    if "links" not in checkout_data:
      checkout_data["links"] = []

    # Ensure payment.handlers exists
    if "payment" not in checkout_data:
      checkout_data["payment"] = {"handlers": []}
    elif "handlers" not in checkout_data["payment"]:
      checkout_data["payment"]["handlers"] = []

    checkout = Checkout(**checkout_data)
    return checkout

  async def update_checkout(
    self,
    checkout_id: str,
    request: UnifiedCheckoutUpdateRequest,
    idempotency_key: str,
    platform_config: PlatformConfig | None = None,
  ) -> Checkout:
    """Update a checkout session."""
    # Get existing checkout
    checkout_data = await db.get_checkout_session(checkout_id)
    if not checkout_data:
      raise ResourceNotFoundError(f"Checkout {checkout_id} not found")

    # Check if modifiable
    if checkout_data.get("status") in [
      CheckoutStatus.COMPLETED.value,
      CheckoutStatus.CANCELED.value,
    ]:
      raise CheckoutNotModifiableError(
        f"Checkout {checkout_id} is not modifiable"
      )

    # Ensure ucp and links fields exist
    if "ucp" not in checkout_data:
      checkout_data["ucp"] = {
        "version": config.get_server_version(),
        "capabilities": [
          {
            "name": "dev.ucp.shopping.checkout",
            "version": config.get_server_version(),
          }
        ],
      }
    if "links" not in checkout_data:
      checkout_data["links"] = []

    # Ensure payment.handlers exists
    if "payment" not in checkout_data:
      checkout_data["payment"] = {"handlers": []}
    elif "handlers" not in checkout_data["payment"]:
      checkout_data["payment"]["handlers"] = []

    # Update fields (simplified - production would need full logic)
    if request.line_items:
      # Recalculate line items
      total_amount = 0
      line_items = []
      for item in request.line_items:
        product = await db.get_product(item.item.id)
        if not product:
          raise InvalidRequestError(f"Product {item.item.id} not found")

        line_item = {
          "id": item.id or str(uuid.uuid4()),
          "item": {
            "id": product["id"],
            "title": product["title"],
            "price": product["price"],
          },
          "quantity": item.quantity,
          "totals": [
            {"type": "subtotal", "amount": product["price"] * item.quantity},
            {"type": "total", "amount": product["price"] * item.quantity},
          ],
        }
        line_items.append(line_item)
        total_amount += product["price"] * item.quantity

      checkout_data["line_items"] = line_items
      checkout_data["totals"] = [
        {"type": "subtotal", "amount": total_amount},
        {"type": "total", "amount": total_amount},
      ]

    # Apply discounts if provided
    if request.discounts and request.discounts.codes:
      discounts = await db.get_discounts_by_codes(request.discounts.codes)

      # Initialize discounts object
      checkout_data["discounts"] = {
        "codes": request.discounts.codes,
        "applied": []
      }

      if discounts:
        # Simplified discount application
        discount = discounts[0]
        subtotal = checkout_data["totals"][0]["amount"]
        if discount["type"] == "percentage":
          discount_amount = int(subtotal * discount["value"] / 100)
        else:
          discount_amount = discount["value"]

        checkout_data["discounts"]["applied"].append({
          "code": discount["code"],
          "title": discount["description"],
          "amount": discount_amount,
          "allocations": [
            {
              "path": "$.totals[?(@.type=='subtotal')]",
              "amount": discount_amount
            }
          ]
        })

        checkout_data["totals"].insert(
          1, {"type": "discount", "amount": discount_amount}
        )
        checkout_data["totals"][-1]["amount"] -= discount_amount

    # Update platform config
    if platform_config:
      checkout_data["platform"] = platform_config.model_dump()

    # Save updated checkout
    await db.save_checkout(
      checkout_id, checkout_data.get("status"), checkout_data
    )

    checkout = Checkout(**checkout_data)
    return checkout

  async def complete_checkout(
    self,
    checkout_id: str,
    payment: PaymentCreateRequest,
    risk_signals: dict[str, Any],
    idempotency_key: str,
    ap2: Ap2CompleteRequest | None = None,
  ) -> Order:
    """Complete checkout and create order."""
    # Get checkout
    checkout_data = await db.get_checkout_session(checkout_id)
    if not checkout_data:
      raise ResourceNotFoundError(f"Checkout {checkout_id} not found")

    # Reserve inventory
    for line_item in checkout_data.get("line_items", []):
      product_id = line_item["item"]["id"]
      quantity = line_item["quantity"]
      success = await db.reserve_stock(product_id, quantity)
      if not success:
        raise OutOfStockError(f"Insufficient stock for {product_id}")

    # Create order
    order_id = str(uuid.uuid4())
    order_data = {
      "id": order_id,
      "checkout_id": checkout_id,
      "line_items": checkout_data["line_items"],
      "buyer": checkout_data.get("buyer"),
      "totals": checkout_data["totals"],
      "currency": checkout_data.get("currency", "USD"),
      "status": "processing",
      "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
      "ucp": checkout_data.get("ucp", {
        "version": config.get_server_version(),
        "capabilities": [
          {
            "name": "dev.ucp.shopping.checkout",
            "version": config.get_server_version(),
          }
        ],
      }),
    }

    await db.save_order(order_id, order_data)

    # Update checkout status
    checkout_data["status"] = CheckoutStatus.COMPLETED.value
    checkout_data["order_id"] = order_id
    await db.save_checkout(checkout_id, CheckoutStatus.COMPLETED.value, checkout_data)

    # Send webhook if configured
    if checkout_data.get("platform", {}).get("webhook_url"):
      webhook_url = checkout_data["platform"]["webhook_url"]
      try:
        async with httpx.AsyncClient() as client:
          await client.post(webhook_url, json=order_data, timeout=10)
      except Exception as e:
        logger.error(f"Failed to send webhook: {e}")

    order = Order(**order_data)
    return order

  async def cancel_checkout(
    self, checkout_id: str, idempotency_key: str
  ) -> Checkout:
    """Cancel a checkout session."""
    checkout_data = await db.get_checkout_session(checkout_id)
    if not checkout_data:
      raise ResourceNotFoundError(f"Checkout {checkout_id} not found")

    # Ensure ucp and links fields exist
    if "ucp" not in checkout_data:
      checkout_data["ucp"] = {
        "version": config.get_server_version(),
        "capabilities": [
          {
            "name": "dev.ucp.shopping.checkout",
            "version": config.get_server_version(),
          }
        ],
      }
    if "links" not in checkout_data:
      checkout_data["links"] = []

    # Ensure payment.handlers exists
    if "payment" not in checkout_data:
      checkout_data["payment"] = {"handlers": []}
    elif "handlers" not in checkout_data["payment"]:
      checkout_data["payment"]["handlers"] = []

    checkout_data["status"] = CheckoutStatus.CANCELED.value
    await db.save_checkout(checkout_id, CheckoutStatus.CANCELED.value, checkout_data)

    return Checkout(**checkout_data)

  async def update_order(self, order_id: str, order_data: dict[str, Any]) -> None:
    """Update an existing order."""
    await db.save_order(order_id, order_data)
