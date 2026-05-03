"""
File xử lý tác vụ theo khung giờ cuối ngày.
Hiện tại: bật NEW_STRATEGY.ENABLED = 1 vào khung giờ scheduler.
"""

import time
from datetime import datetime
from constants import load_config, save_config


def enable_new_strategy():
	"""
	Bật NEW_STRATEGY.ENABLED = 1 và tắt AUTO_REFRESH_V2_FROM_TOP480.ENABLED = 0 trong config.json.
	Trả về True nếu thành công, False nếu có lỗi.
	"""
	try:
		config = load_config()
		if not config:
			print("[NEW_STRATEGY] ❌ Không đọc được config", flush=True)
			return False

		new_strategy = config.get("NEW_STRATEGY", {})
		if not isinstance(new_strategy, dict):
			new_strategy = {}
		new_strategy["ENABLED"] = 1
		config["NEW_STRATEGY"] = new_strategy

		auto_refresh = config.get("AUTO_REFRESH_V2_FROM_TOP480", {})
		if not isinstance(auto_refresh, dict):
			auto_refresh = {}
		auto_refresh["ENABLED"] = 0
		config["AUTO_REFRESH_V2_FROM_TOP480"] = auto_refresh

		# Tái sử dụng mốc ngày để tránh chạy lặp trong cùng ngày
		today = datetime.now().strftime("%Y-%m-%d")
		config["LAST_SWAP_DATE"] = today

		if save_config(config):
			print(
				f"[NEW_STRATEGY] ✅ NEW_STRATEGY.ENABLED=1 & AUTO_REFRESH_V2_FROM_TOP480.ENABLED=0 | Ngày: {today}",
				flush=True,
			)
			return True
		else:
			print("[NEW_STRATEGY] ❌ Không lưu được config", flush=True)
			return False
			
	except Exception as e:
		print(f"[NEW_STRATEGY] ❌ Lỗi khi bật NEW_STRATEGY: {e}", flush=True)
		import traceback
		traceback.print_exc()
		return False


def auto_swap_v2_v3_scheduler():
	"""
	Background task chạy theo khung giờ cuối ngày (giờ máy tính):
	- bật NEW_STRATEGY.ENABLED = 1
	Chạy trong thread riêng, check mỗi 60 giây.
	"""
	print("[SWAP SCHEDULER] 🕐 Đã khởi động scheduler bật NEW_STRATEGY (23:58-23:59 mỗi ngày)", flush=True)
	
	while True:
		try:
			# Lấy giờ hiện tại (giờ máy tính)
			now = datetime.now()
			current_time = now.strftime("%H:%M")
			current_date = now.strftime("%Y-%m-%d")
			
			# Check xem có phải 23:55 không (hoặc 23:56 để tránh race condition)
			if current_time in ["23:58", "23:59"]:
				# Load config để check LAST_SWAP_DATE
				config = load_config()
				last_swap_date = config.get("LAST_SWAP_DATE", "")
				
				# Chỉ chạy nếu chưa chạy hôm nay
				if last_swap_date != current_date:
					enable_new_strategy()
				else:
					# Đã chạy rồi, không làm gì
					pass
			
			# Chờ 60 giây trước khi check lại
			time.sleep(60)
			
		except Exception as e:
			print(f"[SWAP SCHEDULER] ❌ Lỗi trong scheduler: {e}", flush=True)
			import traceback
			traceback.print_exc()
			# Chờ 60 giây trước khi retry
			time.sleep(60)
