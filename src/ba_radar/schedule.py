"""Schedule gating.

GitHub Actions cron is UTC-only, but the digest is promised in Kyiv local time, which
moves between UTC+2 and UTC+3. Rather than pick one and drift twice a year, each
workflow schedules *both* candidate UTC hours and this gate decides which one is
actually 08:00 in Kyiv today.

The tolerance window exists because scheduled runs are routinely late by 5-30 minutes.
A run that starts at 08:24 Kyiv should still deliver; one that starts at 09:30 should
not, and is picked up by the catch-up workflow instead (answers doc Q22).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class GateDecision:
    proceed: bool
    reason: str
    local_time: datetime


def evaluate_gate(
    now: datetime,
    *,
    timezone: str,
    target_hour: int,
    tolerance_minutes: int,
    weekdays_only: bool,
) -> GateDecision:
    tz = ZoneInfo(timezone)
    local = now.astimezone(tz)

    if weekdays_only and local.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        return GateDecision(False, f"{local:%A} is not a working day", local)

    minutes_past = (local.hour - target_hour) * 60 + local.minute
    if minutes_past < 0:
        return GateDecision(
            False,
            f"local time {local:%H:%M} is before the {target_hour:02d}:00 target",
            local,
        )
    if minutes_past > tolerance_minutes:
        return GateDecision(
            False,
            f"local time {local:%H:%M} is {minutes_past} min past the "
            f"{target_hour:02d}:00 target (tolerance {tolerance_minutes} min)",
            local,
        )

    return GateDecision(True, f"local time {local:%H:%M} is within the window", local)
