"""
Session handler: xử lý logic new-session & lịch đặt cược.
- Được gọi bởi ws_manager khi leader_user bắt được sự kiện "new-session".
"""
import asyncio
import random
import requests
from typing import Dict, Any, List, Tuple

from chiaTien_Tho import distribute_for_devices

API_BASE = "http://127.0.0.1:3000"  # URL server.js của bạn

# ======= Cấu hình =======
BET_UNIT = 1                 # 1 "đơn vị" = 1 (đã là tiền thực)
BET_DELAY_RANGE = (5, 25)    # Giây delay kể từ khi nhận new-session


# ------------------- Helpers -------------------

def _build_devices_from_active_ws(active_ws_snapshot: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Lấy danh sách device từ snapshot của active_ws.
    """
    devices = []
    for user, entry in active_ws_snapshot.items():
        acc = entry.get("acc", {}) or {}
        device_name = acc.get("device") or acc.get("nickname") or acc.get("username") or user
        devices.append({
            "username": acc.get("username", user),
            "device": device_name,
            "balance": int(acc.get("balance") or 0),  # có thể cũ
        })
    return devices


async def _delayed_enqueue(queue: asyncio.Queue, user: str, payload: Tuple[str, dict], delay: int):
    await asyncio.sleep(delay)
    try:
        queue.put_nowait(payload)
    except asyncio.QueueFull:
        print(f"🚫 Queue đầy cho {user}, bỏ lệnh: {payload}")


def _fresh_balances_for_online(active_ws_snapshot: Dict[str, Dict[str, Any]]) -> Dict[str, int]:
    """
    Lấy balance mới nhất từ API cho các user đang online.
    Nếu lỗi → fallback theo snapshot.
    """
    balances: Dict[str, int] = {}
    for u in active_ws_snapshot.keys():
        try:
            r = requests.get(f"{API_BASE}/api/users/{u}", timeout=3)
            if r.status_code == 200:
                data = r.json()
                balances[u] = int(data.get("balance") or 0)
            else:
                acc = active_ws_snapshot[u].get("acc") or {}
                balances[u] = int(acc.get("balance") or 0)
        except Exception as e:
            print(f"⚠️ Lỗi lấy balance {u} từ API: {e}")
            acc = active_ws_snapshot[u].get("acc") or {}
            balances[u] = int(acc.get("balance") or 0)
    return balances


def _assign_bets_by_closest_balance_unique(
    bets: List[Tuple[Dict, int, str]],
    active_ws_snapshot: Dict[str, Dict[str, Any]]
) -> List[Tuple[str, int, str]]:
    """
    Gán MỖI bet thô -> 1 user online (unique), sao cho:
      - user có balance >= amount
      - chọn balance lớn hơn mà gần nhất (leftover nhỏ nhất)
      - 1 user chỉ nhận 1 lệnh duy nhất
    Nếu không đủ ứng viên hoặc có bet không gán được → trả [] để hủy phiên.
    Trả về: [(username, amount, door), ...]
    """
    balances = _fresh_balances_for_online(active_ws_snapshot)
    online_users = list(active_ws_snapshot.keys())

    to_assign = sorted([(amt, door) for (_dev, amt, door) in bets], key=lambda x: -x[0])
    used: set[str] = set()
    final: List[Tuple[str, int, str]] = []

    if len(to_assign) > len(online_users):
        print(f"⚠️ Số lệnh ({len(to_assign)}) > số tài khoản online ({len(online_users)}). Hủy phiên.")
        return []

    for amount, door in to_assign:
        candidates = []
        for u in online_users:
            if u in used:
                continue
            bal = balances.get(u, 0)
            if bal >= amount:
                after = bal - amount
                candidates.append((after, u))
        if not candidates:
            print(f"⚠️ Không tìm thấy tài khoản đủ tiền cho {door} {amount}. Hủy phiên.")
            return []

        after, chosen = min(candidates, key=lambda t: t[0])
        used.add(chosen)
        balances[chosen] = after
        final.append((chosen, amount, door))

    return final


# ------------------- Entry point: xử lý new-session -------------------

async def handle_new_session(active_ws_snapshot: Dict[str, Dict[str, Any]], leader_user: str):
    online_users = list(active_ws_snapshot.keys())
    print(f"🟢 NEW-SESSION | Online={len(online_users)} users: {online_users} | Leader={leader_user}")

    if len(online_users) < 2:
        print("ℹ️ <2 tài khoản online → bỏ qua ván này.")
        return

    subset_snapshot = active_ws_snapshot
    devices = _build_devices_from_active_ws(subset_snapshot)

    # B1: Chia thô theo rule
    bets = distribute_for_devices(devices)
    if not bets:
        print("⚠️ Hủy phiên: không thể phân bổ (có acc không đủ tiền hoặc không thỏa điều kiện).")
        return

    # B2: Gán lại 1–1 theo tiêu chí 'balance gần nhất'
    final_bets = _assign_bets_by_closest_balance_unique(bets, subset_snapshot)
    if not final_bets:
        print("⚠️ Hủy phiên: không tìm được acc phù hợp sau khi gán 1–1.")
        return

    # Map (username -> queue) để enqueue
    plan_lines: List[str] = []
    tasks: List[asyncio.Task] = []

    for user, amount, door in final_bets:
        ws_entry = subset_snapshot.get(user)
        if not ws_entry:
            print(f"⚠️ Không tìm thấy ws_entry cho user {user}, bỏ qua.")
            continue

        q: asyncio.Queue = ws_entry["queue"]
        bet_type = "TAI" if door.upper() == "TAI" else "XIU"
        bet_label = "Tài" if bet_type == "TAI" else "Xỉu"

        delay = random.randint(*BET_DELAY_RANGE)
        plan_lines.append(f"Tài Khoản {user} sẽ đặt cược {bet_label} - {amount * BET_UNIT} sau {delay}s")

        payload = ("bet", {"type": bet_type, "amount": amount * BET_UNIT})
        tasks.append(asyncio.create_task(_delayed_enqueue(q, user, payload, delay)))

    for line in plan_lines:
        print(line)

    if tasks:
        await asyncio.gather(*tasks)
