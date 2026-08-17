import unittest

from app.db import User


class CheatAccessTests(unittest.TestCase):
    def test_cheat_access_requires_shop_and_cheat_permissions(self):
        user = User(shop_access=True, cheat_access=True, frozen=False)
        self.assertTrue(user.has_cheat_access())
        self.assertTrue(user.has_access('cheats'))

        user.cheat_access = False
        self.assertFalse(user.has_cheat_access())

        user.cheat_access = True
        user.frozen = True
        self.assertFalse(user.has_cheat_access())
