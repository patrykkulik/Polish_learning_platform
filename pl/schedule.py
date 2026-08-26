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

from pl.domain import Diagnosis, ErrorClass
from pl.models import Card, ErrorEvent, Form, Item, Node, Review, Sense

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
    fresh = FsrsCard()
    return Card(
        user_id=user_id,
        population=population,
        fsrs_state_json=fresh.to_dict(),
        due_at=fresh.due.replace(tzinfo=None),
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


def apply_diagnosis(
    db: Session,
    user_id: int,
    item: Item,
    diagnosis: Diagnosis,
    attempt_id: int,
) -> dict[str, Rating]:
    """Route one diagnosis to the cards it scores. Returns what was applied."""
    cards = cards_for_item(db, user_id, item)
    ratings = ROUTING.get(diagnosis.error_class, {})
    applied: dict[str, Rating] = {}

    for population, card in cards.items():
        rating = ratings.get(population)
        if rating is None:
            continue  # unscored: this card's schedule is deliberately untouched
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
