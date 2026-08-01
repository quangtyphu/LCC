# -*- coding: utf-8 -*-
"""
Chia tiền một bên Tài/Xỉu — logic random chiaTien_Tho (LC79), không cố định số acc.

Mỗi lần: random step .. min(max_per, còn lại); lặp đến hết tổng.
Số acc/bên tự nhiên (1 acc ăn 50k, hoặc 2–5 acc tùy random).
"""

from __future__ import annotations

import random


def split_side_total(
    side_total_vnd: int,
    step_vnd: int,
    *,
    max_per_user_vnd: int = 0,
    n_players: int = 0,
    dump_at_player: int = 0,
) -> list[int]:
    """
    Chia một bên Tài/Xỉu (tổng = side_total_vnd).
    n_players / dump_at_player: bỏ qua (giữ tham số cũ cho tương thích).
    """
    del n_players, dump_at_player

    total = int(side_total_vnd)
    step = max(1, int(step_vnd))
    max_per = int(max_per_user_vnd or 0)
    if max_per <= 0:
        max_per = total

    if total < step:
        raise ValueError(f"Tổng bên phải ≥ {step:,} VND")
    if total % step != 0:
        raise ValueError("side_total_vnd phải chia hết cho bet_step_vnd")

    parts: list[int] = []
    remain = total

    while remain > 0:
        if remain <= step:
            parts.append(remain)
            break

        max_allowed = min(remain, max_per)

        if max_allowed < step:
            if max_allowed > 0:
                parts.append(max_allowed)
            break

        pick = random.choice(range(step, max_allowed + 1, step))
        parts.append(int(pick))
        remain -= pick

    if sum(parts) != total:
        raise RuntimeError(f"split lỗi: {parts} tổng {sum(parts):,} != {total:,}")

    return parts
