# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import time
import unittest

from xoso66_auto_bet import is_immediate_prev_issue
from xoso66_minigame_ws import (
    MultiGameWatchState,
    WsBroadcastCoordinator,
    WsInboundRouter,
    _should_claim_round_events,
    note_round_result_logged,
    note_round_start_claimed,
)
from xoso66_minigame_ws_worker import (
    DedicatedWsListener,
    player_subscribe_spec,
)


def _frame(msg_type: str, game_id: int, **data: object) -> str:
    return json.dumps(
        {
            "type": msg_type,
            "data": {"game_id": game_id, **data},
        }
    )


class TestWsInboundRouter(unittest.IsolatedAsyncioTestCase):
    async def test_open_info_jumps_ahead_of_bulk_backlog(self) -> None:
        router = WsInboundRouter()
        for countdown in range(500):
            router.put_raw(
                _frame(
                    "g_game_info",
                    9,
                    is_open=0,
                    countdown=countdown,
                )
            )
            router.put_raw(_frame("jackpot_money", 9, money=countdown))

        router.put_raw(
            _frame(
                "g_open_info",
                9,
                issue="202609220999",
                next_info={"issue": "202609221000"},
            )
        )

        item = await asyncio.wait_for(router.get(), timeout=0.2)
        self.assertIsNotNone(item)
        self.assertEqual(item.obj["type"], "g_open_info")
        self.assertEqual(router.bulk_depth(), 2)
        self.assertGreater(router.bulk_coalesced, 900)

    async def test_balance_and_open_transition_are_critical(self) -> None:
        router = WsInboundRouter()
        router.put_raw(_frame("g_game_info", 9, is_open=0, countdown=20))
        router.put_raw(_frame("balance", 0, balance=12345))
        router.put_raw(_frame("g_game_info", 9, is_open=1, countdown=15))

        first = await router.get()
        second = await router.get()
        self.assertEqual(first.obj["type"], "balance")
        self.assertEqual(second.obj["type"], "g_game_info")
        self.assertEqual(second.obj["data"]["is_open"], 1)

    async def test_received_timestamp_is_before_dequeue(self) -> None:
        router = WsInboundRouter()
        item_put = router.put_raw(_frame("g_open_info", 9, issue="1"))
        await asyncio.sleep(0.01)
        item_get = await router.get()
        self.assertEqual(item_put, item_get)
        self.assertLess(item_get.received_mono, time.monotonic())


class TestWsBroadcastCoordinator(unittest.TestCase):
    def test_duplicate_and_older_issue_are_rejected(self) -> None:
        coordinator = WsBroadcastCoordinator()
        self.assertTrue(coordinator.claim_round_start(9, "202609220502"))
        self.assertFalse(coordinator.claim_round_start(9, "202609220502"))
        self.assertFalse(coordinator.claim_round_start(9, "202609220501"))
        self.assertTrue(coordinator.claim_round_start(9, "202609220503"))

    def test_result_and_start_have_separate_order(self) -> None:
        coordinator = WsBroadcastCoordinator()
        self.assertTrue(coordinator.claim_round_start(9, "202609220502"))
        self.assertTrue(coordinator.claim_round_result(9, "202609220501"))


class TestDangChoiMissingWs(unittest.TestCase):
    def tearDown(self) -> None:
        from xoso66_ws_pool import unregister_ws_connected

        unregister_ws_connected("acc_missing_ws")

    def test_connected_nick_is_not_missing(self) -> None:
        from xoso66_ws_pool import (
            dang_choi_without_connected_ws,
            register_ws_connected,
        )

        register_ws_connected("acc_missing_ws")

        def _fake_dang(_cfg: dict) -> list[str]:
            return ["acc_missing_ws", "acc_no_ws"]

        import xoso66_ws_pool as pool

        orig = pool.dang_choi_account_ids
        pool.dang_choi_account_ids = _fake_dang
        try:
            missing = dang_choi_without_connected_ws({})
        finally:
            pool.dang_choi_account_ids = orig
        self.assertNotIn("acc_missing_ws", missing)
        self.assertIn("acc_no_ws", missing)


class TestAfterDepositWsOpen(unittest.TestCase):
    def tearDown(self) -> None:
        from xoso66_ws_pool import unregister_ws_connected

        unregister_ws_connected("acc_test_zombie")
        unregister_ws_connected("acc_test_live")

    def test_request_ws_close_keeps_connected_until_stop(self) -> None:
        from xoso66_ws_pool import (
            get_connected_ws_accounts,
            register_ws_connected,
            request_ws_close,
        )

        register_ws_connected("acc_test_zombie")
        self.assertIn("acc_test_zombie", get_connected_ws_accounts())
        closed = request_ws_close(["acc_test_zombie"], cfg={})
        self.assertEqual(closed, ["acc_test_zombie"])
        self.assertIn("acc_test_zombie", get_connected_ws_accounts())

    def test_schedule_after_deposit_queues_when_not_connected(self) -> None:
        from xoso66_minigame_ws_worker import (
            _ws_after_deposit_ids,
            _ws_after_deposit_lock,
            schedule_ws_connect_after_deposit,
        )
        from xoso66_ws_pool import register_ws_connected

        with _ws_after_deposit_lock:
            _ws_after_deposit_ids.discard("acc_test_zombie")
            _ws_after_deposit_ids.discard("acc_test_live")
        register_ws_connected("acc_test_live")
        queued = schedule_ws_connect_after_deposit(
            ["acc_test_zombie", "acc_test_live"]
        )
        self.assertEqual(queued, ["acc_test_zombie"])
        with _ws_after_deposit_lock:
            self.assertIn("acc_test_zombie", _ws_after_deposit_ids)
            _ws_after_deposit_ids.discard("acc_test_zombie")


class TestPhiênMoiViec15(unittest.TestCase):
    def tearDown(self) -> None:
        from xoso66_ws_pool import clear_pending_ws_slot

        clear_pending_ws_slot("acc_low")
        clear_pending_ws_slot("acc_new")

    def _run_reconcile(
        self,
        *,
        dang: set[str],
        rows: dict,
        connected: set[str],
        tasks: list[str],
        min_target: int,
        pending: set[str] | None = None,
        deposit_busy: set[str] | None = None,
    ):
        from unittest.mock import patch

        import xoso66_ws_pool as pool

        def fake_dang(_cfg, **_kw):
            return set(dang)

        def fake_apply(aid, status, **_kw):
            if aid in dang and status != pool.STATUS_DANG_CHOI:
                dang.discard(aid)
            rows.setdefault(aid, {})["status"] = status
            return True

        patches = [
            patch.object(pool, "dang_choi_account_ids", fake_dang),
            patch.object(pool, "apply_account_status_intent", fake_apply),
            patch.object(pool, "get_account", lambda aid: rows.get(aid, {})),
            patch.object(
                pool,
                "is_balance_too_low_for_ws",
                lambda row, cfg: float(row.get("balance") or 0) < 10000,
            ),
            patch.object(
                pool, "account_balance_vnd", lambda row: float(row.get("balance") or 0)
            ),
            patch.object(pool, "list_C_pending_kq_ids", lambda: set()),
            patch.object(pool, "is_win_credit_recheck_pending", lambda *a, **k: False),
            patch.object(pool, "win_credit_recheck_age_sec", lambda *a, **k: None),
            patch.object(pool, "win_credit_recheck_ttl_sec", lambda cfg: 60),
            patch.object(pool, "get_connected_ws_accounts", lambda: set(connected)),
            patch.object(
                pool, "get_pending_ws_slot_ids", lambda: set(pending or ())
            ),
            patch.object(
                pool,
                "account_ws_deposit_busy",
                lambda aid, cfg: aid in (deposit_busy or ()),
            ),
            patch.object(pool, "pick_ws_listener_account", lambda cfg: None),
            patch.object(pool, "is_ws_listener", lambda aid, cfg: False),
            patch.object(pool, "ws_account_count", lambda cfg: min_target),
            patch.object(pool, "min_balance_for_ws", lambda cfg: 10000),
            patch.object(pool, "daily_bet_ws_limit_vnd", lambda cfg: 897000),
            patch.object(
                pool, "filter_ws_target_connectable", lambda cfg, ids, **kw: list(ids)
            ),
            patch.object(pool, "is_row_exhausted_daily_cap", lambda *a, **k: False),
            patch("xoso66_proxy.is_proxy_dead", lambda *_a, **_k: False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return pool.reconcile_ws_pool(
            {},
            list(tasks),
            round_start=True,
            ws_task_ids=list(tasks),
        )

    def test_round_start_closes_het_tien_not_in_c(self) -> None:
        dang = {"acc_low"}
        rows = {
            "acc_low": {
                "id": "acc_low",
                "status": "Đang Chơi",
                "balance": 2000,
            }
        }
        plan = self._run_reconcile(
            dang=dang,
            rows=rows,
            connected={"acc_low"},
            tasks=["acc_low"],
            min_target=0,
        )
        self.assertIsNotNone(plan)
        self.assertIn("acc_low", plan.prune_removed)
        self.assertEqual(rows["acc_low"]["status"], "Hết Tiền")

    def test_round_start_opens_dang_choi_without_ws(self) -> None:
        dang = {"acc_new"}
        rows = {
            "acc_new": {
                "id": "acc_new",
                "status": "Đang Chơi",
                "balance": 50000,
            }
        }
        plan = self._run_reconcile(
            dang=dang,
            rows=rows,
            connected=set(),
            tasks=[],
            min_target=1,
        )
        self.assertIsNotNone(plan)
        self.assertIn("acc_new", plan.fill_connect_ids)

    def test_viec5_skips_depositing_nick_without_ws(self) -> None:
        dang: set[str] = set()
        rows = {
            "acc_dep": {
                "id": "acc_dep",
                "status": "Hết Tiền",
                "balance": 2000,
            }
        }
        plan = self._run_reconcile(
            dang=dang,
            rows=rows,
            connected=set(),
            tasks=[],
            min_target=1,
            pending={"acc_dep"},
            deposit_busy={"acc_dep"},
        )
        if plan is not None:
            self.assertNotIn("acc_dep", plan.prune_removed)


class TestRoundResultClaim(unittest.TestCase):
    def test_immediate_prev_issue(self) -> None:
        self.assertTrue(is_immediate_prev_issue("202609220606", "202609220605"))
        self.assertFalse(is_immediate_prev_issue("202609220606", "202609220604"))
        self.assertFalse(is_immediate_prev_issue("202609220606", "202609220606"))

    def test_listener_claims_player_does_not_when_covered(self) -> None:
        import xoso66_minigame_ws as ws

        listener = MultiGameWatchState(watch_ids=frozenset({9}), full_watch=True)
        player = MultiGameWatchState(watch_ids=frozenset({9}), full_watch=False)
        self.assertTrue(_should_claim_round_events(listener))
        orig = ws.listener_covering_rounds
        ws.listener_covering_rounds = lambda: True
        try:
            self.assertFalse(_should_claim_round_events(player))
        finally:
            ws.listener_covering_rounds = orig

    def test_result_log_is_once_per_issue(self) -> None:
        issue = f"test{int(time.time() * 1000)}"
        self.assertTrue(note_round_result_logged(9, issue))
        self.assertFalse(note_round_result_logged(9, issue))

    def test_round_start_claim_is_once_per_issue(self) -> None:
        issue = f"start{int(time.time() * 1000)}"
        self.assertTrue(note_round_start_claimed(9, issue))
        self.assertFalse(note_round_start_claimed(9, issue))

    def test_round_start_line_prints_once_per_issue(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout
        from xoso66_round_log import log_round_start_line

        issue = f"line{int(time.time() * 1000)}"
        buf = StringIO()
        with redirect_stdout(buf):
            log_round_start_line(
                game_label="Tài xỉu Đại Lộc",
                jackpot_vnd=1_000,
                issue=issue,
                min_jackpot_vnd=2_000,
                game_id=9,
            )
            log_round_start_line(
                game_label="Tài xỉu Đại Lộc",
                jackpot_vnd=1_000,
                issue=issue,
                min_jackpot_vnd=2_000,
                game_id=9,
            )
        self.assertEqual(buf.getvalue().count("BẮT ĐẦU PHIÊN"), 1)


class TestWsProfiles(unittest.TestCase):
    def test_player_subscribe_is_lobby_plus_focus_only(self) -> None:
        self.assertEqual(player_subscribe_spec(9), "0,9")
        self.assertEqual(player_subscribe_spec(17), "0,17")

    def test_dedicated_listener_starts_and_stops(self) -> None:
        runner = DedicatedWsListener()

        async def _forever() -> None:
            while True:
                await asyncio.sleep(1)

        runner.start(lambda: _forever(), name="test-ws-listener")
        deadline = time.time() + 2
        while not runner.is_alive() and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(runner.is_alive())
        runner.stop(timeout=2)
        self.assertFalse(runner.is_alive())


if __name__ == "__main__":
    unittest.main()
