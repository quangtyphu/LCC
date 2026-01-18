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

def save_config(config):
    """
    Lưu config vào file config.json với format đẹp.
    """
    try:
        inline_list_keys = {
            "PRIORITY_USERS",
            "PRIORITY_USERS_V2",
            "PRIORITY_USERS_V3",
        }
        one_per_line_list_keys = {
            "TIME_WINDOWS",
        }

        def is_simple_list(value):
            return isinstance(value, list) and all(
                isinstance(v, (str, int, float, bool)) or v is None for v in value
            )

        def format_json(value, indent=2, level=0, inline=False, list_items_inline=False):
            space = " " * (indent * level)
            if isinstance(value, dict):
                if not value:
                    return "{}"
                if inline:
                    items = [
                        f'{json.dumps(k, ensure_ascii=False)}: {format_json(v, indent, level + 1, inline=True)}'
                        for k, v in value.items()
                    ]
                    return "{" + ", ".join(items) + "}"
                lines = ["{"]
                items = list(value.items())
                for i, (key, val) in enumerate(items):
                    comma = "," if i < len(items) - 1 else ""
                    key_str = json.dumps(key, ensure_ascii=False)
                    val_str = format_json(
                        val,
                        indent,
                        level + 1,
                        inline=isinstance(val, list) and key in inline_list_keys,
                        list_items_inline=isinstance(val, list) and key in one_per_line_list_keys,
                    )
                    lines.append(f'{" " * (indent * (level + 1))}{key_str}: {val_str}{comma}')
                lines.append(f"{space}}}")
                return "\n".join(lines)
            if isinstance(value, list):
                if inline or is_simple_list(value):
                    items = ", ".join(
                        format_json(v, indent, level + 1, inline=True) for v in value
                    )
                    return f"[{items}]"
                if not value:
                    return "[]"
                lines = ["["]
                for i, item in enumerate(value):
                    comma = "," if i < len(value) - 1 else ""
                    item_str = format_json(
                        item,
                        indent,
                        level + 1,
                        inline=list_items_inline,
                    )
                    lines.append(f'{" " * (indent * (level + 1))}{item_str}{comma}')
                lines.append(f"{space}]")
                return "\n".join(lines)
            return json.dumps(value, ensure_ascii=False)

        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write(format_json(config) + "\n")
        return True
    except Exception as e:
        print(f"⚠️ Lỗi ghi config.json: {e}")
        return False