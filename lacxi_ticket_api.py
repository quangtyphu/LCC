import sys
import io
import json
import time

# Fix encoding cho Windows console
if sys.platform == 'win32':
    import os
    os.system('chcp 65001 > nul')

from curl_cffi import requests
from gift_box_api import auto_claim_gifts
from game_api_helper import (
    get_user_auth,
    build_proxies,
    build_common_params,
    build_common_headers,
    refresh_jwt_and_token,
)

LACXI_TICKET_URL = "https://gameapi.tele68.com/v1/lacxi/ticket"


def _build_lacxi_request(username: str):
    auth = get_user_auth(username)
    if not auth:
        print(f"❌ [{username}] Không lấy được auth info", flush=True)
        return None, None, None

    proxy_str, jwt, access_token, _ = auth
    proxies = build_proxies(proxy_str)

    headers = build_common_headers(jwt)
    headers.update({
        "accept-language": "vi-VN,vi;q=0.9,fr-FR;q=0.8,fr;q=0.7,en-US;q=0.6,en;q=0.5",
        "origin": "https://lc79b.bet",
        "referer": "https://lc79b.bet/",
    })

    params = build_common_params(access_token)
    return headers, params, proxies


def _request_lacxi_ticket(username: str, timeout: int = 20):
    headers, params, proxies = _build_lacxi_request(username)
    if headers is None:
        return None

    try:
        return requests.get(
            LACXI_TICKET_URL,
            params=params,
            headers=headers,
            proxies=proxies,
            timeout=timeout,
            impersonate="chrome120",
        )
    except Exception as e:
        print(f"❌ [{username}] Lỗi request: {e}", flush=True)
        return None


def _request_lacxi_open(username: str, timeout: int = 20):
    headers, params, proxies = _build_lacxi_request(username)
    if headers is None:
        return None

    try:
        return requests.put(
            LACXI_TICKET_URL,
            params=params,
            headers=headers,
            json={},
            proxies=proxies,
            timeout=timeout,
            impersonate="chrome120",
        )
    except Exception as e:
        print(f"❌ [{username}] Lỗi request: {e}", flush=True)
        return None


def fetch_lacxi_ticket(username: str) -> dict:
    """
    Gọi API lacxi/ticket và trả về response.
    """
    resp = _request_lacxi_ticket(username)

    if resp is None:
        return {"ok": False, "error": "Không gọi được API lacxi ticket"}

    # Auto-retry nếu token hết hạn
    if resp.status_code in (401, 403):
        if refresh_jwt_and_token(username):
            resp = _request_lacxi_ticket(username)
        else:
            return {"ok": False, "error": "Không refresh được token"}

    if resp is None:
        return {"ok": False, "error": "Không gọi được API lacxi ticket (retry)"}

    result = {"ok": resp.ok, "status": resp.status_code}
    try:
        result["data"] = resp.json()
    except Exception:
        result["text"] = resp.text
    return result


def print_lacxi_ticket(username: str):
    result = fetch_lacxi_ticket(username)
    if not result.get("ok"):
        print(f"❌ [{username}] {result.get('error', 'Lỗi không xác định')}", flush=True)
        return

    data = result.get("data")
    if data is not None:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(result.get("text", ""))


def _parse_ticket_count(data) -> int:
    if isinstance(data, int):
        return data
    if isinstance(data, str):
        try:
            return int(data.strip())
        except Exception:
            return 0
    if isinstance(data, dict):
        for key in ("ticket", "tickets", "count", "total", "value"):
            if key in data:
                try:
                    return int(data.get(key))
                except Exception:
                    return 0
    return 0


def open_lacxi_boxes(username: str):
    """
    Check số lượt mở từ API ticket rồi mở quà tương ứng, mỗi lần cách 10s.
    """
    ticket_result = fetch_lacxi_ticket(username)
    if not ticket_result.get("ok"):
        print(f"❌ [{username}] {ticket_result.get('error', 'Lỗi không xác định')}", flush=True)
        return

    count = _parse_ticket_count(ticket_result.get("data"))
    if count <= 0:
        print(f"⚠️ [{username}] Không có lượt mở (ticket={count})", flush=True)
        return

    print(f"🎁 [{username}] Có {count} lượt mở. Bắt đầu mở...", flush=True)
    for i in range(1, count + 1):
        resp = _request_lacxi_open(username)
        if resp is None:
            print(f"❌ [{username}] Mở quà lần {i}: Không gọi được API", flush=True)
        elif resp.ok:
            try:
                data = resp.json()
                prize_value = None
                if isinstance(data, dict):
                    prize_value = data.get("prizeValue")
                try:
                    prize_value = int(prize_value)
                except Exception:
                    prize_value = None

                if prize_value is not None and prize_value > 1000:
                    print(f"✅ [{username}] Mở quà lần {i}: {json.dumps(data, ensure_ascii=False)}", flush=True)
            except Exception:
                pass
        else:
            print(f"❌ [{username}] Mở quà lần {i}: HTTP {resp.status_code} {resp.text[:200]}", flush=True)

        if i < count:
            time.sleep(10)

    # Sau khi mở hết, check hòm quà
    auto_claim_gifts(username)


if __name__ == "__main__":
    username = input("Nhập username: ").strip()
    if not username:
        print("❌ Username không được để trống")
        exit(1)
    open_lacxi_boxes(username)
