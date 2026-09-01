# SPDX-License-Identifier: MIT-0
"""
Integration test: exercises the confirmation responder end-to-end.

Registers a test email (with expectations), creates an SNS topic, subscribes
the email, polls until the subscription is auto-confirmed, then publishes one
notification that should fail its expectations and one that should pass,
polling for each.

Usage:
    python tests/integration/test_confirmation_flow.py --profile <profile> \
        --function-name sns-email-aide-interface --domain <domain>

Requires:
    - sns-email-aide stack deployed
    - AWS credentials with lambda:InvokeFunction + sns:* permissions

Cleanup: SNS topic is deleted on exit. DynamoDB record expires via TTL.
"""

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

import boto3


def _invoke(lambda_client, function_name, payload):
    resp = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode(),
    )
    raw = resp["Payload"].read().decode()
    result = json.loads(raw) if raw else None
    # A handler exception surfaces as FunctionError with an errorMessage payload.
    if resp.get("FunctionError"):
        raise RuntimeError(f"Lambda error: {result}")
    return result


def _fmt_ts(value):
    """Format an epoch timestamp as a readable UTC string; pass through if unset."""
    if value is None:
        return "-"
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError):
        return str(value)


def _diagnose_rule_set(session, function_name):
    """Inspect SES and report whether the aide's receipt rule set is active.

    Confirmation email is only ever processed when the aide's rule set is the
    active one (CloudFormation does not manage the active rule set). Returns a
    verdict so the caller can fail fast instead of waiting out the timeout:
      "active"   - expected set is active; a delay is something else.
      "inactive" - expected set is not active (or absent); nothing will process.
      "unknown"  - could not determine (e.g. missing SES permissions).
    """
    # Rule set is <stack>-mail-rules; stack derives from <stack>-interface.
    expected = function_name.replace("-interface", "") + "-mail-rules"
    fix = f"   aws ses set-active-receipt-rule-set --rule-set-name {expected}"
    try:
        ses = session.client("ses")
        active = ses.describe_active_receipt_rule_set().get("Metadata")
        active_name = active.get("Name") if active else None
        names = {rs["Name"] for rs in ses.list_receipt_rule_sets().get("RuleSets", [])}

        print("  Checking SES receipt rule set activation...")
        print(f"    active rule set: {active_name or 'none'}")
        print(f"    expected:        {expected}")

        if active_name == expected:
            print("    -> expected set is active; activation looks fine.")
            return "active"
        if expected in names:
            print("    -> expected set exists but is NOT active. Activate it:")
            print(fix)
        else:
            print("    -> expected set not found in this region. Is the stack")
            print("       deployed here, and did you deploy under this name?")
        return "inactive"
    except Exception as e:
        print(f"  (could not check SES rule sets: {e})")
        print("  Did you make the SES receipt rule set active?")
        print(fix)
        return "unknown"


def _poll_for(status_fn, target, timeout, success, fail, interval=10):
    """Poll status_fn() until its 'status' equals target, else exit(1) on timeout."""
    start = time.time()
    while True:
        elapsed = time.time() - start
        if elapsed > timeout:
            print(f"  TIMEOUT after {int(elapsed)}s - {fail}")
            sys.exit(1)
        s = status_fn().get("status")
        print(f"  [{int(elapsed)}s] status={s}")
        if s == target:
            print(f"\n{success}")
            return
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="Test confirmation flow end-to-end")
    parser.add_argument("--profile", default=None, help="AWS CLI profile")
    parser.add_argument("--region", default="us-east-2", help="AWS region")
    parser.add_argument("--domain", default=None, help="Test email domain (or set TEST_EMAIL_DOMAIN)")
    parser.add_argument("--function-name", default=None,
                        help="Interface Lambda name (or set SNS_EMAIL_AIDE_FUNCTION)")
    parser.add_argument("--timeout", type=int, default=120, help="Poll timeout in seconds")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    domain = args.domain or os.environ.get("TEST_EMAIL_DOMAIN")
    function_name = args.function_name or os.environ.get("SNS_EMAIL_AIDE_FUNCTION")

    if not domain:
        print("ERROR: provide --domain or set TEST_EMAIL_DOMAIN")
        sys.exit(1)
    if not function_name:
        print("ERROR: provide --function-name or set SNS_EMAIL_AIDE_FUNCTION")
        sys.exit(1)

    lambda_client = session.client("lambda")

    unique_value = uuid.uuid4().hex[:8]
    test_email = f"aide-test-{unique_value}@{domain}"
    string_to_look_for = f"lookfor-{unique_value}"
    topic_arn = None

    print(f"Test email: {test_email}")
    print(f"Look-for:   {string_to_look_for}")
    print()

    def status():
        return _invoke(lambda_client, function_name, {"action": "status", "email": test_email})

    topic_name = f"sns-email-aide-test-{unique_value}"

    try:
        # Step 1: Register the address, binding the expected topic name and the
        # notification body content we'll look for.
        print("[1/6] Registering test email...")
        body = _invoke(lambda_client, function_name, {
            "action": "register",
            "email": test_email,
            "expectations": {
                "sns-topic-name": topic_name,
                "body-contains-all": string_to_look_for,
            },
        })
        print(f"  OK: status={body.get('status')} registeredAt={body.get('registeredAt')}")

        # Step 2: Check initial status
        print("[2/6] Checking initial status...")
        body = status()
        print(f"  OK: status={body.get('status')}")

        # Step 3: Create SNS topic and subscribe the test email
        print("[3/6] Creating SNS topic and subscribing test email...")
        sns = session.client("sns")
        topic_arn = sns.create_topic(Name=topic_name)["TopicArn"]
        print(f"  Topic: {topic_arn}")
        sns.subscribe(TopicArn=topic_arn, Protocol="email", Endpoint=test_email)
        print(f"  Subscribed {test_email} (confirmation email will be sent)")

        # Step 4: Poll until confirmed
        print(f"[4/6] Polling for confirmation (timeout={args.timeout}s)...")
        start = time.time()
        polls = 0
        while True:
            polls += 1
            elapsed = time.time() - start
            if elapsed > args.timeout:
                print(f"  TIMEOUT after {int(elapsed)}s - subscription not confirmed")
                print("  Check the email processor CloudWatch logs for errors")
                sys.exit(1)
            s = status().get("status")
            print(f"  [{int(elapsed)}s] status={s}")
            if s and s.startswith(("confirmation/success", "notification/")):
                print("\nSUCCESS: SNS subscription auto-confirmed!")
                break
            # After a few polls with no confirmation, check the usual cause: the
            # aide's SES rule set not being active. Fail fast if so, rather than
            # waiting out the full timeout. "unknown" (e.g. no SES read
            # permission) keeps polling.
            if polls == 3 and _diagnose_rule_set(session, function_name) == "inactive":
                print("\nFAIL: the aide's SES receipt rule set is not active, so the "
                      "confirmation email will never be processed. Activate it "
                      "(above) and re-run.")
                sys.exit(1)
            time.sleep(10)

        print("  Waiting 10s for subscription propagation...")
        time.sleep(10)

        # Step 5: Publish a notification WITHOUT the look-for string to exercise
        # the notification/failure path, then poll for it.
        print("[5/6] Publishing a notification that should FAIL its expectations...")
        sns.publish(
            TopicArn=topic_arn,
            Subject="SNS Email Aide Test Message",
            Message="This message intentionally omits the look-for string.",
        )
        print("  Published (missing the look-for string)")
        _poll_for(status, "notification/failure", args.timeout,
                  success="EXPECTED: notification correctly marked notification/failure!",
                  fail="did not reach notification/failure")

        # Step 6: Publish a notification WITH the look-for string; poll until success.
        print("[6/6] Publishing a notification that should PASS its expectations...")
        sns.publish(
            TopicArn=topic_arn,
            Subject="SNS Email Aide Test Message",
            Message=f"This message contains the look-for string: {string_to_look_for}",
        )
        print("  Published (with the look-for string)")
        _poll_for(status, "notification/success", args.timeout,
                  success="SUCCESS: notification met its expectations!",
                  fail="did not reach notification/success")

    finally:
        # Print the audit trail to help interpret the run.
        try:
            hist = _invoke(lambda_client, function_name, {"action": "history", "email": test_email})
            print("\n--- History ---")
            for e in hist.get("events", []):
                event_type = (e.get("event") or "").split("#", 1)[0]
                print(f"  {_fmt_ts(e.get('timestamp')):<23}  {event_type:<14}  {e.get('result')}")
        except Exception as e:
            print(f"\nHistory unavailable: {e}")

        if topic_arn:
            print("\nCleaning up SNS topic...")
            try:
                session.client("sns").delete_topic(TopicArn=topic_arn)
                print(f"  Deleted {topic_arn}")
            except Exception as e:
                print(f"  Cleanup warning: {e}")
        print("DynamoDB records will expire via TTL")


if __name__ == "__main__":
    main()
