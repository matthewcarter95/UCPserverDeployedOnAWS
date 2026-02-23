# UCP Merchant Server - AWS Lambda Deployment

A production-ready UCP (Universal Commerce Protocol) Merchant Server deployed on AWS Lambda with DynamoDB. This implements a flower shop storefront supporting checkout, discounts, fulfillment, and payment processing.

## Features

- **AWS Lambda** deployment with Function URLs (no API Gateway needed)
- **DynamoDB** for scalable persistence
- **FastAPI** REST API with UCP protocol support
- **Multiple payment handlers**: Mock, Shop Pay, Google Pay
- **UCP Extensions**: Checkout, Discount, Fulfillment, Buyer Consent
- **Sample test data** included

## Quick Start

### Prerequisites
- AWS CLI configured with credentials
- SAM CLI installed (`brew install aws-sam-cli`)
- Python 3.11+
- uv package manager (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

### Setup

1. Clone with submodules:
```bash
git clone --recurse-submodules https://github.com/matthewcarter95/UCPserverDeployedOnAWS.git
cd UCPserverDeployedOnAWS
```

2. Install dependencies:
```bash
uv sync
cd sdk/python
uv sync
cd ../..
```

3. Build and deploy:
```bash
./build-lambda.sh
sam build
sam deploy --guided
```

During `sam deploy --guided`, you'll be prompted for:
- Stack name (e.g., `ucp-merchant-server`)
- Region (e.g., `us-east-1`)
- Confirm changeset: Y
- Save config: Y

4. Load test data:
```bash
uv run load_dynamodb_data.py \
  --stack_name=ucp-merchant-server \
  --data_dir=./test_data/flower_shop \
  --region=us-east-1
```

## Testing

After deployment, you'll get a Function URL like:
```
https://XXXXXXX.lambda-url.us-east-1.on.aws/
```

Test the discovery endpoint:
```bash
curl https://YOUR-FUNCTION-URL/.well-known/ucp | python3 -m json.tool
```

Test a complete checkout flow:
```bash
# From the samples repo client directory
cd /path/to/samples/rest/python/client/flower_shop
uv sync
uv run simple_happy_path_client.py --server_url=https://YOUR-FUNCTION-URL/
```

## Architecture

### Components
- **lambda_handler.py** - Mangum adapter bridging FastAPI to Lambda
- **services/*_lambda.py** - Business logic using DynamoDB
- **db_dynamodb.py** - DynamoDB data access layer
- **routes/*_lambda.py** - API endpoint implementations
- **models.py** - Unified Pydantic models combining UCP extensions

### DynamoDB Tables
The stack creates 13 tables:
- Products, Inventory, Promotions
- Checkouts, Orders
- Customers, Customer Addresses
- Discounts, Shipping Rates, Payment Instruments
- Idempotency Records, Request Logs, Transactions

### Key Files
- **template.yaml** - CloudFormation/SAM template
- **build-lambda.sh** - Packages SDK and dependencies for Lambda
- **load_dynamodb_data.py** - Loads CSV test data into DynamoDB

## Configuration

All configuration is via CloudFormation environment variables:
- Table names auto-generated with stack prefix
- Function timeout: 30s
- Memory: 512MB
- Runtime: Python 3.11

## UCP Protocol Support

Capabilities:
- `dev.ucp.shopping.checkout` - Create and manage shopping carts
- `dev.ucp.shopping.discount` - Apply promotional codes
- `dev.ucp.shopping.fulfillment` - Shipping options and rates
- `dev.ucp.shopping.buyer_consent` - Buyer consent tracking
- `dev.ucp.shopping.order` - Order management

Payment Handlers:
- Mock handler (for testing)
- Shop Pay integration
- Google Pay integration

## Development

### Local Testing (SQLite)
For local development without AWS:
```bash
mkdir /tmp/ucp_test
uv run import_csv.py \
  --products_db_path=/tmp/ucp_test/products.db \
  --transactions_db_path=/tmp/ucp_test/transactions.db \
  --data_dir=./test_data/flower_shop

uv run server.py \
  --products_db_path=/tmp/ucp_test/products.db \
  --transactions_db_path=/tmp/ucp_test/transactions.db \
  --port=8182
```

### Code Quality
```bash
# Format code
uv run ruff format .

# Run linter
uv run ruff check .

# Auto-fix issues
uv run ruff check --fix .
```

## Redeployment

After making code changes:
```bash
./build-lambda.sh
sam deploy --no-confirm-changeset
```

## Cleanup

Remove all AWS resources:
```bash
sam delete --stack-name ucp-merchant-server
```

## License

Apache 2.0
