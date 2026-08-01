# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from xoso66_cms_chrome import device_proxy_mismatch, _proxy_key


class TestDeviceProxyMismatch(unittest.TestCase):
    def test_same_proxy_no_mismatch(self):
        row = {
            "device": "XMSB76",
            "proxy": "1.2.3.4:20023:user:pass",
        }
        with patch(
            "xoso66_cms_chrome.resolve_cms_chrome_by_device",
            return_value={"proxy": "1.2.3.4:20023:user:pass"},
        ):
            self.assertIsNone(device_proxy_mismatch(row))

    def test_different_proxy_reports_mismatch(self):
        row = {
            "device": "XMSB76",
            "proxy": "1.2.3.4:20023:a:b",
        }
        with patch(
            "xoso66_cms_chrome.resolve_cms_chrome_by_device",
            return_value={"proxy": "5.6.7.8:20023:c:d"},
        ):
            out = device_proxy_mismatch(row)
        self.assertIsNotNone(out)
        self.assertIn("message", out)

    def test_proxy_key_normalizes_host(self):
        self.assertEqual(
            _proxy_key("Host.Example:99:u:p"),
            _proxy_key("host.example:99:u:p"),
        )


class TestSyncSessionFromChrome(unittest.TestCase):
    @patch("xoso66_session._mark_sync_chrome")
    @patch("xoso66_cf.is_cf_rate_limited", return_value=False)
    @patch("xoso66_session.refresh_account_balance_to_db")
    @patch("xoso66_session.bootstrap_prelogin")
    @patch("xoso66_session.get_user_balance")
    @patch("xoso66_chrome_profile.read_profile_cookies_after_close")
    @patch("xoso66_cms_chrome.resolve_cms_chrome_by_device")
    @patch("xoso66_accounts_db.get_account")
    @patch("xoso66_session.load_sessions")
    @patch("xoso66_session.persist_session")
    def test_happy_path(
        self,
        mock_persist,
        mock_load,
        mock_get_account,
        mock_resolve,
        mock_read_close,
        mock_get_bal,
        _bootstrap,
        mock_bal,
        _mock_rl,
        _mock_mark,
    ):
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            profile = Path(td)
            session = {
                "id": "acc1",
                "username": "u1",
                "password": "p1",
                "proxy": "1.2.3.4:1:a:b",
                "form_token": "ft",
                "cookies": {},
                "headers": {},
            }
            mock_load.return_value = {"acc1": session}
            mock_get_account.return_value = {
                "id": "acc1",
                "device": "XMSB76",
                "proxy": "1.2.3.4:1:a:b",
            }
            mock_resolve.return_value = {
                "device": "XMSB76",
                "proxy": "1.2.3.4:1:a:b",
                "profile_dir": str(profile),
            }

            def _read_close(session, profile_dir, **kwargs):
                session["cookies"] = {"PHPSESSID": "sid", "__cf_bm": "bm"}
                return {
                    "ok": False,
                    "has_clearance": False,
                    "cookie_names": ["PHPSESSID", "__cf_bm"],
                    "restarted_chrome": False,
                }

            mock_read_close.side_effect = _read_close
            mock_get_bal.return_value = {"ok": True, "balance": 1000.0}
            mock_bal.return_value = {"ok": True, "balance": 1000.0}

            from xoso66_session import sync_session_from_chrome

            out = sync_session_from_chrome("acc1", device="XMSB76", timeout_sec=30)
            self.assertTrue(out.get("ok"))
            self.assertEqual(out.get("balance"), 1000.0)
            self.assertEqual(out.get("sync_via"), "chrome_cookies")
            mock_persist.assert_called_once()


if __name__ == "__main__":
    unittest.main()
