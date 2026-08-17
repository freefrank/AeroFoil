import unittest
from unittest.mock import patch


_IMPORT_ERROR = None
flask_app = None
shop_icon_api = None
shop_banner_api = None
try:
    from app.app import app as flask_app
    from app.app import shop_icon_api, shop_banner_api
except ModuleNotFoundError as exc:
    _IMPORT_ERROR = exc


class ShopMediaCacheHeadersTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if _IMPORT_ERROR is not None:
            raise unittest.SkipTest(f"Missing dependency for shop media header tests: {_IMPORT_ERROR}")

    def test_icon_placeholder_response_is_not_cached(self):
        with flask_app.test_request_context('/api/shop/icon/0100000000000001', method='GET'):
            with (
                patch('app.app.titles.load_titledb', return_value=None),
                patch('app.app.titles.release_titledb', return_value=None),
                patch('app.app.titles.get_game_info', side_effect=[{'iconUrl': ''}, {'iconUrl': ''}]),
            ):
                response = shop_icon_api.__wrapped__('0100000000000001')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers.get('Cache-Control'),
            'no-store, no-cache, must-revalidate',
        )
        response.close()

    def test_banner_placeholder_response_is_not_cached(self):
        with flask_app.test_request_context('/api/shop/banner/0100000000000001', method='GET'):
            with (
                patch('app.app.titles.load_titledb', return_value=None),
                patch('app.app.titles.release_titledb', return_value=None),
                patch('app.app.titles.get_game_info', side_effect=[{'bannerUrl': ''}, {'bannerUrl': ''}]),
            ):
                response = shop_banner_api.__wrapped__('0100000000000001')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers.get('Cache-Control'),
            'no-store, no-cache, must-revalidate',
        )
        response.close()


if __name__ == '__main__':
    unittest.main()
