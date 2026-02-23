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

"""Lambda-specific dependencies using DynamoDB instead of SQLite."""

import re
from typing import Annotated

import config_lambda as config
from fastapi import Depends, Header, HTTPException, Request
from pydantic import BaseModel
from services.checkout_service_lambda import CheckoutServiceLambda
from services.fulfillment_service_lambda import FulfillmentService


class CommonHeaders(BaseModel):
  """Common headers used in UCP requests."""

  x_api_key: str | None = None
  ucp_agent: str
  request_signature: str
  request_id: str


async def common_headers(
  x_api_key: str | None = Header(None),
  ucp_agent: str = Header(...),
  request_signature: str = Header(...),
  request_id: str = Header(...),
) -> CommonHeaders:
  """Extract and validate common headers."""
  await validate_ucp_headers(ucp_agent)
  return CommonHeaders(
    x_api_key=x_api_key,
    ucp_agent=ucp_agent,
    request_signature=request_signature,
    request_id=request_id,
  )


async def validate_ucp_headers(ucp_agent: str):
  """Validate UCP headers and version negotiation."""
  server_version = config.get_server_version()
  agent_version = server_version

  match = re.search(
    r"(?:^|;)\s*version=(?:\"([^\"]+)\"|([^;]+))", ucp_agent, re.IGNORECASE
  )
  if match:
    agent_version = match.group(1) or match.group(2)
    agent_version = agent_version.strip()

  if agent_version > server_version:
    raise HTTPException(
      status_code=400,
      detail={
        "status": "error",
        "errors": [
          {
            "code": "VERSION_UNSUPPORTED",
            "message": (
              f"Version {agent_version} is not supported. This merchant"
              f" implements version {server_version}."
            ),
            "severity": "critical",
          }
        ],
      },
    )


async def idempotency_header(
  idempotency_key: str = Header(...),
) -> str:
  """Extract the Idempotency-Key header."""
  return idempotency_key


async def verify_signature(
  request_signature: str = Header(..., alias="Request-Signature"),
) -> None:
  """Verify the request signature (bypassed in sample)."""
  if request_signature == "test":
    return
  return


async def verify_simulation_secret(
  simulation_secret: str | None = Header(None, alias="Simulation-Secret"),
) -> None:
  """Verify the secret for simulation endpoints."""
  expected_secret = config.SIMULATION_SECRET
  if not expected_secret:
    raise HTTPException(
      status_code=500, detail="Simulation secret not configured"
    )

  if not simulation_secret or simulation_secret != expected_secret:
    raise HTTPException(status_code=403, detail="Invalid Simulation Secret")


def get_fulfillment_service() -> FulfillmentService:
  """Dependency provider for FulfillmentService."""
  return FulfillmentService()


def get_checkout_service(
  request: Request,
  fulfillment_service: Annotated[
    FulfillmentService, Depends(get_fulfillment_service)
  ],
) -> CheckoutServiceLambda:
  """Dependency provider for CheckoutService (Lambda/DynamoDB version)."""
  return CheckoutServiceLambda(
    fulfillment_service,
    str(request.base_url),
  )
