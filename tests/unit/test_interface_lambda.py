# SPDX-License-Identifier: MIT-0
"""Unit tests for the Interface Lambda (direct-invoke handler).

Actions: register (with `expectations`), status, history, delete.
Status: not-known / not-registered / registered special cases, else the latest
confirmation|notification event as "<event>/<result>".
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

_LAMBDA_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'lambda', 'interface')
sys.path.insert(0, _LAMBDA_DIR)
sys.modules.pop('index', None)


class _InterfaceImportMixin:
    def setUp(self):
        super().setUp()
        if _LAMBDA_DIR in sys.path:
            sys.path.remove(_LAMBDA_DIR)
        sys.path.insert(0, _LAMBDA_DIR)
        sys.modules.pop('index', None)


def _reg_row(email='a@b.com'):
    return {'email': email, 'event': 'registration', 'result': 'success',
            'expectations': {}, 'registeredAt': 1700000000,
            'timestamp': 1700000000, 'ttl': 1700007200}


def _ev(event, result, ts):
    return {'email': 'a@b.com', 'event': event, 'result': result, 'timestamp': ts}


@patch.dict(os.environ, {'TABLE_NAME': 'test-table'})
class TestRegister(_InterfaceImportMixin, unittest.TestCase):

    def _with_table(self, existing_rows=None):
        import index
        mock_table = MagicMock()
        mock_table.query.return_value = {'Items': existing_rows or []}
        patcher = patch.object(index, 'dynamodb')
        mock_ddb = patcher.start()
        self.addCleanup(patcher.stop)
        mock_ddb.Table.return_value = mock_table
        return index, mock_table

    def test_register_valid(self):
        index, table = self._with_table()
        result = index.handler({
            'action': 'register', 'email': 'test@example.com',
            'expectations': {'body-contains-all': 'jungle'},
        }, None)
        self.assertEqual(result['status'], 'registered')
        item = table.put_item.call_args[1]['Item']
        self.assertEqual(item['event'], 'registration')
        self.assertEqual(item['expectations'], {'body-contains-all': 'jungle'})
        self.assertEqual(item['ttl'], item['registeredAt'] + 7200)
        self.assertEqual(item['timestamp'], item['registeredAt'])

    def test_register_no_expectations(self):
        index, table = self._with_table()
        index.handler({'action': 'register', 'email': 'a@b.com'}, None)
        self.assertEqual(table.put_item.call_args[1]['Item']['expectations'], {})

    def test_register_ttl_honors_env_override(self):
        with patch.dict(os.environ, {'RECORD_TTL_SECONDS': '600'}):
            index, table = self._with_table()
            index.handler({'action': 'register', 'email': 'a@b.com'}, None)
            item = table.put_item.call_args[1]['Item']
            self.assertEqual(item['ttl'], item['registeredAt'] + 600)

    def test_register_missing_email_raises(self):
        index, _ = self._with_table()
        with self.assertRaises(ValueError):
            index.handler({'action': 'register'}, None)

    def test_register_expectations_not_object_raises(self):
        index, _ = self._with_table()
        with self.assertRaises(ValueError):
            index.handler({'action': 'register', 'email': 'a@b.com', 'expectations': 'x'}, None)

    def test_register_unknown_expectation_key_raises(self):
        index, _ = self._with_table()
        with self.assertRaises(ValueError):
            index.handler({'action': 'register', 'email': 'a@b.com',
                           'expectations': {'bogus-key': 'v'}}, None)

    def test_register_overwrites_when_only_registration_exists(self):
        existing = [{'email': 'a@b.com', 'event': 'registration', 'result': 'success'}]
        index, table = self._with_table(existing_rows=existing)
        index.handler({'action': 'register', 'email': 'a@b.com',
                       'expectations': {'body-contains-all': 'new'}}, None)
        self.assertEqual(table.put_item.call_args[1]['Item']['expectations'],
                         {'body-contains-all': 'new'})

    def test_register_allowed_with_only_unexpected_events(self):
        # unexpected events (rogue mail) do NOT block registration; kept as-is.
        existing = [_ev('unexpected#1700000001.0', 'undefined', 1700000001.0)]
        index, table = self._with_table(existing_rows=existing)
        index.handler({'action': 'register', 'email': 'a@b.com'}, None)
        table.put_item.assert_called_once()

    def test_register_rejected_when_confirmation_event_exists(self):
        existing = [_reg_row(), _ev('confirmation#1700000001.0', 'success', 1700000001.0)]
        index, table = self._with_table(existing_rows=existing)
        with self.assertRaises(ValueError):
            index.handler({'action': 'register', 'email': 'a@b.com'}, None)
        table.put_item.assert_not_called()

    def test_register_rejected_when_notification_event_exists(self):
        existing = [_reg_row(), _ev('notification#1700000001.0', 'success', 1700000001.0)]
        index, table = self._with_table(existing_rows=existing)
        with self.assertRaises(ValueError):
            index.handler({'action': 'register', 'email': 'a@b.com'}, None)
        table.put_item.assert_not_called()

    def test_register_malformed_email_raises(self):
        index, _ = self._with_table()
        for bad in ('no-at-sign', '@no-local.com'):
            with self.assertRaises(ValueError):
                index.handler({'action': 'register', 'email': bad}, None)

    def test_register_email_key_lowercased(self):
        # PK is canonicalized to lowercase (local-part included) so the
        # processor's lowercased inbound lookup always matches.
        index, table = self._with_table()
        result = index.handler(
            {'action': 'register', 'email': '  User.Name@Example.COM  '}, None)
        self.assertEqual(result['email'], 'user.name@example.com')
        self.assertEqual(table.put_item.call_args[1]['Item']['email'],
                         'user.name@example.com')


@patch.dict(os.environ, {'TABLE_NAME': 'test-table', 'EMAIL_DOMAIN': 'testing.example.com'})
class TestRegisterDomain(_InterfaceImportMixin, unittest.TestCase):
    """EMAIL_DOMAIN is read at import time; the mixin evicts `index` per test so
    the class-level env applies to the domain check."""

    def _with_table(self):
        import index
        mock_table = MagicMock()
        mock_table.query.return_value = {'Items': []}
        patcher = patch.object(index, 'dynamodb')
        mock_ddb = patcher.start()
        self.addCleanup(patcher.stop)
        mock_ddb.Table.return_value = mock_table
        return index, mock_table

    def test_register_matching_domain_accepted(self):
        index, table = self._with_table()
        index.handler({'action': 'register', 'email': 'aide-test-1@testing.example.com'}, None)
        table.put_item.assert_called_once()

    def test_register_matching_domain_case_insensitive(self):
        index, table = self._with_table()
        index.handler({'action': 'register', 'email': 'x@TESTING.EXAMPLE.COM'}, None)
        table.put_item.assert_called_once()

    def test_register_wrong_domain_rejected(self):
        index, table = self._with_table()
        with self.assertRaises(ValueError):
            index.handler({'action': 'register', 'email': 'x@wrong.example.com'}, None)
        table.put_item.assert_not_called()

    def test_register_subdomain_not_accepted(self):
        # exact domain match; a subdomain is a different domain
        index, table = self._with_table()
        with self.assertRaises(ValueError):
            index.handler({'action': 'register', 'email': 'x@sub.testing.example.com'}, None)


@patch.dict(os.environ, {'TABLE_NAME': 'test-table'})
class TestStatus(_InterfaceImportMixin, unittest.TestCase):

    def _status(self, rows):
        import index
        mock_table = MagicMock()
        mock_table.query.return_value = {'Items': rows}
        patcher = patch.object(index, 'dynamodb')
        mock_ddb = patcher.start()
        self.addCleanup(patcher.stop)
        mock_ddb.Table.return_value = mock_table
        return index.handler({'action': 'status', 'email': 'a@b.com'}, None)['status']

    def test_not_known_when_empty(self):
        self.assertEqual(self._status([]), 'not-known')

    def test_not_registered_when_only_unexpected(self):
        self.assertEqual(self._status([_ev('unexpected#1.0', 'undefined', 1.0)]), 'not-registered')

    def test_registered_only(self):
        self.assertEqual(self._status([_reg_row()]), 'registered')

    def test_registered_with_unexpected_but_no_judged(self):
        rows = [_reg_row(), _ev('unexpected#1700000001.0', 'undefined', 1700000001.0)]
        self.assertEqual(self._status(rows), 'registered')

    def test_confirmation_success(self):
        rows = [_reg_row(), _ev('confirmation#1700000001.0', 'success', 1700000001.0)]
        self.assertEqual(self._status(rows), 'confirmation/success')

    def test_confirmation_mismatch(self):
        rows = [_reg_row(), _ev('confirmation#1700000001.0', 'mismatch', 1700000001.0)]
        self.assertEqual(self._status(rows), 'confirmation/mismatch')

    def test_notification_success(self):
        rows = [_reg_row(),
                _ev('confirmation#1700000001.0', 'success', 1700000001.0),
                _ev('notification#1700000002.0', 'success', 1700000002.0)]
        self.assertEqual(self._status(rows), 'notification/success')

    def test_notification_failure(self):
        rows = [_reg_row(),
                _ev('confirmation#1700000001.0', 'success', 1700000001.0),
                _ev('notification#1700000002.0', 'failure', 1700000002.0)]
        self.assertEqual(self._status(rows), 'notification/failure')

    def test_latest_judged_wins_by_timestamp(self):
        # later notification/success supersedes earlier notification/failure
        rows = [_reg_row(),
                _ev('notification#1700000002.0', 'failure', 1700000002.0),
                _ev('notification#1700000009.0', 'success', 1700000009.0)]
        self.assertEqual(self._status(rows), 'notification/success')

    def test_unexpected_does_not_override_judged(self):
        # an unexpected row with a later timestamp must not become the status
        rows = [_reg_row(),
                _ev('notification#1700000002.0', 'success', 1700000002.0),
                _ev('unexpected#1700000009.0', 'undefined', 1700000009.0)]
        self.assertEqual(self._status(rows), 'notification/success')

    def test_missing_email_raises(self):
        import index
        with self.assertRaises(ValueError):
            index.handler({'action': 'status'}, None)


@patch.dict(os.environ, {'TABLE_NAME': 'test-table'})
class TestHistory(_InterfaceImportMixin, unittest.TestCase):

    def _with_rows(self, rows):
        import index
        mock_table = MagicMock()
        mock_table.query.return_value = {'Items': rows}
        patcher = patch.object(index, 'dynamodb')
        mock_ddb = patcher.start()
        self.addCleanup(patcher.stop)
        mock_ddb.Table.return_value = mock_table
        return index

    def test_history_all_events_and_fields_sorted(self):
        rows = [
            {'email': 'a@b.com', 'event': 'notification#1700000002.5', 'result': 'failure',
             'timestamp': 1700000002.5, 'subject': 'Ready', 'body': 'no token',
             'detail': 'body-contains-all not satisfied'},
            _reg_row(),
            {'email': 'a@b.com', 'event': 'confirmation#1700000001.5', 'result': 'success',
             'timestamp': 1700000001.5, 'subject': 'Sub Confirm', 'body': 'confirm'},
        ]
        index = self._with_rows(rows)
        result = index.handler({'action': 'history', 'email': 'a@b.com'}, None)
        self.assertEqual(result['status'], 'notification/failure')
        self.assertEqual([e['timestamp'] for e in result['events']],
                         sorted(e['timestamp'] for e in result['events']))
        notif = [e for e in result['events'] if e['event'].startswith('notification#')][0]
        self.assertEqual(notif['result'], 'failure')
        self.assertEqual(notif['detail'], 'body-contains-all not satisfied')

    def test_history_empty_returns_not_known(self):
        index = self._with_rows([])
        result = index.handler({'action': 'history', 'email': 'missing@example.com'}, None)
        self.assertEqual(result['status'], 'not-known')
        self.assertEqual(result['events'], [])

    def test_history_surfaces_unexpected(self):
        rows = [{'email': 'a@b.com', 'event': 'unexpected#1700000003.0', 'result': 'undefined',
                 'timestamp': 1700000003.0, 'sender': 'spam@evil.com', 'subject': 'buy now...'}]
        index = self._with_rows(rows)
        result = index.handler({'action': 'history', 'email': 'a@b.com'}, None)
        self.assertEqual(result['status'], 'not-registered')
        self.assertEqual(result['events'][0]['sender'], 'spam@evil.com')


@patch.dict(os.environ, {'TABLE_NAME': 'test-table'})
class TestDelete(_InterfaceImportMixin, unittest.TestCase):

    def test_delete_removes_all_rows(self):
        import index
        rows = [
            {'email': 'a@b.com', 'event': 'registration'},
            {'email': 'a@b.com', 'event': 'confirmation#1700000001.0'},
            {'email': 'a@b.com', 'event': 'unexpected#1700000002.0'},
        ]
        mock_table = MagicMock()
        mock_table.query.return_value = {'Items': rows}
        batch = MagicMock()
        mock_table.batch_writer.return_value.__enter__.return_value = batch
        patcher = patch.object(index, 'dynamodb')
        mock_ddb = patcher.start()
        self.addCleanup(patcher.stop)
        mock_ddb.Table.return_value = mock_table

        result = index.handler({'action': 'delete', 'email': 'a@b.com'}, None)
        self.assertEqual(result['deleted'], 3)
        self.assertEqual(batch.delete_item.call_count, 3)

    def test_delete_missing_email_raises(self):
        import index
        with self.assertRaises(ValueError):
            index.handler({'action': 'delete'}, None)


@patch.dict(os.environ, {'TABLE_NAME': 'test-table'})
class TestQueryPagination(_InterfaceImportMixin, unittest.TestCase):
    """_query must follow DynamoDB pagination (LastEvaluatedKey) so a trail
    larger than one page is never silently truncated."""

    def _paged_table(self, pages):
        import index
        mock_table = MagicMock()
        mock_table.query.side_effect = pages
        patcher = patch.object(index, 'dynamodb')
        mock_ddb = patcher.start()
        self.addCleanup(patcher.stop)
        mock_ddb.Table.return_value = mock_table
        return index, mock_table

    def test_history_collects_all_pages(self):
        p1 = {'Items': [_reg_row(),
                        _ev('notification#1700000001.0', 'failure', 1700000001.0)],
              'LastEvaluatedKey': {'email': 'a@b.com', 'event': 'notification#1700000001.0'}}
        p2 = {'Items': [_ev('notification#1700000009.0', 'success', 1700000009.0)]}
        index, table = self._paged_table([p1, p2])
        result = index.handler({'action': 'history', 'email': 'a@b.com'}, None)
        self.assertEqual(len(result['events']), 3)          # both pages merged
        self.assertEqual(table.query.call_count, 2)         # followed the cursor
        # second call passed the cursor
        self.assertIn('ExclusiveStartKey', table.query.call_args_list[1][1])
        # latest event across pages wins
        self.assertEqual(result['status'], 'notification/success')

    def test_delete_removes_rows_across_pages(self):
        import index
        p1 = {'Items': [{'email': 'a@b.com', 'event': 'registration'}],
              'LastEvaluatedKey': {'email': 'a@b.com', 'event': 'registration'}}
        p2 = {'Items': [{'email': 'a@b.com', 'event': 'notification#1.0'}]}
        mock_table = MagicMock()
        mock_table.query.side_effect = [p1, p2]
        batch = MagicMock()
        mock_table.batch_writer.return_value.__enter__.return_value = batch
        patcher = patch.object(index, 'dynamodb')
        mock_ddb = patcher.start()
        self.addCleanup(patcher.stop)
        mock_ddb.Table.return_value = mock_table

        result = index.handler({'action': 'delete', 'email': 'a@b.com'}, None)
        self.assertEqual(result['deleted'], 2)              # both pages deleted
        self.assertEqual(batch.delete_item.call_count, 2)


@patch.dict(os.environ, {'TABLE_NAME': 'test-table'})
class TestActionRouting(_InterfaceImportMixin, unittest.TestCase):

    def test_unknown_action_raises(self):
        import index
        with self.assertRaises(ValueError):
            index.handler({'action': 'purge', 'email': 'a@b.com'}, None)

    def test_missing_action_raises(self):
        import index
        with self.assertRaises(ValueError):
            index.handler({'email': 'a@b.com'}, None)


if __name__ == '__main__':
    unittest.main()
