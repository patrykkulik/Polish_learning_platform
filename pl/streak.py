"""The streak, with both conditions and a real timezone.

The specification advances the streak on clearing the day's review debt. That
alone advances it for free on a day with nothing due — including day one, and
including a learner who has drifted into having no cards. Requiring the daily
goal *as well* makes both mechanics load-bearing instead of redundant.

The day boundary is the learner's local midnight. A UTC boundary silently breaks
the streak for anyone west of Greenwich studying in the evening, which is most
evenings for most people.

Streak mechanics reliably increase logins and do not reliably increase learning.
Two mitigations ship with them: loss is recoverable through freezes, and
`progress` returns a retention figure that is real, so there is something
truthful to look at on the day the streak eventually breaks.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pl.models import Card, Streak
from pl.session import MASTERY_STABILITY_DAYS, end_of_user_day, user_today

#: Held at most two, earned one per ten advanced days.
MAX_FREEZES = 2
DAYS_PER_FREEZE = 10


def _streak(db: Session, user_id: int) -> Streak:
    row = db.get(Streak, user_id)
    if row is None:
        row = Streak(user_id=user_id, freezes=MAX_FREEZES)
        db.add(row)
        db.flush()
    return row


def debt_remaining(db: Session, user_id: int, settings: dict) -> int:
    """Cards still due before the learner's local midnight."""
    return db.scalar(
        select(func.count())
        .select_from(Card)
        .where(Card.user_id == user_id, Card.due_at <= end_of_user_day(settings))
    )


def _apply_absence(row: Streak, today: date) -> None:
    """Spend freezes across missed days, then break the streak.

    Freezes are consumed silently rather than prompting: a learner returning
    after a gap should find their streak intact, not a decision to make.
    """
    if row.last_completed_on is None:
        return
    missed = (today - row.last_completed_on).days - 1
    if missed <= 0:
        return
    spend = min(missed, row.freezes)
    row.freezes -= spend
    if missed > spend:
        row.current = 0


def record_activity(
    db: Session, user_id: int, settings: dict, items_completed: int
) -> Streak:
    """Advance the streak if, and only if, both conditions hold today."""
    row = _streak(db, user_id)
    today = user_today(settings)

    if row.last_completed_on == today:
        return row

    _apply_absence(row, today)

    goal = settings.get("daily_goal_items", 20)
    goal_met = items_completed >= goal
    debt_clear = debt_remaining(db, user_id, settings) == 0

    if goal_met and debt_clear:
        row.current += 1
        row.longest = max(row.longest, row.current)
        row.last_completed_on = today
        if row.current % DAYS_PER_FREEZE == 0:
            row.freezes = min(MAX_FREEZES, row.freezes + 1)

    db.commit()
    return row


def progress(db: Session, user_id: int, settings: dict) -> dict:
    """Streak alongside a metric that survives it breaking."""
    row = _streak(db, user_id)
    retained = db.scalar(
        select(func.count())
        .select_from(Card)
        .where(
            Card.user_id == user_id,
            Card.stability_max >= MASTERY_STABILITY_DAYS,
        )
    )
    total = db.scalar(
        select(func.count()).select_from(Card).where(Card.user_id == user_id)
    )
    return {
        "streak": row.current,
        "longest": row.longest,
        "freezes": row.freezes,
        "debt": debt_remaining(db, user_id, settings),
        "retained": retained,
        "tracked": total,
    }
