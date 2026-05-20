# -*- coding: utf-8 -*-
"""
Chia tiền một bên Tài/Xỉu — bội step_vnd, tổng = side_total_vnd.

  - Acc 1: random 10k .. 100k (hoặc hết phần còn lại) — có thể 1 acc ăn cả 100k
  - Acc 2+: random 10k .. còn lại
  - Acc 6 (mặc định): dồn hết phần còn lại (nếu còn)

Hai bên Tài/Xỉu mỗi bên tổng = side_total_vnd (cân nhau).
"""

from __future__ import annotations

import random


def split_side_total(
    side_total_vnd: int,
    n_players: int,
    step_vnd: int,
    *,
    dump_at_player: int = 6,
) -> list[int]:
    """
    Chia một bên; số acc thực đánh có thể 1..dump_at_player (không bắt buộc đủ 6).
    """
    total = int(side_total_vnd)
    n = max(1, int(n_players))
    step = int(step_vnd)
    dump_at = max(1, int(dump_at_player))
    n_active = min(n, dump_at)

    if step < 1 or total < step:
        raise ValueError(f"Tổng bên phải ≥ {step:,} VND")
    if total % step != 0:
        raise ValueError("side_total_vnd phải chia hết cho bet_step_vnd")

    remain = total
    parts: list[int] = []

    for i in range(n_active):
        if remain <= 0:
            break

        player_no = i + 1
        if player_no == dump_at:
            parts.append(remain)
            remain = 0
            break

        if remain < step:
            parts.append(remain)
            remain = 0
            break

        pick = random.choice(list(range(step, remain + 1, step)))
        parts.append(int(pick))
        remain -= pick

    if remain > 0:
        if parts:
            parts[-1] += remain
        else:
            parts.append(remain)

    if sum(parts) != total:
        raise RuntimeError(f"split lỗi: {parts} tổng {sum(parts):,} != {total:,}")

    return parts
