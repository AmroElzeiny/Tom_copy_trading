from __future__ import annotations

import math


def floor_volume_to_step(volume: float, min_volume: float, max_volume: float, step: float) -> float:
    if volume <= 0 or min_volume <= 0 or max_volume <= 0 or step <= 0:
        return 0.0
    bounded = min(volume, max_volume)
    steps = math.floor((bounded + 1e-12) / step)
    normalized = steps * step
    if normalized + 1e-12 < min_volume:
        return 0.0
    decimals = max(0, min(8, len(str(step).split(".")[1]) if "." in str(step) else 0))
    return round(normalized, decimals)


def risk_sized_volume(
    *,
    equity: float,
    risk_pct: float,
    loss_per_one_lot: float,
    min_volume: float,
    max_volume: float,
    step: float,
    hard_max_volume: float,
) -> float:
    if equity <= 0 or risk_pct <= 0 or loss_per_one_lot <= 0:
        return 0.0
    risk_money = equity * (risk_pct / 100.0)
    raw = risk_money / loss_per_one_lot
    return floor_volume_to_step(raw, min_volume, min(max_volume, hard_max_volume), step)


def partial_close_volume(
    *, current_volume: float, fraction: float, min_volume: float, step: float
) -> float:
    if current_volume <= 0 or not (0 < fraction <= 1) or min_volume <= 0 or step <= 0:
        return 0.0
    if fraction >= 0.999999:
        return current_volume
    requested = current_volume * fraction
    close_vol = floor_volume_to_step(requested, min_volume, current_volume, step)
    if close_vol <= 0:
        return 0.0
    residual = current_volume - close_vol
    if residual > 1e-12 and residual < min_volume:
        max_partial = floor_volume_to_step(current_volume - min_volume, min_volume, current_volume, step)
        return max_partial
    return close_vol
