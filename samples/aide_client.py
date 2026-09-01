# SPDX-License-Identifier: MIT-0
"""Sample client for invoking the SNS Email Aide Interface Lambda.

Reference code you can copy or import. It wraps ``lambda:InvokeFunction`` with
the aide's action envelope and turns a Lambda function error (the handler
raised) into a Python exception.

    from aide_client import AideClient

    aide = AideClient()  # uses the default boto3 session/region
    aide.register("integ-abc@testing.example.com",
                  expectations={"body-contains-all": "welcome"})
    print(aide.status("integ-abc@testing.example.com"))     # -> 'registered' | 'confirmation/success' | ...
    print(aide.history("integ-abc@testing.example.com"))    # -> full event list

The caller's credentials need ``lambda:InvokeFunction`` on the aide's Interface
Lambda (default name ``sns-email-aide-interface``).
"""

import json

import boto3

DEFAULT_FUNCTION_NAME = "sns-email-aide-interface"


class AideError(RuntimeError):
    """Raised when the aide's handler returns a Lambda function error."""


class AideClient:
    def __init__(self, function_name=DEFAULT_FUNCTION_NAME, region_name=None,
                 lambda_client=None):
        self.function_name = function_name
        self._lambda = lambda_client or boto3.client("lambda", region_name=region_name)

    def _invoke(self, payload):
        resp = self._lambda.invoke(
            FunctionName=self.function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode(),
        )
        result = json.loads(resp["Payload"].read() or "null")
        if resp.get("FunctionError"):
            raise AideError(result)
        return result

    def register(self, email, expectations=None):
        """Register an address with optional expectations (topic/account/subject/body).

        Allowed only while the address has no confirmation/notification events;
        delete it first to reuse. Returns the full response dict.
        """
        return self._invoke({
            "action": "register",
            "email": email,
            "expectations": expectations or {},
        })

    def status(self, email):
        """Return the derived status string (e.g. 'registered', 'confirmation/success',
        'notification/failure')."""
        return self._invoke({"action": "status", "email": email})["status"]

    def history(self, email):
        """Return the full audit-trail event list for the address."""
        return self._invoke({"action": "history", "email": email})["events"]

    def delete(self, email):
        """Remove all records for the address. Returns the count deleted."""
        return self._invoke({"action": "delete", "email": email})["deleted"]
