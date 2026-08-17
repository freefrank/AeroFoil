import unittest
from unittest.mock import patch


_IMPORT_ERROR = None
flask_app = None
_render_motd_template = None
try:
    from app.app import app as flask_app
    from app.app import _render_motd_template
except ModuleNotFoundError as exc:
    _IMPORT_ERROR = exc


class MotdProxyIpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if _IMPORT_ERROR is not None:
            raise unittest.SkipTest(f"Missing dependency for MOTD proxy tests: {_IMPORT_ERROR}")

    def test_motd_remote_addr_uses_trusted_forwarded_client_ip(self):
        settings = {
            'security': {
                'trust_proxy_headers': True,
                'trusted_proxies': ['172.16.0.0/12'],
            }
        }
        with flask_app.test_request_context(
            '/',
            environ_base={'REMOTE_ADDR': '172.20.0.10'},
            headers={'X-Forwarded-For': '203.0.113.42, 172.20.0.10'},
        ):
            with patch('app.app.load_settings', return_value=settings):
                self.assertEqual(_render_motd_template('IP: {remote_addr}'), 'IP: 203.0.113.42')


if __name__ == '__main__':
    unittest.main()
