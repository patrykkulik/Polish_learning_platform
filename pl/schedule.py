"""FSRS scheduling, and the routing table that decides which cards a submission fails.

The routing table is the design's spine. One submission touches several cards,
and the error class decides which of them are scored — not whether the answer was
"right".

Consider a cloze on the genitive of `sklep`:

    sklepie   a well-formed locative. The learner can inflect this noun and
              chose the wrong case, so the *pattern* card fails and the
              morphological card's schedule is left untouched.
    sklepa    not a word. The learner chose the genitive correctly and built it
              with the wrong paradigm's ending, so the *morphological* card fails
              and the pattern card **passes**.

Grading those together — which any string comparison does — teaches the learner
nothing and mis-schedules both.

`None` in the table means no review row is written at all: an unscored card keeps
its due date. That is deliberate, and it is why the table has three states rather
than two.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from fsrs import Card as FsrsCard
from fsrs import Rating, Scheduler
from sqlalchemy import select
from sqlalchemy.orm import Session

from pl.domain import STRICT_ORTHOGRAPHY, Diagnosis, ErrorClass
from pl.models import Attempt, Card, ErrorEvent, Form, Item, Node, Review, Sense

#: Stock parameters. Optimisation needs roughly a thousand reviews and pulls
#: torch; with one learner there is nothing to fit.
SCHEDULER: Final = Scheduler()

LEXICAL, MORPH, PATTERN = "lexical", "morph", "pattern"

#: error class -> {population: rating}. A population absent from the inner dict
#: is not scored.
#:
#: `Hard` has exactly one producer. A spelling slip must not fail the grammar
#: card, but calling it `Good` would pretend the answer was clean; `Hard` is
#: FSRS's own semantics for *recalled, with difficulty*, which is the true state.
#: `Easy` is unused — there is no calibrated signal to justify it until a latency
#: corpus exists.
ROUTING: Final[dict[ErrorClass, dict[str, Rating]]] = {
    ErrorClass.CORRECT: {
        LEXICAL: Rating.Good,
        MORPH: Rating.Good,
        PATTERN: Rating.Good,
    },
    ErrorClass.ORTHOGRAPHY: {
        LEXICAL: Rating.Hard,
        MORPH: Rating.Hard,
        PATTERN: Rating.Hard,
    },
    ErrorClass.CASE_WRONG: {PATTERN: Rating.Again},
    ErrorClass.ANIMACY: {PATTERN: Rating.Again},
    ErrorClass.NUMBER_WRONG: {PATTERN: Rating.Again},
    ErrorClass.GENDER_AGREEMENT: {PATTERN: Rating.Again},
    ErrorClass.ASPECT_WRONG: {PATTERN: Rating.Again},
    # The learner chose the case correctly and built it wrong: the government
    # rule was applied, the paradigm was not.
    ErrorClass.CASE_RIGHT_FORM_WRONG: {MORPH: Rating.Again, PATTERN: Rating.Good},
    ErrorClass.LEXICAL: {LEXICAL: Rating.Again},
    # Nothing is known about what went wrong, so both grammar cards fail.
    ErrorClass.UNANALYSABLE: {MORPH: Rating.Again, PATTERN: Rating.Again},
    # Multi-slot only. Both are syntax rather than morphology: the learner
    # produced the right forms, so the morphological card is left alone and the
    # rule card takes it.
    ErrorClass.WORD_ORDER: {PATTERN: Rating.Again},
    ErrorClass.MISSING_CONSTITUENT: {PATTERN: Rating.Again},
}


def populations_for(node: Node) -> set[str]:
    """Which populations an item under `node` is allowed to score.

    Composes with ROUTING by intersection. The routing table says what rating a
    population *would* get; the node type says which are eligible — so the
    table's `lexical` column is dead under every grammar node, and a cloze never
    credits vocabulary knowledge it did not test, because the prompt printed the
    lemma.
    """
    if node.type == "vocabulary":
        return {LEXICAL}
    if node.type == "grammar":
        return {MORPH, PATTERN}
    return set()


def _new_card(user_id: int, population: str, **ref) -> Card:
    # `due` is passed explicitly rather than left to FsrsCard's default, which
    # reads the `fsrs` package's own clock. In production the two agree to the
    # microsecond and it makes no difference; under a simulated clock they are
    # months apart, and a card whose population goes unscored would keep an
    # initial due date that the simulation can never reach. One clock, named
    # here, is the difference between a measuring instrument and a decoration.
    now = datetime.now(UTC)
    fresh = FsrsCard(due=now)
    return Card(
        user_id=user_id,
        population=population,
        fsrs_state_json=fresh.to_dict(),
        due_at=fresh.due.replace(tzinfo=None),
        # Stamped here rather than by the composer: this is the one place a card
        # comes into existence, so it is the only place the day it was introduced
        # can be recorded without the composer and the scheduler disagreeing.
        created_at=now.replace(tzinfo=None),
        **ref,
    )


def card_for(db: Session, user_id: int, population: str, ref_id: int) -> Card:
    """Fetch or lazily create the card for one referent.

    Cards are created on first exposure rather than up front, so a learner who
    never meets a lexeme never carries its cards.
    """
    column = {LEXICAL: Card.sense_id, MORPH: Card.form_id, PATTERN: Card.pattern_id}[
        population
    ]
    card = db.scalar(
        select(Card).where(
            Card.user_id == user_id, Card.population == population, column == ref_id
        )
    )
    if card is None:
        key = {LEXICAL: "sense_id", MORPH: "form_id", PATTERN: "pattern_id"}[population]
        card = _new_card(user_id, population, **{key: ref_id})
        db.add(card)
        db.flush()
    return card


def cards_for_item(db: Session, user_id: int, item: Item) -> dict[str, Card]:
    """Every card this item is allowed to score, created if absent."""
    node = db.get(Node, item.node_id)
    allowed = populations_for(node)
    out: dict[str, Card] = {}

    if LEXICAL in allowed and item.target_form_id is not None:
        form = db.get(Form, item.target_form_id)
        sense = db.scalar(select(Sense).where(Sense.lexeme_id == form.lexeme_id))
        if sense is not None:
            out[LEXICAL] = card_for(db, user_id, LEXICAL, sense.id)
    if MORPH in allowed and item.target_form_id is not None:
        out[MORPH] = card_for(db, user_id, MORPH, item.target_form_id)
    if PATTERN in allowed and item.pattern_id is not None:
        out[PATTERN] = card_for(db, user_id, PATTERN, item.pattern_id)
    return out


def retrievability(card: Card, at: datetime | None = None) -> float:
    """The card's predicted probability of recall right now.

    The *display* half of mastery (criterion 14). This decays while the learner
    sleeps, which is the whole point of showing it: `stability_max`, which the
    unlock gate reads, is a high-water mark and can only ever rise, so a progress
    bar drawn from it would tell a learner who has not studied in a month that
    they still know everything.
    """
    state = FsrsCard.from_dict(card.fsrs_state_json)
    return SCHEDULER.get_card_retrievability(state, at or datetime.now(UTC))


def apply_rating(db: Session, card: Card, rating: Rating, attempt_id: int) -> None:
    """Advance one card's schedule and record the review."""
    state = FsrsCard.from_dict(card.fsrs_state_json)
    updated, _log = SCHEDULER.review_card(state, rating, review_datetime=datetime.now(UTC))

    card.fsrs_state_json = updated.to_dict()
    card.due_at = updated.due.replace(tzinfo=None)
    card.reps += 1
    if rating is Rating.Again:
        card.lapses += 1
    # `stability` is None throughout the Learning state, which is every card's
    # first several reviews. Without the guard this raises on the first review of
    # every card ever created.
    card.stability_max = max(card.stability_max or 0.0, updated.stability or 0.0)

    db.add(Review(attempt_id=attempt_id, card_id=card.id, rating=int(rating)))


def ratings_for(item: Item, error_class: ErrorClass) -> dict[str, Rating]:
    """The routing entry, with dictation's stricter reading of a spelling slip."""
    ratings = ROUTING.get(error_class, {})
    if error_class is ErrorClass.ORTHOGRAPHY and item.exercise_type in STRICT_ORTHOGRAPHY:
        return dict.fromkeys(ratings, Rating.Again)
    return ratings


#: Advance a card's schedule at most once per day.
#:
#: A pattern card is shared by every item in its stratum, so a session holding
#: ten items of one rule reviewed that rule's card ten times, minutes apart. FSRS
#: grows stability from the interval actually elapsed, so those are ten intervals
#: of nearly zero: measured over ninety days, one card took **447 reviews** and
#: its stability stalled at 6.11, below the seven-day mastery bar, while a
#: low-traffic card in the same node reached 112 on eight reviews. The busiest
#: cards were the least able to master — crushed by their own popularity rather
#: than by anything the learner did or did not know.
#:
#: With this on, the busiest card takes 34-58 reviews instead of 196-578 — the
#: mechanism is gone on every seed tried — and the median ninety-day learner
#: opens eight nodes instead of four. Later encounters the same day still record
#: their attempt and their error events, so remediation still sees what went
#: wrong; only the *schedule* is left alone.
ONE_REVIEW_PER_DAY = True


def cards_advanced_since(db: Session, user_id: int, since: datetime) -> set[int]:
    """Cards whose schedule has already moved since `since`.

    A pattern card is shared by every item in its stratum, so a session holding
    ten items of one rule reviews that rule's card ten times. FSRS reads the
    interval since the last review; ten reviews minutes apart are ten intervals
    of nearly zero, and the card's stability is crushed by its own popularity
    rather than by the learner's ignorance.

    `since` is passed in rather than computed here, because the only correct
    boundary is the learner's local midnight and this module cannot see their
    settings without importing the composer that imports it. An earlier version
    took UTC midnight for itself, which put this rule on a different day from
    the streak, the daily goal and the debt horizon for every learner not living
    in UTC.

    Returned as a set of ids rather than a per-card predicate because the answer
    is needed three times over: to decide what to score, what counts as debt,
    and what the session may re-offer. Those three must agree.
    """
    return set(
        db.scalars(
            select(Review.card_id)
            .join(Attempt, Attempt.id == Review.attempt_id)
            .where(Attempt.user_id == user_id, Attempt.created_at >= since)
        )
    )


def apply_diagnosis(
    db: Session,
    user_id: int,
    item: Item,
    diagnosis: Diagnosis,
    attempt_id: int,
    day_start: datetime,
) -> dict[str, Rating]:
    """Route one diagnosis to the cards it scores. Returns what was applied.

    `day_start` is the learner's local midnight, supplied by the caller — see
    `cards_advanced_since`. It is required rather than defaulted, because the
    one thing this argument must never do is quietly disagree with the day the
    rest of the loop is counting.
    """
    cards = cards_for_item(db, user_id, item)
    ratings = ratings_for(item, diagnosis.error_class)
    settled = (
        cards_advanced_since(db, user_id, day_start) if ONE_REVIEW_PER_DAY else set()
    )
    applied: dict[str, Rating] = {}

    for population, card in cards.items():
        rating = ratings.get(population)
        if rating is None:
            continue  # unscored: this card's schedule is deliberately untouched
        if card.id in settled:
            continue
        apply_rating(db, card, rating, attempt_id)
        applied[population] = rating

    if diagnosis.error_class is not ErrorClass.CORRECT:
        db.add(
            ErrorEvent(
                attempt_id=attempt_id,
                node_id=item.node_id,
                error_class=str(diagnosis.error_class),
                slot_index=0,
                expected=item.expected_answer,
            )
        )
    db.flush()
    return applied
