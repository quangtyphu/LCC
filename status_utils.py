import requests
import sqlite3

API_BASE = "http://127.0.0.1:3000"  # URL CMS Node.js

DB_PATH = r"C:\Users\Quang\Documents\CMS\game_data.db"

def _update_status_db(username: str, status: str) -> bool:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE user_profiles SET status = ? WHERE username = ?",
            (status, username),
        )
        conn.commit()
        changed = cursor.rowcount > 0
        conn.close()
        return changed
    except Exception:
        return False

def update_status(username: str, status: str) -> bool:
    try:
        r = requests.put(
            f"{API_BASE}/api/users/{username}",
            json={"status": status},
            timeout=5,
        )
        if r.status_code == 200:
            return True
    except Exception:
        pass

    # Fallback: cập nhật trực tiếp DB nếu API lỗi
    return _update_status_db(username, status)
