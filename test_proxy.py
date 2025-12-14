#!/usr/bin/env python3
# proxy_check.py
# Kiểm tra SOCKS5 proxy (host:port:user:pass)
# - TCP connect tới wtx.tele68.com:443
# - HTTP request tới https://icanhazip.com (socks5h)
# - WebSocket connect tới WS_URL (dùng same socket)

import socket
import socks
import requests
import asyncio
import websockets
import json
import sys
from urllib.parse import urlparse

WS_URL = "wss://wtx.tele68.com/tx/?EIO=4&transport=websocket"
HTTP_TEST_URL = "https://icanhazip.com/"   # returns your public IP plain text
TCP_TEST_HOST = "wtx.tele68.com"
TCP_TEST_PORT = 443

def parse_proxy(proxy_str: str):
    parts = proxy_str.strip().split(":")
    if len(parts) != 4:
        raise ValueError("Proxy phải có dạng host:port:user:pass")
    host, port_s, user, pwd = parts
    try:
        port = int(port_s)
    except:
        raise ValueError("Port không hợp lệ")
    return host, port, user, pwd

def tcp_test(host, port, host_proxy, port_proxy, user, pwd, timeout=10):
    print(f"\n[1] TCP test -> connect {host}:{port} via {host_proxy}:{port_proxy} (SOCKS5)")
    try:
        sock = socks.socksocket()
        sock.set_proxy(socks.SOCKS5, host_proxy, port_proxy, True, user, pwd)
        sock.settimeout(timeout)
        sock.connect((host, port))
        print("✅ TCP connect OK (socket connected)")
        sock.close()
        return True
    except Exception as e:
        print("❌ TCP connect FAILED:", repr(e))
        return False

def http_test(proxy_host, proxy_port, user, pwd, timeout=20):
    print(f"\n[2] HTTP test -> GET {HTTP_TEST_URL} via socks5h://{proxy_host}:{proxy_port} (with auth)")
    proxy_url = f"socks5h://{user}:{pwd}@{proxy_host}:{proxy_port}"
    proxies = {"http": proxy_url, "https": proxy_url}
    try:
        r = requests.get(HTTP_TEST_URL, proxies=proxies, timeout=timeout)
        print("HTTP status:", r.status_code)
        print("Response (truncated):", (r.text or "").strip()[:200])
        if r.status_code == 200:
            print("✅ HTTP/HTTPS via proxy OK (and DNS resolved through proxy if 'socks5h' supported).")
            return True
        else:
            print("⚠️ HTTP returned non-200 status")
            return False
    except Exception as e:
        print("❌ HTTP test FAILED:", repr(e))
        return False

async def ws_test(ws_url, proxy_host, proxy_port, user, pwd, timeout=10):
    print(f"\n[3] WebSocket test -> connect {ws_url} via socks5 {proxy_host}:{proxy_port}")
    try:
        # Prepare a socks socket like in your handle_ws
        sock = socks.socksocket()
        sock.set_proxy(socks.SOCKS5, proxy_host, proxy_port, True, user, pwd)
        sock.settimeout(timeout)
        # connect underlying TCP
        sock.connect((TCP_TEST_HOST, TCP_TEST_PORT))
        print("-> TCP connect for WS OK, now attempting WebSocket handshake ...")
        # wrap in websockets.connect using existing socket
        async with websockets.connect(ws_url, sock=sock, ssl=True, ping_interval=None) as ws:
            print("✅ WebSocket handshake OK")
            try:
                # try simple handshake recv/send actions similar to handle_ws
                await ws.recv()   # may or may not return; safe to have timeout wrapper
            except Exception:
                # not critical; we got handshake success already
                pass
            return True
    except Exception as e:
        print("❌ WebSocket test FAILED:", repr(e))
        return False

def main():
    print("Proxy quick-check (SOCKS5 with auth)\n")
    proxy_str = input("Nhập proxy (host:port:user:pass) hoặc ENTER để thoát: ").strip()
    if not proxy_str:
        print("Thoát.")
        return

    try:
        host_p, port_p, user, pwd = parse_proxy(proxy_str)
    except Exception as e:
        print("Sai format proxy:", e)
        return

    ok_tcp = tcp_test(TCP_TEST_HOST, TCP_TEST_PORT, host_p, port_p, user, pwd)
    ok_http = http_test(host_p, port_p, user, pwd)

    # WebSocket test only if TCP succeeded
    ok_ws = False
    if ok_tcp:
        try:
            ok_ws = asyncio.run(ws_test(WS_URL, host_p, port_p, user, pwd))
        except Exception as e:
            print("❌ WebSocket async runner error:", repr(e))
            ok_ws = False
    else:
        print("\nBỏ qua WS test do TCP test fail.")

    print("\n=== SUMMARY ===")
    print(f"TCP connect: {'OK' if ok_tcp else 'FAIL'}")
    print(f"HTTP via proxy: {'OK' if ok_http else 'FAIL'}")
    print(f"WebSocket handshake: {'OK' if ok_ws else 'FAIL'}")
    print("Nếu HTTP OK mà WS FAIL => proxy có thể không hỗ trợ long-lived TLS/WS hoặc server chặn proxy.")
    print("Nếu TCP FAIL => proxy không kết nối tới host:port (dead / blocked).")

if __name__ == "__main__":
    main()
