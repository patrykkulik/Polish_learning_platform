"""The M1 learning loop: content, routing, composition, unlocking, streak.

Criterion 11 carries the design's weight and is asserted directly: one answer
moves different cards depending on *which* grammatical decision was wrong.
"""

from __future__ import annotations

import warnings
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from pl import models, schedule
from pl.content import frames, ingest
from pl.domain import ErrorClass
from pl.models import Card, Form, Item, Lexeme, Node, NodeUnlock, Pattern
from pl.schedule import MORPH, PATTERN, ROUTING
from pl.session import (
    DAILY_NEW_CAP,
    build_session,
    evaluate_unlocks,
    is_mastered,
    is_unlocked,
)

warnings.filterwarnings("ignore", category=DeprecationWarning)


@pytest.fixture(scope="module")
def db():
    engine = create_engine("sqlite://", future=True)
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    s = Session()
    ingest.ingest_all(s)
    frames.build(s)
    yield s
    s.close()


@pytest.fixture(scope="module")
def user(db):
    return ingest.ensure_user(db)


def _item(db, lemma: str, fragment: str) -> Item:
    lexeme = db.scalar(select(Lexeme).where(Lexeme.lemma == lemma))
    for item in db.scalars(select(Item).where(Item.exercise_type == "cloze")):
        form = db.get(Form, item.target_form_id)
        if form and form.lexeme_id == lexeme.id and fragment in item.prompt:
            return item
    raise LookupError(f"no cloze item for {lemma} / {fragment}")


# ------------------------------------------------------------------ content


def test_content_build_produces_enough_items(db):
    """Acceptance criterion 16."""
    assert db.scalar(select(func.count()).select_from(Item)) >= 300


def test_every_expected_answer_is_a_real_form(db):
    """Criterion 16's second half, re-asserted over what actually landed.

    A multi-slot item's expected answer is a sentence, so the check there is that
    the inflected target occurs in it as a whole token — not that the entire
    answer is one paradigm cell.
    """
    from pl.content.frames import MULTI_SLOT

    for item in db.scalars(select(Item)):
        form = db.get(Form, item.target_form_id)
        assert form is not None
        if item.exercise_type in MULTI_SLOT:
            from pl.grade.classify import tokenise

            assert form.surface.casefold() in tokenise(item.expected_answer)
        else:
            assert form.surface == item.expected_answer


def test_paradigm_class_splits_what_gender_cannot(db):
    """The stratification key must separate lexemes that inflect differently.

    At M1 the strata happen to align with gender. They must not be *derived* from
    it: `sklep` and `chleb` are both m3 and take different genitives, so a
    gender-keyed stratum would look correct here and silently mix difficulties
    from M2 onward.
    """
    kot = db.scalar(select(Lexeme).where(Lexeme.lemma == "kot:Sm2"))
    pies = db.scalar(select(Lexeme).where(Lexeme.lemma == "pies:Sm2"))
    ptak = db.scalar(select(Lexeme).where(Lexeme.lemma == "ptak"))
    # Same gender, but `pies` has the mobile-e alternation and is genuinely harder.
    assert kot.gender == pies.gender == "m2"
    assert kot.paradigm_class == ptak.paradigm_class
    assert pies.paradigm_class != kot.paradigm_class


def test_accusative_node_is_stratified(db):
    """N05 carries several strata, so the 80% threshold is a real fraction."""
    n05 = db.scalar(select(Node).where(Node.key == "N05"))
    strata = db.scalars(select(Pattern).where(Pattern.node_id == n05.id)).all()
    assert len(strata) >= 3


# ------------------------------------------------------------------ routing


def test_routing_table_covers_every_class_the_classifier_emits():
    # Every class is reachable now that multi-slot items exist; nothing is
    # excepted, so a new class added without a routing entry fails here.
    assert set(ErrorClass) <= set(ROUTING)


def test_wrong_case_fails_the_rule_and_leaves_the_form_alone(db, user):
    """Acceptance criterion 11, first half.

    `kot` for `kota` is a well-formed nominative: the learner can inflect this
    noun and chose the wrong case. The pattern card fails; the morphological
    card's schedule must not move.
    """
    item = _item(db, "kot:Sm2", "Widzę")
    cards = schedule.cards_for_item(db, user.id, item)
    before = cards[MORPH].due_at

    attempt = _attempt(db, user, item, "kot")
    from pl.grade import classify
    from pl.api import expected_slot

    diagnosis = classify(expected_slot(db, item), "kot")
    assert diagnosis.error_class is ErrorClass.ANIMACY

    applied = schedule.apply_diagnosis(db, user.id, item, diagnosis, attempt.id)
    assert PATTERN in applied and MORPH not in applied
    assert cards[MORPH].due_at == before


def test_wrong_ending_fails_the_form_and_passes_the_rule(db, user):
    """Acceptance criterion 11, second half.

    `kotu` is a real dative — wrong case. To exercise the other branch we need a
    non-word built on the right case, which is what CASE_RIGHT_FORM_WRONG means:
    the government rule was applied, the paradigm was not.
    """
    from pl.api import expected_slot
    from pl.grade import classify

    item = _item(db, "sklep", "Widzę")
    diagnosis = classify(expected_slot(db, item), "sklepa")
    assert diagnosis.error_class is ErrorClass.CASE_RIGHT_FORM_WRONG

    attempt = _attempt(db, user, item, "sklepa")
    applied = schedule.apply_diagnosis(db, user.id, item, diagnosis, attempt.id)
    assert applied[MORPH] == 1, "the form is wrong"
    assert applied[PATTERN] == 3, "but the case was chosen correctly"


def test_orthography_does_not_fail_the_grammar_card(db, user):
    from pl.api import expected_slot
    from pl.grade import classify

    item = _item(db, "kawa", "Mam")
    diagnosis = classify(expected_slot(db, item), "kawe")
    assert diagnosis.error_class is ErrorClass.ORTHOGRAPHY

    attempt = _attempt(db, user, item, "kawe")
    applied = schedule.apply_diagnosis(db, user.id, item, diagnosis, attempt.id)
    assert set(applied.values()) == {2}, "Hard on both, failing neither"


def test_one_submission_produces_one_attempt_and_its_fan_out(db, user):
    """Acceptance criterion 10."""
    from pl.api import expected_slot
    from pl.grade import classify

    item = _item(db, "okno", "Widzę")
    attempt = _attempt(db, user, item, "qwerty")
    diagnosis = classify(expected_slot(db, item), "qwerty")
    schedule.apply_diagnosis(db, user.id, item, diagnosis, attempt.id)
    db.flush()

    reviews = db.scalars(
        select(models.Review).where(models.Review.attempt_id == attempt.id)
    ).all()
    errors = db.scalars(
        select(models.ErrorEvent).where(models.ErrorEvent.attempt_id == attempt.id)
    ).all()
    assert len(reviews) == 2 and len(errors) == 1


def _attempt(db, user, item, answer) -> models.Attempt:
    attempt = models.Attempt(
        user_id=user.id,
        item_id=item.id,
        submitted=answer,
        created_at=datetime.now(UTC).replace(tzinfo=None),
    )
    db.add(attempt)
    db.flush()
    return attempt


# ------------------------------------------------------------- stability_max


def test_stability_max_survives_a_lapse(db, user):
    """The gate must not move when the schedule does.

    A lapse lowers current stability and pulls the card forward. The high-water
    mark is what the unlock gate reads, and it may not follow.
    """
    item = _item(db, "dom", "Widzę")
    card = schedule.cards_for_item(db, user.id, item)[MORPH]
    attempt = _attempt(db, user, item, "dom")

    for _ in range(3):
        schedule.apply_rating(db, card, schedule.Rating.Good, attempt.id)
    peak = card.stability_max
    assert peak > 0

    schedule.apply_rating(db, card, schedule.Rating.Again, attempt.id)
    assert card.stability_max == peak


def test_first_review_of_a_fresh_card_does_not_raise(db, user):
    """`Card.stability` is None throughout the Learning state."""
    item = _item(db, "morze", "Widzę")
    card = schedule.cards_for_item(db, user.id, item)[MORPH]
    attempt = _attempt(db, user, item, "morze")
    schedule.apply_rating(db, card, schedule.Rating.Good, attempt.id)
    assert card.stability_max >= 0


# ---------------------------------------------------------------- unlocking


def test_root_nodes_are_open_and_others_are_not(db, user):
    v01 = db.scalar(select(Node).where(Node.key == "V01"))
    n05 = db.scalar(select(Node).where(Node.key == "N05"))
    assert is_unlocked(db, user.id, v01)
    assert not is_unlocked(db, user.id, n05)


def test_unlock_never_re_locks(db, user):
    """Acceptance criterion 13.

    The denominator counts the node's strata, not the cards that happen to
    exist — so creating a card for a stratum the learner had not yet met cannot
    drop the ratio and take back access already granted.
    """
    n01 = db.scalar(select(Node).where(Node.key == "N01"))
    db.add(
        NodeUnlock(
            user_id=user.id,
            node_id=n01.id,
            unlocked_at=datetime.now(UTC).replace(tzinfo=None),
        )
    )
    db.commit()
    assert is_unlocked(db, user.id, n01)

    # Create a brand-new, unmastered card in one of N01's strata.
    pattern = db.scalar(select(Pattern).where(Pattern.node_id == n01.id))
    schedule.card_for(db, user.id, PATTERN, pattern.id)
    db.commit()

    assert is_unlocked(db, user.id, n01), "an unlocked node must stay unlocked"


def test_mastery_gate_uses_the_population_the_node_type_implies(db, user):
    """Acceptance criterion 19.

    A vocabulary node has no pattern cards. Gating it on them would divide by
    zero on the first edge of M1's own graph.
    """
    v01 = db.scalar(select(Node).where(Node.key == "V01"))
    assert is_mastered(db, user.id, v01) is False  # not yet, but no exception


def test_a_node_with_no_gating_referents_fails_loudly(db, user):
    orphan = Node(key="ORPHAN", type="grammar", title="Orphan")
    db.add(orphan)
    db.flush()
    with pytest.raises(AssertionError, match="no gating referents"):
        is_mastered(db, user.id, orphan)
    db.delete(orphan)
    db.commit()


# -------------------------------------------------------------- composition


def test_new_cards_are_capped_per_day(db, user):
    """Acceptance criterion 15."""
    items, stats = build_session(db, user.id, {"tz": "UTC"}, limit=100)
    assert stats["introduced"] <= DAILY_NEW_CAP


def test_debt_is_served_before_anything_new(db, user):
    """Acceptance criterion 9.

    With something overdue, the session must not introduce new material.
    """
    card = db.scalar(select(Card).where(Card.user_id == user.id))
    card.due_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=2)
    db.commit()

    _items, stats = build_session(db, user.id, {"tz": "UTC"}, limit=20)
    assert stats["debt_total"] > 0
    assert stats["introduced"] == 0
