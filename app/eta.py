"""Rough arrival estimates from the two pins. No traffic data: a range, not a promise.

Straight-line distance times a road factor (Kampala's roads wind), at boda speeds from
slow (jam) to fast (clear). Before pickup, add the time for a rider to reach the shop and
collect. Every figure is a tunable guess, kept here in one place.
"""

from __future__ import annotations

import math

ROAD_FACTOR = 1.4
SLOW_KMH, FAST_KMH = 18.0, 25.0
TO_SHOP_MIN = (15, 25)  # a rider reaching the shop and collecting the parcel


def distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance (haversine)."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _round5(minutes: float, up: bool) -> int:
    step = math.ceil(minutes / 5) if up else math.floor(minutes / 5)
    return max(5, int(step * 5))


def estimate_minutes(
    pickup: tuple[float | None, float | None], drop: tuple[float | None, float | None], *, picked_up: bool
) -> tuple[int, int] | None:
    """(low, high) minutes until the parcel arrives, or None if a pin is missing.
    picked_up=False: from now, including reaching the shop. True: from the shop."""
    if None in pickup or None in drop:
        return None
    road = distance_km(*pickup, *drop) * ROAD_FACTOR
    low, high = road / FAST_KMH * 60, road / SLOW_KMH * 60
    if not picked_up:
        low, high = low + TO_SHOP_MIN[0], high + TO_SHOP_MIN[1]
    low_r, high_r = _round5(low, up=False), _round5(high, up=True)
    return low_r, max(high_r, low_r + 10)


def describe(minutes: tuple[int, int] | None) -> str:
    """'about 25–40 min' (or 'about 45 min–1 h 45 min' for long trips); '' with no estimate."""
    if minutes is None:
        return ""
    low, high = minutes
    if high < 90:
        return f"about {low}–{high} min"

    def fmt(m: int) -> str:
        return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"

    return f"about {fmt(low)}–{fmt(high)}"
