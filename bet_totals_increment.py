# bet_totals_increment.py
# Tăng tổng cược theo user trên CMS (cùng API với minigame / slot).
from __future__ import annotations

import requests

_DEFAULT_API = "http://127.0.0.1:3000"


def _api_base() -> str:
    try:
        from constants import load_config

        c = load_config() or {}
        base = c.get("API_BASE")
        if isinstance(base, str) and base.strip():
            return base.rstrip("/")
    except Exception:
        pass
    return _DEFAULT_API


def increment_bet_totals(username: str, amount) -> None:
    """
    POST lên CMS để cộng thêm amount vào tổng cược của username.
    Thử vài dạng body/URL phổ biến; nếu server dùng route khác, chỉnh lại cho khớp CMS.
    """
    try:
        u = str(username or "").strip()
        delta = int(amount)
    except (TypeError, ValueError):
        return
    if not u or delta <= 0:
        return

    base = _api_base()
    attempts: list[tuple[str, dict]] = [
        (f"{base}/api/bet-totals/increment", {"username": u, "amount": delta}),
        (f"{base}/api/bet-totals/increment", {"username": u, "delta": delta}),
        (f"{base}/api/bet-totals", {"op": "increment", "username": u, "amount": delta}),
    ]

    last_status: int | None = None
    for url, payload in attempts:
        try:
            r = requests.post(url, json=payload, timeout=10)
            last_status = r.status_code
            if r.status_code < 400:
                return
        except Exception:
            continue

    if last_status is not None:
        print(
            f"⚠️ [bet_totals_increment] Không ghi được tổng cược cho {u} (+{delta}): "
            f"HTTP {last_status} (kiểm tra route CMS /api/bet-totals/increment).",
            flush=True,
        )
