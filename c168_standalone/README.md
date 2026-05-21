# C168 standalone

Auto đăng ký tài khoản [C168](https://c168b2.cc/) (site mirror `c168b2.cc`).

## Kiến trúc (đã probe)

| Thành phần | Giá trị |
|------------|---------|
| Trang đăng ký | `{base_url}/home/register` |
| API đăng ký | `POST {api_host}/hall/api/member/register` (body mã hóa `x-data-mode: chipher`) |
| API host | Động, ví dụ `https://a9861c.c1689.net` (lấy từ response khi submit) |
| Captcha đăng ký (thực tế API) | **Cloudflare Turnstile** — `sitekey` dạng `0x4AAAAA...` (Capsolver: `AntiTurnstileTaskProxyLess`) |
| GeeTest trên config site | `captcha_id` register = `62c528ead784206de7e6db17765b9ac0` (Capsolver [GeeTest V4](https://docs.capsolver.com/en/guide/captcha/Geetest/) — có thể `unsupported riskType` tùy loại captcha) |
| Site code | `2865` |

Script **không** gọi API register trực tiếp bằng `requests` (payload mã hóa trong JS). Dùng **Playwright** điền form; Turnstile tự chạy trong browser (giống bấm ĐĂNG KÝ tay — popup “Đang xác minh…”).

**Chặn bot:** nếu thấy popup “Mẹo / phát hiện robot”, mặc định script dùng **SOCKS5 từ `game_data.db` LC79** (`user_profiles.proxy`, dạng `host:port:user:pass`) + gõ form chậm hơn.

## Cài đặt

```bash
cd c168_standalone
pip install -r requirements.txt
playwright install chromium
copy c168_config.example.json c168_config.json
```

## Captcha — Capsolver (key trong `c168_config.json`)

Tài liệu GeeTest: [Capsolver GeeTest](https://docs.capsolver.com/en/guide/captcha/Geetest/)

| `captcha.kind` | Capsolver `type` | Cần trong config |
|----------------|------------------|------------------|
| `turnstile` (khuyên dùng C168) | `AntiTurnstileTaskProxyLess` | `sitekey`, `pageurl`, `api_key` |
| `geetest_v4` | `GeeTestTaskProxyLess` | `captcha_id`, `pageurl`, `api_key` |
| `geetest_v3` | `GeeTestTaskProxyLess` | `gt`, `challenge`, `pageurl` (lấy mới khi mở captcha, không copy cũ) |

Đã test key của bạn: **Turnstile OK**; GeeTest V4 với `62c528...` → Capsolver báo `unsupported riskType` (có thể site dùng loại GeeTest Capsolver chưa hỗ trợ, hoặc đăng ký thật dùng Turnstile).

`c168_config.json` → `captcha.provider`:

- **`capsolver`**: dùng API key `CAP-...`
- **`custom`**: POST tới URL của bạn, ví dụ `http://127.0.0.1:9999/solve`

```json
{
  "type": "turnstile",
  "sitekey": "0x4AAAAAABhSPiw6QLnmnJMb",
  "pageurl": "https://c168b2.cc/home/register",
  "action": "register"
}
```

Trả về: `{"ok": true, "token": "..."}` hoặc `{"token": "..."}`.

- **`capsolver`**: đặt `api_key` Capsolver.
- **`2captcha`**: đặt `api_key` 2captcha.

Env: `C168_CAPTCHA_SOLVER_URL`, `C168_CAPTCHA_API_KEY`, `C168_CAPTCHA_PROVIDER`.

## Proxy SOCKS5 (LC79 DB)

Mặc định lấy ngẫu nhiên một proxy từ `C:\Users\Quang\Documents\CMS\game_data.db` (cột `proxy` bảng `user_profiles`). Chromium dùng relay HTTP local (`pproxy`) vì SOCKS5 có user/pass.

```bash
python c168_register.py --random --headed          # khuyên dùng khi bị chặn bot
python c168_register.py --random --proxy host:port:user:pass
python c168_register.py --random --no-proxy        # IP máy, không qua DB
```

`c168_config.json` → `proxy.db_path`, `proxy.enabled`.

## Chạy đăng ký

```bash
python c168_register.py --random
python c168_register.py --username myuser --password Abc123456 --phone 912345678
python c168_register.py --random --headed
python c168_register.py --random --capsolver       # bỏ Turnstile browser, dùng Capsolver
```

Tài khoản thành công lưu vào `c168_accounts.json`.

## Lưu ý SĐT

Form hiển thị prefix `+84`; ô nhập **không** gõ số `0` đầu (vd. `912345678`, không phải `0912345678`).
