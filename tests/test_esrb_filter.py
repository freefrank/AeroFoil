import unittest
from unittest.mock import patch


_IMPORT_ERROR = None
appmod = None
normalize_max_rating = None
try:
    from app import app as appmod
    from app.auth import normalize_max_rating
except ModuleNotFoundError as exc:
    _IMPORT_ERROR = exc


class EsrbFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if _IMPORT_ERROR is not None:
            raise unittest.SkipTest(f"Missing dependency for ESRB filter tests: {_IMPORT_ERROR}")

    # --- _coerce_rating_value --------------------------------------------
    def test_coerce_rating_value(self):
        self.assertIsNone(appmod._coerce_rating_value(None))
        self.assertIsNone(appmod._coerce_rating_value(''))
        self.assertIsNone(appmod._coerce_rating_value('abc'))
        self.assertIsNone(appmod._coerce_rating_value(-1))
        self.assertEqual(appmod._coerce_rating_value(0), 0)
        self.assertEqual(appmod._coerce_rating_value('10'), 10)
        self.assertEqual(appmod._coerce_rating_value(17), 17)

    # --- _title_allowed truth table --------------------------------------
    def test_no_cap_allows_everything(self):
        self.assertTrue(appmod._title_allowed(None, 18, True))
        self.assertTrue(appmod._title_allowed(None, None, True))

    def test_rating_at_or_below_cap_allowed(self):
        self.assertTrue(appmod._title_allowed(13, 0, True))
        self.assertTrue(appmod._title_allowed(13, 13, True))  # boundary
        self.assertFalse(appmod._title_allowed(13, 17, True))

    def test_unrated_fail_closed_vs_open(self):
        # block_unrated=True -> hidden; block_unrated=False -> shown.
        self.assertFalse(appmod._title_allowed(13, None, True))
        self.assertTrue(appmod._title_allowed(13, None, False))

    # --- _enriched_file_allowed ------------------------------------------
    def test_enriched_file_allowed(self):
        below = {'rating': 10, 'unrated': False}
        above = {'rating': 17, 'unrated': False}
        unrated = {'rating': None, 'unrated': True}

        self.assertTrue(appmod._enriched_file_allowed(below, None, True))   # no cap
        self.assertTrue(appmod._enriched_file_allowed(below, 13, True))
        self.assertFalse(appmod._enriched_file_allowed(above, 13, True))
        self.assertFalse(appmod._enriched_file_allowed(unrated, 13, True))
        self.assertTrue(appmod._enriched_file_allowed(unrated, 13, False))

    # --- _filter_sections_payload ----------------------------------------
    def _sample_payload(self):
        return {
            'sections': [
                {'id': 'all', 'title': 'All', 'items': [
                    {'title_id': 'A', 'rating': 0},
                    {'title_id': 'B', 'rating': 17},
                    {'title_id': 'C', 'rating': None},
                ]},
            ]
        }

    def test_filter_sections_no_cap_returns_same_object(self):
        payload = self._sample_payload()
        self.assertIs(appmod._filter_sections_payload(payload, None, True), payload)

    def test_filter_sections_fail_closed(self):
        payload = self._sample_payload()
        out = appmod._filter_sections_payload(payload, 13, True)
        kept = [item['title_id'] for item in out['sections'][0]['items']]
        self.assertEqual(kept, ['A'])
        # Original payload is not mutated.
        self.assertEqual(len(payload['sections'][0]['items']), 3)

    def test_filter_sections_fail_open_keeps_unrated(self):
        payload = self._sample_payload()
        out = appmod._filter_sections_payload(payload, 13, False)
        kept = [item['title_id'] for item in out['sections'][0]['items']]
        self.assertEqual(kept, ['A', 'C'])

    # --- _file_blocked_by_cap (multicontent fail-closed) -----------------
    def test_file_blocked_no_cap(self):
        self.assertFalse(appmod._file_blocked_by_cap(1, None, True))

    def test_file_blocked_unidentified_file(self):
        with patch.object(appmod, '_file_title_ids', return_value=[]):
            self.assertTrue(appmod._file_blocked_by_cap(1, 13, True))
            self.assertFalse(appmod._file_blocked_by_cap(1, 13, False))

    def test_file_blocked_by_most_restrictive_title(self):
        ratings = {'AAAA': 0, 'BBBB': 17}

        def fake_get_game_info(tid):
            return {'rating': ratings.get(tid)}

        with patch.object(appmod, '_file_title_ids', return_value=['AAAA', 'BBBB']), \
             patch.object(appmod.titles, 'titledb_session') as session_mock, \
             patch.object(appmod.titles, 'get_game_info', side_effect=fake_get_game_info):
            session_mock.return_value.__enter__.return_value = True
            session_mock.return_value.__exit__.return_value = False
            # BBBB (17) exceeds a Teen cap -> whole file blocked.
            self.assertTrue(appmod._file_blocked_by_cap(1, 13, True))
            # Both within an adult cap -> allowed.
            self.assertFalse(appmod._file_blocked_by_cap(1, 18, True))

    # --- _blocked_title_ids_for_cap --------------------------------------
    def test_blocked_title_ids_for_cap(self):
        metadata = {'rating_by_title_id': {'A': 0, 'B': 17, 'C': None}}
        with patch.object(appmod, '_get_cached_titles_metadata', return_value=metadata):
            blocked = appmod._blocked_title_ids_for_cap(13, True)
            self.assertEqual(blocked, {'B', 'C'})
            blocked_open = appmod._blocked_title_ids_for_cap(13, False)
            self.assertEqual(blocked_open, {'B'})
            self.assertEqual(appmod._blocked_title_ids_for_cap(None, True), set())

    def test_allowed_title_ids_for_cap_fails_closed_for_missing_ratings(self):
        metadata = {'rating_by_title_id': {'A': 0, 'B': 17, 'C': None}}
        with patch.object(appmod, '_get_cached_titles_metadata', return_value=metadata):
            self.assertEqual(appmod._allowed_title_ids_for_cap(13), {'A'})
        with patch.object(appmod, '_get_cached_titles_metadata', return_value={'rating_by_title_id': {}}):
            self.assertEqual(appmod._allowed_title_ids_for_cap(13), set())

    # --- normalize_max_rating (auth) -------------------------------------
    def test_normalize_max_rating(self):
        self.assertIsNone(normalize_max_rating(None))
        self.assertIsNone(normalize_max_rating(''))
        self.assertIsNone(normalize_max_rating('   '))
        self.assertIsNone(normalize_max_rating('not-a-number'))
        self.assertIsNone(normalize_max_rating(99))   # not a valid ESRB age
        self.assertEqual(normalize_max_rating('13'), 13)
        self.assertEqual(normalize_max_rating(17), 17)
        self.assertEqual(normalize_max_rating(0), 0)


class TitlesIndexRatingTests(unittest.TestCase):
    """Verify the titles index coercion helper independent of TitleDB I/O."""

    def setUp(self):
        try:
            from app import titles as titlesmod
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest(f"Missing dependency for titles tests: {exc}")
        self.titles = titlesmod

    def test_coerce_rating(self):
        self.assertIsNone(self.titles._coerce_rating(None))
        self.assertIsNone(self.titles._coerce_rating(''))
        self.assertIsNone(self.titles._coerce_rating('x'))
        self.assertIsNone(self.titles._coerce_rating(-1))
        self.assertEqual(self.titles._coerce_rating(10), 10)
        self.assertEqual(self.titles._coerce_rating('17'), 17)


if __name__ == '__main__':
    unittest.main()
