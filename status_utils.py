import requests

API_BASE = "http://127.0.0.1:3000"  # URL CMS Node.js

def update_status(username: str, status: str) -> bool:
    try:
        r = requests.put(f"{API_BASE}/api/users/{username}", json={"status": status}, timeout=5)
        return r.status_code == 200
    except Exception:
        return False
