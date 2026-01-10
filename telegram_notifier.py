import requests
import sys

TELEGRAM_TOKEN = "8353962219:AAELQcRiBBGGZdNIjyaaAYBx7VEN8ok2hTY"
CHAT_ID = "7129501938"

def send_telegram(msg: str):
    """
    Gửi tin nhắn về Telegram.
    :param msg: Nội dung tin nhắn (string)
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        res = requests.post(url, json={"chat_id": CHAT_ID, "text": msg})
        res.raise_for_status()
        return True
    except requests.exceptions.HTTPError as e:
        error_msg = f"Loi gui Telegram: {e}"
        if hasattr(e.response, 'text'):
            error_msg += f" - Response: {e.response.text}"
        print(error_msg, file=sys.stderr)
        return False
    except Exception as e:
        print(f"Loi gui Telegram: {e}", file=sys.stderr)
        return False
