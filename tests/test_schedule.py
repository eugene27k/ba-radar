"""The Kyiv local-time gate (answers doc Q21/Q22).

The point of these tests is that the two UTC crons must produce exactly one delivery
per working day on both sides of a DST change — never zero, never two.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ba_radar.schedule import evaluate_gate

# Summer (EEST, UTC+3): 05:00 UTC is 08:00 Kyiv.
# Winter (EET,  UTC+2): 06:00 UTC is 08:00 Kyiv.
SUMMER_DAY = (2026, 8, 3)  # a Monday
WINTER_DAY = (2026, 12, 7)  # a Monday


def gate(year: int, month: int, day: int, hour: int, minute: int = 0, **kwargs: object):
    defaults: dict[str, object] = {
        "timezone": "Europe/Kyiv",
        "target_hour": 8,
        "tolerance_minutes": 59,
        "weekdays_only": True,
    }
    defaults.update(kwargs)
    return evaluate_gate(
        datetime(year, month, day, hour, minute, tzinfo=UTC),
        **defaults,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("day", "utc_hour", "expected"),
    [
        (SUMMER_DAY, 5, True),  # 08:00 Kyiv — deliver
        (SUMMER_DAY, 6, False),  # 09:00 Kyiv — too late
        (WINTER_DAY, 5, False),  # 07:00 Kyiv — too early
        (WINTER_DAY, 6, True),  # 08:00 Kyiv — deliver
    ],
)
def test_exactly_one_cron_fires_per_day(
    day: tuple[int, int, int], utc_hour: int, expected: bool
) -> None:
    assert gate(*day, utc_hour).proceed is expected


@pytest.mark.parametrize("season", [SUMMER_DAY, WINTER_DAY])
def test_both_crons_together_deliver_exactly_once(season: tuple[int, int, int]) -> None:
    fired = [gate(*season, hour).proceed for hour in (5, 6)]
    assert sum(fired) == 1


def test_a_late_run_still_delivers_within_tolerance() -> None:
    """Scheduled runs are routinely 5-30 minutes late; 08:24 Kyiv must still count."""
    assert gate(*SUMMER_DAY, 5, 24).proceed


def test_a_very_late_run_is_left_to_the_catch_up() -> None:
    assert not gate(*SUMMER_DAY, 6, 30).proceed


def test_weekend_is_skipped_when_weekdays_only() -> None:
    saturday = gate(2026, 8, 1, 5)
    sunday = gate(2026, 8, 2, 5)
    assert not saturday.proceed
    assert not sunday.proceed
    assert "not a working day" in saturday.reason


def test_weekend_collection_is_allowed_with_any_day() -> None:
    assert gate(2026, 8, 1, 5, weekdays_only=False).proceed


def test_catchup_hour_is_independent_of_the_digest_hour() -> None:
    # 08:00 UTC is 11:00 Kyiv in summer.
    assert gate(*SUMMER_DAY, 8, target_hour=11).proceed
    assert not gate(*SUMMER_DAY, 5, target_hour=11).proceed
