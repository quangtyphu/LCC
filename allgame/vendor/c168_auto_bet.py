# -*- coding: utf-8 -*-
"""Tự cược BCR khi WS báo GP_NEW_GAME_START — Con/Cái cân bằng, mapping idx đúng."""

from __future__ import annotations

import random
import threading
import time
from typing import Any

from allgame.vendor.c168_bet import SIDE_VN, parse_bet_api_ok, place_bet_via_cdp


def pick_random_stake(
    stake_min: int,
    stake_max: int,
    *,
    unit: str = "k",
    stake_step: int = 10,
) -> int:
    lo, hi = min(stake_min, stake_max), max(stake_min, stake_max)
    if unit == "k":
        step = max(1, int(stake_step))
        amounts = list(range(lo, hi + 1, step))
        return random.choice(amounts) if amounts else lo
    return random.randint(lo, hi)


def pick_balanced_side(player_n: int, banker_n: int) -> str:
    if player_n < banker_n:
        return "player"
    if banker_n < player_n:
        return "banker"
    return random.choice(["player", "banker"])


class C168AutoBettor:
    def __init__(
        self,
        *,
        cdp_url: str,
        table_id: int,
        stake_min: int = 10,
        stake_max: int = 20,
        stake_unit: str = "k",
        stake_step: int = 10,
        bet_limit_id: int = 851101,
        max_rounds: int = 0,
        enabled: bool = True,
    ) -> None:
        self.cdp_url = cdp_url.rstrip("/")
        self.table_id = int(table_id)
        self.stake_min = int(stake_min)
        self.stake_max = int(stake_max)
        self.stake_unit = stake_unit
        self.stake_step = int(stake_step)
        self.bet_limit_id = int(bet_limit_id)
        self.max_rounds = int(max_rounds)
        self.enabled = bool(enabled)
        self._lock = threading.Lock()
        self._bet_keys: set[tuple[int, int]] = set()
        self.side_player_n = 0
        self.side_banker_n = 0
        self.bet_rounds = 0

    def on_round(self, kind: str, ev: dict[str, Any]) -> None:
        if not self.enabled:
            return
        if kind == "round_start":
            self._on_round_start(ev)
        elif kind == "round_result":
            self._on_round_result(ev)

    def _on_round_start(self, ev: dict[str, Any]) -> None:
        shoe = int(ev.get("shoe") or 0)
        rnd = int(ev.get("round") or 0)
        if shoe <= 0 or rnd <= 0:
            return
        key = (shoe, rnd)
        with self._lock:
            if key in self._bet_keys:
                return
            if self.max_rounds > 0 and self.bet_rounds >= self.max_rounds:
                return
            self._bet_keys.add(key)

        stake = pick_random_stake(
            self.stake_min,
            self.stake_max,
            unit=self.stake_unit,
            stake_step=self.stake_step,
        )
        with self._lock:
            side = pick_balanced_side(self.side_player_n, self.side_banker_n)
        side_vn = SIDE_VN.get(side, side)
        print(
            f"[ALLGAME][BET] Phiên mới shoe {shoe} ván {rnd} → "
            f"cược {stake} {side_vn} (idx {1 if side == 'player' else 3 if side == 'banker' else 2})",
            flush=True,
        )
        threading.Thread(
            target=self._place_async,
            args=(shoe, rnd, side, stake),
            daemon=True,
        ).start()

    def _place_async(self, shoe: int, rnd: int, side: str, stake: int) -> None:
        time.sleep(random.uniform(0.4, 1.2))
        out = place_bet_via_cdp(
            table_id=self.table_id,
            game_shoe=shoe,
            game_round=rnd,
            stake=stake,
            side=side,
            bet_limit_id=self.bet_limit_id,
            cdp_url=self.cdp_url,
        )
        ok = parse_bet_api_ok(out)
        side_vn = SIDE_VN.get(side, side)
        line = f"[ALLGAME][BET] Đặt {stake} {side_vn} ván {rnd}"
        if ok:
            with self._lock:
                self.bet_rounds += 1
                if side == "player":
                    self.side_player_n += 1
                elif side == "banker":
                    self.side_banker_n += 1
            line += " | OK"
        else:
            err = str(out.get("text") or out.get("error") or "")[:120]
            line += f" | FAIL: {err}"
        print(line, flush=True)

    def _on_round_result(self, ev: dict[str, Any]) -> None:
        rnd = int(ev.get("round") or 0)
        try:
            from allgame.vendor.c168_vendor_flow import _import_standalone

            _import_standalone()
            from c168_vendor_auto_bet import winner_label  # type: ignore

            w = winner_label(
                ev.get("winner"),
                player_val=ev.get("player_val"),
                banker_val=ev.get("banker_val"),
            )
        except Exception:
            w = str(ev.get("winner") or "?")
        print(f"[ALLGAME][BET] Kết quả ván {rnd}: {w}", flush=True)
