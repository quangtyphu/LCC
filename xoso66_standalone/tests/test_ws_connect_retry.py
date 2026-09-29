# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import time
import unittest

from xoso66_minigame_ws import (
    WS_CONNECT_BUDGET_SEC,
    WS_CONNECT_SLOT_WAIT_SEC,
    WS_TOKEN_BUDGET_SEC,
    WS_HOST,
    _choose_ws_tcp_host,
    _cloudflare_ipv4_from_ipv6,
    _sleep_until_reconnect,
    ws_reconnect_interval_sec,
)
from xoso66_ws_pool import ws_connect_batch_size, ws_stuck_unconnected_sec


class TestWsConnectBatchSize(unittest.TestCase):
    def test_uses_config_not_windows_one(self):
        n = ws_connect_batch_size({"game_worker": {"ws_connect_batch_size": 4}})
        self.assertEqual(n, 4)

    def test_default_four(self):
        n = ws_connect_batch_size({"game_worker": {}})
        self.assertEqual(n, 4)

    def test_caps_at_eight(self):
        n = ws_connect_batch_size({"game_worker": {"ws_connect_batch_size": 99}})
        self.assertEqual(n, 8)

    def test_stuck_unconnected_default_thirty(self):
        self.assertEqual(ws_stuck_unconnected_sec({}), 30.0)
        self.assertEqual(
            ws_stuck_unconnected_sec({"game_worker": {"ws_stuck_unconnected_sec": 12}}),
            12.0,
        )


class TestWsReconnectInterval(unittest.TestCase):
    def test_watch_uses_budget(self):
        self.assertEqual(ws_reconnect_interval_sec(game_watch=True), WS_CONNECT_BUDGET_SEC)

    def test_slot_wait_longer_than_io_budget(self):
        self.assertGreater(WS_CONNECT_SLOT_WAIT_SEC, WS_CONNECT_BUDGET_SEC)

    def test_token_budget_covers_force_refresh(self):
        self.assertGreaterEqual(WS_TOKEN_BUDGET_SEC, WS_CONNECT_BUDGET_SEC)
        self.assertGreaterEqual(WS_TOKEN_BUDGET_SEC, 45.0)

    def test_sleep_keeps_floor_when_budget_used(self):
        async def _run() -> float:
            started = time.time() - (WS_CONNECT_BUDGET_SEC + 1)
            t0 = time.time()
            await _sleep_until_reconnect(
                started,
                interval_sec=WS_CONNECT_BUDGET_SEC,
                is_stopping=lambda: False,
                min_sleep_sec=2.0,
                jitter_sec=0.0,
            )
            return time.time() - t0

        elapsed = asyncio.run(_run())
        self.assertGreaterEqual(elapsed, 1.9)
        self.assertLess(elapsed, 3.5)


class TestWsDnsParkingBypass(unittest.TestCase):
    def test_extracts_cloudflare_ipv4_from_current_aaaa(self):
        self.assertEqual(
            _cloudflare_ipv4_from_ipv6("2606:4700::6812:dd6"),
            "104.18.13.214",
        )

    def test_bypasses_known_parking_a_record(self):
        target = _choose_ws_tcp_host(
            ["103.224.212.141", "2606:4700::6812:cd6"]
        )
        self.assertEqual(target, "104.18.12.214")

    def test_parking_without_aaaa_still_uses_verified_edge(self):
        target = _choose_ws_tcp_host(["103.224.212.141"])
        self.assertEqual(target, "104.18.12.214")

    def test_affected_host_never_downgrades_to_remote_dns(self):
        target = _choose_ws_tcp_host(
            ["104.18.12.214", "2606:4700::6812:cd6"]
        )
        self.assertEqual(target, "104.18.12.214")

    def test_affected_host_with_empty_dns_uses_verified_edge(self):
        self.assertNotEqual(_choose_ws_tcp_host([]), WS_HOST)
        self.assertEqual(_choose_ws_tcp_host([]), "104.18.12.214")


class TestSubscribeFailFlags(unittest.TestCase):
    def test_closed_frame_is_token_only(self):
        from xoso66_minigame_ws import _flags_after_subscribe_fail

        full, ws_only = _flags_after_subscribe_fail(
            "ConnectionClosedError: no close frame received or sent"
        )
        self.assertFalse(full)
        self.assertTrue(ws_only)

    def test_verification_needs_full_refresh(self):
        from xoso66_minigame_ws import _flags_after_subscribe_fail

        full, ws_only = _flags_after_subscribe_fail("verification failed")
        self.assertTrue(full)
        self.assertFalse(ws_only)


if __name__ == "__main__":
    unittest.main()
