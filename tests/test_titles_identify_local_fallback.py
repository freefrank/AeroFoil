import unittest
from unittest.mock import patch

from app import titles


class TitlesIdentifyLocalFallbackTests(unittest.TestCase):
    def test_identify_file_uses_local_metadata_when_cnmt_has_no_content(self):
        filepath = r"X:\fixture-root\Example [010051F0207B2000] [BASE][v0].nsz"

        with patch("app.titles.keys_loaded", return_value=True), patch(
            "app.titles.identify_file_from_cnmt",
            return_value=[],
        ), patch(
            "app.local_file_metadata.extract_local_metadata",
            return_value={"title_id": "010051F0207B2000", "version": 0},
        ):
            identification, success, contents, error = titles.identify_file(filepath)

        self.assertEqual(identification, "cnmt")
        self.assertTrue(success)
        self.assertEqual(error, "")
        self.assertEqual(len(contents), 1)
        self.assertEqual(contents[0]["app_id"], "010051F0207B2000")
        self.assertEqual(contents[0]["title_id"], "010051F0207B2000")
        self.assertEqual(contents[0]["version"], 0)

    def test_identify_file_uses_local_metadata_when_cnmt_raises(self):
        filepath = r"X:\fixture-root\Example [010051F0207B2000] [BASE][v0].xcz"

        with patch("app.titles.keys_loaded", return_value=True), patch(
            "app.titles.identify_file_from_cnmt",
            side_effect=RuntimeError("metadata read failed"),
        ), patch(
            "app.local_file_metadata.extract_local_metadata",
            return_value={"title_id": "010051F0207B2000", "version": 3},
        ):
            identification, success, contents, error = titles.identify_file(filepath)

        self.assertEqual(identification, "cnmt")
        self.assertTrue(success)
        self.assertEqual(error, "")
        self.assertEqual(len(contents), 1)
        self.assertEqual(contents[0]["app_id"], "010051F0207B2000")
        self.assertEqual(contents[0]["title_id"], "010051F0207B2000")
        self.assertEqual(contents[0]["version"], 3)


if __name__ == "__main__":
    unittest.main()

