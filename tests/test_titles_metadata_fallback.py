import unittest
from unittest.mock import patch

from app import settings
from app import titledb
from app import titles


class TitlesMetadataFallbackTests(unittest.TestCase):
    def setUp(self):
        titles._reset_titledb_state()
        titles._titles_index_ready = True
        titles._titles_desc_by_title_id = {}
        titles._titles_images_by_title_id = {}

    def tearDown(self):
        titles._reset_titledb_state()

    def test_fallback_database_fills_missing_primary_entry(self):
        title_id = "0100F6B011028000"
        fallback_info = {
            'name': 'Ring Fit Adventure',
            'bannerUrl': 'https://example.invalid/us-banner.jpg',
            'iconUrl': 'https://example.invalid/us-icon.jpg',
            'id': title_id,
            'category': 'Fitness',
            'nsuId': '333',
            'description': 'US description',
        }
        titles._fallback_titles_index_ready = True

        with patch(
            'app.titles.load_settings',
            return_value={'titles': {'manual_overrides': {}}},
        ), patch(
            'app.titles._get_title_info_from_index',
            return_value=None,
        ), patch(
            'app.titles._get_fallback_region_title_info_from_index',
            return_value=fallback_info,
        ), patch(
            'app.titles._build_local_fallback_info',
            return_value=None,
        ):
            info = titles.get_game_info(title_id)

        self.assertEqual(info['name'], 'Ring Fit Adventure')
        self.assertEqual(info['category'], 'Fitness')
        self.assertEqual(info['description'], 'US description')
        self.assertEqual(info['iconUrl'], 'https://example.invalid/us-icon.jpg')

    def test_primary_entry_wins_over_fallback(self):
        title_id = "0100F6B011028000"
        primary_info = {
            'name': '健身环大冒险',
            'bannerUrl': 'https://example.invalid/cn-banner.jpg',
            'iconUrl': 'https://example.invalid/cn-icon.jpg',
            'id': title_id,
            'category': '健身',
            'nsuId': '111',
            'description': 'CN description',
        }
        titles._fallback_titles_index_ready = True

        with patch(
            'app.titles.load_settings',
            return_value={'titles': {'manual_overrides': {}}},
        ), patch(
            'app.titles._get_title_info_from_index',
            return_value=primary_info,
        ), patch(
            'app.titles._get_fallback_region_title_info_from_index',
        ) as fallback_mock:
            info = titles.get_game_info(title_id)

        self.assertEqual(info['name'], '健身环大冒险')
        fallback_mock.assert_not_called()

    def test_fallback_fills_fields_when_primary_name_is_empty(self):
        title_id = "0100F6B011028000"
        primary_info = {
            'name': '',
            'bannerUrl': 'https://example.invalid/cn-banner.jpg',
            'iconUrl': '',
            'id': title_id,
            'category': '',
            'nsuId': None,
            'description': None,
        }
        fallback_info = {
            'name': 'Ring Fit Adventure',
            'bannerUrl': 'https://example.invalid/us-banner.jpg',
            'iconUrl': 'https://example.invalid/us-icon.jpg',
            'id': title_id,
            'category': 'Fitness',
            'nsuId': '333',
            'description': 'US description',
        }
        titles._fallback_titles_index_ready = True

        with patch(
            'app.titles.load_settings',
            return_value={'titles': {'manual_overrides': {}}},
        ), patch(
            'app.titles._get_title_info_from_index',
            return_value=primary_info,
        ), patch(
            'app.titles._get_fallback_region_title_info_from_index',
            return_value=fallback_info,
        ):
            info = titles.get_game_info(title_id)

        # Fallback fills the gaps, primary non-empty fields still win.
        self.assertEqual(info['name'], 'Ring Fit Adventure')
        self.assertEqual(info['bannerUrl'], 'https://example.invalid/cn-banner.jpg')
        self.assertEqual(info['iconUrl'], 'https://example.invalid/us-icon.jpg')


class SearchNameCandidatesTests(unittest.TestCase):
    def setUp(self):
        titles._reset_titledb_state()
        titles._titles_index_ready = True

    def tearDown(self):
        titles._reset_titledb_state()

    def test_collects_names_from_all_ready_databases(self):
        titles._fallback_titles_index_ready = True
        with patch(
            'app.titles._get_title_info_from_index',
            return_value={'name': '健身環大冒險'},
        ), patch(
            'app.titles._get_fallback_region_title_info_from_index',
            return_value={'name': 'Ring Fit Adventure'},
        ):
            names = titles.get_search_name_candidates('01002FF008C24000')

        self.assertEqual(names, ['健身環大冒險', 'Ring Fit Adventure'])

    def test_skips_databases_that_are_not_ready(self):
        with patch(
            'app.titles._get_title_info_from_index',
            return_value={'name': 'Primary Only'},
        ), patch(
            'app.titles._get_fallback_region_title_info_from_index',
        ) as fallback_mock:
            names = titles.get_search_name_candidates('01002FF008C24000')

        self.assertEqual(names, ['Primary Only'])
        fallback_mock.assert_not_called()


class DownloadQueryFallbackTests(unittest.TestCase):
    def test_build_queries_uses_ascii_candidate_when_primary_name_strips_empty(self):
        from app.downloads import manager

        update = {
            'title_id': '01002FF008C24000',
            'title_name': '健身環大冒險',
            'search_names': ['健身環大冒險', 'RingFit Adventure'],
            'version': 262144,
        }
        with patch(
            'app.downloads.manager.load_settings',
            return_value={'downloads': {}},
        ):
            queries = manager._build_queries(update)

        self.assertEqual(queries[0], 'RingFit Adventure')
        self.assertEqual(queries[1], 'RingFit Adventure update')

    def test_build_queries_falls_back_to_title_id_without_candidates(self):
        from app.downloads import manager

        update = {
            'title_id': '01002FF008C24000',
            'title_name': '健身環大冒險',
            'search_names': [],
            'version': 262144,
        }
        with patch(
            'app.downloads.manager.load_settings',
            return_value={'downloads': {}},
        ), patch(
            'app.downloads.manager.titles_lib.get_search_name_candidates',
            return_value=[],
        ):
            queries = manager._build_queries(update)

        self.assertEqual(queries[0], '01002FF008C24000')


class MetadataFallbackSettingsTests(unittest.TestCase):
    def test_normalize_keeps_order_dedupes_and_drops_invalid(self):
        normalized = settings._normalize_metadata_fallbacks(
            ['US.en', 'bogus', 'JP.ja', 'US.en', 'us.en', '', None]
        )
        self.assertEqual(normalized, ['US.en', 'JP.ja'])

    def test_normalize_caps_length(self):
        entries = ['US.en', 'JP.ja', 'GB.en', 'FR.fr', 'DE.de', 'KR.ko']
        self.assertEqual(len(settings._normalize_metadata_fallbacks(entries)), 4)

    def test_fallback_files_exclude_primary_region_file(self):
        app_settings = {
            'titles': {
                'region': 'US',
                'language': 'en',
                'metadata_fallbacks': ['US.en', 'JP.ja'],
            }
        }
        files = titledb.get_fallback_titles_files(app_settings)
        self.assertEqual(files, ['titles.JP.ja.json'])

    def test_fallback_files_respect_available_filter(self):
        app_settings = {
            'titles': {
                'region': 'CN',
                'language': 'zh',
                'metadata_fallbacks': ['US.en', 'JP.ja'],
            }
        }
        files = titledb.get_fallback_titles_files(
            app_settings, available_files={'titles.US.en.json'}
        )
        self.assertEqual(files, ['titles.US.en.json'])


if __name__ == '__main__':
    unittest.main()
