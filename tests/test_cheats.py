import io
import os
import shutil
import unittest
import zipfile

from app import cheats


class CheatStorageTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = os.path.abspath(os.path.join('.tmp', 'cheat_storage_test'))
        shutil.rmtree(self.test_dir, ignore_errors=True)
        self.original_dir = cheats.CHEATS_DIR
        cheats.CHEATS_DIR = self.test_dir

    def tearDown(self):
        cheats.CHEATS_DIR = self.original_dir
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_save_list_and_delete_cheat(self):
        saved = cheats.save_cheat('0100ABCDEF123000', 'A' * 32, b'[Infinite Example]\n04000000 00000000 00000001\n')

        self.assertEqual(saved['title_id'], '0100ABCDEF123000')
        self.assertEqual(len(cheats.list_cheats()), 1)
        self.assertEqual(cheats.read_cheat('0100abcdef123000', 'a' * 32)[:9], b'[Infinite')
        self.assertTrue(cheats.delete_cheat('0100ABCDEF123000', 'A' * 32))
        self.assertEqual(cheats.list_cheats(), [])

    def test_import_zip_only_accepts_valid_atmosphere_identities(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('bundle/0100ABCDEF123000/' + ('B' * 32) + '.txt', '[Example]\n')
            archive.writestr('0100ABCDEF123000/cheats/' + ('C' * 32) + '.txt', '[Nested Example]\n')
            archive.writestr('atmosphere/contents/[0100ABCDEF123000]/cheats/' + ('D' * 32) + '-v1.txt', '[Atmosphere Example]\n')
            archive.writestr('bundle/not-a-title/' + ('C' * 32) + '.txt', '[Ignored]\n')
            archive.writestr('../0100ABCDEF123000/' + ('E' * 32) + '.txt', '[Safe path]\n')

        result = cheats.import_zip(buffer.getvalue())

        self.assertEqual(result['imported'], 4)
        self.assertEqual(result['skipped'], 0)
        self.assertEqual(result['ignored'], 1)
        self.assertEqual(len(cheats.list_cheats()), 4)
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.test_dir), '0100ABCDEF123000')))

    def test_rejects_invalid_identifiers_and_non_utf8_content(self):
        with self.assertRaises(ValueError):
            cheats.save_cheat('bad', 'A' * 32, b'[Example]')
        with self.assertRaises(ValueError):
            cheats.save_cheat('0100ABCDEF123000', 'A' * 32, b'\xff\xfe')

    def test_note_is_stored_with_the_cheat_and_removed_on_delete(self):
        cheats.save_cheat('0100ABCDEF123000', 'E' * 32, b'[Example]\n')
        cheats.set_cheat_note('0100ABCDEF123000', 'E' * 32, 'For Example Update')

        self.assertEqual(cheats.list_cheats()[0]['note'], 'For Example Update')
        cheats.delete_cheat('0100ABCDEF123000', 'E' * 32)
        self.assertEqual(cheats._load_metadata(), {})
