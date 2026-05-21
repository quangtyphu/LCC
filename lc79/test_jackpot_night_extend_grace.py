"""Unit tests: grace cược sau nổ hũ (POST_JACKPOT_EXTRA_ROUNDS)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

import jackpot_night_extend as jne


def _cfg(extra_rounds: int = 5, enabled: int = 1) -> dict:
    return {
        "JACKPOT_NIGHT_EXTEND": {
            "ENABLED": enabled,
            "JACKPOT_THRESHOLD": 300_000_000,
            "POST_JACKPOT_EXTRA_ROUNDS": extra_rounds,
            "CACHE_SECONDS": 300,
        }
    }


class JackpotGraceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._state_path = os.path.join(self._tmpdir.name, "state.json")
        self._orig_path = jne._STATE_PATH
        jne._STATE_PATH = self._state_path
        jne._last_jackpot_fetch_mono = 0.0

    def tearDown(self) -> None:
        jne._STATE_PATH = self._orig_path
        self._tmpdir.cleanup()

    def _write_state(self, **kwargs) -> None:
        st = jne._state_defaults()
        st.update(kwargs)
        with open(self._state_path, "w", encoding="utf-8") as f:
            json.dump(st, f)

    def test_cancel_sets_grace_rounds(self) -> None:
        jne.cancel_extend_on_jackpot_hit(_cfg(5))
        st = jne.read_state()
        self.assertTrue(st["cancelled"])
        self.assertEqual(st["post_jackpot_rounds_left"], 5)
        self.assertFalse(st["periodic_bet_allowed"])

    def test_cancel_no_grace_when_zero(self) -> None:
        jne.cancel_extend_on_jackpot_hit(_cfg(0))
        st = jne.read_state()
        self.assertTrue(st["cancelled"])
        self.assertEqual(st["post_jackpot_rounds_left"], 0)

    @mock.patch.object(jne, "refresh_jackpot_cache", return_value=True)
    def test_gate_allows_while_grace(self, _refresh: mock.MagicMock) -> None:
        self._write_state(
            cancelled=True,
            periodic_bet_allowed=False,
            post_jackpot_rounds_left=3,
        )
        self.assertTrue(jne.jackpot_periodic_gate_allows_betting(_cfg(5)))

    @mock.patch.object(jne, "refresh_jackpot_cache", return_value=True)
    def test_gate_blocks_after_grace_exhausted(self, _refresh: mock.MagicMock) -> None:
        self._write_state(
            cancelled=True,
            periodic_bet_allowed=False,
            post_jackpot_rounds_left=0,
        )
        self.assertFalse(jne.jackpot_periodic_gate_allows_betting(_cfg(5)))

    @mock.patch.object(jne, "refresh_jackpot_cache", return_value=True)
    def test_five_sessions_then_block(self, _refresh: mock.MagicMock) -> None:
        cfg = _cfg(5)
        jne.cancel_extend_on_jackpot_hit(cfg)
        for i in range(5):
            sid = 9_000_000 + i
            self.assertTrue(
                jne.jackpot_periodic_gate_allows_betting(cfg),
                f"phiên {i+1} phải được cược",
            )
            jne.consume_post_jackpot_round(sid, cfg)
        self.assertFalse(jne.jackpot_periodic_gate_allows_betting(cfg))

    def test_consume_idempotent_same_session(self) -> None:
        cfg = _cfg(5)
        jne.cancel_extend_on_jackpot_hit(cfg)
        jne.consume_post_jackpot_round(1001, cfg)
        jne.consume_post_jackpot_round(1001, cfg)
        st = jne.read_state()
        self.assertEqual(st["post_jackpot_rounds_left"], 4)

    @mock.patch.object(jne, "_fetch_txmini_jackpot", return_value=(500_000_000, None))
    @mock.patch("session_game_total.pick_from_active_ws", return_value="probe_user")
    def test_txmini_above_clears_grace(
        self, _pick: mock.MagicMock, _fetch: mock.MagicMock
    ) -> None:
        self._write_state(
            cancelled=True,
            post_jackpot_rounds_left=2,
            periodic_bet_allowed=False,
        )
        jne.refresh_jackpot_cache(_cfg(5), force=True)
        st = jne.read_state()
        self.assertEqual(st["post_jackpot_rounds_left"], 0)
        self.assertFalse(st["cancelled"])
        self.assertTrue(st["periodic_bet_allowed"])

    @mock.patch.object(jne, "refresh_jackpot_cache", return_value=True)
    def test_gate_disabled_skips_grace_logic(self, _refresh: mock.MagicMock) -> None:
        self._write_state(cancelled=True, post_jackpot_rounds_left=0)
        self.assertTrue(jne.jackpot_periodic_gate_allows_betting(_cfg(5, enabled=0)))


if __name__ == "__main__":
    unittest.main()
