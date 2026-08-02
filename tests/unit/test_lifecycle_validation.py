from datetime import UTC, datetime, time, timedelta

from personal_agent.services.lifecycle import is_valid_proposed_time


def test_time_proposals_must_be_future_preserve_date_and_avoid_quiet_hours() -> None:
    now = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)

    assert (
        is_valid_proposed_time(now - timedelta(minutes=1), now, "Asia/Jerusalem", None, None)
        is False
    )
    assert (
        is_valid_proposed_time(
            now + timedelta(days=1),
            now,
            "Asia/Jerusalem",
            None,
            None,
            explicit_date=now.date(),
        )
        is False
    )
    assert (
        is_valid_proposed_time(
            datetime(2026, 8, 1, 21, 30, tzinfo=UTC),
            now,
            "Asia/Jerusalem",
            time(23, 0),
            time(7, 0),
        )
        is False
    )
    assert (
        is_valid_proposed_time(
            datetime(2026, 8, 1, 16, 0, tzinfo=UTC),
            now,
            "Asia/Jerusalem",
            time(23, 0),
            time(7, 0),
        )
        is True
    )
