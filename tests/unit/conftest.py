# SPDX-License-Identifier: MIT-0
"""Test environment setup for the unit suite.

The Lambda sources build their boto3 clients/resources at import time (e.g.
`dynamodb = boto3.resource('dynamodb')` in the interface Lambda). boto3 needs a
region to construct a client; with none configured it raises
`botocore.exceptions.NoRegionError`. A developer machine usually has a region in
`~/.aws/config` or the environment, so this passes locally - but GitHub-hosted
CI runners have none, which made every interface test error out at import.

Pin a region here, before any test module (and therefore any Lambda source) is
imported, so client construction succeeds regardless of environment. No real AWS
calls happen: the tests mock every client/resource, so credentials are not
needed - only a region for the constructor.
"""

import os

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
