# -*- coding: utf-8 -*-
"""Lưu quỹ hũ mini-game từ WS (snapshot JSON + lịch sử JSONL)."""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DIR = Path(__file__).resolve().parent
_DEFAULT_STATE = _DIR / "data" / "minigame_jackpots.json"
_DEFAULT_HISTORY = _DIR / "data" / "minigame_jackpot_history.jsonl"
_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _parse_money(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(str(val).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


class MinigameJackpotStore:
    """Ghi jackpot_money theo game_id — đọc được từ worker/API khác."""

    def __init__(
        self,
        state_path: Path | str | None = None,
        *,
        history_path: Path | str | None = None,
        write_history: bool = True,
    ) -> None:
        self.state_path = Path(
            state_path or os.environ.get("XOSO66_JACKPOT_STATE", _DEFAULT_STATE)
        )
        self.history_path = Path(
            history_path or os.environ.get("XOSO66_JACKPOT_HISTORY", _DEFAULT_HISTORY)
        )
        self.write_history = write_history

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {"by_game": {}, "updated_at": None}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {"by_game": {}, "updated_at": None}

    def load(self) -> dict[str, Any]:
        with _LOCK:
            return self._read_state()

    def get_money(self, game_id: int) -> float | None:
        row = self.load().get("by_game", {}).get(str(game_id)) or {}
        return _parse_money(row.get("money"))

    def record(
        self,
        game_id: int,
        money: Any,
        *,
        game_name: str = "",
        ws_account: str = "",
        group_id: Any = None,
        source: str = "jackpot_money",
    ) -> bool:
        """Trả True nếu ghi file (số tiền đổi so với lần trước)."""
        amount = _parse_money(money)
        if amount is None:
            return False

        gid = str(int(game_id))
        with _LOCK:
            data = self._read_state()
            by_game = dict(data.get("by_game") or {})
            prev = by_game.get(gid) or {}
            prev_money = _parse_money(prev.get("money"))
            if prev_money is not None and prev_money == amount:
                return False

            at = _now_iso()
            entry = {
                "game_id": int(game_id),
                "name": game_name or prev.get("name") or "",
                "money": amount,
                "money_raw": str(money),
                "group_id": group_id,
                "source": source,
                "updated_at": at,
                "prev_money": prev_money,
            }
            by_game[gid] = entry
            out = {
                "updated_at": at,
                "ws_account": ws_account or data.get("ws_account") or "",
                "by_game": by_game,
            }
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            if self.write_history:
                hist = {
                    "at": at,
                    "game_id": int(game_id),
                    "money": amount,
                    "prev_money": prev_money,
                    "ws_account": ws_account or "",
                    "source": source,
                }
                self.history_path.parent.mkdir(parents=True, exist_ok=True)
                with self.history_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(hist, ensure_ascii=False) + "\n")
            return True


_default_store: MinigameJackpotStore | None = None


def get_jackpot_store() -> MinigameJackpotStore:
    global _default_store
    if _default_store is None:
        _default_store = MinigameJackpotStore()
    return _default_store


def _main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Xem / test lưu jackpot mini-game")
    ap.add_argument("--file", default="", help="đường dẫn minigame_jackpots.json")
    ap.add_argument("--tail", type=int, default=10, help="dòng history gần nhất")
    args = ap.parse_args()
    store = MinigameJackpotStore(args.file or None)
    print(json.dumps(store.load(), ensure_ascii=False, indent=2))
    if store.history_path.is_file() and args.tail > 0:
        lines = store.history_path.read_text(encoding="utf-8").strip().splitlines()
        print("\n--- history ---")
        for line in lines[-args.tail :]:
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
