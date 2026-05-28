# -*- coding: utf-8 -*-
"""
Tự cược sau khi vào bàn: chờ GP_NEW_GAME_START → random Con/Cái, tiền 10–20.

Mặc định **10 hoặc 20** trên API (= 10k / 20k trên sảnh, site tính 1 = 1.000đ).
--stake-unit k --stake-min 10 --stake-max 20 --stake-step 10 → chỉ random 10 hoặc 20.

  python c168_vendor_auto_bet.py 600
  python c168_vendor_auto_bet.py 600 --table-id 1006 --stake-min 10 --stake-max 20

Log:
  Đặt cược 15 vào Con | Số dư sau cược: 133.24
  Kết quả ván 46: Cái thắng | Có KQ | Số dư: 153.24 (+20.00)
"""
from __future__ import annotations

import argparse
import random
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from c168_capture_game_b import CDP_URL, _cdp_alive
from c168_mex_protocol import _bikimex_fields, decode_mex_frame
from c168_vendor_bet import (
    find_vendor_tab,
    is_vendor_hall_redirect_url,
    is_vendor_playable_url,
    is_vendor_table_url,
    parse_bet_api_ok,
    place_bet_http,
    place_bet_via_cdp,
)
from c168_vendor_enter_table import DEFAULT_TABLE_ID, DEFAULT_TABLE_NAME
from c168_vendor_ws_sniff import adopt_or_create_sniffer, decode_frame, is_table_enter_message

SIDE_VN = {"player": "Con", "banker": "Cái", "tie": "Hòa"}
# Bikimex GP_WINNER (xác nhận capture): 1=Cái, 2=Con, 3=Hòa
WINNER_VN = {1: "Cái thắng", 2: "Con thắng", 3: "Hòa"}


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _fmt_money(v: float | None) -> str:
    if v is None:
        return "?"
    return f"{v:,.2f}".replace(",", " ")


def pick_random_stake(
    stake_min: int,
    stake_max: int,
    *,
    unit: str = "k",
    stake_step: int = 10,
) -> int:
    lo, hi = min(stake_min, stake_max), max(stake_min, stake_max)
    if unit == "k":
        # Số gửi API: 10 → ~10.000đ, 20 → ~20.000đ (không nhân 1000)
        step = max(1, int(stake_step))
        amounts = list(range(lo, hi + 1, step))
        return random.choice(amounts) if amounts else lo
    return random.randint(lo, hi)


def pick_balanced_side(player_n: int, banker_n: int) -> str:
    """Cân bằng Con/Cái — luôn chọn bên đang ít hơn (50/50 theo phiên đã cược)."""
    if player_n < banker_n:
        return "player"
    if banker_n < player_n:
        return "banker"
    return random.choice(["player", "banker"])


def _parse_balance_value(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def winner_label(
    winner: Any,
    *,
    player_val: Any = None,
    banker_val: Any = None,
) -> str:
    """Ưu tiên điểm bài (Con/Cái); fallback mã winner WS."""
    try:
        pv = int(player_val)
        bv = int(banker_val)
        if pv > bv:
            return "Con thắng"
        if bv > pv:
            return "Cái thắng"
        return "Hòa"
    except (TypeError, ValueError):
        pass
    try:
        w = int(winner)
    except (TypeError, ValueError):
        return f"KQ #{winner}"
    return WINNER_VN.get(w, f"KQ mã {w}")


@dataclass
class AutoBetState:
    table_id: int
    bet_limit_id: int
    stake_min: int
    stake_max: int
    stake_unit: str
    cdp_base: str
    stake_step: int = 10
    max_rounds: int = 0

    balance: float | None = None
    bet_rounds: int = 0
    results_logged: int = 0
    side_player_n: int = 0
    side_banker_n: int = 0
    _bet_keys: set[tuple[int, int]] = field(default_factory=set)
    _pending: dict[tuple[int, int], dict[str, Any]] = field(default_factory=dict)
    _deferred_results: dict[tuple[int, int], dict[str, Any]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _vendor_tab: dict[str, str] | None = None
    _vendor_tab_hint: str = ""
    headless_mode: bool = False
    headless_session: dict[str, Any] | None = None
    proxy_server: str = ""

    def _round_key(self, shoe: int, rnd: int) -> tuple[int, int]:
        return (shoe, rnd)

    def on_balance(self, bal: float) -> None:
        with self._lock:
            self.balance = bal

    def pending_bet_count(self) -> int:
        with self._lock:
            return sum(
                1
                for p in self._pending.values()
                if p.get("bet_ok") and not p.get("result_logged")
            )

    def _place_bet_async(
        self,
        *,
        shoe: int,
        rnd: int,
        side: str,
        stake: int,
    ) -> None:
        def _run() -> None:
            time.sleep(random.uniform(0.4, 1.8))
            tab = self._vendor_tab or find_vendor_tab(
                self.cdp_base,
                require_table=False,
                prefer_url_contains=self._vendor_tab_hint,
            )
            if not tab:
                tab = find_vendor_tab(self.cdp_base, require_table=False)
            if tab:
                self._vendor_tab = tab
            bal_before = self.balance
            if self.headless_mode and self.headless_session:
                sess = self.headless_session
                out = place_bet_http(
                    origin=str(sess.get("origin") or ""),
                    cookies=dict(sess.get("cookies") or {}),
                    table_id=self.table_id,
                    game_shoe=shoe,
                    game_round=rnd,
                    stake=stake,
                    side=side,
                    bet_limit_id=self.bet_limit_id,
                    referer=str(sess.get("referer") or ""),
                    proxy_server=self.proxy_server,
                )
                out["tab"] = "(headless-http)"
            else:
                out = place_bet_via_cdp(
                    table_id=self.table_id,
                    game_shoe=shoe,
                    game_round=rnd,
                    stake=stake,
                    side=side,
                    bet_limit_id=self.bet_limit_id,
                    cdp_base=self.cdp_base,
                    tab=tab,
                )
            side_vn = SIDE_VN.get(side, side)
            ok = parse_bet_api_ok(out)

            deadline = time.time() + 8
            bal_after = self.balance
            while time.time() < deadline:
                if self.balance is not None and self.balance != bal_before:
                    bal_after = self.balance
                    break
                time.sleep(0.25)

            line = (
                f"Đặt cược {stake} vào {side_vn} | "
                f"Số dư sau cược: {_fmt_money(bal_after)}"
            )
            if not ok:
                line += f" | ❌ API: {str(out.get('text') or out.get('error'))[:120]}"
            elif parsed := out.get("parsed"):
                if isinstance(parsed, dict) and parsed.get("balance") is not None:
                    b = _parse_balance_value(parsed.get("balance"))
                    if b is not None:
                        self.on_balance(b)
            tab_u = str(out.get("tab") or (tab or {}).get("url") or "")
            if tab_u and not is_vendor_playable_url(tab_u):
                line += f" | ⚠ tab: {tab_u[:70]}…"
            prefix = "[headless] " if self.headless_mode else ""
            print(f"[{_ts()}] {prefix}{line}", flush=True)

            deferred: dict[str, Any] | None = None
            with self._lock:
                key = self._round_key(shoe, rnd)
                entry = self._pending.get(key) or {}
                entry.update(
                    {
                        "side": side,
                        "side_vn": side_vn,
                        "stake": stake,
                        "balance_before": bal_before,
                        "balance_after_bet": bal_after,
                        "bet_ok": ok,
                        "awaiting_bet": False,
                        "api": out,
                        "result_logged": False,
                    }
                )
                self._pending[key] = entry
                if ok:
                    self.bet_rounds += 1
                    if side == "player":
                        self.side_player_n += 1
                    else:
                        self.side_banker_n += 1
                    deferred = self._deferred_results.pop(key, None)
                elif not ok:
                    print(
                        f"[{_ts()}] Cược ván {rnd} thất bại — bỏ qua chờ KQ ván này",
                        flush=True,
                    )
                    self._pending.pop(key, None)

            if ok and deferred:
                self._emit_round_result(
                    rnd,
                    pending=entry,
                    winner=deferred.get("winner"),
                    player_val=deferred.get("player_val"),
                    banker_val=deferred.get("banker_val"),
                )

        threading.Thread(target=_run, daemon=True).start()

    def on_round_start(self, shoe: int, rnd: int) -> None:
        key = self._round_key(shoe, rnd)
        with self._lock:
            if key in self._bet_keys:
                return
            if self.max_rounds > 0 and self.bet_rounds >= self.max_rounds:
                return
            self._bet_keys.add(key)
            self._pending[key] = {
                "awaiting_bet": True,
                "bet_ok": False,
                "result_logged": False,
            }

        stake = pick_random_stake(
            self.stake_min,
            self.stake_max,
            unit=self.stake_unit,
            stake_step=self.stake_step,
        )
        with self._lock:
            side = pick_balanced_side(self.side_player_n, self.side_banker_n)
        if self.stake_unit == "chip":
            unit_hint = " (chip)"
        else:
            unit_hint = f" (API {stake} ≈ {stake}k)"
        print(
            f"[{_ts()}] Phiên mới — bàn {self.table_id} shoe {shoe} ván {rnd}"
            f" → chuẩn bị cược {stake}{unit_hint} {SIDE_VN[side]}",
            flush=True,
        )
        self._place_bet_async(shoe=shoe, rnd=rnd, side=side, stake=stake)

    def _emit_round_result(
        self,
        rnd: int,
        *,
        pending: dict[str, Any],
        winner: Any,
        player_val: Any = None,
        banker_val: Any = None,
    ) -> None:
        bal = self.balance
        bal_before = pending.get("balance_before")
        delta_s = ""
        if bal is not None and bal_before is not None:
            d = bal - float(bal_before)
            delta_s = f" ({d:+.2f})"

        wlabel = winner_label(
            winner, player_val=player_val, banker_val=banker_val
        )
        extra = ""
        if player_val is not None and banker_val is not None:
            extra = f" (Con {player_val} — Cái {banker_val})"

        side_vn = pending.get("side_vn") or ""
        stake = pending.get("stake")
        bet_line = ""
        if side_vn and stake is not None:
            bet_line = f" | Cược {stake} {side_vn}"

        print(
            f"[{_ts()}] Kết quả ván {rnd}: {wlabel}{extra}{bet_line} | "
            f"Số dư: {_fmt_money(bal)}{delta_s}",
            flush=True,
        )
        with self._lock:
            self.results_logged += 1

    def on_round_result(
        self,
        shoe: int,
        rnd: int,
        winner: Any,
        *,
        player_val: Any = None,
        banker_val: Any = None,
    ) -> None:
        key = self._round_key(shoe, rnd)
        payload = {
            "winner": winner,
            "player_val": player_val,
            "banker_val": banker_val,
        }
        with self._lock:
            pending = self._pending.get(key)
            if not pending or not pending.get("bet_ok"):
                if pending and pending.get("awaiting_bet"):
                    self._deferred_results[key] = payload
                return
            if pending.get("result_logged"):
                return
            pending["result_logged"] = True
            self._pending.pop(key, None)

        self._emit_round_result(
            rnd,
            pending=pending,
            winner=winner,
            player_val=player_val,
            banker_val=banker_val,
        )


def _table_id_from_obj(obj: dict[str, Any]) -> int | None:
    b = _bikimex_fields(obj)
    tid = b.get("tableID")
    if tid is not None:
        try:
            return int(tid)
        except (TypeError, ValueError):
            pass
    for key in ("tableInfo", "message"):
        block = obj.get(key)
        if isinstance(block, dict):
            t2 = block.get("tableID")
            if t2 is not None:
                try:
                    return int(t2)
                except (TypeError, ValueError):
                    pass
            inner = block.get("tableInfo")
            if isinstance(inner, dict) and inner.get("tableID") is not None:
                try:
                    return int(inner["tableID"])
                except (TypeError, ValueError):
                    pass
    return None


def _extract_event(obj: dict[str, Any], table_id: int) -> dict[str, Any] | None:
    tid = _table_id_from_obj(obj)
    if tid is not None and int(tid) != int(table_id):
        return None
    b = _bikimex_fields(obj)
    et = b.get("eventType") or ""
    if not et and obj.get("messageType") == "UserBalance":
        try:
            return {"kind": "balance", "balance": float(obj.get("balance"))}
        except (TypeError, ValueError):
            return None
    if et not in ("GP_NEW_GAME_START", "GP_WINNER"):
        return None
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    shoe = int(b.get("gameShoe") or msg.get("gameShoe") or 0)
    rnd = int(b.get("gameRound") or msg.get("gameRound") or 0)
    if shoe <= 0 or rnd <= 0:
        return None
    return {
        "kind": "round_start" if et == "GP_NEW_GAME_START" else "round_result",
        "shoe": shoe,
        "round": rnd,
        "winner": msg.get("winner"),
        "player_val": msg.get("playerHandValue"),
        "banker_val": msg.get("bankerHandValue"),
    }


def _proxy_server_for_session(proxy: str, session: dict[str, Any]) -> str:
    p = (proxy or session.get("proxy") or "").strip()
    if not p:
        return ""
    from c168_proxy import ensure_local_http_relay

    return ensure_local_http_relay(p)


def _ws_headers_from_session(cap: dict[str, Any]) -> list[str]:
    from c168_vendor_ws_client import origin_from_ws_url

    hdr: list[str] = []
    h54 = str(cap.get("h54uk_url") or "")
    origin = origin_from_ws_url(h54) or str(cap.get("origin") or "").strip()
    if origin:
        hdr.append(f"Origin: {origin}")
    ref = str(cap.get("referer") or "").strip()
    if ref:
        hdr.append(f"Referer: {ref}")
    cookies = cap.get("cookies") or {}
    if isinstance(cookies, dict) and cookies:
        hdr.append("Cookie: " + "; ".join(f"{k}={v}" for k, v in cookies.items()))
    return hdr


_HEADLESS_BOOT: dict[str, Any] = {}


def _headless_wire_clients(
    cap: dict[str, Any],
    *,
    table_id: int,
    proxy: str,
    on_frame: Any,
) -> tuple[Any, Any, str]:
    """jk17y (cookie) → h54uk (+ initialization). Trả (h54, jk, lỗi)."""
    from c168_vendor_jk17y_client import DEFAULT_JK17Y_URL, headless_register_table
    from c168_vendor_ws_client import H54ukWsClient

    proxy_srv = _proxy_server_for_session(proxy, cap)
    uid = str(cap.get("user_id") or "").strip()
    cookies = cap.get("cookies") if isinstance(cap.get("cookies"), dict) else {}
    jk_url = str(cap.get("jk17y_url") or DEFAULT_JK17Y_URL)

    from c168_vendor_ws_client import origin_from_ws_url

    h54_url = str(cap.get("h54uk_url") or "")
    ws_origin = origin_from_ws_url(h54_url) or str(cap.get("origin") or "")

    jk_client, jk_out = headless_register_table(
        user_id=uid,
        table_id=table_id,
        h54uk_url=h54_url,
        origin=ws_origin,
        proxy_server=proxy_srv,
        jk17y_url=jk_url,
        cookies=cookies,
    )
    if not jk_out.get("ok"):
        if jk_client:
            jk_client.stop()
        return None, None, f"jk17y: {jk_out.get('error', '?')}"

    time.sleep(1.2)

    h54 = H54ukWsClient(
        h54_url,
        on_frame,
        proxy_server=proxy_srv,
        headers=_ws_headers_from_session(cap),
    )
    if not h54.start(retries=3):
        err = (h54._last_error or "không kết nối h54uk").strip()
        jk_client.stop()
        return None, None, f"h54uk: {err[:200]}"

    for _ in range(4):
        time.sleep(0.5)
        jk_client.send_table_click_again()
    return h54, jk_client, ""


def headless_sync_before_chrome_close(
    cap: dict[str, Any],
    *,
    table_id: int,
    proxy: str,
    timeout_sec: float = 40.0,
) -> bool:
    """Thử WS Python khi Chrome còn mở — xác nhận có event bàn trước khi tắt Chrome."""
    global _HEADLESS_BOOT
    _HEADLESS_BOOT.clear()
    table_hits = {"n": 0}

    def _count_frame(direction: str, url: str, data: str) -> None:
        if "h54uk" not in url.lower() or direction != "recv":
            return
        text = decode_frame(data)
        _, obj = decode_mex_frame(text if isinstance(text, str) else data)
        if not isinstance(obj, dict):
            return
        tid = _table_id_from_obj(obj)
        if tid == int(table_id):
            table_hits["n"] += 1

    h54, jk, err = _headless_wire_clients(
        cap, table_id=table_id, proxy=proxy, on_frame=_count_frame
    )
    if err:
        print(f"[{_ts()}] Đồng bộ headless: {err}", flush=True)
        return False
    print(
        f"[{_ts()}] Đồng bộ WS Python (Chrome vẫn mở, tối đa {int(timeout_sec)}s)…",
        flush=True,
    )
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        time.sleep(1.0)
        if table_hits["n"] > 0:
            print(
                f"[{_ts()}] Đã có {table_hits['n']} event bàn {table_id} — tắt Chrome.",
                flush=True,
            )
            _HEADLESS_BOOT["sync_ok"] = True
            _HEADLESS_BOOT["table_events"] = table_hits["n"]
            try:
                jk.stop()
                h54.stop()
            except Exception:
                pass
            return True
    print(
        f"[{_ts()}] Chưa thấy event bàn {table_id} ({h54.frame_count} frame) — "
        "vẫn thử headless sau khi tắt Chrome.",
        flush=True,
    )
    try:
        jk.stop()
        h54.stop()
    except Exception:
        pass
    return False


def run_auto_bet(
    duration_sec: int,
    *,
    table_id: int = DEFAULT_TABLE_ID,
    stake_min: int = 10,
    stake_max: int = 20,
    stake_unit: str = "k",
    stake_step: int = 10,
    bet_limit_id: int = 851101,
    max_rounds: int = 0,
    cdp_base: str = CDP_URL,
    sniffer: Any | None = None,
    vendor_tab_hint: str = "",
    survive_chrome_close: bool = False,
    proxy: str = "",
    headless_only: bool = False,
    vendor_session: dict[str, Any] | None = None,
) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    state = AutoBetState(
        table_id=table_id,
        bet_limit_id=bet_limit_id,
        stake_min=stake_min,
        stake_max=stake_max,
        stake_unit=stake_unit,
        stake_step=stake_step,
        cdp_base=cdp_base,
        max_rounds=max_rounds,
    )
    state._vendor_tab_hint = (vendor_tab_hint or "").strip()
    state.proxy_server = _proxy_server_for_session(proxy, {})
    if headless_only:
        print(
            "  Chế độ Python thuần: không mở Chrome vendor — chỉ WS + HTTP.\n",
            file=sys.stderr,
        )
    elif survive_chrome_close:
        print(
            "  --bet-without-chrome: tắt Chrome vẫn thử cược (HTTP + WS từ cache).\n",
            file=sys.stderr,
        )
    if headless_only:
        state._vendor_tab = None
    else:
        state._vendor_tab = find_vendor_tab(
            cdp_base,
            require_table=True,
            prefer_url_contains=state._vendor_tab_hint,
        )
        if not state._vendor_tab:
            state._vendor_tab = find_vendor_tab(cdp_base, require_table=False)
        if state._vendor_tab:
            u = state._vendor_tab.get("url", "")
            print(f"Tab cược: {u[:95]}", flush=True)
            if "webmain" in u.lower() and state._vendor_tab_hint:
                print(
                    "  (URL webMain + bàn trong iframe — WS GameInfo vẫn đúng bàn)",
                    flush=True,
                )
        else:
            print("Cảnh báo: chưa thấy tab vendor — vẫn nghe WS…", flush=True)
    ws_game_frames = 0
    ws_table_events = 0
    last_status_log = 0.0

    unit_desc = (
        f"{stake_min}–{stake_max} chip"
        if stake_unit == "chip"
        else f"10k hoặc 20k (API {stake_min}/{stake_max}, bội {stake_step})"
    )
    if duration_sec <= 0:
        dur_label = (
            "không giới hạn (Ctrl+C dừng)"
            if headless_only
            else "không giới hạn (tắt Chrome để dừng)"
        )
    else:
        dur_label = f"{duration_sec}s"
    print(
        f"\n══ Auto cược bàn {table_id} | {unit_desc} | Con/Cái random | {dur_label} ══\n",
        flush=True,
    )

    table_focused = False
    table_entered = False

    def on_frame(direction: str, url: str, data: str) -> None:
        nonlocal ws_game_frames, ws_table_events, last_status_log, table_focused, table_entered
        nonlocal headless_ws_table_events
        if "h54uk" not in url.lower():
            return
        if direction != "recv":
            return
        ws_game_frames += 1
        text = decode_frame(data)
        text2, obj = decode_mex_frame(text if isinstance(text, str) else data)
        if not isinstance(obj, dict):
            return

        if obj.get("messageType") == "UserBalance":
            bal = _parse_balance_value(obj.get("balance"))
            if bal is not None:
                state.on_balance(bal)
            return

        tid = _table_id_from_obj(obj)
        mt = str(obj.get("messageType") or "")
        handler = obj.get("handler")

        if tid == int(table_id):
            ws_table_events += 1
            if state.headless_mode:
                headless_ws_table_events += 1
            if not table_entered and is_table_enter_message(obj, table_id):
                table_entered = True
                ti = obj.get("tableInfo") if isinstance(obj.get("tableInfo"), dict) else {}
                tname = ti.get("tableName") or (
                    (obj.get("message") or {}).get("tableName")
                    if isinstance(obj.get("message"), dict)
                    else ""
                )
                h = obj.get("handler")
                print(
                    f"[{_ts()}] ★ Đã vào bàn — GameInfo h{h} "
                    f"{tname or table_id}",
                    flush=True,
                )
            if not table_focused and (
                mt == "GameInfo" or (mt == "GameHallInfo" and handler in (2, 4))
            ):
                table_focused = True
                print(
                    f"[{_ts()}] WS bàn {table_id} ({mt}) — "
                    f"chờ GP_NEW_GAME_START…",
                    flush=True,
                )

        ev = _extract_event(obj, table_id)
        if (
            not ev
            and tid == int(table_id)
            and mt in ("GameInfo", "GameHallInfo")
            and state.headless_mode
            and headless_ws_table_events <= 2
        ):
            b = _bikimex_fields(obj)
            et_dbg = b.get("eventType") or ""
            if et_dbg:
                print(
                    f"[{_ts()}] [headless] bàn {table_id} {mt} h{handler} "
                    f"eventType={et_dbg}",
                    flush=True,
                )
        if not ev:
            now = time.time()
            if now - last_status_log >= 45 and ws_game_frames > 50:
                last_status_log = now
                print(
                    f"[{_ts()}] WS: {ws_game_frames} frame | bàn {table_id}: {ws_table_events} event | "
                    f"số dư {_fmt_money(state.balance)} | "
                    f"Con/Cái {state.side_player_n}/{state.side_banker_n} | "
                    f"KQ {state.results_logged}/{state.bet_rounds} | chờ phiên…",
                    flush=True,
                )
            return
        if ev["kind"] == "round_start":
            state.on_round_start(ev["shoe"], ev["round"])
        elif ev["kind"] == "round_result":
            state.on_round_result(
                ev["shoe"],
                ev["round"],
                ev.get("winner"),
                player_val=ev.get("player_val"),
                banker_val=ev.get("banker_val"),
            )

    headless_client: Any = None
    headless_jk_client: Any = None
    chrome_gone_handled = False
    last_headless_lobby = 0.0
    headless_ws_table_events = 0

    if headless_only:
        from c168_vendor_session_cache import h54uk_jwt_expires_in, load_vendor_session

        cap = vendor_session if vendor_session and vendor_session.get("ok") else load_vendor_session()
        if not cap or not cap.get("ok"):
            print(
                "Chưa có session cache. Chạy:\n"
                "  python c168_login_open_game.py -u USER -p PASS --proxy \"...\" "
                "--auto-bet --headless-play\n",
                file=sys.stderr,
            )
            return 2
        ttl = h54uk_jwt_expires_in(str(cap.get("h54uk_url") or ""))
        if ttl < 8:
            print(
                f"[{_ts()}] Token h54uk hết hạn hoặc sắp hết ({int(ttl)}s) — "
                "chạy lại login --headless-play (kéo captcha).",
                flush=True,
            )
            return 2
        state.headless_mode = True
        state.headless_session = cap
        if _HEADLESS_BOOT.get("sync_ok"):
            print(
                f"[{_ts()}] Đã xác nhận WS bàn lúc còn Chrome — kết nối lại headless…",
                flush=True,
            )
        headless_client, headless_jk_client, err = _headless_wire_clients(
            cap, table_id=table_id, proxy=proxy, on_frame=on_frame
        )
        if err:
            print(f"[{_ts()}] Headless lỗi: {err}", flush=True)
            return 1
        u = str(cap.get("user_id") or "")[-10:]
        print(
            f"[{_ts()}] Headless OK — jk17y + h54uk (user …{u}) | "
            f"{headless_client.frame_count} frame",
            flush=True,
        )
        sniffer = None
    else:
        try:
            sniffer = adopt_or_create_sniffer(cdp_base, on_frame, existing=sniffer)
        except RuntimeError:
            print("Lỗi: Chrome CDP không chạy.", file=sys.stderr)
            return 1

    if sniffer and sniffer.h54uk_urls:
        print(
            f"[{_ts()}] h54uk: {len(sniffer.h54uk_urls)} kết nối | "
            f"{sniffer.h54uk_frame_count} frame",
            flush=True,
        )
    if sniffer and (sniffer.h54uk_frame_count > 0 or sniffer.h54uk_urls):
        print(
            f"[{_ts()}] WS h54uk OK — {sniffer.h54uk_frame_count} frame "
            f"(sảnh đã có h54uk; vào bàn = GameInfo, không F5)",
            flush=True,
        )
    elif sniffer and not sniffer.wait_h54uk(25):
        print(
            f"[{_ts()}] Chưa bắt được h54uk — giữ tab bàn, đợi vài giây (không reload). "
            "Nếu đã vào bàn: vẫn chờ GameInfo GP_NEW_GAME_START…",
            flush=True,
        )
    else:
        print(f"[{_ts()}] WS h54uk OK", flush=True)

    if survive_chrome_close and sniffer and sniffer.h54uk_urls:
        from c168_vendor_session_cache import capture_vendor_session

        capture_vendor_session(
            cdp_base=cdp_base,
            h54uk_url=str(sniffer.h54uk_urls[-1]),
            proxy=proxy,
            tab_hint=state._vendor_tab_hint,
        )
        print(f"[{_ts()}] Đã lưu session cache (cho lúc tắt Chrome).", flush=True)

    last_h54uk_activity = time.time()
    last_virtual_maintain = 0.0
    last_session_save = 0.0

    def _h54uk_frame_count() -> int:
        if headless_client is not None:
            return int(headless_client.frame_count)
        return sniffer.h54uk_frame_count if sniffer else 0

    def _watchdog_tick(*, hall_reenter: bool) -> None:
        nonlocal last_h54uk, warned, last_hall_warn, last_reenter_try, table_entered
        nonlocal last_h54uk_activity, last_virtual_maintain
        if state.headless_mode:
            return
        h54uk_n = sniffer.h54uk_frame_count
        if h54uk_n > last_h54uk:
            last_h54uk = h54uk_n
            last_h54uk_activity = time.time()
            warned = False

        vis = find_vendor_tab(
            cdp_base,
            require_table=False,
            prefer_url_contains=state._vendor_tab_hint,
        )
        if vis:
            vurl = vis.get("url") or ""
            if is_vendor_playable_url(vurl):
                state._vendor_tab = vis
                if hall_reenter and time.time() - last_virtual_maintain > 120:
                    last_virtual_maintain = time.time()
                    from c168_vendor_virtual_table import maintain_virtual_table

                    vm = maintain_virtual_table(table_id, cdp_base=cdp_base)
                    if vm.get("ok"):
                        pass
                    elif time.time() - last_hall_warn > 60:
                        print(
                            f"[{_ts()}] Giữ bàn ảo: {str(vm.get('error') or vm)[:80]}",
                            flush=True,
                        )
            elif hall_reenter and is_vendor_hall_redirect_url(vurl):
                if time.time() - last_hall_warn > 15:
                    last_hall_warn = time.time()
                    print(
                        f"[{_ts()}] ⚠ Mất bàn (Welcome Back) — Back To Game + vào lại C06…",
                        flush=True,
                    )
                if time.time() - last_reenter_try > 30:
                    last_reenter_try = time.time()
                    from c168_vendor_keepalive import recover_vendor_session_via_cdp

                    re = recover_vendor_session_via_cdp(
                        table_name=DEFAULT_TABLE_NAME,
                        table_id=table_id,
                        cdp_base=cdp_base,
                    )
                    if re.get("ok"):
                        table_entered = True
                        print(
                            f"[{_ts()}] Đã vào lại bàn: "
                            f"{str(re.get('table_url') or '')[:90]}",
                            flush=True,
                        )
                        state._vendor_tab = find_vendor_tab(
                            cdp_base,
                            require_table=False,
                            prefer_url_contains=state._vendor_tab_hint,
                        )
                    else:
                        print(f"[{_ts()}] Vào lại bàn thất bại: {re}", flush=True)
            elif hall_reenter and (
                is_vendor_playable_url(vurl)
                and h54uk_n > 0
                and time.time() - last_h54uk_activity > 90
            ):
                if time.time() - last_hall_warn > 20:
                    last_hall_warn = time.time()
                    print(
                        f"[{_ts()}] ⚠ WS h54uk im ~90s — có thể tab idle, thử khôi phục…",
                        flush=True,
                    )
                if time.time() - last_reenter_try > 45:
                    last_reenter_try = time.time()
                    from c168_vendor_keepalive import inject_anti_idle_all
                    from c168_vendor_virtual_table import maintain_virtual_table

                    inject_anti_idle_all(cdp_base)
                    re = maintain_virtual_table(table_id, cdp_base=cdp_base)
                    if re.get("ok"):
                        print(f"[{_ts()}] Giữ bàn ảo (lobbyTableClick) OK", flush=True)

        elif hall_reenter and time.time() - t0 > 25 and h54uk_n == 0 and not warned:
            print(
                f"  … {int(time.time()-t0)}s — chưa có WS h54uk / chưa vào bàn?",
                flush=True,
            )
            warned = True

    t0 = time.time()
    last_h54uk = _h54uk_frame_count()
    warned = False
    last_hall_warn = 0.0
    last_reenter_try = 0.0

    def _try_switch_headless() -> bool:
        nonlocal headless_client, headless_jk_client, chrome_gone_handled, last_h54uk
        nonlocal last_headless_lobby, headless_ws_table_events, table_focused, table_entered
        if chrome_gone_handled or not survive_chrome_close:
            return False
        chrome_gone_handled = True
        h54_url = ""
        if sniffer and sniffer.h54uk_urls:
            h54_url = str(sniffer.h54uk_urls[-1])
        from c168_vendor_session_cache import capture_vendor_session

        from c168_vendor_session_cache import load_vendor_session

        cap = load_vendor_session() or {}
        if not cap.get("ok") and _cdp_alive():
            cap = capture_vendor_session(
                cdp_base=cdp_base,
                h54uk_url=h54_url,
                proxy=proxy,
                tab_hint=state._vendor_tab_hint,
            )
        if not cap or not cap.get("cookies", {}).get("JSESSIONID") or not cap.get(
            "h54uk_url"
        ):
            print(
                f"\n[{_ts()}] Chrome tắt — không đủ session cache "
                f"(cookie/JSESSIONID hoặc URL h54uk). Không cược headless.",
                flush=True,
            )
            return False
        try:
            sniffer.stop()
        except Exception:
            pass
        print(
            f"\n[{_ts()}] ★ Chrome tắt — chuyển headless: HTTP cược + WS Python "
            f"({str(cap.get('origin') or '')[:40]}…)",
            flush=True,
        )
        headless_client, headless_jk_client, err = _headless_wire_clients(
            cap, table_id=table_id, proxy=proxy, on_frame=on_frame
        )
        if err:
            print(f"[{_ts()}] {err}", flush=True)
            chrome_gone_handled = False
            return False
        state.headless_mode = True
        state.headless_session = cap
        state.proxy_server = _proxy_server_for_session(proxy, cap)
        headless_ws_table_events = 0
        table_focused = False
        table_entered = False
        last_headless_lobby = time.time()
        n0 = headless_client.frame_count
        print(
            f"[{_ts()}] WS headless OK — {n0} frame | chờ GameInfo / GP_NEW_GAME_START…",
            flush=True,
        )
        last_h54uk = n0
        return True

    try:
        while duration_sec <= 0 or time.time() - t0 < duration_sec:
            time.sleep(5)
            if headless_only or state.headless_mode:
                h54uk_n = _h54uk_frame_count()
                if h54uk_n > last_h54uk:
                    last_h54uk = h54uk_n
                    last_h54uk_activity = time.time()
                now = time.time()
                if (
                    headless_jk_client
                    and headless_ws_table_events == 0
                    and now - last_headless_lobby > 75
                ):
                    last_headless_lobby = now
                    if headless_jk_client.send_table_click_again():
                        print(
                            f"[{_ts()}] [headless] Gửi lại lobbyTableClick "
                            f"(chưa có event bàn {table_id})",
                            flush=True,
                        )
                if now - last_status_log >= 20:
                    last_status_log = now
                    hint = ""
                    if h54uk_n == 0:
                        hint = " | chưa có frame — token/proxy?"
                    elif headless_ws_table_events == 0:
                        hint = f" | có frame sảnh, chưa event bàn {table_id}"
                    print(
                        f"[{_ts()}] [headless] WS {h54uk_n} frame | "
                        f"bàn {headless_ws_table_events} event | "
                        f"số dư {_fmt_money(state.balance)} | "
                        f"KQ {state.results_logged}/{state.bet_rounds}{hint}",
                        flush=True,
                    )
            elif not _cdp_alive():
                if not _try_switch_headless():
                    if not survive_chrome_close:
                        print(
                            f"\n[{_ts()}] Chrome đã tắt — dừng auto-bet.",
                            flush=True,
                        )
                        break
            elif sniffer:
                vis_tab = find_vendor_tab(
                    cdp_base,
                    require_table=False,
                    prefer_url_contains=state._vendor_tab_hint,
                )
                if (
                    not vis_tab
                    and _h54uk_frame_count() > 0
                    and time.time() - last_h54uk_activity > 20
                ):
                    if survive_chrome_close and _try_switch_headless():
                        continue
                    if time.time() - last_h54uk_activity > 90:
                        print(
                            f"[{_ts()}] Tab Game B đã đóng — WS im. "
                            "Chạy lại: python c168_login_open_game.py ... --headless-play",
                            flush=True,
                        )
                if time.time() - last_session_save > 40 and sniffer:
                    last_session_save = time.time()
                    from c168_vendor_session_cache import capture_vendor_session

                    h54 = str(sniffer.h54uk_urls[-1]) if sniffer.h54uk_urls else ""
                    capture_vendor_session(
                        cdp_base=cdp_base,
                        h54uk_url=h54,
                        proxy=proxy,
                        tab_hint=state._vendor_tab_hint,
                    )
                _watchdog_tick(hall_reenter=True)
    except KeyboardInterrupt:
        print(f"\n[{_ts()}] Dừng auto-bet (Ctrl+C) — thoát ngay.", flush=True)

    if sniffer and not state.headless_mode:
        sniffer.stop()
    if headless_jk_client is not None:
        headless_jk_client.stop()
    if headless_client is not None:
        headless_client.stop()
    still = state.pending_bet_count()
    print(
        f"\n=== Xong auto bet: {state.bet_rounds} lần cược | "
        f"{state.results_logged} kết quả | số dư cuối {_fmt_money(state.balance)} ===",
        flush=True,
    )
    if still > 0:
        print(
            f"⚠ {still} ván chưa có KQ (đã dừng sớm).",
            flush=True,
        )
    if _cdp_alive():
        print(
            "Chrome vẫn dùng proxy local — relay KHÔNG tắt khi còn Chrome port 9340.\n",
            file=sys.stderr,
        )
    else:
        if state.headless_mode:
            print(
                "Chrome đã đóng — auto-bet headless (HTTP + WS Python). "
                "Relay proxy local vẫn chạy trong process này.\n",
                file=sys.stderr,
            )
        else:
            print(
                "Chrome đã đóng — không còn cược/WS. Relay proxy local có thể vẫn chạy (tắt process Python để dừng).\n",
                file=sys.stderr,
            )
    if not state.bet_rounds:
        return 2
    if still > 0 or state.bet_rounds > state.results_logged:
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Auto cược vendor C168 (WS + HTTP)")
    ap.add_argument(
        "duration",
        nargs="?",
        type=int,
        default=0,
        help="Giây chạy (0 = mãi, Ctrl+C dừng). Ví dụ: 600",
    )
    ap.add_argument("--table-id", type=int, default=DEFAULT_TABLE_ID)
    ap.add_argument("--stake-min", type=int, default=10)
    ap.add_argument("--stake-max", type=int, default=20)
    ap.add_argument(
        "--stake-unit",
        choices=("chip", "k"),
        default="k",
        help="k: API 10/20 (=10k/20k); chip: số chip nhỏ",
    )
    ap.add_argument(
        "--stake-step",
        type=int,
        default=10,
        help="Bội khi --stake-unit k (10,20 → chỉ cược 10 hoặc 20 API)",
    )
    ap.add_argument("--bet-limit", type=int, default=851101)
    ap.add_argument("--max-rounds", type=int, default=0, help="0 = không giới hạn")
    ap.add_argument("--cdp", default=CDP_URL)
    ap.add_argument(
        "--bet-without-chrome",
        action="store_true",
        help="Tắt Chrome vẫn thử cược qua HTTP+WS (session cache)",
    )
    args = ap.parse_args()
    return run_auto_bet(
        args.duration,
        table_id=args.table_id,
        stake_min=args.stake_min,
        stake_max=args.stake_max,
        stake_unit=args.stake_unit,
        stake_step=args.stake_step,
        bet_limit_id=args.bet_limit,
        max_rounds=args.max_rounds,
        cdp_base=args.cdp,
        survive_chrome_close=args.bet_without_chrome,
    )


if __name__ == "__main__":
    raise SystemExit(main())
