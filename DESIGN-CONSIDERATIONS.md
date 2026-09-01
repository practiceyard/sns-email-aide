# Background on Design Decisions

### The trust gate

DKIM PASS proves the message was cryptographically signed by its claimed domain
and not altered; DMARC PASS proves the visible `From` domain is aligned with that
signer. Together they make `From: ...@sns.amazonaws.com` trustworthy — it cannot
be spoofed. SPF is intentionally **not** part of the gate: it adds nothing when
DKIM passes and can produce false failures on forwarded mail.

Because the gate runs on the SES `receipt` verdicts and headers, it happens
**before** the S3 body is read. Mail that fails the gate never has its body
fetched — it's logged as `unexpected/undefined` and dropped.

### Why the confirmation body is trusted

For a gate-passing confirmation, the body (which contains the `TopicArn` and
`Token`) is set by AWS SNS, and DKIM's body hash covers it. So parsing the topic
and account from the confirmation URL is trustworthy once the gate passes.

### Why notification topic comes from the footer

A notification's `X-Amz-Sns-Subscription-Arn` header is **not** covered by SNS's
DKIM signature, so it can't be trusted even on gate-passing mail. The unsubscribe
link in the body **is** under the DKIM body hash, so the topic ARN is parsed from
`https://sns.<region>.amazonaws.com/unsubscribe.html?SubscriptionArn=...`. In
practice the topic was already validated at confirmation time, so this is
defense-in-depth; a topic expectation that can't be verified from the footer is
treated as a `mismatch` (fail-closed).
