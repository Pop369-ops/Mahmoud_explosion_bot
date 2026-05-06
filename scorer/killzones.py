"""
ICT Killzones — high-probability time windows.

Times in UTC (Saudi Arabia is UTC+3 — adjust mentally if reading logs):

- Asian Session: 23:00 - 04:00 UTC (low volatility, accumulation)
- London Killzone: 06:00 - 09:00 UTC (high volatility, often sets daily high/low)
- NY Killzone: 13:00 - 16:00 UTC (peak volatility, real moves)
- London-NY Overlap: 13:00 - 16:00 UTC (highest volume period)

Trades during killzones have higher win rate because that's when institutions
are active. Trades outside killzones (esp. Asian session for non-Asian pairs)
should require higher confidence to take.
"""
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta


@dataclass
class KillzoneInfo:
    name: str            # "asian", "london", "ny", "overlap", "off_hours"
    is_killzone: bool    # True for london, ny, overlap
    quality_mult: float  # multiplier for signal confidence (0.7..1.2)
    description_ar: str


def get_current_killzone() -> KillzoneInfo:
    """Returns info about the current trading session in UTC."""
    now = datetime.now(timezone.utc)
    hour = now.hour

    # London Killzone (06:00-09:00 UTC = 09:00-12:00 KSA)
    if 6 <= hour < 9:
        return KillzoneInfo(
            name="london",
            is_killzone=True,
            quality_mult=1.15,
            description_ar="🇬🇧 London Killzone (وقت تداول مثالي)",
        )
    # NY Killzone (13:00-16:00 UTC = 16:00-19:00 KSA)
    if 13 <= hour < 16:
        return KillzoneInfo(
            name="ny",
            is_killzone=True,
            quality_mult=1.20,
            description_ar="🇺🇸 New York Killzone (أعلى سيولة)",
        )
    # London-NY overlap (12:00-13:00 UTC) — sometimes considered
    if hour == 12:
        return KillzoneInfo(
            name="overlap",
            is_killzone=True,
            quality_mult=1.10,
            description_ar="🌍 London-NY Overlap",
        )
    # Pre-London (04:00-06:00) — early breakouts
    if 4 <= hour < 6:
        return KillzoneInfo(
            name="pre_london",
            is_killzone=False,
            quality_mult=0.95,
            description_ar="⏳ ما قبل London (هدوء قبل العاصفة)",
        )
    # Late NY / Pre-Asian (16:00-22:00)
    if 16 <= hour < 22:
        return KillzoneInfo(
            name="late_ny",
            is_killzone=False,
            quality_mult=0.95,
            description_ar="📉 Late NY (تباطؤ الزخم)",
        )
    # Asian session (22:00-04:00) — slow, often manipulation
    return KillzoneInfo(
        name="asian",
        is_killzone=False,
        quality_mult=0.80,
        description_ar="🌏 Asian Session (سيولة منخفضة — حذر)",
    )


def is_high_quality_time() -> bool:
    """Quick check: are we in a killzone right now?"""
    return get_current_killzone().is_killzone
