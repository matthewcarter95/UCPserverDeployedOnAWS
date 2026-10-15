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

# Add vendor directory to path for UCP SDK
vendor_path = os.path.join(os.path.dirname(__file__), 'vendor')
if os.path.exists(vendor_path):
  sys.path.insert(0, vendor_path)

# The shared (SQLite) service code does `import config` / `import db`. In Lambda
# those names resolve to the DynamoDB-backed equivalents.
import config_lambda  # noqa: E402
import db_dynamodb  # noqa: E402

sys.modules['config'] = config_lambda
sys.modules['db'] = db_dynamodb

from fastapi import FastAPI  # noqa: E402
from mangum import Mangum  # noqa: E402

import server as shared_server  # noqa: E402

logging.basicConfig(level=logging.INFO, format='%(levelname)s:%(name)s:%(message)s')

# Reuse the shared app (routes, exception handlers, catalog).
app: FastAPI = shared_server.app

handler = Mangum(app, lifespan="auto")
