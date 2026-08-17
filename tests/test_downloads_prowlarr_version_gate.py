import unittest

_IMPORT_ERROR = None
try:
    from app.downloads.prowlarr import pick_best_result
except ModuleNotFoundError as exc:  # optional deps (flask/sqlalchemy) absent
    _IMPORT_ERROR = exc


def _usenet_result(title):
    return {
        "title": title,
        "protocol": "usenet",
        "download_url": "https://indexer.example/file.nzb",
        "seeders": 0,
        "age_minutes": 120,
    }


# Internal Switch version integer for update "2.0.0".
EXPECTED_VERSION = 131072


class ExactVersionGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if _IMPORT_ERROR is not None:
            raise unittest.SkipTest(f"Missing dependency: {_IMPORT_ERROR}")

    def _pick(self, title, require_exact_version):
        return pick_best_result(
            [_usenet_result(title)],
            title_id="0100000000010000",
            version=EXPECTED_VERSION,
            min_seeders=0,
            min_age_minutes=0,
            allowed_protocols=["usenet"],
            require_exact_version=require_exact_version,
        )

    def test_automatic_keeps_release_without_internal_version_token(self):
        # Regression for #102: normal scene titles carry a marketing version
        # ("v1.2.0"), not the internal token, so the automatic path used to drop
        # every result and queue nothing. The exact file is enforced later at
        # download time, so a tokenless title must survive the search gate.
        result = self._pick("Pokemon Scarlet Update v1.2.0 NSW-VENOM", require_exact_version=True)
        self.assertIsNotNone(result)

    def test_automatic_still_drops_release_advertising_a_different_version(self):
        result = self._pick("Some Game [v65536] NSW-GRP", require_exact_version=True)
        self.assertIsNone(result)

    def test_automatic_keeps_release_with_matching_internal_token(self):
        result = self._pick("Some Game [v131072] NSW-GRP", require_exact_version=True)
        self.assertIsNotNone(result)

    def test_manual_keeps_release_without_token(self):
        result = self._pick("Pokemon Scarlet Update v1.2.0 NSW-VENOM", require_exact_version=False)
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
