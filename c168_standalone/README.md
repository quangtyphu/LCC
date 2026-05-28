# C168 Standalone

Chỉ 2 file chạy chính (mọi thao tác đều qua **proxy SOCKS5**):

## 1. Đăng ký → MK rút → Liên kết bank

Chạy file rồi nhập từng dòng trong terminal:

```bash
python c168_dang_ky.py
```

Hoặc truyền tham số:

```bash
python c168_dang_ky.py --username myacc01 --password Abc123456 ...
```

## 2. Đăng nhập

```bash
python c168_dang_nhap.py
```

Nhập: username, mật khẩu, proxy.

**Chrome:** Script mở **Chrome C168 riêng** (profile `c168-chrome-profile`, port `9333`) — **không tắt** Chrome cá nhân đang dùng. Sau khi chạy xong, cửa sổ C168 **giữ mở** (thêm `--close-browser` nếu muốn tự đóng).

## Cài đặt

```bash
pip install -r requirements.txt
playwright install chromium
copy c168_config.example.json c168_config.json
```
