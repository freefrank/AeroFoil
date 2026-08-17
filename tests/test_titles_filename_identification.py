import unittest
from unittest.mock import patch

from app import titles
from app.constants import APP_TYPE_BASE, APP_TYPE_DLC, APP_TYPE_UPD


class FilenameIdentificationTests(unittest.TestCase):
    BASE_ID = '0100AAAABBBB0000'
    UPDATE_ID = '0100AAAABBBB0800'

    def test_legacy_update_tag_derives_update_app_id_from_base_id(self):
        with patch.object(titles, 'identify_appId', return_value=(self.BASE_ID, APP_TYPE_BASE)):
            app_id, title_id, app_type, version, error = titles.identify_file_from_filename(
                f'Example Title [{self.BASE_ID}] [UPDATE][v65536].nsp'
            )

        self.assertEqual(error, '')
        self.assertEqual(app_id, self.UPDATE_ID)
        self.assertEqual(title_id, self.BASE_ID)
        self.assertEqual(app_type, APP_TYPE_UPD)
        self.assertEqual(version, '65536')

    def test_real_update_app_id_remains_unchanged(self):
        with patch.object(titles, 'identify_appId', return_value=(self.BASE_ID, APP_TYPE_UPD)):
            app_id, title_id, app_type, _version, error = titles.identify_file_from_filename(
                f'Example Title [{self.UPDATE_ID}] [UPDATE][v65536].nsp'
            )

        self.assertEqual(error, '')
        self.assertEqual(app_id, self.UPDATE_ID)
        self.assertEqual(title_id, self.BASE_ID)
        self.assertEqual(app_type, APP_TYPE_UPD)

    def test_dlc_tag_with_base_id_is_not_guessed(self):
        with patch.object(titles, 'identify_appId', return_value=(self.BASE_ID, APP_TYPE_BASE)):
            app_id, title_id, app_type, _version, error = titles.identify_file_from_filename(
                f'Example Title [{self.BASE_ID}] [DLC][v0].nsp'
            )

        self.assertEqual(error, '')
        self.assertEqual(app_id, self.BASE_ID)
        self.assertEqual(title_id, self.BASE_ID)
        self.assertEqual(app_type, APP_TYPE_BASE)
        self.assertNotEqual(app_type, APP_TYPE_DLC)


if __name__ == '__main__':
    unittest.main()
