# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is a reference implementation of a UCP (Universal Commerce Protocol) Merchant Server built with Python/FastAPI. It demonstrates how to implement a UCP Shopping Service, specifically a flower shop storefront that supports checkout, fulfillment, discounts, and payment processing capabilities.

## AWS Lambda Deployment

This server can be deployed to AWS Lambda with Function URLs. See **[DEPLOY.md](DEPLOY.md)** for complete deployment instructions.

Quick deployment:
```bash
./build-lambda.sh
sam build
sam deploy --guided
```

The Lambda deployment uses:
- **DynamoDB** instead of SQLite for persistence
- **Mangum** adapter to bridge FastAPI with Lambda
- **Lambda Function URLs** for HTTP access (no API Gateway needed)
- Separate data initialization Lambda for loading sample data

## Development Setup

### Prerequisites
```bash
# Install uv package manager
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install dependencies
uv sync
```

### SDK Dependency
The server depends on the UCP Python SDK located at `../../../../sdk/python/` (relative path). This SDK must be cloned and installed before running the server:
```bash
# From the repository root
mkdir -p sdk
git clone https://github.com/Universal-Commerce-Protocol/python-sdk.git sdk/python
cd sdk/python
uv sync
```

### Database Initialization
The server uses two SQLite databases (both run in WAL mode for concurrent access):
- **Products DB**: Contains product catalog and promotions
- **Transactions DB**: Contains checkout sessions, orders, inventory, customers, and request logs

Initialize the databases:
```bash
mkdir /tmp/ucp_test
uv run import_csv.py \
    --products_db_path=/tmp/ucp_test/products.db \
    --transactions_db_path=/tmp/ucp_test/transactions.db \
    --data_dir=../test_data/flower_shop
```

### Running the Server
```bash
uv run server.py \
   --products_db_path=/tmp/ucp_test/products.db \
   --transactions_db_path=/tmp/ucp_test/transactions.db \
   --port=8182
```

### Running Tests
```bash
# Run integration tests
uv run integration_test.py
```

### Linting and Formatting
The project uses Ruff for code formatting and linting:
```bash
# Format code
uv run ruff format .

# Run linter
uv run ruff check .

# Auto-fix linting issues
uv run ruff check --fix .
```

**Ruff configuration** (pyproject.toml):
- Line length: 80 characters
- Indent: 2 spaces
- Selected rules: E, F, W, B, C4, SIM, N, UP, D, PTH, T20
- Ignored: D203, D213

## Architecture

### High-Level Structure

The server follows a layered architecture:

1. **Entry Point** (`server.py`): FastAPI application setup, lifespan management, exception handlers
2. **Route Layer**:
   - `generated_routes/ucp_routes.py`: Auto-generated route stubs from OpenAPI spec
   - `routes/ucp_implementation.py`: Business logic implementations injected into generated routes
   - `routes/discovery.py`: Discovery profile endpoint (/.well-known/ucp)
   - `routes/order.py`: Order management and testing endpoints
3. **Service Layer**:
   - `services/checkout_service.py`: Core checkout lifecycle management (create, update, complete, cancel)
   - `services/fulfillment_service.py`: Shipping calculation and fulfillment options
4. **Data Layer** (`db.py`): SQLAlchemy models and async data access helpers
5. **Dependencies** (`dependencies.py`): FastAPI dependency injection (headers, DB sessions, services)

### Key Architectural Patterns

#### Generated Routes + Implementation Injection
The server uses `fastapi-code-generator` to generate route stubs, then injects business logic:
```python
# routes/ucp_implementation.py
def apply_implementation(router: APIRouter):
    # Replaces generated stub endpoints with actual implementations
    # Maps operation_id -> implementation function
```

#### Unified Models (Extension Composition)
The server combines multiple UCP capability extensions using Pydantic model inheritance:
```python
# models.py
class UnifiedCheckout(
    BuyerConsentCheckoutResp,
    FulfillmentCheckout,
    DiscountCheckoutResp,
    Ap2Checkout
):
    """Combines base checkout + fulfillment + discounts + buyer consent + AP2"""
```

This pattern allows the server to support multiple UCP extensions simultaneously without duplicating code.

#### Multi-Database Architecture
- **Products DB**: Read-mostly catalog data (products, promotions)
- **Transactions DB**: Write-heavy transactional data (checkouts, orders, inventory, logs)
- Both use SQLite with WAL mode for concurrent access
- Managed by `DatabaseManager` singleton in `db.py`

#### Idempotency Handling
All mutating operations require an `Idempotency-Key` header. The server:
1. Hashes the request body
2. Stores response in `idempotency_records` table
3. Returns cached response if key+hash match
4. Returns 409 conflict if key exists with different hash

#### Webhook Discovery
The server extracts webhook URLs from agent profiles (via `UCP-Agent` header):
1. Parses profile URL from header
2. Fetches agent profile via HTTP
3. Extracts `webhook_url` from capability config
4. Stores in `PlatformConfig` for order updates

### Service Layer Details

#### CheckoutService
Orchestrates the full checkout lifecycle:
- **create_checkout**: Validates inventory, calculates initial totals, applies promotions
- **update_checkout**: Updates line items, applies discounts, recalculates fulfillment options
- **complete_checkout**: Processes payment, reserves inventory, creates order, sends webhook
- **cancel_checkout**: Releases reserved inventory, marks checkout as canceled

Key behaviors:
- Maintains checkout state machine (CheckoutStatus enum)
- Calculates hierarchical totals (line item → subtotal → shipping → discount → total)
- Supports multiple payment handlers (mock, Google Pay, Shop Pay)
- Sends webhooks asynchronously to agent platforms

#### FulfillmentService
Calculates shipping rates based on:
- Destination country (fallback to "default" rates)
- Service level (standard, express, etc.)
- Hierarchical fulfillment: destination → group → method structure

### Database Models

**Products DB:**
- `Product`: id, title, price, image_url
- `Promotion`: id, type, min_subtotal, eligible_item_ids, description

**Transactions DB:**
- `Inventory`: product_id, quantity
- `CheckoutSession`: id, status, data (JSON blob of checkout object)
- `Order`: id, data (JSON blob of order object)
- `Customer`: id, name, email
- `CustomerAddress`: id, customer_id, street_address, city, state, postal_code, country
- `Discount`: code, type, value, description
- `ShippingRate`: id, country_code, service_level, price, title
- `PaymentInstrument`: id, type, brand, last_digits, token, handler_id
- `RequestLog`: id, timestamp, method, url, checkout_id, payload
- `IdempotencyRecord`: key, request_hash, response_status, response_body, created_at

### UCP Discovery Profile

The server publishes its capabilities at `/.well-known/ucp` (routes/discovery_profile.json):
- **Services**: `dev.ucp.shopping` (REST endpoint)
- **Capabilities**: checkout, discount, fulfillment
- **Payment Handlers**: shop_pay, google_pay, mock_payment_handler

### Testing Endpoints

The server includes simulation endpoints for testing:
- `POST /testing/simulate-shipping/{id}`: Triggers "order shipped" webhook
  - Requires `Simulation-Secret` header matching `--simulation_secret` flag
  - Updates order status and sends webhook to agent platform

## Common Development Workflows

### Adding a New UCP Extension
1. Update SDK dependency if needed (`uv sync`)
2. Add extension models to `models.py` (inherit in `UnifiedCheckout*`)
3. Update `CheckoutService` to handle extension-specific fields
4. Rebuild Pydantic models: `UnifiedCheckout.model_rebuild()`

### Modifying Checkout Logic
- Edit `services/checkout_service.py`
- Key methods: `_calculate_totals()`, `_calculate_checkout_totals()`, `_apply_discounts()`
- Test with `integration_test.py`

### Adding Database Models
1. Add model class to `db.py` (inherit `ProductBase` or `TransactionBase`)
2. Add data access helper functions in `db.py`
3. Re-run `import_csv.py` to create tables

### Debugging Database State
Utility scripts for inspecting database contents:
```bash
uv run dump_inventory.py --transactions_db_path=/tmp/ucp_test/transactions.db
uv run dump_log.py --transactions_db_path=/tmp/ucp_test/transactions.db
uv run dump_transactions.py --transactions_db_path=/tmp/ucp_test/transactions.db
```

## Code Conventions

### Header Validation
All UCP endpoints require these headers (validated in `dependencies.py`):
- `UCP-Agent`: Agent profile URL and version (format: `profile="URL"; version="2026-01-11"`)
- `Request-Signature`: HMAC signature (bypassed in sample with "test" value)
- `Request-Id`: Unique request identifier
- `Idempotency-Key`: For mutating operations

### Error Handling
Custom exceptions in `exceptions.py` map to HTTP status codes:
- `ResourceNotFoundError` → 404
- `IdempotencyConflictError` → 409
- `CheckoutNotModifiableError` → 409
- `OutOfStockError` → 400
- `PaymentFailedError` → 402
- `InvalidRequestError` → 400

All inherit from `UcpError` base class with `message`, `code`, and `status_code`.

### Dependency Injection
Use FastAPI dependencies from `dependencies.py`:
- `common_headers`: Validates UCP headers
- `idempotency_header`: Extracts idempotency key
- `get_products_db`: Products DB session
- `get_transactions_db`: Transactions DB session
- `get_checkout_service`: CheckoutService instance
- `verify_simulation_secret`: Validates simulation endpoints

## Important Notes

- The server is a **reference implementation** for demonstration purposes
- Signature verification is stubbed out (accepts "test" as valid)
- Payment processing is simulated (mock handler uses token strings)
- The server expects the SDK at a specific relative path: `../../../../sdk/python/`
- All prices are stored in cents (integer values)
- Checkout sessions store full state as JSON blobs in the database
- WAL mode is critical for concurrent access between main server and webhook server
