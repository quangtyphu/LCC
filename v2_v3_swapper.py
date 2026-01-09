"""
File xử lý tự động swap V2 và V3 vào 23:55 mỗi ngày
"""

import time
from datetime import datetime
from constants import load_config, save_config


def swap_v2_v3():
	"""
	Swap nội dung của PRIORITY_USERS_V2 và PRIORITY_USERS_V3 trong config.json.
	Trả về True nếu thành công, False nếu có lỗi.
	"""
	try:
		config = load_config()
		if not config:
			print("[SWAP] ❌ Không đọc được config", flush=True)
			return False
		
		v2 = config.get("PRIORITY_USERS_V2", [])
		v3 = config.get("PRIORITY_USERS_V3", [])
		
		# Đếm số user thực tế (bỏ qua string rỗng)
		v2_count = len([u for u in v2 if u and str(u).strip()])
		v3_count = len([u for u in v3 if u and str(u).strip()])
		
		# Swap
		config["PRIORITY_USERS_V2"] = v3
		config["PRIORITY_USERS_V3"] = v2
		
		# Cập nhật LAST_SWAP_DATE
		today = datetime.now().strftime("%Y-%m-%d")
		config["LAST_SWAP_DATE"] = today
		
		# Lưu lại config
		if save_config(config):
			print(f"[SWAP] ✅ Đã swap V2 ↔ V3 thành công!", flush=True)
			print(f"   V2: {v2_count} users → {v3_count} users", flush=True)
			print(f"   V3: {v3_count} users → {v2_count} users", flush=True)
			print(f"   Ngày swap: {today}", flush=True)
			return True
		else:
			print("[SWAP] ❌ Không lưu được config", flush=True)
			return False
			
	except Exception as e:
		print(f"[SWAP] ❌ Lỗi khi swap: {e}", flush=True)
		import traceback
		traceback.print_exc()
		return False


def auto_swap_v2_v3_scheduler():
	"""
	Background task tự động swap V2 và V3 vào 23:55 mỗi ngày (giờ máy tính).
	Chạy trong thread riêng, check mỗi 60 giây.
	"""
	print("[SWAP SCHEDULER] 🕐 Đã khởi động scheduler swap V2/V3 (23:55 mỗi ngày)", flush=True)
	
	while True:
		try:
			# Lấy giờ hiện tại (giờ máy tính)
			now = datetime.now()
			current_time = now.strftime("%H:%M")
			current_date = now.strftime("%Y-%m-%d")
			
			# Check xem có phải 23:55 không (hoặc 23:56 để tránh race condition)
			if current_time in ["23:55", "23:56"]:
				# Load config để check LAST_SWAP_DATE
				config = load_config()
				last_swap_date = config.get("LAST_SWAP_DATE", "")
				
				# Chỉ swap nếu chưa swap hôm nay
				if last_swap_date != current_date:
					print(f"[SWAP SCHEDULER] ⏰ Phát hiện 23:55 - Bắt đầu swap V2/V3...", flush=True)
					swap_v2_v3()
				else:
					# Đã swap rồi, không làm gì (log một lần để debug)
					pass
			
			# Chờ 60 giây trước khi check lại
			time.sleep(60)
			
		except Exception as e:
			print(f"[SWAP SCHEDULER] ❌ Lỗi trong scheduler: {e}", flush=True)
			import traceback
			traceback.print_exc()
			# Chờ 60 giây trước khi retry
			time.sleep(60)
