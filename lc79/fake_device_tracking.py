import requests
from uuid import uuid4

from game_api_helper import NODE_SERVER_URL


def fake_device_tracking(username: str) -> dict:
    """
    Đảm bảo user có uuid trong DB:
    - Nếu đã có uuid thì giữ nguyên
    - Nếu chưa có thì tạo uuid mới và lưu vào DB
    """
    if not username:
        return {"ok": False, "error": "Thiếu username"}

    resp = requests.get(f"{NODE_SERVER_URL}/api/users/{username}", timeout=5)
    if resp.status_code != 200:
        return {"ok": False, "error": f"Không lấy được user_profile: {resp.status_code}"}

    profile = resp.json()
    current_uuid = profile.get("uuid")
    if current_uuid:
        return {"ok": True, "uuid": current_uuid, "updated": False}

    new_uuid = str(uuid4())
    resp_update = requests.put(
        f"{NODE_SERVER_URL}/api/users/{username}",
        json={"uuid": new_uuid},
        timeout=5,
    )
    if resp_update.status_code != 200:
        return {
            "ok": False,
            "error": f"Không cập nhật uuid: {resp_update.status_code}",
        }

    return {"ok": True, "uuid": new_uuid, "updated": True}
