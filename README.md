# SNS Email Aide

Automated aide for testing SNS email flows without a human in the loop.

When you write integration tests that subscribe an email address to an SNS topic,
AWS sends a subscription-confirmation email that normally a person has to click.
This app receives that email via SES, extracts the confirmation link, and confirms
the subscription automatically so tests can run unattended. It can also validate
that a later notification email contains expected content.

It is deployed as its own CloudFormation stack and invoked directly via
`lambda:InvokeFunction`. It has no dependency on the application under test: the
consumer supplies an email domain and invokes the Interface Lambda by name.
Deploy it once and leave it running.

## How it works

```
caller ──invoke {action:register, email, expectations}──▶ Interface Lambda ──▶ DynamoDB
caller ──subscribe email to SNS topic
SNS    ──confirmation email──▶ SES inbound ──▶ S3 ──▶ Processor Lambda
                                                        │  (trust gate: DKIM+DMARC+From)
                                                        ├─ sns:ConfirmSubscription
                                                        └─ record event in DynamoDB
caller ──invoke {action:status, email}──▶ Interface Lambda ──▶ {status}
```

1. The caller registers an email address, optionally with `expectations` (topic/
   account to allow, and subject/body content to look for), by invoking the Interface Lambda.
2. The caller subscribes that address to its SNS topic. AWS emails a confirmation link.
3. SES inbound receipt drops the raw email in S3 and invokes the Processor Lambda.
4. Every message passes the trust gate (DKIM + DMARC + From-domain); genuine SNS
   confirmations for a registered address whose topic/account matches `expectations`
   are confirmed via `sns:ConfirmSubscription` (`confirmation/success`), else
   `confirmation/mismatch` or `confirmation/error`.
5. Genuine notifications are checked against the registration's `expectations`
   (topic/account, then subject/body) → `notification/success` or `notification/failure`.
6. The caller invokes `status` until it sees a terminal state, or `history` to
   inspect the full trail.
7. Mail that fails the trust gate, or a confirmation for an unregistered address,
   is recorded as `unexpected` (logged, not acted on).
8. Re-registration is allowed while an address has no confirmation/notification
   events; otherwise it must be `delete`d first. (`unexpected` events don't block.)

## Data model

The DynamoDB table is an append-only audit trail keyed `email` (partition) +
`event` (sort):

| `event` | Written by | `result` values |
|---------|-----------|-----------------|
| `registration` | register action | `success` (stores `expectations`, `registeredAt`, `ttl`) |
| `confirmation#<epoch>` | processor | `success` \| `mismatch` \| `error` |
| `notification#<epoch>` | processor | `success` \| `failure` \| `mismatch` \| `unregistered` |
| `unexpected#<epoch>` | processor | `undefined` \| `unregistered` (stores cleaned `sender` + truncated `subject`, no body) |

Separate rows (rather than in-place updates on one item) avoid write races when
events arrive close together, and preserve a full history. Registration,
confirmation, and notification rows share the registration's `ttl` so that trail
expires together (~2h). `unexpected` rows have no registration to anchor to, so
each expires independently after the default ttl.

Result meanings:
- `success` — confirmation confirmed / notification met its expectations.
- `mismatch` — genuine SNS mail, but the topic/account didn't match `expectations`.
- `failure` — notification content didn't match `expectations`.
- `error` — infrastructure/API failure (e.g. `sns:ConfirmSubscription` threw).
- `unexpected` event / `undefined` — failed the trust gate (not genuine SNS mail).
- `unexpected` event / `unregistered` — genuine confirmation for an unregistered address.
- `notification` / `unregistered` — genuine notification for an address deleted after it was confirmed.

## Invoke contract

Each action is sent as the invoke payload to the Interface Lambda. Responses
are plain JSON; failures surface as a Lambda function error (raised exception).

**register**
```json
{
  "action": "register",
  "email": "integ-abc@testing.example.com",
  "expectations": {
    "sns-topic-name": "my-topic",
    "account-ids": ["111122223333"],
    "body-contains-all": "your-value-to-look-for"
  }
}
```
- `expectations` is optional; every key is optional and all provided keys must
  hold (AND). Values are a string or a list.
  - Topic (confirmation + notification): `sns-topic-name`, `sns-topic-arn`,
    `sns-topic-like` (glob), `account-ids` (list).
  - Subject (notification only): `subject-matches` (exact), `subject-like` (glob),
    `subject-contains-any`, `subject-contains-all`.
  - Body (notification only): `body-contains-any`, `body-contains-all`,
    `body-like-any`, `body-like-all`.
  - `-any` = OR across the list, `-all` = AND; `like` = glob (`*`, `?`); `contains`
    = literal substring; `matches` = exact. All case-sensitive.
- With no topic/account expectation, any topic is accepted for the address.
- The email must be a valid address under the configured `EmailDomain`;
  registering an address on any other domain is rejected (mail for it would never
  reach the aide).
- Response: `{"email": ..., "status": "registered", "registeredAt": <epoch>}`
- Re-registering is allowed while the address has no `confirmation`/`notification`
  events (a prior registration or `unexpected` events don't block, and `unexpected`
  events are kept). Once a confirmation/notification event exists, register is
  rejected — `delete` the address first to reuse it.

**status** — latest state
```json
{"action": "status", "email": "integ-abc@testing.example.com"}
```
- Response: `{"email": ..., "status": "<state>"}` where `<state>` is one of the
  special states `not-known` (no records), `not-registered` (records but no
  registration), `registered` (registered, no confirmation/notification yet), or
  the latest confirmation/notification event as `"<event>/<result>"` (e.g.
  `confirmation/success`, `notification/failure`, `confirmation/mismatch`).
- The latest confirmation/notification event by timestamp wins; `unexpected`
  events never change the status.

**history** — full audit trail (for troubleshooting)
```json
{"action": "history", "email": "integ-abc@testing.example.com"}
```
- Response: `{"email": ..., "status": "<state>", "events": [ ... ]}` where each event
  is `{"event", "result", "timestamp", "sender", "subject", "body", "detail"}`.
  For registered-flow events, `subject`/`body` hold the received content (body
  truncated) to help diagnose behavior; for `unexpected` events, `sender` and a
  truncated `subject` are recorded with no body.

**delete** — remove all records for an address
```json
{"action": "delete", "email": "integ-abc@testing.example.com"}
```
- Response: `{"email": ..., "deleted": <count>}`. Use it to reuse an address before
  its ttl expires. Not normally needed (records auto-expire).

Example with the CLI:
```
aws lambda invoke --function-name sns-email-aide-interface \
  --payload '{"action":"status","email":"integ-abc@testing.example.com"}' \
  --cli-binary-format raw-in-base64-out out.json --region us-east-2
```

For Python, see the importable boto3 client in [`samples/aide_client.py`](samples/aide_client.py).

## Email routing

Every inbound message first passes a **trust gate**, evaluated from the SES event's
`receipt` verdicts and headers before the body is read: **DKIM PASS**, **DMARC PASS**,
and **From domain `sns.amazonaws.com`**. DKIM/DMARC together make the `From` domain
trustworthy (it can't be spoofed once cryptographically bound), so this proves the
mail genuinely came from AWS SNS. Anything that fails the gate is recorded as
`unexpected`/`undefined` and dropped — no body read, no action.

Genuine mail is then routed by subject:

- **Confirmation** (subject is the SNS confirmation subject): if the address isn't
  registered → `unexpected`/`unregistered`. Otherwise the topic/account is parsed
  from the confirmation URL and checked against `expectations`; match →
  `sns:ConfirmSubscription` + `confirmation/success`; no match → `confirmation/mismatch`.
- **Notification** (any other subject): if a topic/account expectation is set, the
  topic ARN is parsed from the unsubscribe-link footer and checked (mismatch or
  unparseable → `notification/mismatch`); then subject/body `expectations` are
  evaluated → `notification/success` or `notification/failure`.

The registration gate is the primary control: the aide never confirms a subscription
or evaluates content for an address nobody registered.

For the full processing flow, event/result definitions, status derivation, and
expectation semantics, see [STATUS-DEFINITIONS.md](STATUS-DEFINITIONS.md).

## Components

| Resource | Purpose |
|----------|---------|
| Interface Lambda (`<stack>-interface`) | Register emails; report status/history. Invoke target. |
| Processor Lambda (`<stack>-processor`) | Parse inbound mail, confirm subscriptions, validate content |
| DynamoDB table (auto-named) | Append-only audit trail, `email` + `event` (TTL 2h) |
| S3 mail bucket | Transient store for SES inbound mail (1-day expiry) |
| SES receipt rule set (`<stack>-mail-rules`) | Route inbound mail to S3 + Lambda |

## Prerequisites

- AWS Account, with:
- An email domain whose MX records route to SES in your target region, with SES
  inbound receiving enabled and the domain verified. See the
  [SES email receiving setup](https://docs.aws.amazon.com/ses/latest/dg/receiving-email-setting-up.html).

## Deploy

```
aws cloudformation deploy \
  --stack-name sns-email-aide \
  --template-file sns-email-aide.yaml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides EmailDomain=testing.example.com \
  --region us-east-2

aws ses set-active-receipt-rule-set \
  --rule-set-name sns-email-aide-mail-rules \
  --region us-east-2
```

Run both steps. The second is required because the active receipt rule set is an
account/region-global setting that CloudFormation does not manage. See
[SES rule set activation](#ses-rule-set-activation).

### Parameters

| Parameter | Default | Notes |
|-----------|---------|-------|
| `EmailDomain` | (none, required) | The bare domain (not an address) SES receives on; its MX records must route to SES in this region. Registrations must be an address under this domain. |
| `RecordTtlSeconds` | `7200` | Seconds until an email's audit trail expires via DynamoDB TTL. |

## Consuming it

A consumer (for example a CI pipeline) invokes the Interface Lambda directly.
It needs `lambda:InvokeFunction` on the function and knows it by its stack-derived
name, `<stack>-interface`. The `InterfaceLambdaName` stack output exposes it.
With AWS CodeBuild, the build's service role already carries AWS credentials, so
just pass the name as a plain environment variable:
```yaml
EnvironmentVariables:
  - Name: SNS_EMAIL_AIDE_FUNCTION
    Value: sns-email-aide-interface
```

### From a GitHub Actions runner

A GitHub runner has no AWS credentials by default; use GitHub OIDC to assume an
IAM role at run time rather than storing long-lived keys. See
[`samples/github/`](samples/github/) for the one-time AWS setup and a complete
workflow.

## SES rule set activation

SES allows exactly one active receipt rule set per region per account, and
activation is not managed by CloudFormation. After deploy you must run
`set-active-receipt-rule-set`. Deleting this stack fails while its rule set is
active.

## Teardown

Two things can block a clean `delete-stack`, both intentional:

1. **Active SES rule set.** First activate a different rule set (or clear the
   active one with `aws ses set-active-receipt-rule-set` with no `--rule-set-name`).
2. **Non-empty mail bucket.** The bucket has no `DeletionPolicy`, so a delete
   fails if inbound mail is still present rather than silently orphaning it. The
   1-day lifecycle rule self-empties the bucket, so a teardown a day after the last
   test just works; otherwise empty it first (`aws s3 rm s3://<bucket> --recursive`).

The DynamoDB table deletes freely.

## Tests

Unit tests (no AWS account needed):
```
python -m pytest tests/unit
```

### Testing the setup

The integration test exercises a live deployment end to end: it registers a test
email, subscribes it to a throwaway SNS topic, polls until the subscription is
auto-confirmed, then publishes one notification that should fail validation and one
that should pass, polling for each. It prints the audit trail at the end and cleans
up the SNS topic.

```
python tests/integration/run_confirmation_test.py --domain testing.example.com --region us-east-2
```

Prerequisites:
- The `sns-email-aide` stack is deployed and its SES receipt rule set is active
  (see [Deploy](#deploy) and [SES rule set activation](#ses-rule-set-activation)).
- `EmailDomain` set to a domain that routes inbound mail to SES in this region.
- AWS credentials whose identity can:
  - `lambda:InvokeFunction` on the aide's Interface Lambda,
  - `sns:CreateTopic`, `sns:Subscribe`, `sns:Publish`, `sns:DeleteTopic`,
  - `ses:DescribeActiveReceiptRuleSet` and `ses:ListReceiptRuleSets` — used to
    diagnose rule set activation if confirmation stalls (best-effort; the test
    still runs without them, just with a less specific hint).

If confirmation does not arrive, the most common cause is that the receipt rule set
was created but never made active; the test detects this and prints the exact
activation command.

## Notes and caveats

- This is test-support infrastructure, not production. It auto-confirms any SNS
  subscription for a registered email address.
- The template embeds the Lambda code inline. The standalone copies under `src/lambda/`
  are the source of truth for editing and testing; keep them in sync with the
  inline versions in the template using `tests/unit/test_template_sync.py` to check if they
  are out of sync, and `scripts/sync_lambda_code.py --sync` to copy the source files
  into the CloudFormation template.

## License

MIT-0 (MIT No Attribution). See `LICENSE`. Source files carry
`SPDX-License-Identifier: MIT-0`.
