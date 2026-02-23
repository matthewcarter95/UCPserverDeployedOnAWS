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

"""Lambda handler for UCP Merchant Server using Mangum adapter."""

import logging
import os
import sys
from contextlib import asynccontextmanager

# Add vendor directory to path for UCP SDK
vendor_path = os.path.join(os.path.dirname(__file__), 'vendor')
if os.path.exists(vendor_path):
  sys.path.insert(0, vendor_path)

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mangum import Mangum

import config_lambda as config
from exceptions import UcpError
import generated_routes.ucp_routes
from routes.catalog_lambda import router as catalog_router
from routes.discovery import router as discovery_router
from routes.order_lambda import router as order_router
import routes.ucp_implementation_lambda

# Configure logging for Lambda
logging.basicConfig(
  level=logging.INFO,
  format='%(levelname)s:%(name)s:%(message)s'
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
  """Lifespan manager for Lambda - initializes DynamoDB connections."""
  # Initialize DynamoDB connections
  await config.init_dynamodb()
  yield
  # Cleanup
  await config.cleanup_dynamodb()


# Create FastAPI app
app = FastAPI(
  title="UCP Shopping Service",
  version="2026-01-11",
  description="Reference implementation of the UCP Shopping Service (AWS Lambda)",
  lifespan=lifespan,
)


@app.exception_handler(UcpError)
async def ucp_exception_handler(request: Request, exc: UcpError):
  """Handle UCP-specific exceptions and converts them to JSON responses."""
  del request  # Unused.
  return JSONResponse(
    status_code=exc.status_code,
    content={"detail": exc.message, "code": exc.code},
  )


# Apply business logic implementation to generated routes
routes.ucp_implementation_lambda.apply_implementation(
  generated_routes.ucp_routes.router
)
app.include_router(generated_routes.ucp_routes.router)
app.include_router(catalog_router)
app.include_router(order_router)
app.include_router(discovery_router)

# Create Mangum handler for Lambda
handler = Mangum(app, lifespan="auto")
