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
import threading
from flask import Flask, request, jsonify
from deposit_api import update_deposit_order_status


# ========== CẤU HÌNH ==========
NODE_SERVER_URL = os.environ.get("LC79_NODE_SERVER_URL", "http://127.0.0.1:3000").strip()


def _load_urls() -> tuple[str, str, int]:
	"""Banking callback luôn về handler LC79 (:5000), không dùng URL XOSO66."""
	from constants import load_config

	cfg = load_config()
	dep = cfg.get("LC79_DEPOSIT") if isinstance(cfg.get("LC79_DEPOSIT"), dict) else {}
	port = int(
		dep.get("handler_port")
		or os.environ.get("LC79_DEPOSIT_HANDLER_PORT")
		or 5000
	)
	callback = str(
		dep.get("callback_url")
		or os.environ.get("LC79_DEPOSIT_CALLBACK_URL")
		or f"http://127.0.0.1:{port}/callback"
	).strip()
	third = str(
		dep.get("third_party_url")
		or os.environ.get("LC79_THIRD_PARTY_DEPOSIT_URL")
		or "http://127.0.0.1:8888/api/deposit"
	).strip()
	return third, callback, port


THIRD_PARTY_API_URL, CALLBACK_URL, HANDLER_PORT = _load_urls()


def refresh_urls() -> None:
	global THIRD_PARTY_API_URL, CALLBACK_URL, HANDLER_PORT
	THIRD_PARTY_API_URL, CALLBACK_URL, HANDLER_PORT = _load_urls()


def _xoso66_callback_forward_url() -> str:
	"""URL handler XOSO66 — chuyển callback khi đơn không có trên Node LC79."""
	u = os.environ.get("XOSO66_CALLBACK_FORWARD_URL", "").strip()
	if u:
		return u
	try:
		from pathlib import Path
		import json

		from constants import REPO_ROOT

		p = REPO_ROOT / "xoso66_standalone" / "xoso66_config.json"
		if p.is_file():
			cfg = json.loads(p.read_text(encoding="utf-8"))
			ad = cfg.get("auto_deposit") if isinstance(cfg.get("auto_deposit"), dict) else {}
			port = int(ad.get("handler_port") or 5000)
			return str(
				ad.get("callback_url") or f"http://127.0.0.1:{port}/callback"
			).strip()
	except Exception:
		pass
	return "http://127.0.0.1:5000/callback"


def _forward_callback_to_xoso66(data: dict, order_id) -> tuple[dict, int] | None:
	"""Bên thứ 3 gọi :5000 nhưng lệnh thuộc XOSO66 — proxy sang handler standalone."""
	url = _xoso66_callback_forward_url()
	if url.rstrip("/").lower() == CALLBACK_URL.rstrip("/").lower():
		return None
	try:
		fr = requests.post(url, json=data, timeout=30)
		print(
			f"↪️ Callback order #{order_id} → XOSO66 {url} HTTP {fr.status_code}",
			flush=True,
		)
		if fr.content:
			try:
				body = fr.json()
			except Exception:
				body = {"raw": fr.text[:500]}
		else:
			body = {}
		return body, fr.status_code
	except Exception as e:
		print(f"⚠️ Chuyển callback XOSO66 thất bại: {e}", flush=True)
		return None


def _parse_amount(val) -> int:
	"""Đọc số tiền từ DB/API: int, float, string, Decimal JSON."""
	if val is None:
		return 0
	try:
		if isinstance(val, bool):
			return 0
		if isinstance(val, (int, float)):
			return int(val)
		s = str(val).strip().replace(",", "").replace(" ", "")
		if not s:
			return 0
		return int(float(s))
	except (ValueError, TypeError, OverflowError):
		return 0


def _extract_order_dict(js: dict) -> dict:
	"""Chuẩn hóa object order từ response GET (nhiều kiểu bọc data)."""
	if not isinstance(js, dict):
		return {}
	d = js.get("data")
	if isinstance(d, list) and len(d) > 0:
		return d[0] if isinstance(d[0], dict) else {}
	if isinstance(d, dict):
		return d
	inner = js.get("order") or js.get("depositOrder")
	if isinstance(inner, dict):
		return inner
	return js


# ========== FLASK APP ==========
app = Flask(__name__)
_tracking_orders = set()
_tracking_lock = threading.Lock()


def create_deposit_order_with_real_qr(username: str, amount: int) -> dict:
	"""
	Tạo lệnh nạp tiền thật qua deposit_full_process (API chung) để lấy QR base64 và lưu DB.
	"""
	try:
		from deposit_api import deposit_full_process

		# Bỏ log gọi API game

		# Gọi deposit_full_process (API chung) - đã bao gồm deposit, save DB, save QR
		result = deposit_full_process(username, amount, track_history=False)

		if not result.get("ok"):
			return {"ok": False, "error": result.get("error", "Không gọi được API game")}

		# Lấy dữ liệu từ response
		data = result.get("data", {})
		order_id = data.get("orderId")
		effective_amount = data.get("amount") or amount
		payload = data  # data đã chứa accountNumber, accountHolder, transferContent, qrLink, qrImagePath

		if not order_id:
			print(f"❌ Không có order_id trong response", flush=True)
			return {"ok": False, "error": "Không lấy được order_id"}

		# Lấy QR base64 ưu tiên từ API, sau đó file hoặc qrLink
		qr_base64 = data.get("qrBase64") or data.get("qr_base64") or data.get("qr") or ""
		qr_image_path = data.get("qrImagePath")
		qr_link = data.get("qrLink", "")

		# Chuẩn hóa base64 nếu chưa có prefix
		if qr_base64 and isinstance(qr_base64, str) and not qr_base64.startswith("data:image"):
			qr_base64 = f"data:image/png;base64,{qr_base64}"

		# Nếu có file QR đã lưu, đọc base64 từ file
		if not qr_base64 and qr_image_path and os.path.exists(qr_image_path):
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
			"amount": effective_amount,
			"qr_base64": qr_base64,
			"transfer_content": data.get("transferContent", ""),
			"account_number": data.get("accountNumber", ""),
			"account_holder": data.get("accountHolder", ""),
			"qr_link": qr_link,
			"qr_image_path": qr_image_path,
			"bank": data.get("bank", "")
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
	refresh_urls()
	order_id = order_data.get("order_id")
	qr_base64 = order_data.get("qr_base64", "")
	tc = (order_data.get("transfer_content") or "").strip()

	payload = {
		"orderId": str(order_id),
		"qrBase64": qr_base64,
		"username": username,
		"amount": amount,
		"accountNumber": order_data.get("account_number", ""),
		"accountHolder": order_data.get("account_holder", ""),
		"bank": order_data.get("bank", ""),
		"qrLink": order_data.get("qr_link", ""),
		"qrImagePath": order_data.get("qr_image_path", ""),
		"receiver": order_data.get("account_number", ""),
		"name": order_data.get("account_holder", ""),
		"type": order_data.get("bank", ""),
	}
	# Chỉ gửi NDCK khi có; không có thì không thêm transferContent/msg
	if tc:
		payload["transferContent"] = tc
		payload["msg"] = tc
	payload["callbackUrl"] = CALLBACK_URL
	payload["callback_url"] = CALLBACK_URL
	print(
		f"🔗 [LC79] Gửi Banking order #{order_id} | callback={CALLBACK_URL}",
		flush=True,
	)
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

	refresh_urls()
	from deposit_callback_routing import resolve_callback_game

	game = resolve_callback_game(order_id, username, transfer_content)
	if game == "xoso66":
		fwd = _forward_callback_to_xoso66(data, order_id)
		if fwd is not None:
			body, code = fwd
			return jsonify(body), code
		return jsonify({"error": "Không chuyển được callback sang XOSO66"}), 502
	if game == "unknown":
		print(
			f"⚠️ [LC79] Callback #{order_id} chưa thấy trên Node — xử lý tại :5000 (không chuyển XOSO66)",
			flush=True,
		)

	callback_amount = _parse_amount(data.get("amount"))

	# --- Đã Nạp: đọc DB TRƯỚC khi ghi để không ghi đè "Thành Công" (sync game thường tới trước callback) ---
	if status == "Đã Nạp":
		order_data = {}
		prev_status = None
		amount = 0
		for attempt in range(2):
			try:
				r = requests.get(f"{NODE_SERVER_URL}/api/deposit-orders/{order_id}", timeout=5)
				if r.ok:
					js = r.json() or {}
					order_data = _extract_order_dict(js)
					prev_status = (order_data or {}).get("status") or (order_data or {}).get("Status")
					amount = _parse_amount((order_data or {}).get("amount") or (order_data or {}).get("Amount"))
					username = (order_data or {}).get("username") or username
					transfer_content = (order_data or {}).get("transferContent") or (order_data or {}).get("transfer_content") or transfer_content
					break
				time.sleep(1)
			except Exception as e:
				print(f"⚠️ GET order trước callback: {e}", flush=True)
				time.sleep(1)

		if amount <= 0 and transfer_content:
			try:
				r2 = requests.get(
					f"{NODE_SERVER_URL}/api/deposit-orders/check-transfer-content",
					params={"transferContent": transfer_content, "exact": "true"},
					timeout=5,
				)
				if r2.ok:
					data2 = r2.json() or {}
					orders = (data2.get("data") or data2.get("orders") or []) if isinstance(data2, dict) else []
					for o in (orders if isinstance(orders, list) else []):
						if str(o.get("id")) == str(order_id):
							amount = _parse_amount(o.get("amount") or o.get("Amount"))
							username = o.get("username") or username
							break
			except Exception:
				pass

		if amount <= 0 and callback_amount > 0:
			amount = callback_amount

		if prev_status == "Thành Công":
			print(
				f"ℹ️ Order #{order_id} đã Thành Công (đã khớp lịch sử game trước) — bỏ qua cập nhật «Đã Nạp» và không cần tracking thêm.",
				flush=True,
			)
			return jsonify({"success": True, "order_id": order_id, "status": status, "skipped": "already_thanh_cong"}), 200

		print(f"📝 Cập nhật order #{order_id} → {status}", flush=True)
		update_deposit_order_status(order_id, status)

		if username and amount > 0:
			with _tracking_lock:
				if order_id in _tracking_orders:
					print(f"ℹ️ Order #{order_id} đã được theo dõi, bỏ qua", flush=True)
				else:
					_tracking_orders.add(order_id)
					from deposit_api import wait_and_check_deposit

					threading.Thread(
						target=wait_and_check_deposit,
						args=(username, transfer_content or "", order_id, amount),
						daemon=True,
					).start()
		else:
			print(
				f"⚠️ Bỏ qua tracking: thiếu username hoặc amount (DB+callback). order_id={order_id}",
				flush=True,
			)

	elif status in ["Thất Bại", "Huỷ"]:
		# Không hạ trạng thái nếu lệnh đã Thành Công (tránh callback muộn ghi đè).
		try:
			r_prev = requests.get(f"{NODE_SERVER_URL}/api/deposit-orders/{order_id}", timeout=5)
			if r_prev.ok:
				prev_order = _extract_order_dict(r_prev.json() or {})
				prev_status = (prev_order.get("status") or prev_order.get("Status") or "").strip()
				if prev_status == "Thành Công":
					print(
						f"ℹ️ Order #{order_id} đã Thành Công — bỏ qua callback {status}",
						flush=True,
					)
					return jsonify({"success": True, "order_id": order_id, "status": prev_status, "skipped": "already_thanh_cong"}), 200
		except Exception as e:
			print(f"⚠️ Không đọc được trạng thái hiện tại trước khi set {status}: {e}", flush=True)

		print(f"📝 Cập nhật order #{order_id} → {status}", flush=True)
		update_deposit_order_status(order_id, status)
		if username:
			try:
				from auto_deposit_on_out_of_money import remove_from_deposit_cache
				remove_from_deposit_cache(username)
			except Exception as e:
				print(f"⚠️ Không xóa được khỏi cache: {e}", flush=True)
	else:
		print(f"📝 Cập nhật order #{order_id} → {status}", flush=True)
		update_deposit_order_status(order_id, status)

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

	# Nếu đang có lệnh treo trong cache thì không tạo mới
	try:
		from auto_deposit_on_out_of_money import can_create_deposit_order
		if not can_create_deposit_order(username):
			return jsonify({"error": "Tài khoản đang có lệnh nạp chưa hoàn thành"}), 409
	except Exception as e:
		print(f"⚠️ Không kiểm tra được cache: {e}", flush=True)

	# 1) Tạo lệnh nạp thật (lấy QR, lưu DB)
	result = create_deposit_order_with_real_qr(username, amount)
	if not result.get("ok"):
		print(f"❌ Lỗi tạo order: {result.get('error')}", flush=True)
		return jsonify(result), 400

	order_id = result.get("order_id")
	effective_amount = result.get("amount") or amount  # Lệnh B lấy thành công (có NDCK)
	# In thông tin LỆNH LẤY THÀNH CÔNG (lệnh B), không phải lệnh A
	print("📋 Thông tin lệnh nạp thành công (đã gửi đi):", flush=True)
	print(f"   Username:       {username}", flush=True)
	print(f"   Amount:         {effective_amount:,}đ", flush=True)
	print(f"   Ngân hàng:      {result.get('bank', '')}", flush=True)
	print(f"   STK:            {result.get('account_number', '')}", flush=True)
	print(f"   Chủ Tài khoản:  {result.get('account_holder', '')}", flush=True)
	_tc = (result.get('transfer_content') or '').strip()
	print(f"   NDCK:           {_tc if _tc else '(trống)'}", flush=True)
	print("-" * 60, flush=True)

	# Lưu cache ngay sau khi tạo được order để tránh tạo trùng
	try:
		from auto_deposit_on_out_of_money import load_deposit_cache, save_deposit_cache
		cache = load_deposit_cache()
		cache[username] = time.time()
		save_deposit_cache(cache)
	except Exception as e:
		print(f"⚠️ Không lưu được cache sau khi tạo order: {e}", flush=True)

	refresh_urls()
	print(f"📍 [LC79] Handler callback URL: {CALLBACK_URL}", flush=True)

	# 2) Gửi thông tin cho bên thứ 3
	third_party_result = send_to_third_party(username, effective_amount, result)
	if not third_party_result.get("ok"):
		update_deposit_order_status(order_id, "Thất Bại")
		# Xóa cache để cho phép tạo lại
		try:
			from auto_deposit_on_out_of_money import remove_from_deposit_cache
			remove_from_deposit_cache(username)
		except Exception as e:
			print(f"⚠️ Không xóa được cache khi gửi bên thứ 3 thất bại: {e}", flush=True)
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
			"amount": effective_amount,
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

	refresh_urls()
	app.run(host="0.0.0.0", port=HANDLER_PORT, debug=False)
