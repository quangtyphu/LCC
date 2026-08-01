# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

from xoso66_session import (
    merge_session_cookies,
    strip_identity_cookies,
)


class TestSessionIdentityCookies(unittest.TestCase):
    def test_merge_blocks_phpsessid_when_disallowed(self):
        session = {"cookies": {"cf_clearance": "old", "PHPSESSID": "mine"}}
        merge_session_cookies(
            session,
            {"cf_clearance": "new", "PHPSESSID": "other", "__cf_bm": "bm"},
            allow_identity=False,
        )
        self.assertEqual(session["cookies"]["cf_clearance"], "new")
        self.assertEqual(session["cookies"]["__cf_bm"], "bm")
        self.assertEqual(session["cookies"]["PHPSESSID"], "mine")

    def test_merge_allows_phpsessid_when_enabled(self):
        session = {"cookies": {"PHPSESSID": "old"}}
        merge_session_cookies(
            session,
            {"PHPSESSID": "fresh"},
            allow_identity=True,
        )
        self.assertEqual(session["cookies"]["PHPSESSID"], "fresh")

    def test_strip_identity(self):
        session = {"cookies": {"PHPSESSID": "x", "cf_clearance": "y"}}
        strip_identity_cookies(session)
        self.assertNotIn("PHPSESSID", session["cookies"])
        self.assertEqual(session["cookies"]["cf_clearance"], "y")


if __name__ == "__main__":
    unittest.main()
