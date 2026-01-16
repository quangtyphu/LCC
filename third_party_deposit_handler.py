"""
File xử lý nạp tiền qua bên thứ 3

Chức năng:
1. Tạo lệnh nạp qua deposit_api.py (API game thật) để lấy QR/base64
2. Gửi thông tin cho bên thứ 3 (HTTP POST)
3. Nhận callback từ bên thứ 3; nếu SUCCESS thì bắt đầu check lịch sử 5 lần
"""

import os
import sys

# In log ngay lập tức
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

import requests
import time
from flask import Flask, request, jsonify
from deposit_api import update_deposit_order_status


# ========== CẤU HÌNH ==========
NODE_SERVER_URL = "http://127.0.0.1:3000"                  # DB server
THIRD_PARTY_API_URL = "http://localhost:8888/api/deposit"  # API bên thứ 3
CALLBACK_URL = "http://localhost:5000/callback"            # URL callback của bạn


# ========== FLASK APP ==========
app = Flask(__name__)


def create_deposit_order_with_real_qr(username: str, amount: int) -> dict:
	"""
	Tạo lệnh nạp tiền thật qua deposit_full_process (API chung) để lấy QR base64 và lưu DB.
	"""
	try:
		from deposit_api import deposit_full_process

		print("💰 Đang gọi API game để tạo lệnh nạp thật...", flush=True)

		# Gọi deposit_full_process (API chung) - đã bao gồm deposit, save DB, save QR
		result = deposit_full_process(username, amount)

		if not result.get("ok"):
			return {"ok": False, "error": result.get("error", "Không gọi được API game")}

		# Lấy dữ liệu từ response
		data = result.get("data", {})
		order_id = data.get("orderId")
		payload = data  # data đã chứa accountNumber, accountHolder, transferContent, qrLink, qrImagePath

		if not order_id:
			print(f"❌ Không có order_id trong response", flush=True)
			return {"ok": False, "error": "Không lấy được order_id"}

		# Lấy QR base64 từ qrImagePath hoặc qrLink
		qr_base64 = ""
		qr_image_path = data.get("qrImagePath")
		qr_link = data.get("qrLink", "")

		# Nếu có file QR đã lưu, đọc base64 từ file
		if qr_image_path and os.path.exists(qr_image_path):
			try:
				import base64
				with open(qr_image_path, "rb") as f:
					qr_base64 = f"data:image/png;base64,{base64.b64encode(f.read()).decode()}"
			except Exception as e:
				print(f"⚠️ Không đọc được QR từ file: {e}", flush=True)

		# Fallback: tải từ qr_link nếu chưa có
		if not qr_base64 and qr_link:
			try:
				import base64
				resp = requests.get(qr_link, timeout=10)
				if resp.ok:
					qr_base64 = f"data:image/png;base64,{base64.b64encode(resp.content).decode()}"
			except Exception:
				pass

		return {
			"ok": True,
			"order_id": order_id,
			"qr_base64": qr_base64,
			"transfer_content": data.get("transferContent", ""),
			"account_number": data.get("accountNumber", ""),
			"account_holder": data.get("accountHolder", ""),
			"qr_link": qr_link
		}
	except Exception as e:
		print(f"❌ EXCEPTION trong create_deposit_order_with_real_qr: {e}", flush=True)
		import traceback
		traceback.print_exc()
		return {"ok": False, "error": str(e)}


def send_to_third_party(username: str, amount: int, order_data: dict) -> dict:
	"""
	Gửi thông tin nạp tiền cho bên thứ 3 (theo format của họ).
	"""
	order_id = order_data.get("order_id")
	qr_base64 = order_data.get("qr_base64", "")

	payload = {
		"orderId": str(order_id),
		"qrBase64": qr_base64,
		"username": username,
		"amount": amount,
		"transferContent": order_data.get("transfer_content", ""),
		"accountNumber": order_data.get("account_number", ""),
		"accountHolder": order_data.get("account_holder", "")
	}
	try:
		resp = requests.post(THIRD_PARTY_API_URL, json=payload, timeout=15)
		data = resp.json()

		if resp.ok and data.get("ok"):
			return {
				"ok": True,
				"transaction_id": data.get("data", {}).get("orderId", ""),
				"message": data.get("message", "")
			}
		else:
			error = data.get("error", "Unknown error")
			print(f"❌ Bên thứ 3 trả lỗi: {error}", flush=True)
			return {"ok": False, "error": error}

	except Exception as e:
		print(f"❌ Lỗi kết nối bên thứ 3: {e}", flush=True)
		return {"ok": False, "error": str(e)}


@app.route('/callback', methods=['POST'])
def receive_callback():
	"""
	API nhận callback từ bên thứ 3.
	- Cập nhật status vào DB
	- Nếu status = "Đã Nạp" → bắt đầu check lịch sử 5 lần
	"""
	data = request.json
	# Hỗ trợ cả camelCase và snake_case
	order_id = data.get("order_id") or data.get("orderId")
	status = data.get("status")
	transaction_id = data.get("transaction_id") or data.get("transactionId")
	message = data.get("message", "")
	amount = data.get("amount", 0)
	username = data.get("username", "")
	transfer_content = data.get("transferContent") or data.get("transfer_content", "")

	if not order_id:
		return jsonify({"error": "Missing order_id"}), 400

	if not status:
		return jsonify({"error": "Missing status"}), 400

	# Cập nhật status vào DB (bất kể status nào)
	print(f"📝 Cập nhật order #{order_id} → {status}", flush=True)
	success = update_deposit_order_status(order_id, status)
	# Nếu status = "Đã Nạp" → bắt đầu check lịch sử
	if status == "Đã Nạp":
		print(f"💰 Bắt đầu check lịch sử nạp tiền cho order #{order_id}", flush=True)
		
		# Nếu callback không gửi username/transferContent → Lấy từ DB
		if not username or not transfer_content:
			print(f"📡 Lấy thông tin order từ DB...", flush=True)
			try:
				import requests
				r = requests.get(f"{NODE_SERVER_URL}/api/deposit-orders/{order_id}", timeout=5)
				if r.ok:
					db_order = r.json()
					username = db_order.get("username", username)
					transfer_content = db_order.get("transferContent", transfer_content)
					amount = db_order.get("amount", amount)
					print(f"✅ Lấy từ DB: {username}, {transfer_content}, {amount}đ", flush=True)
			except Exception as e:
				print(f"⚠️ Không lấy được từ DB: {e}", flush=True)
		
		# Xóa username khỏi cache khi đã nạp thành công
		if username:
			try:
				from auto_deposit_on_out_of_money import remove_from_deposit_cache
				remove_from_deposit_cache(username)
			except Exception as e:
				print(f"⚠️ Không xóa được khỏi cache: {e}", flush=True)
		# Check lịch sử trong thread nền (không block callback)
		import threading
		from deposit_api import wait_and_check_deposit

		threading.Thread(
			target=wait_and_check_deposit,
			args=(username, transfer_content, order_id, amount),
			daemon=True
		).start()

	return jsonify({
		"success": True,
		"order_id": order_id,
		"status": status,
		"received_at": time.time()
	}), 200


@app.route('/create-deposit', methods=['POST'])
def create_deposit():
	"""
	API tạo lệnh nạp tiền (gọi API game thật + gửi cho bên thứ 3).
	"""
	data = request.json
	username = data.get("username")
	amount = data.get("amount")

	if not username or not amount:
		return jsonify({"error": "Missing username or amount"}), 400

	print("\n" + "="*60)
	print("🎮 Tạo lệnh nạp tiền mới", flush=True)
	print(f"   Username: {username}", flush=True)
	print(f"   Amount: {amount:,}đ", flush=True)
	print("="*60 + "\n", flush=True)

	# 1) Tạo lệnh nạp thật (lấy QR, lưu DB)
	result = create_deposit_order_with_real_qr(username, amount)
	if not result.get("ok"):
		print(f"❌ Lỗi tạo order: {result.get('error')}", flush=True)
		return jsonify(result), 400

	order_id = result.get("order_id")

	# 2) Gửi thông tin cho bên thứ 3
	third_party_result = send_to_third_party(username, amount, result)
	if not third_party_result.get("ok"):
		update_deposit_order_status(order_id, "Thất Bại")
		return jsonify({
			"ok": False,
			"error": f"Không gửi được cho bên thứ 3: {third_party_result.get('error')}"
		}), 500

	# 3) Thành công
	return jsonify({
		"ok": True,
		"order_id": order_id,
		"transaction_id": third_party_result.get("transaction_id"),
		"status": "PENDING",
		"message": "Đã gửi yêu cầu nạp tiền cho bên thứ 3",
		"data": {
			"username": username,
			"amount": amount,
			"transferContent": result.get("transfer_content"),
			"accountNumber": result.get("account_number"),
			"accountHolder": result.get("account_holder")
		}
	}), 200


@app.route('/health', methods=['GET'])
def health_check():
	return jsonify({
		"status": "running",
		"callback_url": CALLBACK_URL,
		"third_party_url": THIRD_PARTY_API_URL
	})


if __name__ == '__main__':
	print("\n" + "="*60)
	print("🚀 Third Party Deposit Handler")
	print("="*60)
	print(f"📍 Callback URL: {CALLBACK_URL}")
	print(f"📍 Third Party API: {THIRD_PARTY_API_URL}")
	print("="*60 + "\n")

	app.run(host='0.0.0.0', port=5000, debug=False)
