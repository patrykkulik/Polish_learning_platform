"""Session composition and the node unlock gate.

Composition runs **debt, then remediation, then new**, and introduces nothing new
while anything is overdue. That ordering is what makes the streak mean retention
rather than novelty: a learner with forty due cards cannot bank a day by starting
a fresh lesson.

The unlock gate reads `stability_max`, never current stability. FSRS schedules a
card to fall due exactly when its retrievability decays to 0.9, so "retrievability
>= 0.9" is a statement about being up to date, not about knowing anything — gate on
it and four days off would re-lock everything downstream. Stability is what does
not move while the learner sleeps.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pl.models import (
    Attempt,
    Card,
    ErrorEvent,
    Form,
    Item,
    Node,
    NodeLexeme,
    NodePrereq,
    NodeUnlock,
    Pattern,
    Review,
    Sense,
)
from pl.schedule import LEXICAL, MORPH, PATTERN, Rating, populations_for

#: Days of retention a card must once have reached to count toward mastery.
MASTERY_STABILITY_DAYS = 7.0
#: Fraction of a node's strata that must clear it.
MASTERY_FRACTION = 0.8
#: Successful reviews, and the calendar span they must cover.
MASTERY_MIN_REVIEWS = 3
MASTERY_MIN_SPAN_DAYS = 7

#: New cards introduced per day. Without a cap, one enthusiastic evening creates
#: a debt spike three days later that reads as punishment for engagement.
DAILY_NEW_CAP = 10
#: Total session length is deliberately **not** capped at M1 — see the design's
#: Optional hardening. Path A has one learner who can simply stop.


def user_today(settings: dict) -> date:
    tz = ZoneInfo(settings.get("tz", "UTC"))
    return datetime.now(tz).date()


def end_of_user_day(settings: dict) -> datetime:
    tz = ZoneInfo(settings.get("tz", "UTC"))
    local = datetime.now(tz)
    midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time())
    return midnight.replace(tzinfo=tz).astimezone(UTC).replace(tzinfo=None)


# ------------------------------------------------------------------ unlocking


def _prereqs(db: Session, node_id: int) -> list[Node]:
    ids = db.scalars(
        select(NodePrereq.prereq_node_id).where(NodePrereq.node_id == node_id)
    ).all()
    return [db.get(Node, i) for i in ids]


def is_unlocked(db: Session, user_id: int, node: Node) -> bool:
    """A node with no prerequisites is open from the start; everything else is
    latched by a row that is written once and never deleted."""
    if not _prereqs(db, node.id):
        return True
    return db.get(NodeUnlock, (user_id, node.id)) is not None


def _gating_refs(db: Session, node: Node) -> tuple[str, list[int]]:
    """The referents mastery is measured over, and their population.

    Content-fixed, not learner-fixed. The denominator counts the node's *strata*,
    not the cards that happen to exist — a stratum the learner has not met yet
    counts as unmastered rather than shrinking the denominator. Counting existing
    cards instead would let a node unlock at 3/3 and re-lock at 3/4 the moment a
    fourth card was lazily created.
    """
    if node.type == "grammar":
        ids = db.scalars(select(Pattern.id).where(Pattern.node_id == node.id)).all()
        return PATTERN, list(ids)
    if node.type == "vocabulary":
        lex_ids = db.scalars(
            select(NodeLexeme.lexeme_id).where(NodeLexeme.node_id == node.id)
        ).all()
        ids = db.scalars(
            select(Sense.id).where(Sense.lexeme_id.in_(lex_ids))
        ).all()
        return LEXICAL, list(ids)
    return "", []


def _card_is_mastered(db: Session, card: Card | None) -> bool:
    """Retained for a week, genuinely recalled, and known for a week.

    All three conditions are read from *this card's* review history:

    - `card.reps` counts every review and `card.lapses` counts `Again` only in
      the Review state, so `reps - lapses` is not the number of successful
      recalls and must not stand in for it.
    - The calendar span is a property of the card, not of the account. Measured
      account-wide it passes unconditionally once the learner is a week old, and
      the gate silently stops gating from then on.
    """
    if card is None or card.stability_max < MASTERY_STABILITY_DAYS:
        return False

    successes, first_success = db.execute(
        select(func.count(Review.id), func.min(Attempt.created_at))
        .join(Attempt, Attempt.id == Review.attempt_id)
        .where(Review.card_id == card.id, Review.rating >= int(Rating.Good))
    ).one()

    if successes < MASTERY_MIN_REVIEWS or first_success is None:
        return False
    return (datetime.now(UTC).replace(tzinfo=None) - first_success) >= timedelta(
        days=MASTERY_MIN_SPAN_DAYS
    )


def is_mastered(db: Session, user_id: int, node: Node) -> bool:
    """Mastery, evaluated over the population appropriate to the node's type.

    A function node introduces no competency of its own — it composes earlier
    nodes — so it is mastered exactly when its prerequisites are.
    """
    if node.type == "function":
        return all(is_mastered(db, user_id, p) for p in _prereqs(db, node.id))

    population, ref_ids = _gating_refs(db, node)
    if not ref_ids:
        raise AssertionError(
            f"node {node.key!r} of type {node.type!r} has no gating referents; "
            f"its unlock condition is undefined"
        )

    column = {LEXICAL: Card.sense_id, MORPH: Card.form_id, PATTERN: Card.pattern_id}[
        population
    ]
    mastered = 0
    for ref_id in ref_ids:
        card = db.scalar(
            select(Card).where(
                Card.user_id == user_id,
                Card.population == population,
                column == ref_id,
            )
        )
        if _card_is_mastered(db, card):
            mastered += 1
    return (mastered / len(ref_ids)) >= MASTERY_FRACTION


def evaluate_unlocks(db: Session, user_id: int) -> list[str]:
    """Latch any node whose prerequisites are now all mastered.

    Run at session end, never per review — a node unlocking mid-session would
    inject material into a session already composed.
    """
    newly: list[str] = []
    for node in db.scalars(select(Node)):
        if is_unlocked(db, user_id, node):
            continue
        prereqs = _prereqs(db, node.id)
        if prereqs and all(
            is_unlocked(db, user_id, p) and is_mastered(db, user_id, p)
            for p in prereqs
        ):
            db.add(
                NodeUnlock(
                    user_id=user_id,
                    node_id=node.id,
                    unlocked_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )
            newly.append(node.key)
    db.commit()
    return newly


# --------------------------------------------------------------- composition


def _items_for_card(db: Session, card: Card) -> list[Item]:
    if card.population == MORPH:
        stmt = select(Item).where(Item.target_form_id == card.form_id)
    elif card.population == PATTERN:
        stmt = select(Item).where(Item.pattern_id == card.pattern_id)
    else:
        sense = db.get(Sense, card.sense_id)
        stmt = (
            select(Item)
            .join(Node, Node.id == Item.node_id)
            .where(Node.type == "vocabulary", Item.pattern_id.is_(None))
        )
        return [
            i
            for i in db.scalars(stmt)
            if _lexeme_of_item(db, i) == sense.lexeme_id
        ]
    return list(db.scalars(stmt))


def _lexeme_of_item(db: Session, item: Item) -> int | None:
    from pl.models import Form

    if item.target_form_id is None:
        return None
    form = db.get(Form, item.target_form_id)
    return form.lexeme_id if form else None


def _started_referents(db: Session, user_id: int) -> set[tuple[str, int]]:
    """Every (population, referent) the learner already holds a card for.

    Keyed on the referent rather than on one referent *column*: a lexical card
    carries `sense_id` and leaves `form_id` null, so reading `form_id` alone
    reports every vocabulary item as never-seen forever.
    """
    started: set[tuple[str, int]] = set()
    for card in db.scalars(select(Card).where(Card.user_id == user_id)):
        ref = card.sense_id or card.form_id or card.pattern_id
        if ref is not None:
            started.add((card.population, ref))
    return started


def _sense_by_form(db: Session) -> dict[int, int]:
    """form id -> the sense of its lexeme, so a vocabulary item can name its card."""
    rows = db.execute(
        select(Form.id, Sense.id).join(Sense, Sense.lexeme_id == Form.lexeme_id)
    ).all()
    return {form_id: sense_id for form_id, sense_id in rows}


def _item_referents(
    node: Node, item: Item, sense_by_form: dict[int, int]
) -> set[tuple[str, int]]:
    """The cards this item would score, as (population, referent) pairs.

    Mirrors `schedule.cards_for_item` without creating anything — the same
    intersection of the routing table with what the node type permits.
    """
    allowed = populations_for(node)
    refs: set[tuple[str, int]] = set()
    if MORPH in allowed and item.target_form_id is not None:
        refs.add((MORPH, item.target_form_id))
    if PATTERN in allowed and item.pattern_id is not None:
        refs.add((PATTERN, item.pattern_id))
    if LEXICAL in allowed and item.target_form_id is not None:
        sense_id = sense_by_form.get(item.target_form_id)
        if sense_id is not None:
            refs.add((LEXICAL, sense_id))
    return refs


def weakest_node(db: Session, user_id: int, days: int = 14) -> Node | None:
    """The node with the highest error *rate* over the trailing window.

    A rate rather than a count, or the node with the most items always wins.
    """
    since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
    rows = db.execute(
        select(ErrorEvent.node_id, func.count(ErrorEvent.id))
        .join(Attempt, Attempt.id == ErrorEvent.attempt_id)
        .where(Attempt.user_id == user_id, Attempt.created_at >= since)
        .group_by(ErrorEvent.node_id)
    ).all()
    if not rows:
        return None

    best, best_rate = None, 0.0
    for node_id, errors in rows:
        total = db.scalar(
            select(func.count())
            .select_from(Attempt)
            .join(Item, Item.id == Attempt.item_id)
            .where(
                Attempt.user_id == user_id,
                Attempt.created_at >= since,
                Item.node_id == node_id,
            )
        )
        rate = errors / total if total else 0.0
        if rate > best_rate:
            best, best_rate = node_id, rate
    return db.get(Node, best) if best else None


def build_session(
    db: Session, user_id: int, settings: dict, limit: int = 20
) -> tuple[list[Item], dict]:
    """Compose the day's queue: debt, then remediation, then new."""
    horizon = end_of_user_day(settings)
    picked: list[Item] = []
    seen: set[int] = set()

    def add(item: Item) -> bool:
        if item.id in seen or len(picked) >= limit:
            return False
        seen.add(item.id)
        picked.append(item)
        return True

    # 1 — debt
    due = db.scalars(
        select(Card)
        .where(Card.user_id == user_id, Card.due_at <= horizon)
        .order_by(Card.due_at)
    ).all()
    for card in due:
        for item in _items_for_card(db, card):
            if add(item):
                break
    debt_total = len(due)
    debt_served = len(picked)

    # 2 — remediation, only once debt is exhausted
    if len(picked) < limit:
        weak = weakest_node(db, user_id)
        if weak is not None:
            for item in db.scalars(select(Item).where(Item.node_id == weak.id)):
                # `add` returns False both when the session is full and when the
                # item is already picked. Only the first is a reason to stop —
                # treating the second as one ends remediation on its first
                # overlap with the debt queue, which is the common case.
                if len(picked) >= limit:
                    break
                add(item)

    # 3 — new, and only while nothing is overdue
    introduced = 0
    if not due and len(picked) < limit:
        started = _started_referents(db, user_id)
        sense_by_form = _sense_by_form(db)
        # Round-robin across nodes rather than draining them in turn. Node order
        # is arbitrary, and taking one node at a time means the first unlocked
        # node swallows the whole daily cap — a learner would spend day one on
        # vocabulary alone and never reach the grammar the course is *for*.
        pools = [
            (node, iter(db.scalars(select(Item).where(Item.node_id == node.id)).all()))
            for node in db.scalars(select(Node))
            if is_unlocked(db, user_id, node)
        ]
        while pools and introduced < DAILY_NEW_CAP and len(picked) < limit:
            for entry in list(pools):
                node, pool = entry
                if introduced >= DAILY_NEW_CAP or len(picked) >= limit:
                    break
                for item in pool:
                    refs = _item_referents(node, item, sense_by_form)
                    # Skip only when the item is entirely old. A pattern card is
                    # shared by every lexeme in its stratum, so testing for *any*
                    # overlap lets the first item of a stratum claim it for all
                    # the others — the queue then dries up having offered a small
                    # fraction of the curriculum, with the rest reachable only by
                    # chance through remediation.
                    if refs and refs <= started:
                        continue
                    if add(item):
                        started |= refs
                        introduced += 1
                    break
                else:
                    pools.remove(entry)

    return picked, {
        "debt_total": debt_total,
        "debt_served": debt_served,
        "introduced": introduced,
    }
