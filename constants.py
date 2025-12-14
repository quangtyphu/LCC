# Hằng số & config chung

# Địa chỉ WebSocket của server game
WS_URL = "wss://wtx.tele68.com/tx/?EIO=4&transport=websocket"

# ================== Các biến runtime ==================
session_seen = None
# Các event được phép xử lý
allowed_events = {"new-session", "bet-result", "session-result", "won-session", "bet_refund"}

# active_ws: username -> { "task": Task, "queue": asyncio.Queue, "acc": dict }
active_ws = {}

# Task global chịu trách nhiệm enqueue cho 1 phiên
assign_task = None

# Lock để tránh race khi nhiều connection cùng tạo/hủy assign_task
import asyncio
assign_lock = asyncio.Lock()

# ================== Load config ==================
import json, os

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")

def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Lỗi đọc config.json: {e}")
        return {}
