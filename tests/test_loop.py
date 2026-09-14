"""The M1 learning loop: content, routing, composition, unlocking, streak.

Criterion 11 carries the design's weight and is asserted directly: one answer
moves different cards depending on *which* grammatical decision was wrong.
"""

from __future__ import annotations

import warnings
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from pl import models, schedule
from pl.content import frames, ingest
from pl.domain import ErrorClass
from pl.models import Attempt, Card, Form, Item, Lexeme, Node, NodeUnlock, Pattern
from pl.schedule import MORPH, PATTERN, ROUTING
from pl.session import (
    DAILY_NEW_CAP,
    DEBT_TOLERANCE,
    build_session,
    evaluate_unlocks,
    is_mastered,
    is_unlocked,
)

warnings.filterwarnings("ignore", category=DeprecationWarning)


def _day_start():
    """The learner day these tests count in. `apply_diagnosis` needs the
    boundary explicitly so it cannot silently pick a different one."""
    from pl.session import start_of_user_day

    return start_of_user_day({"tz": "UTC"})


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

    applied = schedule.apply_diagnosis(
        db, user.id, item, diagnosis, attempt.id, _day_start()
    )
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
    applied = schedule.apply_diagnosis(
        db, user.id, item, diagnosis, attempt.id, _day_start()
    )
    assert applied[MORPH] == 1, "the form is wrong"
    assert applied[PATTERN] == 3, "but the case was chosen correctly"


def test_orthography_does_not_fail_the_grammar_card(db, user):
    from pl.api import expected_slot
    from pl.grade import classify

    item = _item(db, "kawa", "Mam")
    diagnosis = classify(expected_slot(db, item), "kawe")
    assert diagnosis.error_class is ErrorClass.ORTHOGRAPHY

    attempt = _attempt(db, user, item, "kawe")
    applied = schedule.apply_diagnosis(
        db, user.id, item, diagnosis, attempt.id, _day_start()
    )
    assert set(applied.values()) == {2}, "Hard on both, failing neither"


def test_one_submission_produces_one_attempt_and_its_fan_out(db, user):
    """Acceptance criterion 10."""
    from pl.api import expected_slot
    from pl.grade import classify

    item = _item(db, "okno", "Widzę")
    attempt = _attempt(db, user, item, "qwerty")
    diagnosis = classify(expected_slot(db, item), "qwerty")
    schedule.apply_diagnosis(
        db, user.id, item, diagnosis, attempt.id, _day_start()
    )
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


def test_debt_past_the_bound_stops_anything_new(db, user):
    """Acceptance criterion 9, as bounded.

    The criterion as first written forbade new material while *anything* was
    overdue, and this test asserted that with a single overdue card. Measurement
    replaced the rule with `DEBT_TOLERANCE`: one overdue card is now explicitly
    allowed to introduce, so the old assertion described behaviour the code had
    deliberately stopped having.

    It went on passing anyway. The fixtures here are module-scoped, so by the
    time this ran, earlier tests had left well past five cards due and the day's
    budget already spent — the assertion held for reasons that had nothing to do
    with the sentence above it, and failed the moment it was run on its own.
    Both are fixed here: the backlog is built past the bound explicitly, and the
    budget is freed so that "introduced nothing" can only mean the bound bit.
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    # Build the cards this test needs rather than inheriting whatever earlier
    # tests happened to leave. Depending on that is the other half of why the
    # old version passed: it ran green in a full suite and failed on its own.
    for item in db.scalars(select(Item).limit(DEBT_TOLERANCE + 3)):
        schedule.cards_for_item(db, user.id, item)
    db.commit()

    cards = list(db.scalars(select(Card).where(Card.user_id == user.id)))
    assert len(cards) > DEBT_TOLERANCE, "too few cards to build a backlog past the bound"
    for index, card in enumerate(cards):
        card.due_at = (
            now - timedelta(days=2) if index <= DEBT_TOLERANCE else now + timedelta(days=30)
        )
        # Yesterday's introductions, so today's cap is not already spent — or
        # this test passes without the bound doing anything.
        if card.created_at is not None:
            card.created_at = now - timedelta(days=1)
    # A backlog is work left over from previous days; a card whose schedule
    # already moved today is not debt.
    for attempt in db.scalars(select(Attempt).where(Attempt.user_id == user.id)):
        attempt.created_at = now - timedelta(days=1)
    db.commit()

    _items, stats = build_session(db, user.id, {"tz": "UTC"}, limit=20)
    assert stats["debt_total"] > 0
    assert stats["introduced"] == 0


# ------------------------------------------------- the build's own invariants


def test_the_blank_replaces_a_whole_token_not_a_substring():
    """`str.replace` finds the target inside a longer word and blanks that.

    `Mama ma kota.` blanked for `ma` produced `M___ma ma kota.` — the exercise
    is nonsense and the answer is still sitting in the prompt.
    """
    assert frames._blank("Mama ma kota.", "ma") == "Mama ___ kota."


def test_the_blank_finds_a_sentence_initial_target():
    """Targets are stored as paradigm cells, which are lower-case; a sentence
    capitalises its first word. A case-sensitive replace matched nothing, so the
    prompt was returned verbatim — showing the learner the answer."""
    assert frames._blank("Kota nie ma.", "kota") == "___ nie ma."


def test_a_target_that_is_absent_is_a_build_error():
    """Never a silent pass-through: that is exactly the leak."""
    with pytest.raises(AssertionError, match="does not occur"):
        frames._blank("Widzę psa.", "kota")


def test_no_prompt_contains_its_own_answer(db):
    """The property the blanking exists to guarantee, over the real corpus."""
    for item in db.scalars(select(Item).where(Item.exercise_type == "cloze")):
        assert "___" in item.prompt, f"{item.prompt!r} has no blank"
        assert item.expected_answer.casefold() not in item.prompt.casefold(), (
            f"prompt {item.prompt!r} leaks its answer {item.expected_answer!r}"
        )


def test_a_rejected_sentence_leaves_no_half_built_database(monkeypatch):
    """The build is one transaction or it is a trap.

    `build_items` used to commit before `build_sentence_items` ran, so a corpus
    error left a database holding template items and no sentence items — a state
    that looks like a successful build to everything downstream.
    """
    engine = create_engine("sqlite://", future=True)
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    ingest.ingest_all(session)

    monkeypatch.setattr(
        frames,
        "_sentences",
        lambda: [
            {
                "text": "Widzę zzzzz.",
                "target": "zzzzz",
                "lemma": "kot:Sm2",
                "case": "acc",
                "rule_key": "acc.direct-object",
                "gloss": "nonsense",
            }
        ],
    )
    with pytest.raises(AssertionError):
        frames.build(session)

    session.rollback()
    assert session.scalar(select(func.count()).select_from(Item)) == 0, (
        "a failed build committed items anyway"
    )
    session.close()


# ------------------------------------------- reconciling against a live database


def _fresh():
    engine = create_engine("sqlite://", future=True)
    models.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)()


def test_dropping_an_aspect_partner_clears_the_link(monkeypatch):
    """The upsert wrote links and never removed one.

    `aspect_partner` drives the ASPECT_WRONG diagnosis. Removing the declaration
    from the curriculum left the old id in place, so the grader kept contrasting
    a verb against a partner the curriculum no longer paired it with.
    """
    session = _fresh()
    lexemes = ingest.ingest_lexemes(session)
    linked = next(
        lx for lx in lexemes.values() if lx.aspect_partner_id is not None
    )
    lemma = linked.lemma

    real = ingest._load

    def without_partners(name):
        rows = real(name)
        if name == "lexemes.yaml":
            return [{k: v for k, v in r.items() if k != "aspect_partner"} for r in rows]
        return rows

    monkeypatch.setattr(ingest, "_load", without_partners)
    ingest.ingest_lexemes(session)

    again = session.scalar(select(Lexeme).where(Lexeme.lemma == lemma))
    assert again.aspect_partner_id is None, (
        "the curriculum no longer pairs these verbs, but the link survived"
    )
    session.close()


def test_a_withdrawn_lexeme_is_reported_not_left_behind(monkeypatch):
    """Deleting it would take the learner's cards with it, so this must be loud.

    Silently keeping it is worse than either: its items stay offerable and its
    strata stay in the unlock denominators of nodes it was withdrawn from.
    """
    session = _fresh()
    ingest.ingest_lexemes(session)

    real = ingest._load

    def missing_one(name):
        rows = real(name)
        if name != "lexemes.yaml":
            return rows
        partners = {r["aspect_partner"] for r in rows if r.get("aspect_partner")}
        victim = next(
            r for r in rows
            if r.get("pos", "subst") == "subst" and r["lemma"] not in partners
        )
        return [r for r in rows if r is not victim]

    monkeypatch.setattr(ingest, "_load", missing_one)
    with pytest.raises(AssertionError, match="withdrawn"):
        ingest.ingest_lexemes(session)
    session.close()


def test_a_stale_form_is_removed_when_nothing_points_at_it():
    """A cell the generator no longer produces must not stay answerable."""
    session = _fresh()
    lexemes = ingest.ingest_lexemes(session)
    lexeme = next(iter(lexemes.values()))
    ghost = Form(
        lexeme_id=lexeme.id, surface="wymyślony", morph_tag="subst:sg:nom:f9"
    )
    session.add(ghost)
    session.flush()

    ingest.ingest_lexemes(session)
    assert session.get(Form, ghost.id) is None, "a withdrawn cell survived the ingest"
    session.close()


def test_restratification_that_orphans_items_is_refused(db, monkeypatch):
    """Changing a rule's stratification re-partitions every stratum.

    The old Pattern rows stay, still carrying items, and their strata stay in
    their nodes' unlock denominators — where nothing new can ever satisfy them.
    That is precisely the permanently-unmasterable node `ensure_patterns` exists
    to prevent, arriving by the back door on the second build rather than the
    first.
    """
    real = frames.rule_stratification

    def shifted():
        table = dict(real())
        key = next(iter(table))
        table[key] = (*table[key], "loc")
        return table

    monkeypatch.setattr(frames, "rule_stratification", shifted)
    with pytest.raises(AssertionError, match="stratification"):
        frames.ensure_patterns(db)
    db.rollback()


# ------------------------------------------------------------------- schema


def test_a_column_added_to_a_model_reaches_an_existing_database(tmp_path):
    """`create_all` creates tables; it never alters one it finds.

    Alembic is deferred, which is a defensible call only while adding a column
    still reaches a database that already exists. It did not: two columns were
    added to `card` and `streak` in this branch, and on any learner's existing
    `polish.db` they were simply absent. The failure is delayed and misleading —
    the content build touches none of those tables and reports success, then the
    first page load raises `no such column`, and the only remedy on offer was
    deleting every schedule, unlock and streak the learner had.

    Asserted generically rather than on the two columns by name, so the next
    column added is covered by the test that exists rather than by the one
    somebody remembers to write.
    """
    from sqlalchemy import inspect, text

    from pl import db as database

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}", future=True)
    models.Base.metadata.create_all(engine)
    # An "old" database: the same schema, minus a column the model has since
    # grown. Dropping one is the cheapest way to build that shape honestly.
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE card DROP COLUMN created_at"))
    assert "created_at" not in {c["name"] for c in inspect(engine).get_columns("card")}

    original, database.engine = database.engine, engine
    try:
        database.add_missing_columns()
        repaired = {c["name"] for c in inspect(engine).get_columns("card")}
        assert "created_at" in repaired, "an added column never reached the database"
        database.add_missing_columns()  # idempotent: a second run must be a no-op
    finally:
        database.engine = original
        engine.dispose()
