# -*- coding: utf-8 -*-
"""Liet ke tab Chrome CDP — kiem tra script co gan duoc tab game khong."""
from benbet_cdp_ws import list_cdp_targets
from benbet_chrome import CDP_URL, cdp_is_alive

if __name__ == "__main__":
    if not cdp_is_alive():
        print(f"Chrome CDP khong chay tai {CDP_URL}")
        print("Chay: python benbet_capture_browser.py ... --fresh-chrome")
    else:
        list_cdp_targets(CDP_URL)
