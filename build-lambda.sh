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
if [ -d "sdk/python/src/ucp_sdk" ]; then
  # Copy SDK module directly into vendor directory
  mkdir -p vendor
  cp -r sdk/python/src/ucp_sdk vendor/
  echo "Successfully packaged ucp_sdk module"
elif [ -d "../../../../sdk/python/src/ucp_sdk" ]; then
  # Fallback to old path for compatibility
  mkdir -p vendor
  cp -r ../../../../sdk/python/src/ucp_sdk vendor/
  echo "Successfully packaged ucp_sdk module"
else
  echo "Error: UCP SDK not found at sdk/python/src/ucp_sdk or ../../../../sdk/python/src/ucp_sdk"
  echo "Please ensure the SDK is cloned at the expected location"
  exit 1
fi

echo "Build preparation complete."
echo ""
echo "Running SAM build..."
sam build

# The Lambda reuses the shared server code (server.py, config.py, services/,
# routes/) and swaps config -> config_lambda and db -> db_dynamodb at import
# time (see lambda_handler.py). Only strip files that are never imported in
# Lambda and would drag in SQLAlchemy / absl.
echo ""
echo "Cleaning up local-only files from Lambda package..."
for fn in UcpServerFunction DataInitFunction; do
  rm -f .aws-sam/build/$fn/db.py
  rm -f .aws-sam/build/$fn/dump_*.py
  rm -f .aws-sam/build/$fn/import_csv.py
  rm -f .aws-sam/build/$fn/integration_test.py
done

echo "Cleanup complete. Lambda package ready for deployment."
echo ""
echo "Next steps:"
echo "  sam deploy  # Deploy to AWS"
