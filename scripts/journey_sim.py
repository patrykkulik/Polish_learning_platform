"""Simulate a learner day by day, and report what the curriculum actually gives them.

    uv run python scripts/journey_sim.py [days] [session-limit] [accuracy]
    uv run python scripts/journey_sim.py 60 20 0.85

The headline content counts — 1,024 items, 76 lexemes, 13 nodes — say what was
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
from pl.api import grade_item
from pl.content import frames, ingest
from pl.models import Attempt, Card, Form, Item, Lexeme, Node, NodeUnlock
from pl.schedule import apply_diagnosis
from pl.session import build_session, evaluate_unlocks

#: Mutated by the day loop; read by `FakeDatetime.now`.
OFFSET = _dt.timedelta(0)


class FakeDatetime(_dt.datetime):
    """`datetime` with the simulated day added to every `now()`."""

    @classmethod
    def now(cls, tz=None):
        return _dt.datetime.now(tz) + OFFSET


_session.datetime = FakeDatetime
_schedule.datetime = FakeDatetime


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


def simulate(days: int, limit: int, accuracy: float, seed: int = 7) -> list[dict]:
    """Run the loop for `days`, answering correctly `accuracy` of the time."""
    engine = create_engine("sqlite://", future=True)
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    ingest.ingest_all(db)
    frames.build(db)
    user = ingest.ensure_user(db)

    settings = {"tz": "UTC", "daily_goal_items": limit}
    rng = random.Random(seed)
    form_lexeme = {f.id: f.lexeme_id for f in db.scalars(select(Form))}

    seen: set[int] = set()
    lexemes: set[int] = set()
    by_type: Counter = Counter()
    rows: list[dict] = []

    global OFFSET
    for day in range(1, days + 1):
        OFFSET = _dt.timedelta(days=day - 1)

        picked, stats = build_session(db, user.id, settings, limit=limit)
        for item in picked:
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
                db, user.id, item, grade_item(db, item, submitted), attempt.id
            )
        db.commit()
        evaluate_unlocks(db, user.id)

        rows.append(
            {
                "day": day,
                "served": len(picked),
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
    db.close()
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
        f"{'day':>4} {'served':>7} {'new':>5} {'debt':>6} {'seen':>6} "
        f"{'lexemes':>8} {'nodes':>6} {'cards':>6}"
    )
    for r in rows:
        if r["day"] <= 10 or r["day"] % 5 == 0 or r["day"] == days:
            print(
                f"{r['day']:>4} {r['served']:>7} {r['new_items']:>5} {r['debt']:>6} "
                f"{r['items_seen']:>6} {r['lexemes']:>8} {r['unlocked']:>6} "
                f"{r['cards']:>6}"
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


if __name__ == "__main__":
    main()
