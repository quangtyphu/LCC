# -*- coding: utf-8 -*-
"""
Auto-spin PG Soft (Wild Bounty Showdown / wild-bounty-sd).

Luồng mỗi ván:
  1. POST Spin (id mới, tb > 0) — trừ cược
  2. While dt.si.nst != 1: POST Spin (id = sid bước trước) — cascade

Cấu hình: copy pgsoft_config.example.json → pgsoft_config.json
  - atk, cookies (__cf_bm), referer: copy từ DevTools sau khi mở game

Chạy:
  python pgsoft_spin.py
  python pgsoft_spin.py --config pgsoft_config.json --rounds 10
"""

from __future__ import annotations

import argparse
import json
import random
import re
import string
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    from curl_cffi import requests as http
    _IMPERSONATE = "chrome120"
except ImportError:
    import requests as http  # type: ignore
    _IMPERSONATE = None

NST_IDLE = 1
_SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = _SCRIPT_DIR / "pgsoft_config.json"
EXAMPLE_CONFIG = _SCRIPT_DIR / "pgsoft_config.example.json"


def _trace_id() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=8))


def _new_round_id() -> str:
    # Giống id dạng số lớn trên browser (ms * 1e6 + random)
    return str(int(time.time() * 1000) * 1_000_000 + random.randint(100_000, 999_999))


def _looks_placeholder(value: str) -> bool:
    u = value.strip().upper()
    if not u or "PASTE" in u:
        return True
    for bad in (
        "UUID_MOI",
        "COOKIE_DAY_DU",
        "URL_DAY_DU",
        "YOUR_OT",
        "PASTE_ATK",
        "PASTE_FROM",
    ):
        if bad in u:
            return True
    if "..." in value or value.strip() in ("...", "…"):
        return True
    return False


def _valid_atk(value: str) -> bool:
    return bool(re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", value, re.I))


def _otk_from_referer(referer: str) -> str | None:
    q = parse_qs(urlparse(referer).query)
    for key in ("ot", "otk"):
        vals = q.get(key)
        if vals and vals[0]:
            return vals[0]
    return None


def _dig_atk(obj: Any) -> str | None:
    if isinstance(obj, dict):
        for key in ("atk", "tk"):
            val = obj.get(key)
            if isinstance(val, str) and _valid_atk(val):
                return val
        for v in obj.values():
            found = _dig_atk(v)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _dig_atk(item)
            if found:
                return found
    return None


class PgSoftSpinClient:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.base_url = cfg["base_url"].rstrip("/")
        self.game_slug = cfg["game_slug"]
        self.atk = cfg["atk"]
        self.cookies = cfg.get("cookies") or {}
        self.referer = cfg.get("referer") or f"{self.base_url}/135/index.html"
        self.spin_fields = {
            "cs": cfg.get("cs", 0.02),
            "ml": cfg.get("ml", 1),
            "sn": cfg.get("sn", 1),
            "wk": cfg.get("wk", "0_C"),
            "fb": str(cfg.get("fb", False)).lower(),
            "btt": cfg.get("btt", 1),
            "pf": cfg.get("pf", 1),
        }
        self._session = self._make_session()
        self._last_sid: str | None = None
        self._last_request_id: str | None = None
        self.game_id = int(cfg.get("game_id", 135))

    def _make_session(self):
        if _IMPERSONATE:
            s = http.Session(impersonate=_IMPERSONATE)
        else:
            s = http.Session()
        return s

    def _headers(self) -> dict[str, str]:
        return {
            "accept": "*/*",
            "accept-language": "vi,en-US;q=0.9,en;q=0.8",
            "content-type": "application/x-www-form-urlencoded",
            "origin": self.base_url,
            "referer": self.referer,
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        }

    def _api_url(self, resource: str) -> str:
        return (
            f"{self.base_url}/server/game-api/{self.game_slug}/v2/{resource}"
            f"?traceId={_trace_id()}"
        )

    def _post_form(self, resource: str, data: dict[str, Any]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "url": self._api_url(resource),
            "headers": self._headers(),
            "data": data,
            "cookies": self.cookies,
            "timeout": 30,
        }
        resp = self._session.post(**kwargs)
        resp.raise_for_status()
        body = resp.json()
        if body.get("err"):
            raise RuntimeError(f"API err: {body['err']}")
        return body

    def refresh_atk(self, otk: str | None = None) -> str:
        """Lấy atk mới từ verifyOperatorPlayerSession (cần otk trong referer hoặc --otk)."""
        otk = (otk or _otk_from_referer(self.referer) or "").strip()
        if not otk or _looks_placeholder(otk):
            raise RuntimeError(
                "Không có otk. referer phải chứa ?ot=... (copy nguyên URL tab game), "
                "hoặc truyền --otk."
            )
        url = (
            f"{self.base_url}/server/web-api/auth/session/v2/"
            f"verifyOperatorPlayerSession?traceId={_trace_id()}"
        )
        data = {
            "btt": 1,
            "vc": 2,
            "pf": 1,
            "l": "vi",
            "gi": self.game_id,
            "os": 1,
            "otk": otk,
        }
        resp = self._session.post(
            url,
            headers=self._headers(),
            data=data,
            cookies=self.cookies,
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("err"):
            raise RuntimeError(f"verify session: {body['err']}")
        atk = _dig_atk(body.get("dt"))
        if not atk:
            raise RuntimeError(f"verify session OK nhưng không tìm thấy atk trong response: {body}")
        self.atk = atk
        print(f"  atk mới: {atk}", flush=True)
        return atk

    def get_game_info(self) -> dict[str, Any]:
        data = {
            "btt": self.spin_fields["btt"],
            "atk": self.atk,
            "pf": self.spin_fields["pf"],
        }
        return self._post_form("GameInfo/Get", data)

    @staticmethod
    def _find_last_spin(dt: dict[str, Any]) -> dict[str, Any] | None:
        if not dt:
            return None
        ls = dt.get("ls")
        if isinstance(ls, dict):
            si = ls.get("si")
            if isinstance(si, dict) and ("nst" in si or "sid" in si):
                return si
        for key in ("si", "lts", "lastSpin"):
            val = dt.get(key)
            if isinstance(val, dict) and ("nst" in val or "sid" in val):
                return val
        return None

    def _next_paid_spin_id(self) -> str:
        """id spin trả phí = sid response ván trước, hoặc ls.sid từ GameInfo (không random)."""
        if self._last_sid and str(self._last_sid).isdigit():
            return str(self._last_sid)
        try:
            gi = self.get_game_info()
            ls = self._find_last_spin(gi.get("dt") or {})
            if ls:
                sid = str(ls.get("sid") or "")
                if sid.isdigit():
                    return sid
        except Exception as e:
            print(f"  GameInfo (id): {e}", flush=True)
        return _new_round_id()

    def _post_spin(self, spin_id: str) -> dict[str, Any]:
        data = {**self.spin_fields, "id": spin_id, "atk": self.atk}
        return self._post_form("Spin", data)

    def _advance_until_idle(self, spin_id: str, label: str = "") -> list[dict[str, Any]]:
        """Gọi Spin lặp đến nst==1 (resume cascade / đóng round dở)."""
        steps: list[dict[str, Any]] = []
        prefix = f"{label} " if label else ""
        while True:
            body = self._post_spin(spin_id)
            si = (body.get("dt") or {}).get("si") or {}
            steps.append(si)
            nst = int(si.get("nst", NST_IDLE))
            sid = si.get("sid")
            print(
                f"  {prefix}step i={si.get('i')} st={si.get('st')} nst={nst} "
                f"tb={si.get('tb')} tw={si.get('tw')} bl={si.get('bl')}",
                flush=True,
            )
            if sid:
                self._last_sid = str(sid)
            if nst == NST_IDLE:
                return steps
            if not sid:
                raise RuntimeError("nst != 1 nhưng không có sid")
            spin_id = str(sid)
            time.sleep(0.3)

    def sync_session(self) -> None:
        """GameInfo + đóng round dở trên server (nếu có)."""
        try:
            gi = self.get_game_info()
        except Exception as e:
            print(f"  GameInfo lỗi (bỏ qua): {e}", flush=True)
            return
        dt = gi.get("dt") or {}
        bl = dt.get("bl")
        if bl is not None:
            print(f"  GameInfo balance: {bl}", flush=True)
        ls = self._find_last_spin(dt)
        if not ls:
            return
        nst = int(ls.get("nst", NST_IDLE))
        sid = ls.get("sid")
        if nst != NST_IDLE and sid:
            print(f"  Round dở trên server (nst={nst}) — đóng cascade...", flush=True)
            self._advance_until_idle(str(sid), label="[resume]")
        elif nst == NST_IDLE and int(ls.get("st") or 0) == 4:
            # Đã idle nhưng ls vẫn lưu bước cascade cuối — không resume
            pass

    def _is_paid_spin_step(self, si: dict[str, Any], *, first_step: bool) -> bool:
        tb = float(si.get("tb") or 0)
        if tb > 0:
            return True
        if not first_step:
            return False
        i = int(si.get("i") or 0)
        st = int(si.get("st") or 0)
        # Chỉ bước cascade cuối: i>=1, st=4, tb=0 — không phải quay mới
        if i > 0 or st == 4:
            return False
        return tb > 0

    def play_one_round(self) -> dict[str, Any]:
        """Một lần bấm quay: spin trả phí + cascade đến nst==1."""
        steps: list[dict[str, Any]] = []
        spin_id = self._next_paid_spin_id()
        total_bet = 0.0
        total_win = 0.0
        bl_start = None
        bl_end = None
        paid_ok = False

        while True:
            body = self._post_spin(spin_id)
            si = (body.get("dt") or {}).get("si") or {}
            steps.append(si)
            self._last_request_id = spin_id

            tb = float(si.get("tb") or 0)
            tw = float(si.get("tw") or 0)
            total_bet += tb
            total_win += tw

            if bl_start is None:
                bl_start = si.get("blb", si.get("bl"))
            bl_end = si.get("bl", si.get("blab"))

            nst = int(si.get("nst", NST_IDLE))
            sid = si.get("sid")
            if sid:
                self._last_sid = str(sid)

            first = len(steps) == 1
            if first and not self._is_paid_spin_step(si, first_step=True):
                raise RuntimeError(
                    "Không phải spin trả phí (tb=0, có vẻ cascade/round cũ). "
                    "Mở game trên browser, chơi hết animation, copy atk + __cf_bm MỚI, "
                    "hoặc chạy lại — script sẽ gọi sync_session() lúc khởi động."
                )
            if tb > 0:
                paid_ok = True

            print(
                f"  step i={si.get('i')} st={si.get('st')} nst={nst} tb={tb} tw={tw} "
                f"blb={si.get('blb')} bl={si.get('bl')} id={spin_id[-8:]} ssaw={si.get('ssaw')}",
                flush=True,
            )

            if nst == NST_IDLE:
                break
            if not sid:
                raise RuntimeError("nst != 1 nhưng không có sid — không thể cascade")
            spin_id = str(sid)
            time.sleep(0.3)

        if not paid_ok:
            raise RuntimeError("Ván không có bước trả phí (tb>0)")

        return {
            "steps": len(steps),
            "total_bet": total_bet,
            "total_win": total_win,
            "net": total_win - total_bet,
            "bl_start": bl_start,
            "bl_end": bl_end,
            "ssaw": steps[-1].get("ssaw") if steps else 0,
        }


def resolve_config_path(path: Path) -> Path:
    """Tìm config: đường dẫn tuyệt đối, cwd, hoặc cạnh pgsoft_spin.py."""
    if path.is_file():
        return path.resolve()
    tried = [path]
    if not path.is_absolute():
        tried.extend([Path.cwd() / path, _SCRIPT_DIR / path, _SCRIPT_DIR / path.name])
    for p in tried:
        if p.is_file():
            return p.resolve()
    if path.name == "pgsoft_config.json" or path == DEFAULT_CONFIG:
        if EXAMPLE_CONFIG.is_file() and not DEFAULT_CONFIG.is_file():
            DEFAULT_CONFIG.write_text(EXAMPLE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"Đã tạo {DEFAULT_CONFIG} từ file mẫu — hãy sửa atk + cookies.", flush=True)
            return DEFAULT_CONFIG
    raise FileNotFoundError(
        f"Không thấy config: {path}\n"
        f"  Chạy: copy {_SCRIPT_DIR / 'pgsoft_config.example.json'} "
        f"{DEFAULT_CONFIG}\n"
        f"  Rồi điền atk + __cf_bm từ DevTools (Network → Spin)."
    )


def load_config(path: Path) -> dict[str, Any]:
    path = resolve_config_path(path)
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        description="PG Soft auto-spin (cascade loop)",
        epilog=(
            "Ví dụ:\n"
            "  python pgsoft_spin.py --atk UUID --cf-bm COOKIE_VALUE --rounds 5\n"
            "Hoặc sửa pgsoft_standalone/pgsoft_config.json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--rounds", type=int, default=0, help="0 = chạy vô hạn")
    ap.add_argument("--delay", type=float, default=None, help="Giây giữa mỗi ván (override config)")
    ap.add_argument("--atk", type=str, default=None, help="Token atk (Form Data của request Spin)")
    ap.add_argument("--cf-bm", type=str, default=None, help="Cookie __cf_bm")
    ap.add_argument("--referer", type=str, default=None, help="Header Referer từ request Spin")
    ap.add_argument("--otk", type=str, default=None, help="Operator token (?ot= trong URL game)")
    ap.add_argument(
        "--refresh-atk",
        action="store_true",
        help="Gọi verifyOperatorPlayerSession để lấy atk mới (cần otk + cookie)",
    )
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.atk:
        cfg["atk"] = args.atk.strip()
    if args.cf_bm:
        cfg.setdefault("cookies", {})["__cf_bm"] = args.cf_bm.strip()
    if args.referer:
        cfg["referer"] = args.referer.strip()

    delay = args.delay if args.delay is not None else float(cfg.get("delay_seconds", 5))
    max_rounds = args.rounds or int(cfg.get("max_rounds", 0))

    atk = str(cfg.get("atk") or "").strip()
    referer = str(cfg.get("referer") or "").strip()
    cf = str((cfg.get("cookies") or {}).get("__cf_bm") or "").strip()

    if _looks_placeholder(referer) or not referer.startswith("http"):
        print(
            "referer không hợp lệ — copy NGUYÊN URL tab game, ví dụ:\n"
            "  https://v100.pgsoft.gold/135/index.html?ot=abc123....&l=vi&btt=1&ops=1\n"
            "Không dùng chữ URL_DAY_DU / UUID_MOI (đó chỉ là ví dụ trong hướng dẫn).",
            file=sys.stderr,
        )
        return 1
    if _looks_placeholder(cf):
        print(
            "Cookie __cf_bm không hợp lệ — copy đầy đủ từ Chrome → Application → Cookies "
            "(không có dấu ...).",
            file=sys.stderr,
        )
        return 1

    client = PgSoftSpinClient(cfg)
    need_refresh = args.refresh_atk or _looks_placeholder(atk) or not _valid_atk(atk)
    if need_refresh:
        print("Lấy atk mới (verifyOperatorPlayerSession)...", flush=True)
        try:
            client.refresh_atk(args.otk)
        except Exception as e:
            print(f"Lỗi refresh atk: {e}", file=sys.stderr)
            print(
                "Cách khác: mở game → Network → Spin → copy atk + __cf_bm + referer thật.",
                file=sys.stderr,
            )
            return 1
    elif _looks_placeholder(atk):
        print("atk không hợp lệ — dùng UUID thật hoặc --refresh-atk", file=sys.stderr)
        return 1

    print(f"Base: {client.base_url} | game: {client.game_slug} | delay: {delay}s", flush=True)
    try:
        gi0 = client.get_game_info()
        bl0 = float((gi0.get("dt") or {}).get("bl") or 0)
        print(f"  Số dư GameInfo: {bl0}", flush=True)
        bet = float(cfg.get("cs", 0.02)) * float(cfg.get("ml", 1)) * 20
        if bl0 < bet:
            print(
                f"  Số dư ({bl0}) thấp hơn cược ~{bet} — nạp thêm hoặc mở game trên Chrome "
                "(URL + cookie mới nếu ot hết hạn).",
                flush=True,
            )
    except Exception as e:
        print(f"  GameInfo: {e}", flush=True)

    print("Đồng bộ phiên (GameInfo)...", flush=True)
    client.sync_session()

    n = 0
    try:
        while True:
            n += 1
            print(f"\n=== Round {n} ===", flush=True)
            try:
                summary = client.play_one_round()
                try:
                    bl_now = float((client.get_game_info().get("dt") or {}).get("bl") or 0)
                except Exception:
                    bl_now = summary["bl_end"]
                print(
                    f"  → steps={summary['steps']} bet={summary['total_bet']} "
                    f"win={summary['total_win']} net={summary['net']:.2f} "
                    f"bl {summary['bl_start']} → {summary['bl_end']} "
                    f"(GameInfo: {bl_now})",
                    flush=True,
                )
            except Exception as e:
                err_s = str(e)
                print(f"  Lỗi round {n}: {e}", flush=True)
                if "1302" in err_s or "Invalid player session" in err_s:
                    print(
                        "  Phiên hết hạn (atk/otk/cookie). Mở game trên Chrome, copy referer + "
                        "__cf_bm mới, chạy lại với --refresh-atk",
                        flush=True,
                    )
                    break
                if "403" in err_s or "401" in err_s or "cf" in err_s.lower():
                    print("  Có thể hết cookie Cloudflare — copy lại từ browser.", flush=True)

            if max_rounds and n >= max_rounds:
                break
            print(f"  Chờ {delay}s...", flush=True)
            time.sleep(delay)
    except KeyboardInterrupt:
        print("\nDừng (Ctrl+C).", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
