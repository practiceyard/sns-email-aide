# SPDX-License-Identifier: MIT-0
"""Email Processor Lambda - SES inbound email handler.

Triggered by an SES receipt rule (S3 action stores the raw mail, then this
Lambda runs). Every message is first put through a trust gate; only genuine
SNS mail for a registered address is acted on.

Trust gate (from the SES event's receipt object + headers, no body read):
  DKIM PASS and DMARC PASS and the From domain is sns.amazonaws.com.
  Fail -> record an `unexpected`/`undefined` event and stop.

Confirmation message (subject is a known SNS confirmation subject):
  - unregistered address       -> unexpected/unregistered
  - parse topic/account from the confirmation URL (body is trusted post-gate)
  - topic/account expectations match -> sns:ConfirmSubscription, confirmation/success
  - do not match               -> confirmation/mismatch
  - infrastructure failure      -> confirmation/error

Notification message (any other subject):
  - unregistered address       -> notification/unregistered
  - parse topic/account from the unsubscribe footer URL; a required
    topic/account expectation that mismatches or can't be parsed
    -> notification/mismatch
  - remaining subject/body expectations pass -> notification/success
  - otherwise                   -> notification/failure

Registration/confirmation/notification rows share the registration's ttl;
unexpected rows use the default ttl and expire independently.
"""

import email as emaillib
import fnmatch
import logging
import os
import re
import time
from decimal import Decimal
from urllib.parse import urlparse, parse_qs

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ['TABLE_NAME']
MAIL_BUCKET = os.environ.get('MAIL_BUCKET', '')
RECORD_TTL_SECONDS = int(os.environ.get('RECORD_TTL_SECONDS', '7200'))

ddb = boto3.resource('dynamodb')
table = ddb.Table(TABLE_NAME)
s3 = boto3.client('s3')
sns_client = boto3.client('sns')

SNS_FROM_DOMAIN = 'sns.amazonaws.com'
# Subjects that mark a message as an SNS subscription confirmation. Kept as a
# list so localized variants can be added.
CONFIRMATION_SUBJECTS = ['AWS Notification - Subscription Confirmation']

CONFIRMATION_URL_PATTERN = re.compile(
    r'https://sns\.[a-z0-9-]+\.amazonaws\.com/confirmation\.html\?[^\s"<]+')
UNSUBSCRIBE_URL_PATTERN = re.compile(
    r'https://sns\.[a-z0-9-]+\.amazonaws\.com/unsubscribe\.html\?[^\s"<]+')

MAX_BODY = 8000
MAX_UNEXPECTED_SUBJECT = 20


def handler(event, context):
    for record in event.get('Records', []):
        ses = record['ses']
        mail = ses['mail']
        receipt = ses.get('receipt', {})
        message_id = mail['messageId']
        headers = mail['commonHeaders']
        recipients = headers.get('to', [])
        subject = headers.get('subject', '')
        sender = _extract_email(_first(headers.get('from', [])))

        genuine = _is_genuine_sns(receipt, sender)
        logger.info('Event: id=%s from=%s subject=%s genuine=%s to=%s',
                    message_id, sender, subject, genuine, recipients)

        is_confirmation = subject in CONFIRMATION_SUBJECTS
        body = None  # read lazily, only when needed for a trusted, registered msg
        for recipient in recipients:
            addr = _extract_email(recipient)
            if not genuine:
                _unexpected(addr, sender, subject, 'undefined')
                continue
            reg = _registration(addr)
            if not reg:
                # Genuine SNS mail, but this address was never (or no longer) registered.
                if is_confirmation:
                    _unexpected(addr, sender, subject, 'unregistered')
                else:
                    _record(addr, 'notification', 'unregistered', None, subject, '')
                continue
            if body is None:
                body = _get_body(message_id)
            if is_confirmation:
                _confirm(addr, reg, subject, body)
            else:
                _notify(addr, reg, subject, body)

    return {'statusCode': 200}


def _is_genuine_sns(receipt, sender):
    dkim = receipt.get('dkimVerdict', {}).get('status')
    dmarc = receipt.get('dmarcVerdict', {}).get('status')
    from_domain = sender.split('@')[-1].lower() if '@' in sender else ''
    return dkim == 'PASS' and dmarc == 'PASS' and from_domain == SNS_FROM_DOMAIN


# --- Confirmation path ---

def _confirm(addr, reg, subject, body):
    ttl = reg.get('ttl')
    exp = reg.get('expectations', {}) or {}
    match = CONFIRMATION_URL_PATTERN.search(body)
    if not match:
        _record(addr, 'confirmation', 'error', ttl, subject, body,
                detail='no confirmation URL in body')
        return
    params = parse_qs(urlparse(match.group(0)).query)
    topic_arn = params.get('TopicArn', [None])[0]
    token = params.get('Token', [None])[0]
    if not topic_arn or not token:
        _record(addr, 'confirmation', 'error', ttl, subject, body,
                detail='missing TopicArn or Token in confirmation URL')
        return
    ok, why = _topic_ok(exp, topic_arn)
    if not ok:
        _record(addr, 'confirmation', 'mismatch', ttl, subject, body,
                detail=why, topicArn=topic_arn)
        return
    try:
        sns_client.confirm_subscription(TopicArn=topic_arn, Token=token)
    except Exception as e:
        _record(addr, 'confirmation', 'error', ttl, subject, body,
                detail=f'ConfirmSubscription failed: {e}', topicArn=topic_arn)
        return
    _record(addr, 'confirmation', 'success', ttl, subject, body, topicArn=topic_arn)
    logger.info('Confirmed subscription for %s', addr)


# --- Notification path ---

def _notify(addr, reg, subject, body):
    ttl = reg.get('ttl')
    exp = reg.get('expectations', {}) or {}

    if _has_topic_expectation(exp):
        topic_arn = _topic_arn_from_footer(body)
        if topic_arn is None:
            _record(addr, 'notification', 'mismatch', ttl, subject, body,
                    detail='topic expectation set but no unsubscribe link found')
            return
        ok, why = _topic_ok(exp, topic_arn)
        if not ok:
            _record(addr, 'notification', 'mismatch', ttl, subject, body,
                    detail=why, topicArn=topic_arn)
            return

    ok, why = _content_ok(exp, subject, body)
    if ok:
        _record(addr, 'notification', 'success', ttl, subject, body)
    else:
        _record(addr, 'notification', 'failure', ttl, subject, body, detail=why)


def _topic_arn_from_footer(body):
    m = UNSUBSCRIBE_URL_PATTERN.search(body)
    if not m:
        return None
    arn = parse_qs(urlparse(m.group(0)).query).get('SubscriptionArn', [None])[0]
    if not arn:
        return None
    # SubscriptionArn = <topic-arn>:<sub-id>; drop the trailing subscription id.
    return arn.rsplit(':', 1)[0]


# --- Expectation matching (keys AND'd; -any=OR, -all=AND; like=glob) ---

def _has_topic_expectation(exp):
    return any(k in exp for k in ('sns-topic-name', 'sns-topic-arn',
                                  'sns-topic-like', 'account-ids'))


def _topic_ok(exp, topic_arn):
    """Check topic/account expectations against a parsed topic ARN.

    ARN form: arn:aws:sns:<region>:<account>:<topic-name>
    """
    parts = topic_arn.split(':')
    account = parts[4] if len(parts) > 5 else ''
    topic_name = parts[5] if len(parts) > 5 else ''

    if 'account-ids' in exp:
        if account not in _as_list(exp['account-ids']):
            return False, f'account {account} not in expected account-ids'
    if 'sns-topic-arn' in exp:
        if topic_arn not in _as_list(exp['sns-topic-arn']):
            return False, 'topic ARN does not match sns-topic-arn'
    if 'sns-topic-name' in exp:
        if topic_name not in _as_list(exp['sns-topic-name']):
            return False, 'topic name does not match sns-topic-name'
    if 'sns-topic-like' in exp:
        if not any(fnmatch.fnmatchcase(topic_name, p) for p in _as_list(exp['sns-topic-like'])):
            return False, 'topic name does not match sns-topic-like'
    return True, None


def _content_ok(exp, subject, body):
    checks = [
        ('subject-matches', lambda vs: subject in vs),
        ('subject-like', lambda vs: any(fnmatch.fnmatchcase(subject, v) for v in vs)),
        ('subject-contains-any', lambda vs: any(v in subject for v in vs)),
        ('subject-contains-all', lambda vs: all(v in subject for v in vs)),
        ('body-contains-any', lambda vs: any(v in body for v in vs)),
        ('body-contains-all', lambda vs: all(v in body for v in vs)),
        ('body-like-any', lambda vs: any(fnmatch.fnmatchcase(body, v) for v in vs)),
        ('body-like-all', lambda vs: all(fnmatch.fnmatchcase(body, v) for v in vs)),
    ]
    for key, test in checks:
        if key in exp and not test(_as_list(exp[key])):
            return False, f'{key} not satisfied'
    return True, None


def _as_list(value):
    return value if isinstance(value, list) else [value]


# --- DynamoDB writes ---

def _record(addr, kind, result, ttl, subject, body, detail=None, topicArn=None):
    """Append an immutable audit record: event = "<kind>#<epoch.micros>"."""
    now = time.time()
    item = {
        'email': addr,
        'event': f'{kind}#{now:.6f}',
        'result': result,
        'timestamp': Decimal(f'{now:.6f}'),
        'subject': subject,
        'body': (body or '')[:MAX_BODY],
    }
    if ttl is not None:
        item['ttl'] = int(ttl)
    if detail is not None:
        item['detail'] = detail
    if topicArn is not None:
        item['topicArn'] = topicArn
    table.put_item(Item=item)


def _unexpected(addr, sender, subject, result):
    """Record mail that should not have been acted on: alert to activity, not
    debug it. No body is read or stored. `result` is 'undefined' (failed the
    trust gate) or 'unregistered' (genuine confirmation for an unknown address).
    Uses the default ttl and expires independently."""
    now = time.time()
    table.put_item(Item={
        'email': addr,
        'event': f'unexpected#{now:.6f}',
        'result': result,
        'timestamp': Decimal(f'{now:.6f}'),
        'sender': sender,
        'subject': _clip(subject),
        'ttl': int(now) + RECORD_TTL_SECONDS,
    })


def _clip(subject):
    subject = subject or ''
    return subject[:MAX_UNEXPECTED_SUBJECT] + '...' if len(subject) > MAX_UNEXPECTED_SUBJECT else subject


def _get_body(message_id):
    if not MAIL_BUCKET:
        return ''
    try:
        raw = s3.get_object(Bucket=MAIL_BUCKET, Key=message_id)['Body'].read().decode('utf-8', errors='replace')
        msg = emaillib.message_from_string(raw)
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() in ('text/plain', 'text/html'):
                    payload = part.get_payload(decode=True)
                    if payload:
                        return payload.decode('utf-8', errors='replace')
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                return payload.decode('utf-8', errors='replace')
        # No decodable text payload. Return empty rather than the raw MIME, so
        # header content (e.g. Subject) never leaks into body checks.
        return ''
    except Exception as e:
        logger.error('S3 fetch %s failed: %s', message_id, e)
        return ''


def _registration(addr):
    # Exact PK+SK lookup (never more than one item / one page), but follow
    # pagination for consistency and defensiveness.
    kwargs = {'KeyConditionExpression': Key('email').eq(addr) & Key('event').eq('registration')}
    while True:
        resp = table.query(**kwargs)
        items = resp.get('Items', [])
        if items:
            return items[0]
        start = resp.get('LastEvaluatedKey')
        if not start:
            return None
        kwargs['ExclusiveStartKey'] = start


def _first(seq):
    return seq[0] if seq else ''


def _extract_email(value):
    # Lowercase to match the canonical PK the interface Lambda registers under;
    # otherwise a mixed-case address would silently miss its registration.
    match = re.search(r'[\w.+-]+@[\w.-]+', value or '')
    return (match.group(0) if match else (value or '')).lower()
