# -*- coding: utf-8 -*-
"""
Mở game play.3dbenbet.net sau khi login.

  python benbet_open_game.py -u USER -p PASS
  python benbet_open_game.py -u USER -p PASS --game-id 14001 --json
  python benbet_open_game.py -u USER -p PASS --list-games
"""
from __future__ import annotations

import argparse
import json
import sys
import webbrowser

from benbet_game import (
    GAME_TAI_XIU_CAN_BANG,
    HOST_3D,
    TAIXIU_WS_EVENTS,
    fetch_game_catalog,
    open_tai_xiu_session,
)
from benbet_login import login


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    p = argparse.ArgumentParser(description="Mở game BEN 3D (play.3dbenbet.net)")
    p.add_argument("-u", "--username", required=True)
    p.add_argument("-p", "--password", required=True)
    p.add_argument("--game-id", default=GAME_TAI_XIU_CAN_BANG, help="Menu id (Tài Xỉu Cân Bảng=14001)")
    p.add_argument("--list-games", action="store_true", help="In catalog từ game_data")
    p.add_argument("--open-browser", action="store_true", help="Mở launch URL trên trình duyệt")
    p.add_argument("--json", action="store_true")
    p.add_argument("--proxy", default="", help="SOCKS5 host:port hoặc host:port:user:pass")
    args = p.parse_args(argv)
    proxy = (args.proxy or "").strip() or None

    if args.list_games:
        r = login(args.username, args.password, proxy=proxy)
        if not r.get("ok"):
            print(f"Login fail: {r.get('message')}")
            return 1
        import requests
        from benbet_proxy import BenbetProxy

        http_sess = requests.Session()
        bp = BenbetProxy.from_string(proxy)
        if bp:
            bp.mount_session(http_sess)
        games = fetch_game_catalog(str(r["lt"]), session=http_sess)
        if args.json:
            print(json.dumps(games, ensure_ascii=False, indent=2))
        else:
            for g in games:
                if "tài" in (g.get("name") or "").lower() or "xiu" in (g.get("name") or "").lower():
                    print(f"  {g['id']:>6}  gamecode={g.get('gamecode')}  {g.get('name')}")
        return 0

    out = open_tai_xiu_session(
        args.username, args.password, game_menu_id=args.game_id, proxy=proxy
    )
    if not out.get("ok"):
        print(f"Thất bại: {out.get('login', {}).get('message')}")
        return 1

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print("Launch URL:")
        print(out["launch_url"])
        if out.get("ws_url"):
            print("\nWebSocket (sau negotiate):")
            print(out["ws_url"][:120] + "...")
        elif out.get("negotiate_error"):
            print(f"\nNegotiate lỗi (có thể DNS tai.{HOST_3D}): {out['negotiate_error']}")
        print("\nSự kiện WS Tài Xỉu:", ", ".join(TAIXIU_WS_EVENTS.keys()))

    if args.open_browser:
        webbrowser.open(out["launch_url"])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
