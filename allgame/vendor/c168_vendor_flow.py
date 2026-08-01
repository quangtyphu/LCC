# -*- coding: utf-8 -*-
"""Luồng C168 Game B — copy c168_post_open_chrome + c168_listen_ws (vào bàn + nghe phiên)."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Callable

from allgame.vendor.config import vendor_table_cfg

_SNIFFERS_BY_CDP: dict[str, Any] = {}


def _standalone_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "c168_standalone"


def _import_standalone() -> Path:
    d = _standalone_dir()
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))
    return d


def get_listen_sniffer(cdp_url: str | None = None) -> Any | None:
    base = (cdp_url or "").strip().rstrip("/")
    if base:
        sn = _SNIFFERS_BY_CDP.get(base)
        if sn is not None:
            return sn
        return None
    if len(_SNIFFERS_BY_CDP) == 1:
        return next(iter(_SNIFFERS_BY_CDP.values()))
    return None


def _register_sniffer(cdp_url: str, sniffer: Any) -> None:
    base = (cdp_url or "").strip().rstrip("/")
    if base and sniffer is not None:
        _SNIFFERS_BY_CDP[base] = sniffer


def release_listen_sniffer(cdp_url: str | None = None) -> None:
    """Dừng sniffer khi đóng Chrome — tránh frame lẫn account."""
    base = (cdp_url or "").strip().rstrip("/")
    if not base:
        return
    sn = _SNIFFERS_BY_CDP.pop(base, None)
    if sn is None:
        return
    try:
        sn.stop()
    except Exception:
        pass


def _has_round_signal(state: dict[str, Any]) -> bool:
    """Đã thấy PHIEN MOI hoặc KET QUA PHIEN trên WS bàn."""
    if int(state.get("round_events") or 0) > 0:
        return True
    if state.get("ws_new_round_event") or state.get("ws_round_result_event"):
        return True
    return False


def _wait_ws_table_ready(
    sniffer: Any,
    state: dict[str, Any],
    *,
    timeout_sec: float,
    label: str,
) -> bool:
    try:
        sniffer.wait_h54uk(min(35.0, timeout_sec * 0.5))
        sniffer.reinject_ws_hooks()
        sniffer._attach_existing_targets()
    except Exception:
        pass
    deadline = time.time() + max(5.0, float(timeout_sec))
    while time.time() < deadline:
        if state.get("ws_table_session_ok") or int(state.get("round_events") or 0) > 0:
            return True
        time.sleep(0.35)
    print(
        f"[ALLGAME][C168] {label} — chưa có WS phiên bàn "
        f"(recv={state.get('recv')} parse={state.get('parsed')})",
        flush=True,
    )
    return False


def _wait_round_events(
    sniffer: Any,
    state: dict[str, Any],
    *,
    timeout_sec: float,
    label: str,
) -> bool:
    """Chờ tối đa timeout_sec cho PHIEN MOI / KET QUA trên bàn."""
    try:
        sniffer.wait_h54uk(min(25.0, timeout_sec * 0.25))
        sniffer.reinject_ws_hooks()
        sniffer._attach_existing_targets()
    except Exception:
        pass
    deadline = time.time() + max(5.0, float(timeout_sec))
    last_attach = 0.0
    last_status = 0.0
    while time.time() < deadline:
        if _has_round_signal(state):
            return True
        now = time.time()
        recv = int(state.get("recv") or 0)
        if recv == 0 and now - last_attach >= 12.0:
            last_attach = now
            try:
                sniffer.reinject_ws_hooks()
                sniffer._attach_existing_targets()
            except Exception:
                pass
        if now - last_status >= 25.0:
            last_status = now
            h54 = int(getattr(sniffer, "h54uk_frame_count", 0) or 0)
            print(
                f"[ALLGAME][C168] {label} — đang chờ phiên… "
                f"recv={recv} parse={state.get('parsed')} "
                f"h54uk_frames={h54} sniffer_frames={getattr(sniffer, 'frame_count', 0)}",
                flush=True,
            )
        time.sleep(0.35)
    print(
        f"[ALLGAME][C168] {label} — hết {timeout_sec:.0f}s, chưa có PHIEN MOI/KET QUA "
        f"(recv={state.get('recv')} events={state.get('round_events')})",
        flush=True,
    )
    return False


def _prime_sniffer_cdp(
    cdp_url: str,
    on_frame: Callable[[str, str, str], None],
) -> Any:
    """
    Bật CDP sniffer TRƯỚC vào bàn — giống c168_post_open_chrome.prime_ws_capture.
    Hook WS phải có trước khi Game B mở socket h54uk.
    """
    _import_standalone()
    from c168_vendor_ws_sniff import BrowserCdpSniffer  # type: ignore

    base = cdp_url.rstrip("/")
    existing = get_listen_sniffer(base)
    if existing is not None:
        ex_base = str(getattr(existing, "cdp_base", "") or "").rstrip("/")
        if ex_base and ex_base != base:
            release_listen_sniffer(base)
            existing = None
        elif not getattr(existing, "_ws", None):
            release_listen_sniffer(base)
            existing = None

    if existing is not None:
        existing.on_frame = on_frame
        sniffer = existing
    else:
        sniffer = BrowserCdpSniffer(base, on_frame)
        if not sniffer.start():
            raise RuntimeError(f"CDP sniffer không start: {base}")
    _register_sniffer(base, sniffer)
    sniffer.reinject_ws_hooks()
    sniffer._attach_existing_targets()
    time.sleep(0.5)
    return sniffer


def _refresh_sniffer_after_enter(
    sniffer: Any,
    cdp_url: str,
    on_frame: Callable[[str, str, str], None],
    *,
    table_id: int,
    table_name: str,
) -> Any:
    """Sau click vào bàn — gắn lại target/iframe + fake_enter (post_open_chrome)."""
    _import_standalone()
    from c168_vendor_keepalive import inject_anti_idle_all  # type: ignore
    from c168_vendor_virtual_table import fake_enter_table_via_cdp  # type: ignore

    base = cdp_url.rstrip("/")
    sniffer.on_frame = on_frame
    _register_sniffer(base, sniffer)
    try:
        inject_anti_idle_all(base)
        fake_enter_table_via_cdp(table_id, cdp_base=base)
    except Exception:
        pass
    sniffer.reinject_ws_hooks()
    sniffer._attach_existing_targets()
    time.sleep(0.6)
    sniffer.reset_h54uk_counters()
    if sniffer.h54uk_urls:
        print(
            f"[ALLGAME][C168] h54uk: {len(sniffer.h54uk_urls)} kết nối | "
            f"frames={sniffer.h54uk_frame_count}",
            flush=True,
        )
    elif not sniffer.wait_h54uk(20):
        print(
            f"[ALLGAME][C168] Chưa bắt h54uk sau vào {table_name} — gắn lại target…",
            flush=True,
        )
        sniffer.reinject_ws_hooks()
        sniffer._attach_existing_targets()
        sniffer.wait_h54uk(10)
    return sniffer


def _ensure_hall_a_page(context: Any, hall: Any, cfg: dict[str, Any]) -> Any:
    """Focus tab sảnh C168 (sảnh A) — không phải tab vendor Game B."""
    homes = cfg.get("portal_home_urls") or {}
    home = str(homes.get("c168") or "https://c168b2.cc/")
    candidate = hall
    for pg in context.pages:
        ul = (pg.url or "").lower()
        if any(k in ul for k in ("c168b2.cc", "c1686.net", "c168f.com")) and not any(
            k in ul for k in ("bpcdf.", "mhuxu", "tgmeq", "intplaynet", "singlebactable")
        ):
            candidate = pg
            break
    try:
        candidate.bring_to_front()
    except Exception:
        pass
    ul = (candidate.url or "").lower()
    if any(k in ul for k in ("bpcdf.", "intplaynet", "singlebactable")):
        try:
            candidate.goto(home, wait_until="domcontentloaded", timeout=60_000)
            candidate.wait_for_timeout(1500)
        except Exception:
            pass
    return candidate


def make_listen_on_frame(
    *,
    table_id: int,
    table_name: str,
    log_prefix: str = "[ALLGAME][WS]",
    state: dict[str, Any] | None = None,
    on_round: Callable[[str, dict[str, Any]], None] | None = None,
) -> tuple[Callable[[str, str, str], None], dict[str, Any]]:
    """Giống hệt c168_listen_ws._make_on_frame — decode mex + _extract_event."""
    _import_standalone()
    from c168_mex_protocol import decode_mex_frame, extract_baccarat_event, table_id_from_obj  # type: ignore
    from c168_vendor_auto_bet import _extract_event, _table_id_from_obj, winner_label  # type: ignore
    from c168_vendor_ws_sniff import decode_frame  # type: ignore

    tid_filter = int(table_id or 0)
    st: dict[str, Any] = state if state is not None else {}
    st.setdefault("recv", 0)
    st.setdefault("parsed", 0)
    st.setdefault("ws_table_frame_ok", False)
    st.setdefault("ws_table_session_ok", False)
    st.setdefault("ws_streaming_info_ok", False)
    st.setdefault("ws_table_session_sample", "")
    st.setdefault("ws_new_round_event", "")
    st.setdefault("ws_round_result_event", "")
    st.setdefault("ws_frame_samples", [])
    st.setdefault("ws_urls", [])
    st.setdefault("round_events", 0)
    seen_round: set[tuple[Any, ...]] = set(st.get("seen_round") or set())
    st["seen_round"] = seen_round
    table_focus_printed = bool(st.get("table_focus_printed"))

    def _mark_sample(text: str) -> None:
        if not st.get("ws_table_session_sample"):
            st["ws_table_session_sample"] = text[:320]

    def on_frame(direction: str, url: str, data: str) -> None:
        nonlocal table_focus_printed
        if "h54uk" not in str(url or "").lower() or direction != "recv":
            return
        st["recv"] = int(st.get("recv") or 0) + 1
        st["last_ws_recv"] = time.time()
        urls = st.setdefault("ws_urls", [])
        if url and len(urls) < 5:
            urls.append(str(url))

        text = decode_frame(data)
        _, obj = decode_mex_frame(text if isinstance(text, str) else data)
        if not isinstance(obj, dict):
            return
        st["parsed"] = int(st.get("parsed") or 0) + 1

        tid_seen = _table_id_from_obj(obj) or table_id_from_obj(obj)
        mt = str(obj.get("messageType") or "")
        handler = obj.get("handler")
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        et = str(msg.get("eventType") or "")

        if tid_filter > 0 and tid_seen == tid_filter:
            st["ws_table_frame_ok"] = True
            shoe = msg.get("gameShoe")
            rnd = msg.get("gameRound")
            if shoe is not None and rnd is not None:
                st["ws_table_session_ok"] = True
                _mark_sample(str(text or "")[:320])
            if not table_focus_printed and mt in ("GameInfo", "GameHallInfo"):
                table_focus_printed = True
                st["table_focus_printed"] = True
                print(
                    f"{log_prefix} ★ WS bàn {tid_filter} ({table_name}, {mt} h{handler})",
                    flush=True,
                )
            if et == "GP_NEW_GAME_START" and not st.get("ws_new_round_event"):
                st["ws_new_round_event"] = str(text or "")[:320]
            if et in ("GP_WINNER", "GP_RESULT") and not st.get("ws_round_result_event"):
                st["ws_round_result_event"] = str(text or "")[:320]

        ev = _extract_event(obj, tid_filter) if tid_filter > 0 else None
        if not ev and tid_filter > 0:
            ev = extract_baccarat_event(obj, table_id=tid_filter)
        if not ev or ev.get("kind") not in ("round_start", "round_result"):
            return

        dedupe_key = (
            str(ev.get("kind")),
            int(ev.get("shoe") or 0),
            int(ev.get("round") or 0),
            tid_filter or tid_seen,
        )
        if dedupe_key in seen_round:
            return
        seen_round.add(dedupe_key)
        st["round_events"] = int(st.get("round_events") or 0) + 1

        if ev.get("kind") == "round_start":
            print(
                f"{log_prefix} PHIEN MOI | bàn {tid_filter or tid_seen} "
                f"shoe {ev.get('shoe')} ván {ev.get('round')}",
                flush=True,
            )
        else:
            w = winner_label(
                ev.get("winner"),
                player_val=ev.get("player_val"),
                banker_val=ev.get("banker_val"),
            )
            extra = ""
            if ev.get("player_val") is not None and ev.get("banker_val") is not None:
                extra = f" (Con {ev['player_val']} — Cái {ev['banker_val']})"
            print(
                f"{log_prefix} KET QUA PHIEN | bàn {tid_filter or tid_seen} "
                f"ván {ev.get('round')}: {w}{extra}",
                flush=True,
            )
        if on_round:
            on_round(str(ev.get("kind") or ""), ev)

    return on_frame, st


def attach_c168_ws_listener(
    cdp_url: str,
    *,
    table_id: int,
    table_name: str,
    log_prefix: str = "[ALLGAME][WS]",
    on_frame: Callable[[str, str, str], None] | None = None,
    existing_sniffer: Any | None = None,
) -> Any:
    """Gắn callback nghe phiên — tái dùng sniffer đúng CDP của account."""
    base = cdp_url.rstrip("/")
    if on_frame is None:
        on_frame, _ = make_listen_on_frame(
            table_id=table_id,
            table_name=table_name,
            log_prefix=log_prefix,
        )
    prior = existing_sniffer or get_listen_sniffer(base)
    if prior is not None:
        prior.on_frame = on_frame
        _register_sniffer(base, prior)
        try:
            prior.reinject_ws_hooks()
            prior._attach_existing_targets()
            prior.reset_h54uk_counters()
        except Exception:
            pass
        return prior
    return _prime_sniffer_cdp(base, on_frame)


def connect_c168_vendor_ws(
    account: dict[str, Any],
    *,
    chrome: dict[str, Any],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """
    Gọi trực tiếp c168_post_open_chrome.run_post_open_chrome (standalone đã chạy ổn).
    Chrome do chrome_transport mở sẵn — launch_chrome=False.
    """
    user = str(account.get("username") or "").strip()
    cdp_url = str(chrome.get("cdp_url") or "").strip().rstrip("/")
    if not cdp_url:
        return {"ok": False, "error": "missing_cdp_url"}

    _import_standalone()
    try:
        from c168_capture_game_b import configure_chrome_session  # type: ignore
        from c168_chrome_session import chrome_session_from_override  # type: ignore
        from c168_post_open_chrome import run_post_open_chrome  # type: ignore
        from c168_vendor_ws_sniff import _ACTIVE_SNIFFER  # type: ignore
    except Exception as e:
        return {"ok": False, "error": f"import_c168_standalone_failed:{e}"}

    vc = vendor_table_cfg(cfg)
    table_name = str(vc.get("table_name") or "C06")
    table_id = int(vc.get("table_id") or 1006)
    round_wait = int(vc.get("ws_round_wait_sec") or 120)
    profile_dir = str(chrome.get("profile_dir") or account.get("chrome_browser_dir") or "")
    try:
        from urllib.parse import urlparse

        cdp_port = int(urlparse(cdp_url).port or 0)
    except (TypeError, ValueError):
        cdp_port = 0

    cms = chrome_session_from_override(
        username=user,
        profile_dir=profile_dir,
        cdp_port=cdp_port,
        proxy=str(account.get("proxy") or ""),
    )
    if cms is None:
        return {"ok": False, "error": "chrome_session_override_failed"}

    configure_chrome_session(cdp_url, profile_dir, cms=True)

    print(
        f"[ALLGAME][C168] {user} | run_post_open_chrome "
        f"(bàn {table_name}, nghe {round_wait}s)…",
        flush=True,
    )

    bettor = None
    if vc.get("auto_bet_enabled"):
        from allgame.vendor.c168_auto_bet import C168AutoBettor

        bettor = C168AutoBettor(
            cdp_url=cdp_url,
            table_id=table_id,
            stake_min=int(vc.get("stake_min") or 10),
            stake_max=int(vc.get("stake_max") or 20),
            stake_unit=str(vc.get("stake_unit") or "k"),
            stake_step=int(vc.get("stake_step") or 10),
            bet_limit_id=int(vc.get("bet_limit_id") or 851101),
            max_rounds=int(vc.get("max_bet_rounds") or 0),
            enabled=True,
        )

    on_frame = None
    if vc.get("auto_bet_enabled") and bettor is not None:

        def _on_round(kind: str, ev: dict[str, Any]) -> None:
            bettor.on_round(kind, ev)

        on_frame, _ = make_listen_on_frame(
            table_id=table_id,
            table_name=table_name,
            log_prefix="[ALLGAME][WS]",
            on_round=_on_round,
        )

    post_kw: dict[str, Any] = {
        "username": user,
        "launch_chrome": False,
        "chrome_session": cms,
        "cdp_wait_sec": 2.0,
        "table_name": table_name,
        "table_id": table_id,
        "enter_table_timeout_sec": int(vc.get("enter_table_timeout_sec") or 90),
    }

    if bettor is not None:
        post_out = run_post_open_chrome(
            **post_kw,
            auto_bet=True,
            auto_bet_sec=round_wait,
            stake_min=int(vc.get("stake_min") or 10),
            stake_max=int(vc.get("stake_max") or 20),
            stake_unit=str(vc.get("stake_unit") or "k"),
            stake_step=int(vc.get("stake_step") or 10),
            bet_limit_id=int(vc.get("bet_limit_id") or 851101),
            max_bet_rounds=int(vc.get("max_bet_rounds") or 0),
        )
    else:
        post_out = run_post_open_chrome(
            **post_kw,
            listen_sec=round_wait,
        )

    enter_out = post_out.get("enter_table") if isinstance(post_out.get("enter_table"), dict) else {}
    enter_ok = bool(enter_out.get("ok"))

    if bettor is not None:
        round_events = 1 if post_out.get("ok") and enter_ok else 0
        recv_frames = round_events
    else:
        stats = post_out.get("listen_stats") if isinstance(post_out.get("listen_stats"), dict) else {}
        round_events = int(stats.get("round_events") or 0)
        recv_frames = int(stats.get("recv") or 0)
    ws_round_ok = round_events > 0

    # listen_vendor_ws gọi sniffer.stop() — gắn lại cho reporter / phiên tiếp theo
    sniffer = _ACTIVE_SNIFFER
    if sniffer is not None and getattr(sniffer, "_ws", None):
        _register_sniffer(cdp_url, sniffer)
    elif on_frame is not None:
        try:
            attach_c168_ws_listener(
                cdp_url,
                table_id=table_id,
                table_name=table_name,
                on_frame=on_frame,
            )
        except Exception:
            pass
    elif not bettor:
        on_frame, reporter_state = make_listen_on_frame(
            table_id=table_id,
            table_name=table_name,
            log_prefix="[ALLGAME][WS]",
        )
        try:
            attach_c168_ws_listener(
                cdp_url,
                table_id=table_id,
                table_name=table_name,
                on_frame=on_frame,
            )
        except Exception:
            pass

    ws_connected = recv_frames > 0 or ws_round_ok
    ready = bool(post_out.get("ok") and enter_ok and ws_round_ok)

    if ready:
        print(
            f"[ALLGAME][C168] {user} | OK post_open — phiên/KQ={round_events} "
            f"recv≈{recv_frames}",
            flush=True,
        )
    else:
        print(
            f"[ALLGAME][C168] {user} | post_open xong nhưng chưa có phiên "
            f"(enter={enter_ok} events={round_events} action={post_out.get('action')})",
            flush=True,
        )

    return {
        "ok": ready,
        "ready_to_bet": ready,
        "ws_connected": ws_connected,
        "ws_table_frame_ok": ws_round_ok,
        "ws_table_session_ok": ws_round_ok,
        "ws_streaming_info_ok": False,
        "ws_table_session_sample": "",
        "ws_new_round_event": "",
        "ws_round_result_event": "",
        "enter_table_ok": enter_ok,
        "enter_table_method": str(enter_out.get("method") or ""),
        "table_name": table_name,
        "table_id": table_id,
        "ws_urls": [],
        "final_page_url": str(enter_out.get("url") or ""),
        "ws_frame_samples": [],
        "sniff_impl": "c168_post_open_chrome.run_post_open_chrome",
        "enter_detail": enter_out,
        "open_game": post_out.get("open_game"),
        "parsed_frames": recv_frames,
        "round_events": round_events,
        "keepalive_state": {"last_ws_recv": time.time() if ws_round_ok else 0},
        "ws_round_ok": ws_round_ok,
        "recovery_log": [],
        "post_open": post_out,
    }
