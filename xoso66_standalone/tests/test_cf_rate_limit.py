# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from unittest.mock import patch

from xoso66_cf import (
    cf_rate_limit_message,
    cf_rate_limit_remaining,
    is_cf_rate_limited,
    is_cloudflare_rate_limited,
    mark_cf_rate_limited,
    proxy_cooldown_key,
)


class TestCloudflareRateLimitDetection(unittest.TestCase):
    def test_detects_error_1015_html(self):
        html = "<title>Error 1015</title><p>You are being rate limited.</p>"
        self.assertTrue(is_cloudflare_rate_limited(html))

    def test_detects_429_status(self):
        self.assertTrue(is_cloudflare_rate_limited("", 429))

    def test_normal_json_not_rate_limited(self):
        self.assertFalse(is_cloudflare_rate_limited('{"code":1,"msg":"ok"}', 200))


class TestCfRateLimitCooldown(unittest.TestCase):
    def test_mark_and_check_by_proxy(self):
        px = "1.2.3.4:8080:user:pass"
        key = proxy_cooldown_key(px)
        self.assertTrue(key)

        self.assertFalse(is_cf_rate_limited(px))
        mark_cf_rate_limited(px, cooldown_sec=120)
        self.assertTrue(is_cf_rate_limited(px))
        self.assertGreater(cf_rate_limit_remaining(px), 0)
        self.assertIn("1015", cf_rate_limit_message(px))


class TestEnsureSessionLightProbe(unittest.TestCase):
    @patch("xoso66_session.persist_session")
    @patch("xoso66_session.get_user_balance")
    @patch("xoso66_session.load_sessions")
    @patch("xoso66_proxy.ensure_proxy")
    def test_skips_refresh_when_balance_ok_without_clearance(
        self, _ensure_proxy, mock_load, mock_bal, mock_persist
    ):
        import time

        session = {
            "id": "acc1",
            "proxy": "1.2.3.4:1:a:b",
            "form_token": "ft",
            "cookies": {"PHPSESSID": "abc"},
            "headers": {},
            "session_login_at": time.time(),  # còn TTL — không force login
        }
        mock_load.return_value = {"acc1": session}
        mock_bal.return_value = {"ok": True, "balance": 50}

        with patch("xoso66_session.refresh_cloudflare") as mock_refresh:
            from xoso66_session import ensure_session

            out = ensure_session("acc1")
            self.assertEqual(out["cookies"]["PHPSESSID"], "abc")
            mock_refresh.assert_not_called()
            mock_persist.assert_called()


class TestSyncChromeRateLimit(unittest.TestCase):
    @patch("xoso66_session._mark_sync_chrome")
    @patch("xoso66_cf.is_cf_rate_limited")
    @patch("xoso66_cms_chrome.resolve_cms_chrome_by_device")
    @patch("xoso66_accounts_db.get_account")
    @patch("xoso66_session.load_sessions")
    def test_sync_aborts_when_rate_limited(
        self, mock_load, mock_get_account, mock_resolve, mock_rl, _mark
    ):
        from pathlib import Path
        import tempfile

        mock_rl.return_value = True
        with tempfile.TemporaryDirectory() as td:
            profile = Path(td)
            session = {"id": "acc1", "proxy": "1.2.3.4:1:a:b", "cookies": {}}
            mock_load.return_value = {"acc1": session}
            mock_get_account.return_value = {"id": "acc1", "device": "XMSB76"}
            mock_resolve.return_value = {
                "device": "XMSB76",
                "proxy": "1.2.3.4:1:a:b",
                "profile_dir": str(profile),
            }

            from xoso66_session import sync_session_from_chrome

            out = sync_session_from_chrome("acc1", device="XMSB76")
            self.assertFalse(out.get("ok"))
            self.assertTrue(out.get("rate_limited"))


if __name__ == "__main__":
    unittest.main()
