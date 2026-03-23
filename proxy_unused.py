"""
Một file duy nhất: đăng nhập proxy.vn → lấy danh sách → so sánh với CMS → in ra proxy CHƯA DÙNG.

Chạy: python proxy_unused.py [09xxx matkhau]
      python proxy_unused.py --socks5    # Chỉ SOCKS5
"""

import os
import sys
import io
import requests
from bs4 import BeautifulSoup

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

# --- Config ---
BASE_URL = "https://proxy.vn"
PROXY_LIST_URL = f"{BASE_URL}/?home=donhangproxy"
COOKIES_FILE = "proxy_vn_cookies.txt"
PROXY_LIST_FILE = "proxy_list.txt"
CMS_API = "http://127.0.0.1:3000"


def normalize_proxy(s: str) -> str:
    if not s or not isinstance(s, str):
        return ""
    s = s.strip()
    for prefix in ("socks5h://", "socks5://", "http://", "https://"):
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
    return s.strip().lower()


# --- Proxy.vn: fetch danh sách ---
def fetch_proxy_list(cookies: dict) -> list[dict]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Referer": BASE_URL + "/",
    }
    resp = requests.get(PROXY_LIST_URL, cookies=cookies, headers=headers, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", id="proxyTable")
    if not table:
        raise ValueError("Không tìm thấy bảng proxy.")
    tbody = table.find("tbody") or []
    rows = []
    for tr in tbody.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue
        proxy_goc = (tds[4].get_text(strip=True) or "").strip()
        proxy_tg = (tds[5].get_text(strip=True) or "").strip()
        ptype = (tds[6].get_text(strip=True) or "").strip()
        if proxy_tg or proxy_goc:
            rows.append({
                "proxy_trung_gian": proxy_tg,
                "proxy_goc": proxy_goc,
                "type": ptype,
            })
    return rows


def login_http(username: str, password: str) -> dict | None:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Origin": BASE_URL,
        "Referer": BASE_URL + "/",
    })
    try:
        session.get(BASE_URL + "/", timeout=15)
        r = session.post(
            BASE_URL + "/dangnhap.php",
            data={"Ten_Login": username, "password": password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
            allow_redirects=True,
        )
        if "Số Dư" in r.text or "Đăng xuất" in r.text or "donhangproxy" in r.url:
            cookies = {c.name: c.value for c in session.cookies}
            if cookies:
                with open(COOKIES_FILE, "w", encoding="utf-8") as f:
                    for k, v in cookies.items():
                        f.write(f"{k}={v}\n")
                return cookies
    except Exception:
        pass
    return None


def login_playwright(username: str, password: str) -> dict:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        page = context.new_page()
        try:
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
            page.locator("#Ten_Login, input[name='Ten_Login']").first.fill(username)
            page.locator("#password, input[name='password']").first.fill(password)
            page.locator("button:has-text('Đăng nhập'), #dnbtdevModal button[type='submit']").first.click()
            page.wait_for_timeout(3000)
            if page.locator("#dnbtdevModal").is_visible():
                browser.close()
                raise ValueError("Sai tên đăng nhập hoặc mật khẩu")
            cookies = {c["name"]: c["value"] for c in context.cookies() if "proxy.vn" in c.get("domain", "")}
            browser.close()
            if cookies:
                with open(COOKIES_FILE, "w", encoding="utf-8") as f:
                    for k, v in cookies.items():
                        f.write(f"{k}={v}\n")
                return cookies
        except Exception as e:
            browser.close()
            raise e


def get_proxies_from_web(socks5_only: bool = False) -> tuple[list[str], bool]:
    """
    Lấy danh sách proxy HIỆN TẠI từ proxy.vn (nguồn chính xác).
    Chỉ dùng proxy_list.txt khi không fetch được (fallback).
    Returns: (proxies, from_cache) - from_cache=True nếu đọc từ file (có thể cũ).
    """
    # 1. Ưu tiên fetch MỚI từ proxy.vn (đúng với trang web hiện tại)
    cookies = {}
    try:
        with open(COOKIES_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    cookies[k.strip()] = v.strip()
    except FileNotFoundError:
        pass

    if cookies:
        try:
            rows = fetch_proxy_list(cookies)
            proxies = [(r["proxy_trung_gian"] or r["proxy_goc"]) for r in rows]
            if socks5_only:
                proxies = [p for p, r in zip(proxies, rows) if (r.get("type") or "").upper() == "SOCKS5"]
            with open(PROXY_LIST_FILE, "w", encoding="utf-8") as f:
                f.write("\n".join(proxies))
            return proxies, False
        except Exception:
            pass

    # 2. Fallback: đọc từ file (có thể cũ, thiếu proxy đã đổi)
    if os.path.exists(PROXY_LIST_FILE):
        with open(PROXY_LIST_FILE, encoding="utf-8") as f:
            proxies = [line.strip() for line in f if line.strip()]
        if proxies:
            return proxies, True

    return [], False


def get_proxies_from_cms() -> list[tuple[str, str]]:
    try:
        resp = requests.get(f"{CMS_API}/api/users", timeout=10)
        resp.raise_for_status()
        users = resp.json()
    except Exception as e:
        print(f"❌ Không kết nối CMS ({CMS_API}): {e}")
        sys.exit(1)
    return [(u.get("username", ""), (u.get("proxy") or "").strip()) for u in users if (u.get("proxy") or "").strip()]


def main():
    socks5_only = "--all" not in sys.argv  # Mặc định chỉ SOCKS5, --all để lấy cả HTTP
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    # Lấy credentials
    username = os.environ.get("PROXY_VN_USER", "").strip() or os.environ.get("PROXY_VN_PHONE", "").strip()
    password = os.environ.get("PROXY_VN_PASS", "").strip()
    if len(args) >= 2:
        username, password = args[0], args[1]
    elif len(args) == 1:
        password = args[0]
        username = input("Tên đăng nhập proxy.vn: ").strip()

    if username and password:
        print("Đang đăng nhập proxy.vn...", end=" ")
        cookies = login_http(username, password)
        if not cookies:
            try:
                cookies = login_playwright(username, password)
            except Exception as e:
                print(f"❌ {e}")
                sys.exit(1)
        print("OK")

    proxies_web, from_cache = get_proxies_from_web(socks5_only=socks5_only)
    if not proxies_web:
        print("❌ Không có proxy.")
        print("   Chạy: python proxy_unused.py 09xxx matkhau")
        sys.exit(1)

    if from_cache:
        print("⚠️ Đang dùng proxy_list.txt (có thể cũ). Chạy với user/pass để lấy danh sách mới nhất.\n")

    used = get_proxies_from_cms()
    used_norm = {normalize_proxy(p) for _, p in used}

    unused = [p for p in proxies_web if normalize_proxy(p) and normalize_proxy(p) not in used_norm]

    # In ra màn hình
    print(f"\n🔒 Proxy CHƯA DÙNG ({len(unused)}/{len(proxies_web)}):\n")
    for p in unused:
        print(p)
    if not unused:
        print("(Không có - tất cả proxy đã được sử dụng)")


if __name__ == "__main__":
    main()
