"""Unit tests for the Email Processor Lambda (SES inbound handler).

Trust gate: DKIM PASS + DMARC PASS + From-domain sns.amazonaws.com. Fail ->
unexpected/undefined. Genuine mail for a registered address is then routed by
subject to the confirmation or notification path; expectations decide the
result (success | mismatch | failure), infra errors -> confirmation/error.
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

_LAMBDA_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'lambda', 'processor')
sys.path.insert(0, _LAMBDA_DIR)

SNS_SENDER = 'no-reply@sns.amazonaws.com'
SNS_SUBJECT = 'AWS Notification - Subscription Confirmation'
ACCOUNT = '123456789012'
TOPIC_ARN = f'arn:aws:sns:us-east-1:{ACCOUNT}:MyTopic'
CONFIRM_URL = ('https://sns.us-east-1.amazonaws.com/confirmation.html'
               f'?TopicArn={TOPIC_ARN}&Token=abc123')
UNSUB_URL = ('https://sns.us-east-1.amazonaws.com/unsubscribe.html'
             f'?SubscriptionArn={TOPIC_ARN}:8294-sub-id&Endpoint=test@example.com')
REG_TTL = 1700007200


def _make_ses_event(recipients, subject, sender=SNS_SENDER, dkim='PASS', dmarc='PASS'):
    return {'Records': [{'ses': {
        'mail': {
            'messageId': 'm1',
            'commonHeaders': {'to': recipients, 'subject': subject, 'from': [sender]},
        },
        'receipt': {
            'dkimVerdict': {'status': dkim},
            'dmarcVerdict': {'status': dmarc},
        },
    }}]}


def _raw_email(body_text):
    return ('From: sender@example.com\r\nTo: test@example.com\r\n'
            'Subject: s\r\nContent-Type: text/plain; charset="UTF-8"\r\n\r\n' + body_text)


def _reg(email='test@example.com', expectations=None):
    return {'email': email, 'event': 'registration',
            'expectations': expectations or {}, 'ttl': REG_TTL}


@patch.dict(os.environ, {'TABLE_NAME': 'test-table', 'MAIL_BUCKET': 'test-bucket'})
@patch('boto3.client')
@patch('boto3.resource')
class TestProcessor(unittest.TestCase):

    def _import_handler(self):
        if _LAMBDA_DIR in sys.path:
            sys.path.remove(_LAMBDA_DIR)
        sys.path.insert(0, _LAMBDA_DIR)
        sys.modules.pop('index', None)
        import index
        return index

    def _wire(self, mock_resource, mock_client, raw_email, registration):
        ddb_mock = MagicMock()
        mock_resource.return_value = ddb_mock
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock
        table_mock.query.return_value = {'Items': [registration] if registration else []}

        s3_mock = MagicMock()
        sns_mock = MagicMock()
        mock_client.side_effect = lambda name, *a, **k: (s3_mock if name == 's3' else sns_mock)
        body_stream = MagicMock()
        body_stream.read.return_value = raw_email.encode('utf-8')
        s3_mock.get_object.return_value = {'Body': body_stream}
        return self._import_handler(), table_mock, sns_mock, s3_mock

    def _items(self, table):
        return [c[1]['Item'] for c in table.put_item.call_args_list]

    def _one(self, table):
        items = self._items(table)
        self.assertEqual(len(items), 1)
        return items[0]

    # --- Trust gate ---

    def test_gate_dkim_fail_unexpected_undefined(self, mock_resource, mock_client):
        index, table, sns, s3 = self._wire(mock_resource, mock_client,
                                            _raw_email(CONFIRM_URL), _reg())
        index.handler(_make_ses_event(['test@example.com'], SNS_SUBJECT, dkim='FAIL'), None)
        rec = self._one(table)
        self.assertTrue(rec['event'].startswith('unexpected#'))
        self.assertEqual(rec['result'], 'undefined')
        s3.get_object.assert_not_called()      # no body read
        sns.confirm_subscription.assert_not_called()

    def test_gate_dmarc_fail_unexpected_undefined(self, mock_resource, mock_client):
        index, table, sns, s3 = self._wire(mock_resource, mock_client,
                                            _raw_email(CONFIRM_URL), _reg())
        index.handler(_make_ses_event(['test@example.com'], SNS_SUBJECT, dmarc='FAIL'), None)
        self.assertEqual(self._one(table)['result'], 'undefined')

    def test_gate_wrong_from_domain_unexpected(self, mock_resource, mock_client):
        index, table, sns, s3 = self._wire(mock_resource, mock_client,
                                            _raw_email(CONFIRM_URL), _reg())
        index.handler(_make_ses_event(['test@example.com'], SNS_SUBJECT,
                                       sender='no-reply@evil.com'), None)
        self.assertEqual(self._one(table)['result'], 'undefined')

    # --- Confirmation path ---

    def test_confirmation_success(self, mock_resource, mock_client):
        index, table, sns, s3 = self._wire(mock_resource, mock_client,
                                            _raw_email(CONFIRM_URL), _reg())
        index.handler(_make_ses_event(['test@example.com'], SNS_SUBJECT), None)
        sns.confirm_subscription.assert_called_once_with(TopicArn=TOPIC_ARN, Token='abc123')
        rec = self._one(table)
        self.assertTrue(rec['event'].startswith('confirmation#'))
        self.assertEqual(rec['result'], 'success')

    def test_recipient_lowercased_matches_registration(self, mock_resource, mock_client):
        # Mixed-case inbound recipient is canonicalized to the lowercase PK the
        # interface registered under, so the registration lookup hits.
        index, table, sns, s3 = self._wire(mock_resource, mock_client,
                                            _raw_email(CONFIRM_URL), _reg())
        index.handler(_make_ses_event(['Test@Example.COM'], SNS_SUBJECT), None)
        # the row is written under the lowercased PK, and confirmation proceeded
        self.assertEqual(self._one(table)['email'], 'test@example.com')
        sns.confirm_subscription.assert_called_once()

    def test_confirmation_topic_match_success(self, mock_resource, mock_client):
        index, table, sns, s3 = self._wire(
            mock_resource, mock_client, _raw_email(CONFIRM_URL),
            _reg(expectations={'sns-topic-name': 'MyTopic', 'account-ids': [ACCOUNT]}))
        index.handler(_make_ses_event(['test@example.com'], SNS_SUBJECT), None)
        self.assertEqual(self._one(table)['result'], 'success')
        sns.confirm_subscription.assert_called_once()

    def test_confirmation_topic_mismatch(self, mock_resource, mock_client):
        index, table, sns, s3 = self._wire(
            mock_resource, mock_client, _raw_email(CONFIRM_URL),
            _reg(expectations={'sns-topic-name': 'OtherTopic'}))
        index.handler(_make_ses_event(['test@example.com'], SNS_SUBJECT), None)
        rec = self._one(table)
        self.assertTrue(rec['event'].startswith('confirmation#'))
        self.assertEqual(rec['result'], 'mismatch')
        sns.confirm_subscription.assert_not_called()

    def test_confirmation_account_mismatch(self, mock_resource, mock_client):
        index, table, sns, s3 = self._wire(
            mock_resource, mock_client, _raw_email(CONFIRM_URL),
            _reg(expectations={'account-ids': ['999999999999']}))
        index.handler(_make_ses_event(['test@example.com'], SNS_SUBJECT), None)
        self.assertEqual(self._one(table)['result'], 'mismatch')

    def test_confirmation_no_url_error(self, mock_resource, mock_client):
        index, table, sns, s3 = self._wire(mock_resource, mock_client,
                                            _raw_email('no link'), _reg())
        index.handler(_make_ses_event(['test@example.com'], SNS_SUBJECT), None)
        self.assertEqual(self._one(table)['result'], 'error')

    def test_confirmation_sns_error(self, mock_resource, mock_client):
        index, table, sns, s3 = self._wire(mock_resource, mock_client,
                                            _raw_email(CONFIRM_URL), _reg())
        sns.confirm_subscription.side_effect = Exception('boom')
        index.handler(_make_ses_event(['test@example.com'], SNS_SUBJECT), None)
        self.assertEqual(self._one(table)['result'], 'error')

    def test_confirmation_unregistered_unexpected(self, mock_resource, mock_client):
        index, table, sns, s3 = self._wire(mock_resource, mock_client,
                                            _raw_email(CONFIRM_URL), registration=None)
        index.handler(_make_ses_event(['unknown@example.com'], SNS_SUBJECT), None)
        rec = self._one(table)
        self.assertTrue(rec['event'].startswith('unexpected#'))
        self.assertEqual(rec['result'], 'unregistered')
        sns.confirm_subscription.assert_not_called()

    # --- Notification path ---

    def test_notification_success(self, mock_resource, mock_client):
        index, table, sns, s3 = self._wire(
            mock_resource, mock_client, _raw_email('welcome friends'),
            _reg(expectations={'body-contains-all': 'welcome'}))
        index.handler(_make_ses_event(['test@example.com'], 'Any Subject'), None)
        rec = self._one(table)
        self.assertTrue(rec['event'].startswith('notification#'))
        self.assertEqual(rec['result'], 'success')

    def test_notification_failure_body(self, mock_resource, mock_client):
        index, table, sns, s3 = self._wire(
            mock_resource, mock_client, _raw_email('nothing here'),
            _reg(expectations={'body-contains-all': 'welcome'}))
        index.handler(_make_ses_event(['test@example.com'], 'Any Subject'), None)
        self.assertEqual(self._one(table)['result'], 'failure')

    def test_notification_topic_match_from_footer(self, mock_resource, mock_client):
        index, table, sns, s3 = self._wire(
            mock_resource, mock_client, _raw_email('welcome\n' + UNSUB_URL),
            _reg(expectations={'sns-topic-name': 'MyTopic', 'body-contains-all': 'welcome'}))
        index.handler(_make_ses_event(['test@example.com'], 'Any Subject'), None)
        self.assertEqual(self._one(table)['result'], 'success')

    def test_notification_topic_mismatch_from_footer(self, mock_resource, mock_client):
        index, table, sns, s3 = self._wire(
            mock_resource, mock_client, _raw_email('welcome\n' + UNSUB_URL),
            _reg(expectations={'sns-topic-name': 'OtherTopic'}))
        index.handler(_make_ses_event(['test@example.com'], 'Any Subject'), None)
        self.assertEqual(self._one(table)['result'], 'mismatch')

    def test_notification_topic_expected_but_no_footer_mismatch(self, mock_resource, mock_client):
        index, table, sns, s3 = self._wire(
            mock_resource, mock_client, _raw_email('welcome, no unsubscribe link'),
            _reg(expectations={'sns-topic-name': 'MyTopic'}))
        index.handler(_make_ses_event(['test@example.com'], 'Any Subject'), None)
        self.assertEqual(self._one(table)['result'], 'mismatch')

    def test_notification_no_expectations_success(self, mock_resource, mock_client):
        index, table, sns, s3 = self._wire(mock_resource, mock_client,
                                            _raw_email('anything'), _reg(expectations={}))
        index.handler(_make_ses_event(['test@example.com'], 'Any Subject'), None)
        self.assertEqual(self._one(table)['result'], 'success')

    def test_notification_unregistered(self, mock_resource, mock_client):
        index, table, sns, s3 = self._wire(mock_resource, mock_client,
                                            _raw_email('welcome'), registration=None)
        index.handler(_make_ses_event(['nobody@example.com'], 'Any Subject'), None)
        rec = self._one(table)
        self.assertTrue(rec['event'].startswith('notification#'))
        self.assertEqual(rec['result'], 'unregistered')

    # --- Expectation matcher variants ---

    def test_body_contains_any(self, mock_resource, mock_client):
        index, table, sns, s3 = self._wire(
            mock_resource, mock_client, _raw_email('has beta only'),
            _reg(expectations={'body-contains-any': ['alpha', 'beta']}))
        index.handler(_make_ses_event(['test@example.com'], 'S'), None)
        self.assertEqual(self._one(table)['result'], 'success')

    def test_body_contains_all_fails_when_one_missing(self, mock_resource, mock_client):
        index, table, sns, s3 = self._wire(
            mock_resource, mock_client, _raw_email('has alpha only'),
            _reg(expectations={'body-contains-all': ['alpha', 'beta']}))
        index.handler(_make_ses_event(['test@example.com'], 'S'), None)
        self.assertEqual(self._one(table)['result'], 'failure')

    def test_subject_like_glob(self, mock_resource, mock_client):
        index, table, sns, s3 = self._wire(
            mock_resource, mock_client, _raw_email('body'),
            _reg(expectations={'subject-like': 'Order * shipped'}))
        index.handler(_make_ses_event(['test@example.com'], 'Order 123 shipped'), None)
        self.assertEqual(self._one(table)['result'], 'success')

    def test_subject_matches_exact(self, mock_resource, mock_client):
        index, table, sns, s3 = self._wire(
            mock_resource, mock_client, _raw_email('body'),
            _reg(expectations={'subject-matches': 'Exact Subject'}))
        index.handler(_make_ses_event(['test@example.com'], 'Exact Subject'), None)
        self.assertEqual(self._one(table)['result'], 'success')

    def test_keys_anded_one_fails(self, mock_resource, mock_client):
        # subject ok but body fails -> overall failure (keys AND'd)
        index, table, sns, s3 = self._wire(
            mock_resource, mock_client, _raw_email('body without the term'),
            _reg(expectations={'subject-matches': 'S', 'body-contains-all': 'keyword'}))
        index.handler(_make_ses_event(['test@example.com'], 'S'), None)
        self.assertEqual(self._one(table)['result'], 'failure')

    def test_registration_lookup_follows_pagination(self, mock_resource, mock_client):
        # First page empty with a cursor; registration found on the second page.
        index, table, sns, s3 = self._wire(mock_resource, mock_client,
                                            _raw_email(CONFIRM_URL), _reg())
        table.query.side_effect = [
            {'Items': [], 'LastEvaluatedKey': {'email': 'test@example.com', 'event': 'x'}},
            {'Items': [_reg()]},
        ]
        index.handler(_make_ses_event(['test@example.com'], SNS_SUBJECT), None)
        self.assertEqual(table.query.call_count, 2)
        sns.confirm_subscription.assert_called_once()      # reg found -> confirmed


if __name__ == '__main__':
    unittest.main()
