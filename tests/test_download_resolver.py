import unittest
from unittest.mock import Mock, patch

from app.downloads.resolver import resolve_download_url


class ResolverTests(unittest.TestCase):
    @patch("app.downloads.resolver.requests.Session")
    def test_resolve_preserves_session_cookies_across_redirects(self, session_cls_mock):
        session = Mock()
        session_cls_mock.return_value = session

        first = Mock(status_code=302, headers={"Location": "/next"})
        first.raise_for_status = Mock()
        second = Mock(status_code=200, headers={"Content-Type": "text/plain"}, content=b"not-a-torrent")
        second.raise_for_status = Mock()
        session.get.side_effect = [first, second]

        resolved_type, resolved_data = resolve_download_url("https://indexer.example/start", timeout=5)

        self.assertEqual(resolved_type, "url")
        self.assertEqual(resolved_data, "https://indexer.example/next")
        self.assertEqual(session.get.call_count, 2)

    @patch("app.downloads.resolver.time.monotonic")
    @patch("app.downloads.resolver.requests.Session")
    def test_resolve_enforces_total_timeout_budget(self, session_cls_mock, monotonic_mock):
        session = Mock()
        session_cls_mock.return_value = session
        response = Mock(status_code=302, headers={"Location": "/again"})
        response.raise_for_status = Mock()
        session.get.return_value = response

        # start, first remaining calc, second remaining calc -> timeout exhausted before second request
        monotonic_mock.side_effect = [0.0, 0.0, 2.1]

        resolved_type, _resolved_data = resolve_download_url("https://indexer.example/start", timeout=2)

        self.assertEqual(resolved_type, "url")
        self.assertEqual(session.get.call_count, 1)

    @patch("app.downloads.resolver.requests.Session")
    def test_resolve_detects_torrent_content(self, session_cls_mock):
        session = Mock()
        session_cls_mock.return_value = session
        response = Mock(
            status_code=200,
            headers={"Content-Type": "application/x-bittorrent"},
            content=b"d8:announce5:testee",
        )
        response.raise_for_status = Mock()
        session.get.return_value = response

        resolved_type, resolved_data = resolve_download_url("https://indexer.example/file", timeout=5)

        self.assertEqual(resolved_type, "torrent_content")
        self.assertEqual(resolved_data, b"d8:announce5:testee")


if __name__ == "__main__":
    unittest.main()
