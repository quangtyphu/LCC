# telegram_notifier.py
import requests

TELEGRAM_TOKEN = "8406349210:AAElIYSbfvlDum8l0TZ0vs_4YdNqL2tlCQ8"
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
    except Exception as e:
        print("❌ Lỗi gửi Telegram:", e)
