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

from pl import concepts, models, schedule
from pl import db as database
from pl.content import frames, ingest
from pl.domain import ErrorClass
from pl.models import (
    AppUser,
    Attempt,
    Card,
    ConceptRead,
    Form,
    Item,
    ItemSlot,
    ItemVariant,
    Lexeme,
    Node,
    NodeUnlock,
    Pattern,
    Review,
)
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


def test_a_word_naming_a_node_that_is_not_a_vocabulary_node_is_refused(monkeypatch):
    """`vocabulary_node` is hand-edited, and a typo must say which word it was.

    A key that does not exist failed with a bare `KeyError`; a real *grammar*
    node failed silently in four places at once — the word owned by no
    vocabulary node, its meaning item scoring grammar cards, the word-first rule
    no longer applying to it, and the reachability guard unable to see it.
    """
    real = ingest._load

    def edited(name):
        data = real(name)
        if name != "lexemes.yaml":
            return data
        data = [dict(e) for e in data]
        data[0]["vocabulary_node"] = "N03"
        return data

    monkeypatch.setattr(ingest, "_load", edited)
    with pytest.raises(AssertionError, match=r"kawa.*N03"):
        ingest.lexeme_vocabulary_nodes()


def test_content_build_produces_enough_items(db):
    """Acceptance criterion 16."""
    assert db.scalar(select(func.count()).select_from(Item)) >= 300


def test_a_form_the_analyser_cannot_read_back_fails_the_build():
    """Criterion 16's "asserted at build time", made able to fail.

    Every item's answer is copied from its form row, so comparing the two
    compared the build with itself: a form row corrupted to a non-word built
    cleanly, and would have reached the learner as an answer nobody can type.
    The analyser is the independent source — it generated the row, and it must
    read the surface back as a form of the same word.
    """
    engine = create_engine("sqlite://", future=True)
    models.Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    ingest.ingest_all(s)
    kawa = s.scalar(select(Lexeme).where(Lexeme.lemma == "kawa"))
    form = s.scalar(
        select(Form).where(Form.lexeme_id == kawa.id, Form.morph_tag == "subst:sg:acc:f")
    )
    form.surface = "qwxzqwxz"
    s.commit()
    with pytest.raises(AssertionError, match=r"qwxzqwxz.*not read.*kawa"):
        frames.build(s)
    s.close()


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
    assert MORPH not in applied
    assert applied[PATTERN] == 1, "the rule card is failed, not merely scored"
    assert cards[MORPH].due_at == before


def test_the_named_pair_sklepie_for_sklepu_fails_only_the_rule(db):
    """Acceptance criterion 11, first half, on the pair it names.

    `sklepie` is a real locative where the genitive was wanted: the learner can
    inflect `sklep` and chose the wrong case, so the pattern card fails — Again,
    not merely scored — and the form card does not move at all. The ANIMACY test
    above reaches a different routing row, so `CASE_WRONG` went through
    `apply_diagnosis` nowhere, and failing the form card as well would have
    broken no test.

    A learner of its own: a card takes one review a day, and on this module's
    shared learner an earlier test may already have spent it.
    """
    from pl.api import expected_slot
    from pl.grade import classify

    learner = _new_learner(db, "criterion-11")
    item = _item(db, "sklep", "Nie ma")
    assert item.expected_answer == "sklepu"
    cards = schedule.cards_for_item(db, learner.id, item)
    form_card = cards[MORPH]
    before = (form_card.due_at, form_card.fsrs_state_json, form_card.reps)

    diagnosis = classify(expected_slot(db, item), "sklepie")
    assert diagnosis.error_class is ErrorClass.CASE_WRONG
    attempt = _attempt(db, learner, item, "sklepie")
    applied = schedule.apply_diagnosis(
        db, learner.id, item, diagnosis, attempt.id, _day_start()
    )

    assert applied == {PATTERN: 1}, "the rule failed, and nothing else moved"
    assert (form_card.due_at, form_card.fsrs_state_json, form_card.reps) == before


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


# ------------------------------------------------------------- criterion 18


def _sentence(db, lemma: str, expected: str) -> Item:
    lexeme = db.scalar(select(Lexeme).where(Lexeme.lemma == lemma))
    for item in db.scalars(
        select(Item).where(Item.exercise_type == "free_translation")
    ):
        form = db.get(Form, item.target_form_id)
        if form and form.lexeme_id == lexeme.id and item.expected_answer == expected:
            return item
    raise LookupError(f"no free-translation item {expected!r}")


def _submit(db, user, item, answer: str):
    """Grade and record one answer, the way `/api/submit` does."""
    from pl.api import grade_item

    diagnosis = grade_item(db, item, answer)
    attempt = _attempt(db, user, item, answer)
    schedule.apply_diagnosis(db, user.id, item, diagnosis, attempt.id, _day_start())
    return diagnosis


def _queued(db, item: Item) -> list[str]:
    return list(
        db.scalars(
            select(ItemVariant.accepted_answer).where(
                ItemVariant.item_id == item.id, ItemVariant.source == "queued"
            )
        )
    )


def test_a_right_answer_in_the_wrong_order_is_kept_for_the_owner(db, user):
    """Acceptance criterion 18, as the owner scoped it: word order only.

    `Kota widzę` is good Polish that the item did not anticipate — a native
    speaker accepts it under contrastive stress. The grader is right to call it
    `WORD_ORDER`, because the course teaches one neutral order at A1, but the
    answer was then discarded. It is kept, once, for the owner to judge — and
    kept is all: a queued answer is not an accepted one.
    """
    from pl.api import grade_item

    item = _sentence(db, "kot:Sm2", "Widzę kota")
    assert _submit(db, user, item, "Kota widzę.").error_class is ErrorClass.WORD_ORDER
    assert _queued(db, item) == ["kota widzę"]

    _submit(db, user, item, "kota  Widzę")
    assert _queued(db, item) == ["kota widzę"], "the same answer is queued once"

    assert grade_item(db, item, "Kota widzę").error_class is ErrorClass.WORD_ORDER, (
        "queued is not accepted: nothing promotes itself"
    )


@pytest.mark.parametrize(
    ("answer", "because"),
    [
        ("Widzę kota", "a right answer needs no judging"),
        ("Widzę kot", "every word is real Polish, and it is still an animacy error"),
        ("Widzę psa", "a different real word"),
        ("Widzę", "a missing word"),
    ],
)
def test_nothing_but_word_order_is_queued(db, user, answer, because):
    """Read literally, 'every token analyses' queued 39 of the golden corpus's
    58 mistakes — `kot` for `kota` is a real word. A mistake the grader can name
    is not an answer the item failed to anticipate.

    Asserted as *nothing but the one reordering*, not as *not this answer*: a
    sentence diagnosed at one position narrows the diagnosis to that word, so a
    leak would queue `kot` rather than `widzę kot` — and a check for the whole
    sentence passed straight over it.
    """
    item = _sentence(db, "kot:Sm2", "Widzę kota")
    _submit(db, user, item, answer)
    assert set(_queued(db, item)) <= {"kota widzę"}, because


def test_a_word_the_analyser_does_not_know_is_not_queued(db, user):
    """The criterion's own condition: every token analyses cleanly.

    A reordering holds only the expected words, so through the grader this
    fails only for a sentence whose own words the analyser does not know. The
    queue is for good Polish, and a word Morfeusz cannot read is no evidence of
    that.
    """
    item = _sentence(db, "kot:Sm2", "Widzę kota")
    schedule.queue_for_promotion(db, item, ErrorClass.WORD_ORDER, "kota qwxz")
    assert "kota qwxz" not in _queued(db, item)


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


@pytest.fixture
def fresh_db():
    """A curriculum of its own, for a test that edits the curriculum.

    The module's `db` is shared, and a pattern added to N01 there would change
    N01's strata for every test that runs after this one.
    """
    engine = create_engine("sqlite://", future=True)
    models.Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    ingest.ingest_all(s)
    frames.build(s)
    yield s
    s.close()


def _new_learner(db, name: str) -> AppUser:
    """A learner with no history, so no earlier test has spent a card's review."""
    learner = AppUser(
        email=f"{name}@example.invalid",
        created_at=datetime.now(UTC).replace(tzinfo=None),
        settings_json={"tz": "UTC"},
    )
    db.add(learner)
    db.flush()
    return learner


def _earn_mastery(db, learner: AppUser, node_key: str, leave_out: int = 0) -> list[int]:
    """Master a node through the rows its gate reads, never through its result.

    Three Good reviews, the first ten days ago, and a card in the Review state
    with a month's stability: every condition `_card_is_mastered` checks, set
    where it checks them, on whichever population the gate reads for this node.
    No `node_unlock` row is written here — a latch written by hand tests the
    table, and a gate that deletes unjustified latches would rightly delete it.
    The last `leave_out` gating referents are left unmet, and returned.
    """
    from fsrs import Card as FsrsCard
    from fsrs import Rating, State

    from pl.session import _gating_refs

    node = db.scalar(select(Node).where(Node.key == node_key))
    population, ref_ids = _gating_refs(db, node)
    kept, unmet = ref_ids[: len(ref_ids) - leave_out], ref_ids[len(ref_ids) - leave_out :]
    item = db.scalar(select(Item).where(Item.node_id == node.id))
    now = datetime.now(UTC)
    for ref_id in kept:
        card = schedule.card_for(db, learner.id, population, ref_id)
        card.stability_max = 30.0
        card.fsrs_state_json = FsrsCard(
            state=State.Review,
            stability=30.0,
            difficulty=5.0,
            due=now + timedelta(days=29),
            last_review=now - timedelta(days=1),
        ).to_dict()
        for days_ago in (10, 9, 8):
            attempt = Attempt(
                user_id=learner.id,
                item_id=item.id,
                submitted=item.expected_answer,
                created_at=(now - timedelta(days=days_ago)).replace(tzinfo=None),
            )
            db.add(attempt)
            db.flush()
            db.add(Review(attempt_id=attempt.id, card_id=card.id, rating=int(Rating.Good)))
    db.flush()
    return unmet


def _reach_n02(db, learner: AppUser, leave_out: int = 0) -> list[int]:
    """V01 mastered opens N01; N01 mastered opens N02 — both through the gate."""
    _earn_mastery(db, learner, "V01")
    evaluate_unlocks(db, learner.id)
    unmet = _earn_mastery(db, learner, "N01", leave_out=leave_out)
    evaluate_unlocks(db, learner.id)
    return unmet


def test_an_earned_unlock_survives_every_event_the_criterion_names(fresh_db):
    """Acceptance criterion 13, with the latch earned rather than inserted.

    The test above writes the `node_unlock` row itself and reads it back, which a
    gate that re-evaluated mastery on every read would pass as well. Here N02 is
    unlocked by `evaluate_unlocks` because N01 was mastered, and each event the
    criterion names is applied in turn with the gate evaluated again after it:
    a lapse, a card for a stratum not yet met, and a curriculum edit adding
    patterns to the prerequisite. The last one does take N01's mastery away —
    asserted, so the latch is shown holding against a gate that really moved.
    """
    from fsrs import Rating

    db = fresh_db
    learner = _new_learner(db, "criterion-13")
    n01 = db.scalar(select(Node).where(Node.key == "N01"))
    n02 = db.scalar(select(Node).where(Node.key == "N02"))
    (unmet,) = _reach_n02(db, learner, leave_out=1)
    assert is_mastered(db, learner.id, n01), "four strata of five should master N01"
    assert is_unlocked(db, learner.id, n02), "the latch was not earned"

    mastered_card = db.scalar(
        select(Card).where(Card.user_id == learner.id, Card.population == PATTERN)
    )
    attempt = _attempt(db, learner, db.scalar(select(Item).where(Item.node_id == n01.id)), "x")
    schedule.apply_rating(db, mastered_card, Rating.Again, attempt.id)
    evaluate_unlocks(db, learner.id)
    assert is_unlocked(db, learner.id, n02), "a lapse re-locked the node"

    schedule.card_for(db, learner.id, PATTERN, unmet)
    evaluate_unlocks(db, learner.id)
    assert is_unlocked(db, learner.id, n02), "a new card for an unmet stratum re-locked it"

    for index in range(10):
        db.add(Pattern(node_id=n01.id, rule_key=f"criterion-13-{index}", paradigm_class="edit"))
    db.flush()
    assert not is_mastered(db, learner.id, n01), "the edit should move N01's gate"
    evaluate_unlocks(db, learner.id)
    assert is_unlocked(db, learner.id, n02), "a curriculum edit re-locked the node"


def test_mastery_display_decays_while_an_earned_gate_holds(fresh_db):
    """Acceptance criterion 14, with a gate that had something to lose.

    `test_api.py` shows the display falling over four months away, but the node
    it reads was never unlocked, so its "the gate did not move" compared false
    with false. Here N01 is mastered and N02 latched before the months pass.
    """
    from pl.session import node_mastery

    db = fresh_db
    learner = _new_learner(db, "criterion-14")
    n01 = db.scalar(select(Node).where(Node.key == "N01"))
    n02 = db.scalar(select(Node).where(Node.key == "N02"))
    _reach_n02(db, learner)
    before = node_mastery(db, learner.id, n01)["retention"]
    assert is_mastered(db, learner.id, n01) and is_unlocked(db, learner.id, n02)

    for card in db.scalars(select(Card).where(Card.user_id == learner.id)):
        state = dict(card.fsrs_state_json)
        for field in ("last_review", "due"):
            state[field] = (
                datetime.fromisoformat(state[field]) - timedelta(days=120)
            ).isoformat()
        card.fsrs_state_json = state
    db.flush()

    assert node_mastery(db, learner.id, n01)["retention"] < before, "the display did not decay"
    assert is_mastered(db, learner.id, n01), "four months away moved the gate"
    assert is_unlocked(db, learner.id, n02), "four months away re-locked a node"


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

# ------------------------------------------------------------ the build itself
# `python -m pl.content.ingest` is the schema step, and since the teaching surface
# it is also where concepts are validated, a learner mid-course is reconciled, and
# a stored default goal is brought forward. Each of those was tested by calling
# its function directly — and deleting the *call* from the build left the suite
# green, the gap `test_the_build_itself_refuses_an_unreachable_stratum` was
# written to close for the reachability guard. These drive `main()`.


@pytest.fixture
def built(tmp_path, monkeypatch):
    """A real database file, built by `main()`, and a session factory onto it."""
    engine = create_engine(f"sqlite:///{tmp_path / 'build.db'}", future=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    ingest.main([])
    yield factory
    engine.dispose()


def _learner(factory) -> AppUser:
    with factory() as db:
        return db.scalar(select(AppUser))


def _forget_the_build_ran(factory, *keys: str) -> None:
    """What a database from before this change looks like: no stamps."""
    with factory() as db:
        user = db.scalar(select(AppUser))
        settings = dict(user.settings_json)
        for key in keys:
            settings.pop(key, None)
        user.settings_json = settings
        db.commit()


# ------------------------------------------------------------ reconciliation


def test_the_build_reconciles_a_learner_who_was_mid_course(built):
    """Acceptance criterion 8, through the build rather than the function."""
    _forget_the_build_ran(built, "concepts_reconciled_at")
    with built() as db:
        user = db.scalar(select(AppUser))
        for key in ("N01", "N03"):
            node = db.scalar(select(Node).where(Node.key == key))
            db.add(
                NodeUnlock(
                    user_id=user.id,
                    node_id=node.id,
                    unlocked_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )
        db.commit()

    ingest.main([])

    with built() as db:
        read = concepts.read_keys(db, _learner(built).id)
    assert {"VOCAB_GENDER", "CASES", "ACCUSATIVE"} <= read, (
        "the build did not reconcile — a learner mid-course would be handed "
        "lessons for material they have been answering for weeks"
    )


def test_the_build_refuses_a_concept_the_graph_cannot_teach(built, monkeypatch):
    real = concepts._load()
    monkeypatch.setattr(
        concepts,
        "_load",
        lambda: {**real, "concepts": [*real["concepts"], {"key": "GHOST", "title": "x", "introduced_by": "N99"}]},
    )
    with pytest.raises(AssertionError, match="graph does not contain"):
        ingest.main([])


# ------------------------------------------------------------- the daily goal


def test_the_build_brings_a_stored_default_goal_forward(built):
    """P01-5. A default changed in code never reaches a row that stored the old one.

    Every database built before the goal became `DAILY_NEW_CAP` holds an explicit
    `daily_goal_items: 20`, and `record_activity` falls back to the new default
    only for an absent key — so the measured streak improvement was not
    delivered to the one learner the measurements were for.
    """
    _forget_the_build_ran(built, "goal_migrated_at")
    with built() as db:
        user = db.scalar(select(AppUser))
        user.settings_json = {**user.settings_json, "daily_goal_items": 20}
        db.commit()

    ingest.main([])

    settings = _learner(built).settings_json
    assert settings["daily_goal_items"] == DAILY_NEW_CAP
    assert settings.get("goal_migrated_at")


def test_a_goal_the_learner_chose_is_left_alone(built):
    """Only the old *default* moves. A learner who set 15 meant 15."""
    _forget_the_build_ran(built, "goal_migrated_at")
    with built() as db:
        user = db.scalar(select(AppUser))
        user.settings_json = {**user.settings_json, "daily_goal_items": 15}
        db.commit()

    ingest.main([])
    assert _learner(built).settings_json["daily_goal_items"] == 15


def test_a_goal_of_twenty_chosen_after_the_change_survives_a_rebuild(built):
    """The stamp, not the value, says the migration has happened."""
    with built() as db:
        user = db.scalar(select(AppUser))
        user.settings_json = {**user.settings_json, "daily_goal_items": 20}
        db.commit()

    ingest.main([])
    assert _learner(built).settings_json["daily_goal_items"] == 20


# --------------------------------------------------------------- stale items


def _stale_item(factory, answered: bool) -> int:
    """An item no current frame produces — what a corrected frame leaves behind."""
    with factory() as db:
        template = db.scalar(select(Item).where(Item.exercise_type == "cloze"))
        stale = Item(
            node_id=template.node_id,
            pattern_id=template.pattern_id,
            exercise_type="cloze",
            prompt="Idę do ___.",
            gloss="I am going to the post office.",
            expected_answer="poczty-stale" + ("-answered" if answered else ""),
            target_form_id=template.target_form_id,
            source="template",
        )
        db.add(stale)
        db.flush()
        db.add(ItemSlot(item_id=stale.id, slot_index=0, expected_surface="poczty"))
        if answered:
            db.add(
                Attempt(
                    user_id=db.scalar(select(AppUser.id)),
                    item_id=stale.id,
                    submitted="poczty",
                    created_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )
        db.commit()
        return stale.id


def test_stale_items_are_reported_and_kept_by_default(built, capsys):
    stale = _stale_item(built, answered=False)
    ingest.main([])
    assert "--drop-unanswered-stale" in capsys.readouterr().out
    with built() as db:
        assert db.get(Item, stale) is not None, "nothing may be deleted unasked"


def test_the_flag_drops_stale_items_nobody_answered_and_keeps_the_rest(built):
    """P01-6. A corrected sentence the owner flagged as wrong Polish went on being
    served from every database built before the correction. Dropping the ones
    with no attempts costs no history; the ones with attempts are the learner's
    record and stay."""
    untouched = _stale_item(built, answered=False)
    answered = _stale_item(built, answered=True)

    ingest.main(["--drop-unanswered-stale"])

    with built() as db:
        assert db.get(Item, untouched) is None
        assert not db.scalars(select(ItemSlot).where(ItemSlot.item_id == untouched)).all()
        assert db.get(Item, answered) is not None
