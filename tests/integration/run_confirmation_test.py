# SPDX-License-Identifier: MIT-0
"""Wrapper: looks up config from CloudFormation, then runs test_confirmation_flow.py."""

import argparse
import subprocess
import sys
from pathlib import Path

import boto3

STACK_NAME = "sns-email-aide"


def _get_stack_output(cfn, stack_name, key):
    stacks = cfn.describe_stacks(StackName=stack_name)["Stacks"]
    for output in stacks[0].get("Outputs", []):
        if output["OutputKey"] == key:
            return output["OutputValue"]
    return None


def main():
    parser = argparse.ArgumentParser(description="Run confirmation flow integration test")
    parser.add_argument("--profile", default=None, help="AWS CLI profile")
    parser.add_argument("--region", default="us-east-2", help="AWS region")
    parser.add_argument("--domain", required=True, help="Test email domain")
    args, passthrough = parser.parse_known_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    cfn = session.client("cloudformation")

    function_name = _get_stack_output(cfn, STACK_NAME, "InterfaceLambdaName")
    if not function_name:
        print(f"ERROR: Could not find InterfaceLambdaName output in stack {STACK_NAME}")
        print("Make sure the stack is deployed (see README).")
        sys.exit(1)

    print(f"TEST_EMAIL_DOMAIN={args.domain}")
    print(f"SNS_EMAIL_AIDE_FUNCTION={function_name}")
    print()

    script = Path(__file__).parent / "test_confirmation_flow.py"
    cmd = [
        sys.executable, str(script),
        "--domain", args.domain,
        "--function-name", function_name,
        "--region", args.region,
    ]
    if args.profile:
        cmd += ["--profile", args.profile]
    cmd += passthrough

    sys.exit(subprocess.call(cmd))


if __name__ == "__main__":
    main()
