"""Simulate a learner day by day, and report what the curriculum actually gives them.

    uv run python scripts/journey_sim.py [days] [session-limit] [accuracy]
    uv run python scripts/journey_sim.py 60 20 0.85

The headline content counts — 983 items, 82 lexemes, 13 nodes — say what was
built. They say nothing about what a learner *meets*, and the two turned out to
differ by a factor of fifty: before the composition order was fixed, a diligent
learner answered 1,190 questions over sixty days and saw twenty distinct items,
no grammar at all, and unlocked nothing. Every headline number was true.

This script exists so that pacing claims are measured rather than asserted. It
is the thing that caught that defect, and `tests/test_journey.py` pins the floor
it establishes.

**The clock is advanced by substituting `datetime` in the two modules that read
it**, rather than by back-dating rows. That distinction matters: FSRS intervals,
the end-of-day horizon, the daily introduction budget, the mastery elapsed-time
gate and the unlock timestamps then all see the same simulated day. A harness
that shifts some clocks and not others measures itself — which is exactly how
the first version of this simulation hid the budget bug for an afternoon.

Nothing here imports pytest, and nothing asserts. It prints a table; reading it
is the point.
"""

from __future__ import annotations

import datetime as _dt
import random
import sys
from collections import Counter

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from pl import models
from pl import schedule as _schedule
from pl import session as _session
from pl import streak as streaks
from pl.api import grade_item
from pl.content import frames, ingest
from pl.models import Attempt, Card, Form, Item, Lexeme, Node, NodeUnlock
from pl.schedule import apply_diagnosis
from pl.session import build_session, evaluate_unlocks, start_of_user_day

#: A fixed point to start from. Arbitrary, but it must be *fixed*: an earlier
#: version of this file read the real clock and added the simulated day to it,
#: and that made the whole simulation irreproducible. FSRS grows stability from
#: the interval actually elapsed, so real microseconds between reviews leak into
#: card schedules; a node unlock is a cliff, and one card mastering a day earlier
#: opens a node with hundreds of items behind it. Two runs of the *same*
#: configuration came back with 209 and 370 items met. A measuring instrument
#: that disagrees with itself by 80% cannot settle an argument about pacing.
EPOCH = _dt.datetime(2026, 1, 5, 9, 0, tzinfo=_dt.UTC)

#: The simulated present. Advanced only by `start_day` and `tick`.
CLOCK = EPOCH

#: How long the learner is taken to spend on one item.
SECONDS_PER_ITEM = 45


def start_day(day: int) -> None:
    """Begin simulated day `day` (1-based) at the epoch's hour."""
    global CLOCK
    CLOCK = EPOCH + _dt.timedelta(days=day - 1)


def tick(seconds: int = SECONDS_PER_ITEM) -> None:
    """Advance the simulated present, as answering an item would."""
    global CLOCK
    CLOCK += _dt.timedelta(seconds=seconds)


class FakeDatetime(_dt.datetime):
    """`datetime` whose `now()` is the simulated present, not the real one.

    Subclasses `datetime` rather than standing in for it, because the modules
    under test also use `datetime.combine`, `datetime.min` and
    `datetime.fromisoformat`, and those must keep working.
    """

    @classmethod
    def now(cls, tz=None):
        return CLOCK.astimezone(tz) if tz is not None else CLOCK.replace(tzinfo=None)


def wrong_answer(db, item: Item, rng: random.Random) -> str:
    """A *real* wrong answer: another cell of the same paradigm where one exists.

    Typing gibberish would be classified `UNANALYSABLE` and route differently
    from the mistakes learners actually make, so the debt it generates would not
    resemble a learner's debt — and debt is the thing criterion 9 blocks new
    material on.
    """
    if item.target_form_id is not None:
        form = db.get(Form, item.target_form_id)
        if form is not None:
            others = [
                f.surface
                for f in db.scalars(
                    select(Form).where(Form.lexeme_id == form.lexeme_id)
                )
                if f.surface != form.surface
            ]
            if others:
                return rng.choice(others)
    return "xxx"


def simulate(
    days: int, limit: int, accuracy: float, seed: int = 7, inspect=None
) -> tuple[list[dict], dict]:
    """Run the loop for `days`, answering correctly `accuracy` of the time.

    `inspect`, if given, is called with `(db, user)` once the last day is over
    and before the session closes — for asking the finished world questions the
    per-day rows cannot answer, such as *which* mastery condition is holding a
    node shut.
    """
    # Patched here rather than at import, and restored afterwards. As a module
    # side effect this rewrote `datetime` inside two production modules for
    # anything that imported the harness — and the symptom, mastery spans and
    # FSRS intervals that never elapse, looks like a product bug rather than a
    # harness one. The design's own lesson about instruments that shift some
    # clocks and not others applies equally to one that shifts clocks for code
    # it is not measuring.
    restore = (_session.datetime, _schedule.datetime, random.getstate())
    _session.datetime = FakeDatetime
    _schedule.datetime = FakeDatetime

    engine = create_engine("sqlite://", future=True)
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    ingest.ingest_all(db)
    frames.build(db)
    user = ingest.ensure_user(db)

    # The goal the app gives a new learner, not the session length. Setting it to
    # `limit` measured a goal the product never had once the two differed.
    settings = dict(user.settings_json, tz="UTC")
    rng = random.Random(seed)
    # FSRS applies a few percent of random "fuzz" to every interval it computes,
    # deliberately, to stop reviews piling onto one day — and it draws that from
    # the *global* RNG. Seeding it keeps production's real behaviour (fuzz stays
    # on) while making the run reproducible. Left unseeded, two runs of the same
    # configuration disagreed by 80% on how much of the curriculum was reached,
    # because a fuzzed interval can flip a card's mastery day and a node unlock
    # is a cliff with hundreds of items behind it.
    random.seed(seed)
    form_lexeme = {f.id: f.lexeme_id for f in db.scalars(select(Form))}

    seen: set[int] = set()
    lexemes: set[int] = set()
    by_type: Counter = Counter()
    rows: list[dict] = []

    for day in range(1, days + 1):
        start_day(day)

        picked, stats = build_session(db, user.id, settings, limit=limit)
        for item in picked:
            # Answering takes time, and FSRS reads that time. Advancing the
            # simulated clock per item is what keeps within-session intervals
            # realistic *and* identical from run to run.
            tick()
            seen.add(item.id)
            by_type[item.exercise_type] += 1
            if item.target_form_id in form_lexeme:
                lexemes.add(form_lexeme[item.target_form_id])

            submitted = (
                item.expected_answer
                if rng.random() < accuracy
                else wrong_answer(db, item, rng)
            )
            attempt = Attempt(
                user_id=user.id,
                item_id=item.id,
                submitted=submitted,
                latency_ms=4000,
                created_at=FakeDatetime.now(_dt.UTC).replace(tzinfo=None),
            )
            db.add(attempt)
            db.flush()
            apply_diagnosis(
                db, user.id, item, grade_item(db, item, submitted), attempt.id,
                start_of_user_day(settings),
            )
        db.commit()
        evaluate_unlocks(db, user.id)
        # Finish the day the way the review page does. Without this the
        # instrument cannot see criterion 12 at all — which is how a defect that
        # made the streak unearnable on every productive day went unmeasured
        # through a ninety-day simulation.
        streak_row = streaks.record_activity(db, user.id, settings)

        rows.append(
            {
                "day": day,
                "served": len(picked),
                "streak": streak_row.current,
                "advanced": streak_row.last_completed_on == _session.user_today(settings),
                "debt_after": streaks.debt_remaining(db, user.id, settings),
                "new_cards": stats["introduced"],
                "new_items": stats["introduced_items"],
                "debt": stats["debt_total"],
                "items_seen": len(seen),
                "lexemes": len(lexemes),
                "unlocked": db.scalar(
                    select(func.count())
                    .select_from(NodeUnlock)
                    .where(NodeUnlock.user_id == user.id)
                ),
                "cards": db.scalar(
                    select(func.count())
                    .select_from(Card)
                    .where(Card.user_id == user.id)
                ),
            }
        )

    totals = {
        "items": db.scalar(select(func.count()).select_from(Item)),
        "lexemes": db.scalar(select(func.count()).select_from(Lexeme)),
        "nodes": db.scalar(select(func.count()).select_from(Node)),
        "by_type": dict(by_type),
    }
    if inspect is not None:
        inspect(db, user)
    db.close()
    engine.dispose()
    _session.datetime, _schedule.datetime, state = restore
    random.setstate(state)
    return rows, totals


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    accuracy = float(sys.argv[3]) if len(sys.argv) > 3 else 0.85

    rows, totals = simulate(days, limit, accuracy)

    print(
        f"content built: {totals['items']} items, {totals['nodes']} nodes, "
        f"{totals['lexemes']} lexemes"
    )
    print(f"learner: {days} days, {limit} per session, {accuracy:.0%} accuracy\n")
    print(
        f"{'day':>4} {'served':>7} {'new':>5} {'debt':>6} {'left':>5} {'seen':>6} "
        f"{'lexemes':>8} {'nodes':>6} {'cards':>6} {'streak':>7}"
    )
    for r in rows:
        if r["day"] <= 10 or r["day"] % 5 == 0 or r["day"] == days:
            print(
                f"{r['day']:>4} {r['served']:>7} {r['new_items']:>5} {r['debt']:>6} "
                f"{r['debt_after']:>5} {r['items_seen']:>6} {r['lexemes']:>8} "
                f"{r['unlocked']:>6} {r['cards']:>6} {r['streak']:>7}"
            )

    last = rows[-1]
    print(f"\nafter {days} days:")
    print(
        f"  distinct items met     {last['items_seen']:>5} of {totals['items']}"
        f"  ({100 * last['items_seen'] / totals['items']:.1f}%)"
    )
    print(f"  distinct lexemes met   {last['lexemes']:>5} of {totals['lexemes']}")
    print(f"  nodes unlocked         {last['unlocked']:>5} of {totals['nodes']}")
    print(f"  exercise types met     {totals['by_type']}")
    introduced = [r["day"] for r in rows if r["new_items"] > 0]
    print(f"  last day new material was introduced: {max(introduced, default=0)}")
    # Criterion 12, which this instrument could not see until it started
    # finishing each day the way the review page does. A streak stuck at zero
    # across a diligent ninety days means the day's debt is never clearing, and
    # that is a defect however healthy the coverage numbers above look.
    #
    # Days it *advanced*, not days it stood above zero. The second counts a
    # streak carried by freezes or merely not yet broken, and was once quoted as
    # the first.
    advanced = sum(1 for r in rows if r["advanced"])
    print(
        f"  streak                 {last['streak']:>5} days, advanced on "
        f"{advanced} of {days}"
    )


if __name__ == "__main__":
    main()
