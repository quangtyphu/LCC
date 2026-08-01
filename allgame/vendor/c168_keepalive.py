# -*- coding: utf-8 -*-
"""Giữ trạng thái ở bàn C168 — anti-idle + lobbyTableClick định kỳ + recover Welcome Back."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any


def _import_standalone() -> None:
    d = Path(__file__).resolve().parents[2] / "c168_standalone"
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))


def tick_table_keepalive(
    *,
    cdp_url: str,
    table_id: int,
    table_name: str,
    state: dict[str, Any],
    maintain_interval_sec: float = 90.0,
    anti_idle_interval_sec: float = 300.0,
    recover_cooldown_sec: float = 45.0,
) -> None:
    """
    Gọi định kỳ từ ws_reporter (mỗi ~20s).
    state keys: last_maintain, last_anti_idle, last_recover_try, last_ws_recv (unix time).
    """
    _import_standalone()
    from c168_vendor_bet import (  # type: ignore
        find_vendor_tab,
        is_vendor_hall_redirect_url,
        is_vendor_playable_url,
    )

    base = (cdp_url or "").rstrip("/")
    now = time.time()
    tab = find_vendor_tab(base, require_table=False)
    if not tab:
        return

    vurl = str(tab.get("url") or "")
    last_ws = float(state.get("last_ws_recv") or 0)

    if is_vendor_hall_redirect_url(vurl):
        last_recover = float(state.get("last_recover_try") or 0)
        if now - last_recover >= recover_cooldown_sec:
            state["last_recover_try"] = now
            from c168_vendor_keepalive import recover_vendor_session_via_cdp  # type: ignore

            print(
                f"[ALLGAME][KEEP] Mất bàn (Welcome Back) — khôi phục {table_name}…",
                flush=True,
            )
            re = recover_vendor_session_via_cdp(
                table_name=table_name,
                table_id=int(table_id),
                cdp_base=base,
            )
            if re.get("ok"):
                print(
                    f"[ALLGAME][KEEP] Đã vào lại bàn — {str(re.get('method') or 'ok')}",
                    flush=True,
                )
            else:
                print(
                    f"[ALLGAME][KEEP] Khôi phục thất bại: {str(re.get('error') or re)[:100]}",
                    flush=True,
                )
        return

    if not is_vendor_playable_url(vurl):
        return

    last_anti = float(state.get("last_anti_idle") or 0)
    if now - last_anti >= anti_idle_interval_sec:
        state["last_anti_idle"] = now
        from c168_vendor_keepalive import inject_anti_idle_all  # type: ignore

        n = inject_anti_idle_all(base)
        if n:
            print(f"[ALLGAME][KEEP] Anti-idle inject {n} tab vendor", flush=True)

    last_maintain = float(state.get("last_maintain") or 0)
    ws_stale = last_ws > 0 and (now - last_ws) > 75.0
    due_maintain = (now - last_maintain) >= maintain_interval_sec
    if due_maintain or ws_stale:
        state["last_maintain"] = now
        from c168_vendor_virtual_table import maintain_virtual_table  # type: ignore

        vm = maintain_virtual_table(int(table_id), cdp_base=base)
        lobby_err = ""
        if isinstance(vm.get("lobbyView"), dict):
            lobby_err = str(vm["lobbyView"].get("error") or "")
        if lobby_err == "no_open_jk17y" or str(vm.get("error") or "") == "no_open_jk17y":
            return
        if vm.get("ok"):
            if ws_stale:
                print(
                    "[ALLGAME][KEEP] WS im lâu — gửi lobbyTableClick giữ bàn",
                    flush=True,
                )
        elif due_maintain and now - float(state.get("last_maintain_warn") or 0) > 120:
            state["last_maintain_warn"] = now
            print(
                f"[ALLGAME][KEEP] maintain_virtual_table: "
                f"{str(vm.get('error') or vm)[:80]}",
                flush=True,
            )
