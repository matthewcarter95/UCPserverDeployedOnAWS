#!/bin/bash
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

# Build script for packaging Lambda deployment

set -e

echo "Building Lambda package..."

# Create requirements.txt for Lambda
cat requirements-lambda.txt > requirements.txt

# Package UCP SDK
echo "Packaging UCP SDK..."
if [ -d "../../../../sdk/python/src/ucp_sdk" ]; then
  # Copy SDK module directly into vendor directory
  mkdir -p vendor
  cp -r ../../../../sdk/python/src/ucp_sdk vendor/
  echo "Successfully packaged ucp_sdk module"
else
  echo "Error: UCP SDK not found at ../../../../sdk/python/src/ucp_sdk"
  echo "Please ensure the SDK is cloned at the expected location"
  exit 1
fi

echo "Build preparation complete."
echo ""
echo "Running SAM build..."
sam build

# Clean up SQLAlchemy-dependent files from Lambda package
echo ""
echo "Cleaning up SQLAlchemy dependencies from Lambda package..."
rm -f .aws-sam/build/UcpServerFunction/db.py
rm -f .aws-sam/build/UcpServerFunction/config.py
rm -f .aws-sam/build/UcpServerFunction/dependencies.py
rm -f .aws-sam/build/UcpServerFunction/server.py
rm -f .aws-sam/build/UcpServerFunction/services/checkout_service.py
rm -f .aws-sam/build/UcpServerFunction/services/fulfillment_service.py
rm -f .aws-sam/build/UcpServerFunction/routes/order.py
rm -f .aws-sam/build/UcpServerFunction/routes/ucp_implementation.py
rm -f .aws-sam/build/UcpServerFunction/dump_*.py
rm -f .aws-sam/build/UcpServerFunction/import_csv.py
rm -f .aws-sam/build/UcpServerFunction/integration_test.py
rm -f .aws-sam/build/DataInitFunction/db.py
rm -f .aws-sam/build/DataInitFunction/config.py
rm -f .aws-sam/build/DataInitFunction/dependencies.py
rm -f .aws-sam/build/DataInitFunction/server.py
rm -f .aws-sam/build/DataInitFunction/services/checkout_service.py
rm -f .aws-sam/build/DataInitFunction/services/fulfillment_service.py
rm -f .aws-sam/build/DataInitFunction/routes/order.py
rm -f .aws-sam/build/DataInitFunction/routes/ucp_implementation.py
rm -f .aws-sam/build/DataInitFunction/dump_*.py
rm -f .aws-sam/build/DataInitFunction/import_csv.py
rm -f .aws-sam/build/DataInitFunction/integration_test.py

echo "Cleanup complete. Lambda package ready for deployment."
echo ""
echo "Next steps:"
echo "  sam deploy  # Deploy to AWS"
