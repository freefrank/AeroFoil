import unittest
from types import SimpleNamespace
from unittest.mock import patch

from werkzeug.security import generate_password_hash, check_password_hash


_IMPORT_ERROR = None
flask_app = None
reset_user_password = None
try:
    from app.app import app as flask_app
    from app.auth import reset_user_password
except ModuleNotFoundError as exc:
    _IMPORT_ERROR = exc


class _QueryResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _UserQueryStub:
    def __init__(self, users_by_id):
        self._users_by_id = users_by_id

    def filter_by(self, **kwargs):
        user_id = kwargs.get('id')
        return _QueryResult(self._users_by_id.get(int(user_id)))


class AuthPasswordChangeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if _IMPORT_ERROR is not None:
            raise unittest.SkipTest(f"Missing dependency for auth password tests: {_IMPORT_ERROR}")

    def test_standard_user_can_change_own_password_with_current_password(self):
        handler = reset_user_password.__wrapped__
        db_user = SimpleNamespace(
            id=7,
            user='alice',
            password=generate_password_hash('old-secret', method='scrypt'),
        )
        user_query_stub = _UserQueryStub({7: db_user})
        fake_current_user = SimpleNamespace(is_authenticated=True, is_admin=False, id=7, user='alice')

        with flask_app.test_request_context('/api/user/password', method='PATCH', json={
            'password': 'new-secret',
            'current_password': 'old-secret',
        }):
            with (
                patch('app.auth.current_user', fake_current_user),
                patch('app.auth.User.query', user_query_stub),
                patch('app.auth.db.session.commit') as commit_mock,
            ):
                response = handler()

        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['errors'], [])
        commit_mock.assert_called_once()
        self.assertTrue(check_password_hash(db_user.password, 'new-secret'))

    def test_standard_user_cannot_change_other_users_password(self):
        handler = reset_user_password.__wrapped__
        db_user = SimpleNamespace(
            id=99,
            user='other',
            password=generate_password_hash('other-secret', method='scrypt'),
        )
        user_query_stub = _UserQueryStub({99: db_user})
        fake_current_user = SimpleNamespace(is_authenticated=True, is_admin=False, id=7, user='alice')

        with flask_app.test_request_context('/api/user/password', method='PATCH', json={
            'user_id': 99,
            'password': 'new-secret',
            'current_password': 'other-secret',
        }):
            with (
                patch('app.auth.current_user', fake_current_user),
                patch('app.auth.User.query', user_query_stub),
                patch('app.auth.db.session.commit') as commit_mock,
            ):
                response = handler()

        payload = response.get_json()
        self.assertFalse(payload['success'])
        self.assertIn('You can only change your own password.', payload['errors'])
        commit_mock.assert_not_called()

    def test_admin_can_reset_other_user_password_without_current_password(self):
        handler = reset_user_password.__wrapped__
        db_user = SimpleNamespace(
            id=99,
            user='other',
            password=generate_password_hash('other-secret', method='scrypt'),
        )
        user_query_stub = _UserQueryStub({99: db_user})
        fake_current_user = SimpleNamespace(is_authenticated=True, is_admin=True, id=1, user='admin')

        with flask_app.test_request_context('/api/user/password', method='PATCH', json={
            'user_id': 99,
            'password': 'admin-reset-secret',
        }):
            with (
                patch('app.auth.current_user', fake_current_user),
                patch('app.auth.User.query', user_query_stub),
                patch('app.auth.db.session.commit') as commit_mock,
            ):
                response = handler()

        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['errors'], [])
        commit_mock.assert_called_once()
        self.assertTrue(check_password_hash(db_user.password, 'admin-reset-secret'))


if __name__ == '__main__':
    unittest.main()
