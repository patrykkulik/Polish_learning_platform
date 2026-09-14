"""The streak, asserted. Criterion 12 had no tests at all.

`pl/streak.py` shipped with the learning loop and was never once exercised,
which is how two defects survived in about fifteen lines of arithmetic: the
absence reckoning re-ran on every call, spending the freezes it had already
spent and then breaking the streak they had just saved; and the daily-goal
condition believed whatever number the client put in the query string.

Both are mechanics the learner is asked to care about. A gamification feature
that silently miscounts is worse than none, because the learner cannot tell it
is wrong — they simply conclude they misremembered.
"""

from __future__ import annotations

import warnings
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from pl import models, schedule, streak as streaks
from pl.content import frames, ingest
from pl.models import Attempt, AppUser, Card, Item, Streak
from pl.session import user_today

warnings.filterwarnings("ignore", category=DeprecationWarning)

GOAL = 3
SETTINGS = {"tz": "UTC", "daily_goal_items": GOAL}


@pytest.fixture(scope="module")
def engine():
    """One built curriculum for the module.

    Safe to share, unlike in `test_journey.py`, because everything the streak
    reads — `streak`, `card`, `attempt` — is keyed by user, and each test gets a
    learner of its own. The curriculum itself is immutable here.
    """
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    ingest.ingest_all(session)
    frames.build(session)
    session.close()
    return engine


@pytest.fixture
def db(engine):
    session = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    yield session
    session.close()


@pytest.fixture
def user(db):
    row = AppUser(
        created_at=datetime.now(UTC).replace(tzinfo=None), settings_json=dict(SETTINGS)
    )
    db.add(row)
    db.commit()
    return row


def _answer(db, user, count: int) -> None:
    """Record `count` distinct items answered today, the way `/api/submit` does."""
    items = list(db.scalars(select(Item).limit(count)))
    assert len(items) == count, "the curriculum is too small for this test"
    for item in items:
        db.add(
            Attempt(
                user_id=user.id,
                item_id=item.id,
                submitted=item.expected_answer,
                created_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
    db.commit()


def _owe_a_card(db, user) -> None:
    """Give the learner one overdue card, so the debt condition fails."""
    item = db.scalar(select(Item))
    cards = schedule.cards_for_item(db, user.id, item)
    assert cards, "the item scored nothing, so it created no debt"
    for card in cards.values():
        card.due_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
    db.commit()


# --------------------------------------------------- both conditions, or neither


def test_the_streak_advances_when_both_conditions_hold(db, user):
    _answer(db, user, GOAL)
    row = streaks.record_activity(db, user.id, SETTINGS)
    assert row.current == 1
    # The learner's own day, not the machine's. These differ for several hours
    # daily in any zone offset from UTC, which would make this flake at dawn.
    assert row.last_completed_on == user_today(SETTINGS)


def test_the_streak_does_not_advance_on_debt_alone(db, user):
    """Clearing debt without meeting the goal is not a day.

    The specification gives only this condition, which advances the streak for
    free on any day with nothing due — including day one, and including a learner
    who has drifted into holding no cards at all.
    """
    _answer(db, user, GOAL - 1)
    assert streaks.record_activity(db, user.id, SETTINGS).current == 0


def test_the_streak_does_not_advance_with_work_left_undone(db, user):
    _answer(db, user, GOAL)
    _owe_a_card(db, user)
    assert streaks.record_activity(db, user.id, SETTINGS).current == 0


def test_a_second_session_the_same_day_does_not_advance_it_twice(db, user):
    _answer(db, user, GOAL)
    assert streaks.record_activity(db, user.id, SETTINGS).current == 1
    assert streaks.record_activity(db, user.id, SETTINGS).current == 1


# ------------------------------------------------------- the goal is a server fact


def test_the_goal_is_read_from_the_attempt_table(db, user):
    """Not from the caller.

    `complete()` used to take the count as a query parameter, so the browser
    reported its own homework and any learner who read the network tab could
    hand themselves a streak. The server already holds the authoritative count.
    """
    assert streaks.items_completed_today(db, user.id, SETTINGS) == 0
    assert streaks.record_activity(db, user.id, SETTINGS).current == 0

    _answer(db, user, GOAL)
    assert streaks.items_completed_today(db, user.id, SETTINGS) == GOAL
    assert streaks.record_activity(db, user.id, SETTINGS).current == 1


def test_answering_one_item_repeatedly_is_one_item(db, user):
    """The goal counts items met, not keystrokes.

    Otherwise "Another round" on a single item satisfies a twenty-item goal.
    """
    item = db.scalar(select(Item))
    for _ in range(GOAL + 5):
        db.add(
            Attempt(
                user_id=user.id,
                item_id=item.id,
                submitted=item.expected_answer,
                created_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
    db.commit()
    assert streaks.items_completed_today(db, user.id, SETTINGS) == 1


def test_yesterdays_work_does_not_count_toward_today(db, user):
    _answer(db, user, GOAL)
    for attempt in db.scalars(select(Attempt).where(Attempt.user_id == user.id)):
        attempt.created_at -= timedelta(days=1)
    db.commit()
    assert streaks.items_completed_today(db, user.id, SETTINGS) == 0


# ------------------------------------------------------------------ absences


def _returning_learner(db, user, *, gap_days: int, freezes: int) -> Streak:
    """A learner with a streak going, who was last here `gap_days` ago."""
    row = Streak(
        user_id=user.id,
        current=5,
        longest=5,
        freezes=freezes,
        last_completed_on=user_today(SETTINGS) - timedelta(days=gap_days),
    )
    db.add(row)
    db.commit()
    return row


def test_a_freeze_covers_a_missed_day(db, user):
    _returning_learner(db, user, gap_days=2, freezes=2)
    _answer(db, user, GOAL)
    row = streaks.record_activity(db, user.id, SETTINGS)
    assert row.freezes == 1, "one missed day should cost exactly one freeze"
    assert row.current == 6, "the freeze should have carried the streak"


def test_an_absence_is_paid_for_once_however_often_the_day_is_finished(db, user):
    """The defect this file exists for.

    `complete()` is a plain POST that the review page fires at the end of every
    round, and nothing stops a learner finishing two. The absence reckoning ran
    on each call: the first spent both freezes correctly, and the second found
    the same two-day gap with an empty freeze balance and broke a streak that
    had just been saved. The learner sees five days become zero for having
    studied harder.
    """
    _returning_learner(db, user, gap_days=3, freezes=2)

    for _ in range(4):
        row = streaks.record_activity(db, user.id, SETTINGS)

    assert row.freezes == 0, f"the gap cost {2 - row.freezes} freezes more than once"
    assert row.current == 5, (
        "the streak broke even though the freezes covered the whole absence"
    )


def test_a_gap_wider_than_the_freeze_balance_breaks_the_streak(db, user):
    """Freezes are a mitigation, not an exemption."""
    _returning_learner(db, user, gap_days=6, freezes=2)
    row = streaks.record_activity(db, user.id, SETTINGS)
    assert row.freezes == 0
    assert row.current == 0


def test_a_learner_who_never_left_pays_nothing(db, user):
    _returning_learner(db, user, gap_days=1, freezes=2)
    _answer(db, user, GOAL)
    row = streaks.record_activity(db, user.id, SETTINGS)
    assert row.freezes == 2, "a consecutive day must not cost a freeze"
    assert row.current == 6


# ------------------------------------------------------------------ timezone


def test_the_day_boundary_is_the_learners_own_midnight(db, user):
    """Criterion 12 evaluates the streak in the user's timezone.

    A UTC boundary silently breaks the streak for anyone west of Greenwich
    studying in the evening, which is most evenings for most people.
    """
    from zoneinfo import ZoneInfo

    from pl.session import end_of_user_day, start_of_user_day, user_today

    warsaw = {"tz": "Europe/Warsaw", "daily_goal_items": GOAL}
    los_angeles = {"tz": "America/Los_Angeles", "daily_goal_items": GOAL}

    # Not merely "different": the boundary must be that zone's own midnight.
    # Zones whose offsets differ by exactly 24 hours share a midnight instant,
    # so comparing two arbitrary zones can pass while proving nothing.
    for settings in (warsaw, los_angeles):
        tz = ZoneInfo(settings["tz"])
        boundary = start_of_user_day(settings).replace(tzinfo=UTC).astimezone(tz)
        assert (boundary.hour, boundary.minute) == (0, 0), (
            f"{settings['tz']} day starts at {boundary:%H:%M} local"
        )
        assert boundary.date() == user_today(settings)
        # 23 or 25 hours on the two days a year the clocks move, so the span is
        # bounded rather than fixed.
        span = end_of_user_day(settings) - start_of_user_day(settings)
        assert timedelta(hours=23) <= span <= timedelta(hours=25)

    assert start_of_user_day(warsaw) != start_of_user_day(los_angeles), (
        "two zones nine hours apart must not share a day boundary"
    )


# ------------------------------------------------------------------ progress


def test_progress_reports_retention_not_only_the_streak(db, user):
    """The mitigation that ships with the streak mechanic.

    Streaks reliably increase logins and do not reliably increase learning, so
    there has to be a number that is still true on the day the streak breaks.
    """
    figures = streaks.progress(db, user.id, SETTINGS)
    assert set(figures) >= {"streak", "longest", "freezes", "debt", "retained", "tracked"}
    assert figures["retained"] == 0 and figures["tracked"] == 0

    item = db.scalar(select(Item))
    for card in schedule.cards_for_item(db, user.id, item).values():
        card.stability_max = 30.0
    db.commit()

    figures = streaks.progress(db, user.id, SETTINGS)
    assert figures["tracked"] > 0
    assert figures["retained"] == figures["tracked"]


# ------------------------------------------- criterion 12, against real cards


def test_a_learner_who_finishes_a_real_session_earns_the_streak(db, user):
    """Criterion 12, against a learner who actually owns cards.

    **This is the test whose absence let the streak break.** Every other
    positive test in this file answers items without creating a single `Card`,
    so `debt_remaining` counts nothing and `debt_clear` is true for the wrong
    reason — the file's own docstring names "a learner who has drifted into
    holding no cards" as the hazard, and then builds every happy path on one.

    What that hid: FSRS puts a new card's first steps minutes apart, so a day's
    work leaves cards due within the hour. A rule capping each card at one
    schedule advance per day then made the day's debt unclearable by any amount
    of answering, and the streak unearnable on exactly the days the learner met
    new material.

    Nothing here rewrites `due_at` or `created_at`. Hand-setting the schedule is
    precisely what let the harness disagree with the scheduler.
    """
    from pl.api import grade_item
    from pl.session import build_session, start_of_user_day

    picked, _ = build_session(db, user.id, SETTINGS, limit=GOAL + 2)
    assert len(picked) >= GOAL, "the session was too short to meet the daily goal"

    for item in picked:
        attempt = Attempt(
            user_id=user.id,
            item_id=item.id,
            submitted=item.expected_answer,
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
        db.add(attempt)
        db.flush()
        schedule.apply_diagnosis(
            db,
            user.id,
            item,
            grade_item(db, item, item.expected_answer),
            attempt.id,
            start_of_user_day(SETTINGS),
        )
    db.commit()

    held = db.scalar(
        select(func.count()).select_from(Card).where(Card.user_id == user.id)
    )
    assert held > 0, (
        "the session created no cards, so this test cannot see criterion 12 at "
        "all — which is the exact blind spot it exists to close"
    )

    row = streaks.record_activity(db, user.id, SETTINGS)
    assert row.current == 1, (
        "a learner who answered a whole session correctly did not earn the "
        f"streak; {streaks.debt_remaining(db, user.id, SETTINGS)} cards are "
        f"still counted as due after the work that was supposed to clear them"
    )


def test_opening_the_app_mid_gap_costs_no_more_than_staying_away(db, user):
    """Showing up must never be punished harder than staying away.

    `missed` is recomputed from `last_completed_on` on every later day, while
    the freezes an earlier visit already spent are gone. The break test then
    compared the *whole* gap against *this* visit's spend, so a learner who
    opened the app during their absence paid a freeze for it and then lost the
    streak anyway — while the learner who stayed on the sofa kept it. Same gap,
    same freezes, opposite outcomes.
    """
    visitor = _returning_learner(db, user, gap_days=3, freezes=2)
    # What an earlier visit, one day into the gap, would have left behind.
    visitor.absence_settled_on = user_today(SETTINGS) - timedelta(days=1)
    visitor.freezes = 1
    db.commit()
    streaks.record_activity(db, user.id, SETTINGS)
    showed_up = (visitor.current, visitor.freezes)

    db.query(Streak).delete()
    db.commit()
    absentee = _returning_learner(db, user, gap_days=3, freezes=2)
    streaks.record_activity(db, user.id, SETTINGS)
    stayed_away = (absentee.current, absentee.freezes)

    assert showed_up == stayed_away, (
        f"opening the app mid-gap left the learner at {showed_up} while staying "
        f"away left them at {stayed_away} — the same absence, charged twice"
    )
    assert showed_up[0] == 5, "two freezes cover a two-day gap; the streak stands"
