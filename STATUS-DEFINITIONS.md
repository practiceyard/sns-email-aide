# Status Definitions & Processing Logic

The authoritative description of how the aide decides what to do with an inbound
message and what each recorded event, result, and status means. The
[README](README.md) covers *how to use* the aide; this doc covers *how it
decides*.

## Model at a glance

Every inbound email produces exactly one **event** per recipient. An event has a
**type** (what stage/kind it is) and a **result** (the processing outcome). The `status`
action reports the latest meaningful event as `"<type>/<result>"`, plus a few
special states.

| Event type | Meaning | Possible results |
|------------|---------|------------------|
| `unexpected` | Mail that was not acted on; could be a genine SNS email, or not | `undefined`, `unregistered` |
| `registration` | Written by the `register` action, not by mail | `success` |
| `confirmation` | An SNS subscription-confirmation email was processed | `success`, `mismatch`, `error` |
| `notification` | Any other genuine SNS email was processed | `success`, `failure`, `mismatch`, `unregistered` |

Rows are keyed `email` (partition) + `event` (sort), where per-message events use
`"<type>#<epoch>"` so each is a distinct, immutable row. Registration,
confirmation, and notification rows inherit the registration's TTL and expire as a
set; `unexpected` rows carry the default TTL and expire independently.

## Processing flow

```
inbound email
   │
   ▼
[trust gate]  DKIM PASS  AND  DMARC PASS  AND  From-domain = sns.amazonaws.com
   │ fail ─────────────────────────────▶ unexpected / undefined   (no body read)
   │ pass
   ▼
subject is a known SNS confirmation subject?
   │
   ├─ yes ── CONFIRMATION ──────────────────────────────────────────────┐
   │           registered? ── no ──▶ unexpected / unregistered           │
   │           yes: parse TopicArn from confirmation URL (body trusted)  │
   │             topic/account expectations match?                       │
   │               ├─ match ──▶ sns:ConfirmSubscription                  │
   │               │             success ──▶ confirmation / success      │
   │               │             throw   ──▶ confirmation / error        │
   │               └─ no match ─────────────▶ confirmation / mismatch    │
   │           (no confirmation URL / bad ARN) ▶ confirmation / error    │
   │                                                                     │
   └─ no ─── NOTIFICATION ───────────────────────────────────────────────┤
               registered? ── no ──▶ notification / unregistered          │
               yes: if a topic/account expectation is set,                │
                 parse topic from the unsubscribe-footer URL              │
                   not found, or mismatch ─▶ notification / mismatch      │
               evaluate subject/body expectations                        │
                 all pass ─▶ notification / success                       │
                 any fail ─▶ notification / failure                       │
```

## Event / result definitions

- **`confirmation/success`** — genuine confirmation for a registered address whose
  topic/account met the expectations; the subscription was confirmed.
- **`confirmation/mismatch`** — genuine, registered, but the confirmation's
  topic/account did not match the expectations. Not confirmed.
- **`confirmation/error`** — genuine and registered, but processing failed for an
  infrastructure/API reason (no parseable confirmation URL, missing ARN/token, or
  `sns:ConfirmSubscription` threw).
- **`notification/success`** — genuine notification for a registered address that
  met all expectations (topic/account if set, then subject/body).
- **`notification/failure`** — genuine, registered, topic OK, but subject/body
  expectations were not met.
- **`notification/mismatch`** — genuine, registered, but the topic/account did not
  match (or a topic expectation was set and no footer ARN could be parsed).
- **`notification/unregistered`** — genuine notification for an address with no
  registration. Only reachable if the address was confirmed and then deleted while
  a notification was in flight.
- **`unexpected/undefined`** — failed the trust gate; not treated as genuine SNS
  mail. Stores a cleaned sender and truncated subject only; no body.
- **`unexpected/unregistered`** — a genuine confirmation arrived for an address
  that was never registered. Not confirmed.

`error` is reserved for infrastructure/processing problems. A topic/account that
does not match expectations is a `mismatch`, not an error. Content that does not
match is a `failure`, not an error.

## Status (the `status` action)

`status` reports one value:

| Status | Condition |
|--------|-----------|
| `not-known` | No rows at all for the address. |
| `not-registered` | Rows may exist (only `unexpected` events) but no registration. |
| `registered` | A registration row, and zero or more `unexpected` events, but no confirmation/notification event yet. |
| `"<type>/<result>"` | Otherwise, the latest `confirmation`/`notification` event by timestamp, e.g. `confirmation/success`, `notification/failure`. |

`unexpected` events never determine status — they are noise in the trail, not
state. The latest confirmation/notification event wins, so a later `success`
supersedes an earlier `failure`/`mismatch`.

## Registration rules

- The email must be a valid address whose domain exactly equals the configured
  `EmailDomain` (case-insensitive), whatever that value is — an apex domain
  (`domain.com`) or a subdomain (`sub.domain.com`) are both valid to configure.
  Only that exact domain matches; an address on any other domain, including a
  sub- or parent domain of the configured one, is rejected. SES only routes the
  configured domain to the aide, so an off-domain address could never be
  exercised.
- It is recommended to use random mailboxes as opposed to well-known or static
  mailboxes.  aj238sdfuw2@mydomain is preferred over user@mydomain.
- An address is **registerable** while it has no `confirmation` or `notification`
  events. A prior `registration` row (not yet exercised) may be overwritten;
  `unexpected` events do not block registration and are kept.
- Once a `confirmation` or `notification` event exists, `register` is rejected —
  those events were judged against a specific expectations set, so changing it
  would make the trail inconsistent. `delete` the address to reuse it.

## Expectations

All keys optional; **different keys are AND'd** (every provided key must hold).
Each value is a single string or a list. Within a list-valued key, `-any` means
OR (any element satisfies it) and `-all` means AND (every element must). Matching
is case-sensitive.

| Key | Applies to | Match |
|-----|-----------|-------|
| `sns-topic-name` | confirmation + notification | topic name equals a value |
| `sns-topic-arn` | confirmation + notification | full ARN equals a value |
| `sns-topic-like` | confirmation + notification | topic name matches a glob (`*`, `?`) |
| `account-ids` | confirmation + notification | parsed account is in the list |
| `subject-matches` | notification | subject equals a value |
| `subject-like` | notification | subject matches a glob |
| `subject-contains-any` / `-all` | notification | subject contains any / all substrings |
| `body-contains-any` / `-all` | notification | body contains any / all substrings |
| `body-like-any` / `-all` | notification | body matches any / all globs |

- Subject and body are set by AWS (not the caller), except the topic name embedded
  in the URLs, so content expectations only apply to notifications; confirmations
  are checked on topic/account only.
- With no topic/account expectation, any topic is accepted for the address.
