import os
import unittest
from types import SimpleNamespace

_IMPORT_ERROR = None
try:
    from app.constants import APP_TYPE_BASE
    from app.library import _build_destination, _is_base_dir_template
except ModuleNotFoundError as exc:  # optional deps (flask/sqlalchemy) absent
    _IMPORT_ERROR = exc


class FlatPresetBaseFolderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if _IMPORT_ERROR is not None:
            raise unittest.SkipTest(f"Missing dependency: {_IMPORT_ERROR}")

    def _dest(self, folder_tpl):
        app = SimpleNamespace(
            app_type=APP_TYPE_BASE,
            app_version="0",
            app_id="",  # empty -> skip versions.txt / titledb lookups
            title=SimpleNamespace(title_id="01000CA004DCA000"),
        )
        file_entry = SimpleNamespace(filename="Human Fall Flat.xci", extension="xci")
        template = {
            "base": {
                "folder": folder_tpl,
                "filename": "{title} [{title_id}] [BASE][v{version}].{ext}",
            }
        }
        return _build_destination(
            "/games", file_entry, app, "Human Fall Flat", None, active_template=template
        )

    def test_flat_preset_dot_places_file_in_base_directory(self):
        # Regression for #122: the Flat preset uses folder="." which must map to
        # the library base directory, not /games/Other.
        folder, _filename = self._dest(".")
        self.assertEqual(folder, "/games")

    def test_named_folder_template_unchanged(self):
        folder, _filename = self._dest("{title} [{title_id}]")
        self.assertEqual(folder, os.path.join("/games", "Human Fall Flat [01000CA004DCA000]"))

    def test_default_preset_subfolder_unchanged(self):
        folder, _filename = self._dest("{title} [{title_id}]/Base")
        self.assertEqual(
            folder, os.path.join("/games", "Human Fall Flat [01000CA004DCA000]", "Base")
        )

    def test_is_base_dir_template_detection(self):
        self.assertTrue(_is_base_dir_template("."))
        self.assertTrue(_is_base_dir_template("./"))
        self.assertTrue(_is_base_dir_template(".."))
        self.assertFalse(_is_base_dir_template("{title}"))
        self.assertFalse(_is_base_dir_template(""))
        self.assertFalse(_is_base_dir_template("Base"))


if __name__ == "__main__":
    unittest.main()
