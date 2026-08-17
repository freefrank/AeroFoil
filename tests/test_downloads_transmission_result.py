import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

_IMPORT_ERROR = None
torrent_client = None
try:
    _MODULE_PATH = Path(__file__).resolve().parents[1] / "app" / "downloads" / "torrent_client.py"
    _SPEC = importlib.util.spec_from_file_location("torrent_client_txn_module", _MODULE_PATH)
    torrent_client = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(torrent_client)
except ModuleNotFoundError as exc:  # optional deps (flask/sqlalchemy) absent
    _IMPORT_ERROR = exc


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.headers = {}

    def json(self):
        return self._json_data


class _FakeTransmissionSession:
    """Transmission always answers HTTP 200; the logical status is in 'result'."""

    def __init__(self, result="success", arguments=None):
        self.headers = {}
        self._result = result
        self._arguments = arguments if arguments is not None else {}

    def post(self, url, json=None, timeout=None):
        body = {"arguments": self._arguments}
        if self._result is not None:
            body["result"] = self._result
        return _FakeResponse(200, body)


class TransmissionResultTests(unittest.TestCase):
    MAGNET = "magnet:?xt=urn:btih:3b245504cf5f11bbdbb2e120e036ff83aeb8c145&dn=test"

    @classmethod
    def setUpClass(cls):
        if _IMPORT_ERROR is not None:
            raise unittest.SkipTest(f"Missing dependency: {_IMPORT_ERROR}")

    def _add(self, session, update_only):
        with patch.object(torrent_client, "_new_client_session", return_value=session):
            return torrent_client._add_transmission(
                url="http://transmission.local",
                username="u",
                password="p",
                download_url=self.MAGNET,
                torrent_content=None,
                category="aerofoil",
                download_path="",
                timeout_seconds=1,
                expected_name="Test",
                update_only=update_only,
            )

    def test_update_add_surfaces_real_error_not_torrent_id_message(self):
        # Regression for #120: a rejected add returned the misleading
        # "Unable to resolve torrent id for file selection." instead of the
        # actual Transmission error.
        session = _FakeTransmissionSession(result="download directory not writable")
        ok, message, _hash = self._add(session, update_only=True)
        self.assertFalse(ok)
        self.assertIn("download directory not writable", message.lower())
        self.assertNotIn("resolve torrent id", message.lower())

    def test_plain_add_reports_failure_instead_of_false_success(self):
        session = _FakeTransmissionSession(result="invalid or corrupt torrent file")
        ok, message, _hash = self._add(session, update_only=False)
        self.assertFalse(ok)
        self.assertIn("invalid or corrupt", message.lower())

    def test_missing_result_reports_failure_instead_of_false_success(self):
        session = _FakeTransmissionSession(result=None)
        ok, message, _hash = self._add(session, update_only=False)
        self.assertFalse(ok)
        self.assertIn("missing result", message.lower())

    def test_successful_add_still_accepted(self):
        session = _FakeTransmissionSession(
            result="success",
            arguments={"torrent-added": {"id": 7, "hashString": "abc", "name": "Test"}},
        )
        ok, message, torrent_hash = self._add(session, update_only=False)
        self.assertTrue(ok)
        self.assertEqual(torrent_hash, "abc")


if __name__ == "__main__":
    unittest.main()
