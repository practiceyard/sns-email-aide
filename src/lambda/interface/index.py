# SPDX-License-Identifier: MIT-0
"""Interface Lambda - direct-invoke handler.

Invoked via lambda:InvokeFunction (no HTTP/Function URL). Actions:

  register: {"action": "register", "email": "...", "expectations": {...}}
  status:   {"action": "status", "email": "..."}     -> derived overall status
  history:  {"action": "history", "email": "..."}     -> full audit trail
  delete:   {"action": "delete", "email": "..."}      -> remove all rows

`expectations` (all keys optional; keys are AND'd) constrain what the processor
will act on / accept:
  sns-topic-name | sns-topic-arn | sns-topic-like   (topic identity)
  account-ids                                        (list of allowed accounts)
  subject-matches | subject-like |
    subject-contains-any | subject-contains-all      (notification subject)
  body-contains-any | body-contains-all |
    body-like-any | body-like-all                    (notification body)
Values are a string or a list; `-any` = OR across the list, `-all` = AND.

The table is an append-only audit trail keyed (email [PK], event [SK]):
  registration            registration facts + expectations
  confirmation#<epoch>     result success | mismatch | error
  notification#<epoch>     result success | failure | mismatch | unregistered
  unexpected#<epoch>       result undefined | unregistered (recorded, not acted on)

Registration/confirmation/notification rows share the registration ttl;
unexpected rows expire independently.
"""

import os
import time
import logging
import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
TABLE_NAME = os.environ['TABLE_NAME']
RECORD_TTL_SECONDS = int(os.environ.get('RECORD_TTL_SECONDS', '7200'))
# The bare domain SES receives on. Registrations must be under it, since mail
# for any other domain never reaches the aide.
EMAIL_DOMAIN = os.environ.get('EMAIL_DOMAIN', '').strip().lower()

REGISTRATION_EVENT = 'registration'
# Events that represent activity against a registration's expectations; their
# presence blocks re-registration (the trail was judged against those rules).
JUDGED_PREFIXES = ('confirmation#', 'notification#')

VALID_EXPECTATION_KEYS = {
    'sns-topic-name', 'sns-topic-arn', 'sns-topic-like', 'account-ids',
    'subject-matches', 'subject-like', 'subject-contains-any', 'subject-contains-all',
    'body-contains-any', 'body-contains-all', 'body-like-any', 'body-like-all',
}


def handler(event, context):
    action = (event or {}).get('action')
    if action == 'register':
        return _register(event)
    if action == 'status':
        return _status(event)
    if action == 'history':
        return _history(event)
    if action == 'delete':
        return _delete(event)
    raise ValueError(f"unknown or missing action: {action!r}")


def _register(event):
    email = _require_email(event)
    _require_configured_domain(email)
    expectations = event.get('expectations', {}) or {}
    _validate_expectations(expectations)

    # Registration is allowed while the address has no confirmation/notification
    # events. A prior registration row (not yet exercised) or unexpected events
    # (rogue mail that predates or is independent of registration) do not block;
    # unexpected events are kept. Once judged events exist, they were evaluated
    # against a specific expectations set, so the caller must `delete` to reuse.
    items = _query(email)
    if any(_judged(it) for it in items):
        raise ValueError(
            f"{email} already has confirmation/notification events; "
            f"delete it before re-registering")

    now = int(time.time())
    _table().put_item(Item={
        'email': email,
        'event': REGISTRATION_EVENT,
        'result': 'success',
        'expectations': expectations,
        'registeredAt': now,
        'timestamp': now,
        'ttl': now + RECORD_TTL_SECONDS,
    })
    logger.info("Registered %s with %d expectation(s)", email, len(expectations))
    return {'email': email, 'status': 'registered', 'registeredAt': now}


def _validate_expectations(expectations):
    if not isinstance(expectations, dict):
        raise ValueError("'expectations' must be an object")
    unknown = set(expectations) - VALID_EXPECTATION_KEYS
    if unknown:
        raise ValueError(f"unknown expectation key(s): {sorted(unknown)}")


def _status(event):
    email = _require_email(event)
    return {'email': email, 'status': _derive_status(_query(email))}


def _history(event):
    email = _require_email(event)
    items = _query(email)
    events = [{
        'event': it['event'],
        'result': it.get('result'),
        'timestamp': _to_num(it.get('timestamp')),
        'sender': it.get('sender'),
        'subject': it.get('subject'),
        'body': it.get('body'),
        'detail': it.get('detail'),
    } for it in items]
    events.sort(key=lambda e: e['timestamp'] or 0)
    return {'email': email, 'status': _derive_status(items), 'events': events}


def _delete(event):
    email = _require_email(event)
    items = _query(email)
    table = _table()
    with table.batch_writer() as batch:
        for it in items:
            batch.delete_item(Key={'email': email, 'event': it['event']})
    logger.info("Deleted %d row(s) for %s", len(items), email)
    return {'email': email, 'deleted': len(items)}


def _derive_status(items):
    """Overall status:
      not-known       no rows at all
      not-registered  only unexpected events, no registration
      registered      registration (+ zero or more unexpected), no judged events
      <event>/<result>  the latest confirmation/notification event otherwise
    """
    if not items:
        return 'not-known'
    has_registration = any(it['event'] == REGISTRATION_EVENT for it in items)
    judged = sorted((it for it in items if _judged(it)),
                    key=lambda it: _to_num(it.get('timestamp')) or 0)
    if not has_registration:
        return 'not-registered'
    if not judged:
        return 'registered'
    latest = judged[-1]
    kind = latest['event'].split('#', 1)[0]
    return f"{kind}/{latest.get('result')}"


def _judged(item):
    return item['event'].startswith(JUDGED_PREFIXES)


def _to_num(value):
    # DynamoDB returns numbers as Decimal; normalize for JSON output.
    if value is None:
        return None
    return float(value)


def _require_email(event):
    # Canonicalize to lowercase so the DynamoDB PK is stable: the processor
    # looks up inbound mail by the same lowercased address, and no real mail
    # provider treats local-parts case-sensitively.
    email = (event.get('email') or '').strip().lower()
    if not email:
        raise ValueError("missing or empty 'email'")
    return email


def _require_configured_domain(email):
    """Reject a registration whose domain isn't the one SES receives on; mail
    for any other domain would never reach the aide (SES routes only the
    configured domain), so such a registration could never be exercised."""
    if '@' not in email or not email.split('@')[0]:
        raise ValueError(f"'{email}' is not a valid email address")
    if not EMAIL_DOMAIN:
        return  # domain not configured for this Lambda; skip the check
    domain = email.rsplit('@', 1)[1].lower()
    if domain != EMAIL_DOMAIN:
        raise ValueError(
            f"email domain '{domain}' must be the configured domain '{EMAIL_DOMAIN}'")


def _query(email):
    """Return all rows for an email, following DynamoDB pagination so a trail
    larger than one 1 MB page is never silently truncated."""
    items = []
    kwargs = {'KeyConditionExpression': Key('email').eq(email)}
    while True:
        resp = _table().query(**kwargs)
        items.extend(resp.get('Items', []))
        start = resp.get('LastEvaluatedKey')
        if not start:
            return items
        kwargs['ExclusiveStartKey'] = start


def _table():
    return dynamodb.Table(TABLE_NAME)
