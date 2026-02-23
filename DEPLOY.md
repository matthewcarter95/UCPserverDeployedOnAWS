# AWS Lambda Deployment Guide

This guide explains how to deploy the UCP Merchant Server to AWS Lambda with Function URLs.

## Prerequisites

1. **AWS CLI** installed and configured
   ```bash
   aws configure
   ```

2. **AWS SAM CLI** installed
   ```bash
   # macOS
   brew install aws-sam-cli

   # Or download from https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html
   ```

3. **UCP Python SDK** cloned locally
   ```bash
   # From repository root
   mkdir -p sdk
   git clone https://github.com/Universal-Commerce-Protocol/python-sdk.git sdk/python
   cd sdk/python
   uv sync
   ```

## Deployment Steps

### 1. Prepare Lambda Package

From the server directory:

```bash
cd /path/to/samples/rest/python/server

# Run build preparation script
./build-lambda.sh
```

### 2. Build the SAM Application

```bash
sam build
```

This will:
- Package Python dependencies
- Bundle the UCP SDK
- Prepare Lambda deployment package

### 3. Deploy to AWS

```bash
sam deploy --guided
```

On first deployment, answer the prompts:
- **Stack Name**: `ucp-merchant-server` (or your choice)
- **AWS Region**: Your preferred region (e.g., `us-east-1`)
- **Confirm changes**: Y
- **Allow SAM CLI IAM role creation**: Y
- **Disable rollback**: N
- **UcpServerFunction has no authentication**: Y (confirm Function URL with no auth)
- **Save arguments to config file**: Y
- **SAM configuration file**: `samconfig.toml`
- **SAM configuration environment**: `default`

For subsequent deployments, simply run:
```bash
sam deploy
```

### 4. Initialize Sample Data

After deployment, invoke the data initialization function:

```bash
# Get the function name from outputs
STACK_NAME="ucp-merchant-server"  # Use your stack name

# Invoke data init function
aws lambda invoke \
  --function-name ${STACK_NAME}-data-init \
  --payload '{}' \
  response.json

cat response.json
```

### 5. Get Your Function URL

```bash
# Get the Function URL from stack outputs
aws cloudformation describe-stacks \
  --stack-name ucp-merchant-server \
  --query 'Stacks[0].Outputs[?OutputKey==`UcpServerFunctionUrl`].OutputValue' \
  --output text
```

Or view in the AWS Console:
- CloudFormation → Stacks → ucp-merchant-server → Outputs

## Testing Your Deployment

### Test Discovery Endpoint

```bash
FUNCTION_URL="<your-function-url>"  # From deployment output

curl -X GET ${FUNCTION_URL}.well-known/ucp | python3 -m json.tool
```

### Create a Checkout Session

```bash
curl -X POST ${FUNCTION_URL}checkout-sessions \
  -H "UCP-Agent: profile=\"https://agent.example/profile\"" \
  -H "request-signature: test" \
  -H "idempotency-key: $(uuidgen)" \
  -H "request-id: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{
  "line_items": [
    {
      "item": {
        "id": "bouquet_roses",
        "title": "Red Rose"
      },
      "quantity": 1
    }
  ],
  "buyer": {
    "full_name": "John Doe",
    "email": "john.doe@example.com"
  },
  "currency": "USD",
  "payment": {
    "instruments": [],
    "handlers": []
  }
}'
```

## Architecture Overview

### DynamoDB Tables

The deployment creates these DynamoDB tables (all PAY_PER_REQUEST):

- **Products**: Product catalog
- **Inventory**: Stock levels
- **Checkouts**: Checkout sessions
- **Orders**: Completed orders
- **Customers**: Customer data
- **CustomerAddresses**: Shipping addresses
- **Discounts**: Discount codes
- **ShippingRates**: Shipping options
- **Promotions**: Active promotions
- **Idempotency**: Idempotency tracking (with TTL)
- **RequestLogs**: HTTP request logs
- **PaymentInstruments**: Payment methods

### Lambda Functions

1. **UcpServerFunction**: Main FastAPI application
   - Runtime: Python 3.11
   - Memory: 512 MB
   - Timeout: 30 seconds
   - Function URL enabled (no auth)

2. **DataInitFunction**: Sample data loader
   - Invoke manually to populate sample data
   - Runtime: Python 3.11
   - Memory: 1024 MB
   - Timeout: 300 seconds

## Key Differences from Local Deployment

### Database Layer
- **Local**: Uses SQLite with SQLAlchemy
- **Lambda**: Uses DynamoDB with boto3
- **Data Access**: `db_dynamodb.py` provides compatible interface

### Configuration
- **Local**: Uses `config.py` with command-line flags
- **Lambda**: Uses `config_lambda.py` with environment variables

### Handler
- **Local**: Runs uvicorn ASGI server
- **Lambda**: Uses Mangum adapter to bridge FastAPI with Lambda events

## Monitoring and Logs

View Lambda logs:
```bash
sam logs -n UcpServerFunction --stack-name ucp-merchant-server --tail
```

Or in AWS Console:
- CloudWatch → Log Groups → /aws/lambda/ucp-merchant-server-ucp-server

## Updating the Deployment

After making code changes:

```bash
# Rebuild and redeploy
sam build && sam deploy
```

## Cleanup

To delete all resources:

```bash
sam delete --stack-name ucp-merchant-server
```

This will remove:
- Lambda functions
- DynamoDB tables (data will be lost!)
- IAM roles
- Function URLs
- CloudFormation stack

## Cost Considerations

- **Lambda**: Free tier includes 1M requests/month, typically low cost for testing
- **DynamoDB**: Free tier includes 25 GB storage, 25 read/write units
- **Function URLs**: No additional cost
- **CloudWatch Logs**: Minimal cost for log storage

For production, consider:
- Enabling DynamoDB auto-scaling if needed
- Setting up CloudWatch alarms
- Implementing API Gateway for additional features (throttling, API keys)
- Using provisioned concurrency for consistent latency

## Troubleshooting

### "No such file or directory: ucp_sdk"

The UCP SDK wasn't packaged. Ensure:
1. SDK is cloned to `../../../../sdk/python`
2. Run `./build-lambda.sh` before `sam build`

### Cold Start Latency

First request after deployment may be slow (2-5 seconds). Subsequent requests are faster.

### DynamoDB Access Denied

Ensure the Lambda execution role has DynamoDB permissions (SAM template handles this automatically).

### Function URL Not Working

Verify the Function URL was created:
```bash
aws lambda get-function-url-config --function-name ucp-merchant-server-ucp-server
```

## Additional Resources

- [AWS SAM Documentation](https://docs.aws.amazon.com/serverless-application-model/)
- [Lambda Function URLs](https://docs.aws.amazon.com/lambda/latest/dg/lambda-urls.html)
- [DynamoDB Best Practices](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/best-practices.html)
- [Mangum Documentation](https://mangum.io/)
