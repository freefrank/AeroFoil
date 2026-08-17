import os
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from app import local_file_metadata


class LocalFileMetadataCompressedTests(unittest.TestCase):
    def setUp(self):
        self.test_root = os.path.join(
            os.getcwd(),
            ".tmp",
            "local-metadata-tests",
            "case-compressed",
        )
        shutil.rmtree(self.test_root, ignore_errors=True)
        os.makedirs(self.test_root, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.test_root, ignore_errors=True)

    def _touch(self, path, payload=b"fixture"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(payload)

    def test_extract_local_metadata_nsz_routes_to_nsp_parser(self):
        nsz_path = os.path.join(self.test_root, "Example Title.nsz")
        self._touch(nsz_path)

        with patch.object(
            local_file_metadata,
            "resolve_switch_guides_scripts_dir",
            return_value="app/vendor/switch_ghidra_scripts",
        ), patch.object(
            local_file_metadata,
            "_load_switch_guides_modules",
            return_value={},
        ), patch.object(
            local_file_metadata,
            "_extract_from_nsp",
            return_value={"title_id": "0100AAAABBBBCCCC"},
        ) as extract_nsp:
            out = local_file_metadata.extract_local_metadata(
                nsz_path,
                preferred_language="en",
                preferred_region="US",
            )

        self.assertEqual(out.get("title_id"), "0100AAAABBBBCCCC")
        extract_nsp.assert_called_once()

    def test_extract_local_metadata_xcz_uses_decompress_then_xci_parser(self):
        xcz_path = os.path.join(self.test_root, "Example Title.xcz")
        self._touch(xcz_path)

        with patch.object(
            local_file_metadata,
            "resolve_switch_guides_scripts_dir",
            return_value="app/vendor/switch_ghidra_scripts",
        ), patch.object(
            local_file_metadata,
            "_load_switch_guides_modules",
            return_value={},
        ), patch.object(
            local_file_metadata,
            "_extract_from_xci",
            return_value={"title_id": "0100DDDDEEEEFFFF"},
        ) as extract_xci:
            out = local_file_metadata.extract_local_metadata(
                xcz_path,
                preferred_language="en",
                preferred_region="US",
            )

        self.assertEqual(out.get("title_id"), "0100DDDDEEEEFFFF")
        extract_xci.assert_called_once()

    def test_extract_local_metadata_nsz_uses_persistent_cache_after_first_parse(self):
        nsz_path = os.path.join(self.test_root, "Cached Title.nsz")
        cache_dir = Path(self.test_root) / "cache"
        self._touch(nsz_path)

        with patch.object(
            local_file_metadata,
            "DEFAULT_LOCAL_METADATA_CACHE_DIR",
            cache_dir,
        ), patch.object(
            local_file_metadata,
            "resolve_switch_guides_scripts_dir",
            return_value="app/vendor/switch_ghidra_scripts",
        ), patch.object(
            local_file_metadata,
            "_load_switch_guides_modules",
            return_value={},
        ), patch.object(
            local_file_metadata,
            "_extract_from_nsp",
            return_value={"title_id": "0100AAAABBBBCCCC", "icon_bytes": b"abc"},
        ) as extract_nsp:
            out_first = local_file_metadata.extract_local_metadata(
                nsz_path,
                preferred_language="en",
                preferred_region="US",
            )
            out_second = local_file_metadata.extract_local_metadata(
                nsz_path,
                preferred_language="en",
                preferred_region="US",
            )

        self.assertEqual(out_first.get("title_id"), "0100AAAABBBBCCCC")
        self.assertEqual(out_second.get("title_id"), "0100AAAABBBBCCCC")
        self.assertEqual(extract_nsp.call_count, 1)

if __name__ == "__main__":
    unittest.main()
