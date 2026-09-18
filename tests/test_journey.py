"""The learner's journey, end to end, on a database with no history.

Every test in `test_loop.py` pokes one function with hand-built state. That is
why three defects that make the product unusable shipped with the suite green:
the code paths they live in are never executed, or are executed against a
fixture that has already drifted into a state where they cannot fail.

These tests use a clean database per test and drive the loop through the same
calls the API makes, so a learner who cannot progress produces a red test.
"""

from __future__ import annotations

import random
import warnings
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from pl import models
from pl.api import expected_slot
from pl.content import frames, ingest
from pl.grade import classify
from pl.models import Attempt, Card, Form, Item, Node, Pattern
from pl.schedule import apply_diagnosis
# The instrument's learner-error model, not a second copy of it: these tests
# pin the floors `journey_sim` establishes, which only holds while both model
# the same learner.
from scripts.journey_sim import wrong_answer as _wrong_form
from pl.session import (
    _item_referents,
    _sense_by_form,
    _started_referents,
    build_session,
    evaluate_unlocks,
    start_of_user_day,
    weakest_node,
)

warnings.filterwarnings("ignore", category=DeprecationWarning)


SETTINGS = {"tz": "UTC", "daily_goal_items": 5}


def answer(db, user, item, submitted: str | None = None):
    """Answer one item through exactly the path `/api/submit` uses.

    Going through the real classifier and the real routing table is the point:
    a test that hand-builds ratings cannot notice that the loop as assembled
    fails to move a learner forward.
    """
    submitted = item.expected_answer if submitted is None else submitted
    attempt = Attempt(
        user_id=user.id,
        item_id=item.id,
        submitted=submitted,
        created_at=datetime.now(UTC).replace(tzinfo=None),
    )
    db.add(attempt)
    db.flush()
    diagnosis = classify(expected_slot(db, item), submitted)
    apply_diagnosis(
        db, user.id, item, diagnosis, attempt.id, start_of_user_day(SETTINGS)
    )
    db.commit()
    return diagnosis


def advance_one_day(db):
    """Move the whole world back one day, so the next review sees elapsed time.

    FSRS grows stability from the interval actually waited, so reviewing twice in
    the same instant leaves stability flat and no card ever reaches the mastery
    threshold. Back-dating `last_review` and the attempt history is the cheapest
    honest way to simulate days passing without threading a clock through the
    scheduler.
    """
    day = timedelta(days=1)
    for card in db.scalars(select(Card)):
        state = dict(card.fsrs_state_json)
        if state.get("last_review"):
            state["last_review"] = (
                datetime.fromisoformat(state["last_review"]) - day
            ).isoformat()
        if state.get("due"):
            state["due"] = (datetime.fromisoformat(state["due"]) - day).isoformat()
        card.fsrs_state_json = state
        card.due_at -= day
        # The daily introduction budget is counted from this column. A helper
        # that moves every other clock but leaves it alone pins every card in
        # "today" forever, the budget reads as permanently spent, and the test
        # measures the harness rather than the composer.
        if card.created_at is not None:
            card.created_at -= day
    for attempt in db.scalars(select(Attempt)):
        attempt.created_at -= day
    db.commit()


def _let_time_pass(db, days: int = 1):
    """Push every card past its due date and back-date the history.

    FSRS puts a new card's first steps minutes apart, so without this the learner
    is permanently in debt and the introduction path never runs.
    """
    later = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=days)
    for card in db.scalars(select(Card)):
        card.due_at = later
        if card.created_at is not None:
            card.created_at -= timedelta(days=days)
    for attempt in db.scalars(select(Attempt)):
        attempt.created_at -= timedelta(days=days)
    db.commit()


@pytest.fixture
def user(db):
    return ingest.ensure_user(db)


def read_every_concept(db, user):
    """Take the lessons as read.

    A concept is taught before it is drilled, so a node whose lesson is unread
    introduces nothing — V01 included, from the first session. Every test below
    that drives *introduction* is about what the composer chooses once teaching
    has happened, so it says so here rather than measuring the gate by accident.
    `test_an_unread_lesson_withholds_new_material_and_nothing_else` is the one
    that leaves them unread on purpose.
    """
    from pl import concepts

    for concept in concepts.all_concepts():
        concepts.mark_read(db, user.id, concept["key"])


@pytest.fixture
def db():
    """A fresh, fully built curriculum with no learner history.

    Function-scoped on purpose: learner state must not cross a test boundary,
    or a test can pass because an earlier test left the world in a shape where
    the code under test cannot run.
    """
    engine = create_engine("sqlite://", future=True)
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    ingest.ingest_all(session)
    frames.build(session)
    yield session
    session.close()


def test_every_stratum_has_at_least_one_item(db):
    """A pattern card the learner can never be shown is a locked door.

    Mastery counts a node's strata, not the cards that exist, so a stratum with
    no items sits in the denominator forever and every node downstream of it is
    permanently unreachable. The gate cannot detect this: `is_mastered` raises
    only when a node has *no* strata, and these strata exist — they are merely
    unpopulatable.
    """
    empty = [
        (p.rule_key, p.paradigm_class)
        for p in db.scalars(select(Pattern))
        if not db.scalar(
            select(func.count()).select_from(Item).where(Item.pattern_id == p.id)
        )
    ]
    assert not empty, f"strata with no items: {empty}"


def test_form_selection_multiple_choice_exists(db):
    """MCQ does double duty: meaning recall under a vocabulary node, and form
    selection under a grammar node.

    Only the vocabulary half is load-bearing for lexical cards; the grammar half
    is the low-stakes first exposure to a stratum. A dedup key that cannot tell
    two frames apart drops one of them silently, and a `>= 300` item count is
    far too coarse to notice a whole exercise type going missing.
    """
    form_mcq = db.scalar(
        select(func.count())
        .select_from(Item)
        .join(Node, Node.id == Item.node_id)
        .where(Node.type == "grammar", Item.exercise_type == "mcq")
    )
    assert form_mcq > 0, "no form-selection MCQ items were built"


def test_answered_items_are_not_offered_again_as_new(db, user):
    """Novelty must be judged per referent, not per referent *column*.

    A vocabulary item scores a lexical card, which carries `sense_id` and leaves
    `form_id` null. Judging novelty from `form_id` alone therefore never marks a
    vocabulary item as started, so the daily cap is spent re-serving the same
    handful of items every session and the rest of the node is never reached —
    which makes the node's mastery threshold unreachable by construction.
    """
    read_every_concept(db, user)
    first, stats = build_session(db, user.id, SETTINGS, limit=10)
    assert stats["introduced"] > 0, "no new material was introduced at all"

    for item in first:
        answer(db, user, item)
    _let_time_pass(db)

    second, _ = build_session(db, user.id, SETTINGS, limit=10)
    repeated = {i.id for i in first} & {i.id for i in second}
    assert not repeated, f"{len(repeated)} answered items were re-offered as new"


def test_remediation_does_not_stop_at_its_first_overlap_with_debt(db, user):
    """`add` reports "the session is full" and "already picked" with one False.

    Debt and remediation draw from the same node, so their candidates overlap
    heavily — and because the debt queue is served first, remediation's *first*
    candidate is usually one debt has already taken. Treating that as a reason to
    stop ends the segment before it contributes anything.

    The overlap is constructed rather than hoped for. An earlier version of this
    test ran on day one, where every item the learner had met was also in the
    debt queue, so the only way remediation could add anything was by
    introducing new material — which is segment 2's job, bounded by a cap and a
    gate that segment 3 answers to neither of. That test passed for the wrong
    reason and went on passing when the stop-on-overlap defect was reintroduced.
    """
    read_every_concept(db, user)
    from pl.schedule import cards_for_item

    for _ in range(3):
        picked, _ = build_session(db, user.id, SETTINGS, limit=10)
        for item in picked:
            answer(db, user, item)
        advance_one_day(db)

    wrong, _ = build_session(db, user.id, SETTINGS, limit=6)
    for item in wrong:
        answer(db, user, item, submitted="zzzzzz")
    weak = weakest_node(db, user.id)
    assert weak is not None, "no node became weakest, so remediation never runs"

    # Everything the learner has met under the weak node, in the order
    # remediation will walk it.
    started = _started_referents(db, user.id)
    sense_by_form = _sense_by_form(db)
    met = [
        item
        for item in db.scalars(
            select(Item).where(Item.node_id == weak.id).order_by(Item.id)
        )
        if not (_item_referents(weak, item, sense_by_form) - started)
    ]
    assert len(met) > 4, f"only {len(met)} met items under {weak.key}; too few to prove anything"

    # Only the first two are due, so debt takes exactly remediation's opening
    # candidates and everything after them is reachable only by continuing.
    later = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30)
    for card in db.scalars(select(Card).where(Card.user_id == user.id)):
        card.due_at = later
    now = datetime.now(UTC).replace(tzinfo=None)
    for item in met[:2]:
        for card in cards_for_item(db, user.id, item).values():
            card.due_at = now - timedelta(hours=1)
    for attempt in db.scalars(select(Attempt).where(Attempt.user_id == user.id)):
        attempt.created_at -= timedelta(days=1)
    db.commit()

    picked, stats = build_session(db, user.id, SETTINGS, limit=20)
    remediated = len(picked) - stats["debt_served"] - stats["introduced_items"]
    assert stats["debt_served"] > 0, "the constructed backlog was not served"
    assert remediated > 0, (
        "remediation contributed nothing: it stopped at the first candidate the "
        "debt queue had already taken, which is its usual first candidate"
    )


def test_a_diligent_learner_reaches_the_grammar(db, user):
    """The tracer: study every day, answer correctly, and the course opens up.

    This is the test whose absence let three separate defects ship green. Each
    of them lives in a code path that no single-function test executes, and each
    of them stops a learner dead — but only when the loop is run as a loop.

    It asserts the product's minimum promise: a learner who does the work gets
    past the vocabulary node and reaches the morphology the course exists to
    teach.
    """
    read_every_concept(db, user)
    # Generous, because the bound is not what is under test. Measured at ~25
    # simulated days today: debt suppresses new material most days, so a 44-word
    # vocabulary node takes far longer to clear than the daily cap implies. That
    # pacing is worth revisiting, but a tight bound here would only make the test
    # flake when it changes.
    horizon = 40
    unlocked: list[str] = []
    reached_n01: int | None = None

    # Deliberately runs the whole horizon rather than stopping at the first
    # unlock. Breaking early made this test blind to everything past V01 — it
    # could not see that introduction stalled at 14% of the curriculum, which is
    # exactly the kind of plateau a longitudinal test exists to catch.
    for day in range(1, horizon + 1):
        picked, _ = build_session(db, user.id, SETTINGS, limit=20)
        for item in picked:
            answer(db, user, item)
        unlocked += evaluate_unlocks(db, user.id)
        if reached_n01 is None and "N01" in unlocked:
            reached_n01 = day
        advance_one_day(db)

    assert reached_n01 is not None, (
        f"a learner answering everything correctly every day for {horizon} days "
        f"never unlocked the first grammar node; unlocked={unlocked}"
    )
    assert reached_n01 < horizon, (
        f"unlocked only on the final day ({reached_n01}); the bound is too tight"
    )
    # NOT asserted: that a second node ever unlocks. It does not, and the cause
    # is the design rather than a defect. Criterion 9 forbids introducing new
    # material while anything is overdue, and once the learner holds enough
    # cards for at least one to fall due every day, that condition never clears
    # again — measured here, `introduced` is 10, 0, 10, then 0 forever from day
    # four. The learner meets roughly twenty items and plateaus.
    #
    # `test_introduction_does_not_stall_while_material_remains` covers the part
    # that *is* a defect — novelty wrongly skipping items — by clearing debt
    # explicitly. Leaving the plateau unasserted here keeps this test honest
    # about what the loop currently promises, and the comment keeps it from
    # being rediscovered as a surprise.


def test_re_ingest_recomputes_derived_fields(db, monkeypatch):
    """A curriculum edit must actually reach the database.

    `paradigm_class` is derived from the cases the curriculum teaches, so adding
    a case re-partitions every stratum. The design records that re-ingest as the
    known cost of deriving the stratum rather than hand-assigning it — which only
    holds if re-ingest recomputes. An ingest that short-circuits on the lemma
    leaves stale classes behind and then builds new `pattern` rows from them,
    and the operator sees a successful build either way.
    """
    from pl.content import ingest as ingest_mod
    from pl.models import Lexeme

    before = {
        lx.lemma: lx.paradigm_class for lx in db.scalars(select(Lexeme))
    }
    # sklep takes -u and chleb takes -a in the genitive; nothing in the
    # nominative or accusative separates them.
    assert before["sklep"] == before["chleb"]

    monkeypatch.setattr(
        ingest_mod, "STRATIFICATION_CASES", ("nom", "acc", "gen")
    )
    ingest_mod.ingest_lexemes(db)
    db.commit()

    after = {lx.lemma: lx.paradigm_class for lx in db.scalars(select(Lexeme))}
    assert after["sklep"] != after["chleb"], (
        "adding the genitive did not re-partition the strata; "
        f"both are still {after['sklep']!r}"
    )


def test_mastery_span_is_measured_per_card_not_per_account(db, user):
    """The seven-day span belongs to the card, not to the account.

    Measuring it from the learner's first-ever attempt makes the condition pass
    unconditionally once the account is a week old. Every card met after that
    masters on stability and a rep count alone, so every node from then on
    unlocks on a gate that has quietly stopped gating.
    """
    from pl import schedule
    from pl.models import Sense
    from pl.session import is_mastered

    v01 = db.scalar(select(Node).where(Node.key == "V01"))
    vocab = [
        i
        for i in db.scalars(select(Item).where(Item.node_id == v01.id))
    ]

    # A fortnight of account history, so the account-wide span is satisfied.
    answer(db, user, vocab[0])
    for attempt in db.scalars(select(Attempt)):
        attempt.created_at -= timedelta(days=14)
    db.commit()

    # Now drill every card in the node — genuinely, three times each — but all
    # of it today. No card has been known for a week.
    for _ in range(3):
        for item in vocab:
            answer(db, user, item)
    for card in db.scalars(select(Card).where(Card.user_id == user.id)):
        card.stability_max = 30.0
    db.commit()

    assert not is_mastered(db, user.id, v01), (
        "a node whose every card was first seen today was reported mastered"
    )


def test_strata_reflect_the_rule_they_belong_to(db):
    """Each rule stratifies on the cases that rule actually teaches.

    `sklep` and `dom` inflect identically in the accusative and differently in
    the locative — `w sklepie` against `w domu`. One global key cannot serve
    both rules: computed over the accusative it merges two locative behaviours
    into a single card, and computed over every case it splits the accusative
    node into two dozen strata the learner has never seen, taking back a node
    they had already mastered.
    """
    from pl.models import Form, Lexeme

    def pattern_of(lemma: str, rule: str):
        lexeme = db.scalar(select(Lexeme).where(Lexeme.lemma == lemma))
        return db.scalar(
            select(Item.pattern_id)
            .join(Pattern, Pattern.id == Item.pattern_id)
            .join(Form, Form.id == Item.target_form_id)
            .where(Pattern.rule_key == rule, Form.lexeme_id == lexeme.id)
        )

    assert pattern_of("sklep", "ACC_AFTER_TRANSITIVE_VERB") == pattern_of(
        "dom", "ACC_AFTER_TRANSITIVE_VERB"
    ), "two nouns with the same accusative belong in one accusative stratum"

    assert pattern_of("sklep", "LOC_PREPOSITION") != pattern_of(
        "dom", "LOC_PREPOSITION"
    ), "two nouns with different locatives were put in the same locative stratum"


def test_aspect_items_offer_exactly_the_pair(db):
    """The learner chooses an aspect and nothing else.

    Both options are the same cell of the two partners, so tense, person and
    form are held constant and only the aspect varies. If the options ever
    differed in anything else the exercise would stop testing what it claims to.
    """
    from pl.models import Lexeme

    n12 = db.scalar(select(Node).where(Node.key == "N12"))
    items = list(db.scalars(select(Item).where(Item.node_id == n12.id)))
    assert items, "no aspect items were built"

    for item in items:
        assert item.exercise_type == "aspect_choice"
        assert len(item.options_json) == 2, item.options_json
        assert item.expected_answer in item.options_json

    # The habitual frame must want the imperfective, and the completed frame the
    # perfective — the whole contrast rests on the context word.
    habitual = [i for i in items if i.prompt.startswith("Codziennie")]
    assert habitual, "no habitual-context aspect items"
    for item in habitual:
        form = db.get(Form, item.target_form_id)
        lexeme = db.get(Lexeme, form.lexeme_id)
        assert lexeme.aspect == "imperf", (
            f"{item.expected_answer!r} is {lexeme.aspect}, but 'Codziennie' is habitual"
        )
        assert lexeme.aspect_partner_id is not None, "a verb shipped without its pair"


def test_free_translation_items_have_a_slot_per_token(db):
    """Multi-slot items carry their expected analysis per position.

    A single-slot cloze can hold its answer in one column. A whole typed
    sentence cannot: grading it means knowing what was expected at each
    position, which is the only way `WORD_ORDER` and `MISSING_CONSTITUENT` can
    ever be distinguished from a lexical error.
    """
    from pl.models import ItemSlot

    items = list(
        db.scalars(select(Item).where(Item.exercise_type == "free_translation"))
    )
    assert items, "no free-translation items were built"

    for item in items:
        slots = list(
            db.scalars(
                select(ItemSlot)
                .where(ItemSlot.item_id == item.id)
                .order_by(ItemSlot.slot_index)
            )
        )
        assert len(slots) >= 2, f"{item.expected_answer!r} has {len(slots)} slot(s)"
        assert [s.slot_index for s in slots] == list(range(len(slots)))
        # The slots must reconstruct the expected answer exactly.
        from pl.grade.classify import tokenise

        assert [s.expected_surface for s in slots] == tokenise(item.expected_answer)
        # Exactly one slot is the inflected target; the rest are fixed context.
        assert sum(1 for s in slots if s.target_form_id is not None) == 1


def _free_item(db, lemma: str, starts: str):
    from pl.models import Lexeme

    lexeme = db.scalar(select(Lexeme).where(Lexeme.lemma == lemma))
    for item in db.scalars(
        select(Item).where(Item.exercise_type == "free_translation")
    ):
        form = db.get(Form, item.target_form_id)
        if form.lexeme_id == lexeme.id and item.expected_answer.startswith(starts):
            return item
    raise LookupError(f"no free-translation item for {lemma}")


def test_a_whole_sentence_is_graded_position_by_position(db):
    """The two error classes single-slot items cannot produce.

    Right words in the wrong order is a different mistake from the wrong word,
    and a missing word is a third. All three look identical to a grader that
    only knows one expected string, which is why they waited for multi-slot
    items rather than being approximated earlier.
    """
    from pl.api import grade_item
    from pl.domain import ErrorClass

    item = _free_item(db, "kot:Sm2", "Widzę")
    assert item.expected_answer == "Widzę kota"

    assert grade_item(db, item, "Widzę kota").error_class is ErrorClass.CORRECT
    assert grade_item(db, item, "kota Widzę").error_class is ErrorClass.WORD_ORDER
    assert grade_item(db, item, "Widzę").error_class is ErrorClass.MISSING_CONSTITUENT
    # A real inflection error inside the sentence is still diagnosed as itself.
    assert grade_item(db, item, "Widzę kot").error_class is ErrorClass.ANIMACY


def test_vocabulary_distractors_are_nouns(db):
    """A distractor must be a plausible answer to the question being asked.

    A verb's paradigm contains participles, and a participle carries case — so a
    naive nominative lookup over every lexeme offers `nieprzeczytana` as a
    candidate answer to "which word means coffee?". The learner can then rule it
    out on shape alone, which teaches them to read shapes rather than meanings.
    """
    from pl.models import Form as FormRow
    from pl.models import Lexeme

    noun_surfaces = {
        row.surface
        for row in db.scalars(
            select(FormRow).join(Lexeme, Lexeme.id == FormRow.lexeme_id)
            .where(Lexeme.pos == "subst")
        )
    }
    vocab = db.scalars(
        select(Item).join(Node, Node.id == Item.node_id).where(Node.type == "vocabulary")
    )
    for item in vocab:
        for option in item.options_json or []:
            assert option in noun_surfaces, (
                f"{option!r} is offered as a noun but is not a form of any noun"
            )


def test_dictation_items_are_graded_on_spelling(db):
    """Dictation is the one exercise where a spelling slip is a failure.

    Everywhere else `ORTHOGRAPHY` scores `Hard` and leaves the grammar card
    standing, because the learner knew the grammar and lacked a keyboard. A
    dictation item exists to test spelling, so being lenient there would leave
    it testing nothing it claims to.
    """
    from pl.domain import ErrorClass
    from pl.schedule import ratings_for

    item = db.scalar(
        select(Item).where(Item.exercise_type == "listening_dictation")
    )
    assert item is not None, "no dictation items were built"

    strict = ratings_for(item, ErrorClass.ORTHOGRAPHY)
    assert strict and set(strict.values()) == {1}, "spelling must fail here"

    cloze = db.scalar(select(Item).where(Item.exercise_type == "cloze"))
    lenient = ratings_for(cloze, ErrorClass.ORTHOGRAPHY)
    assert set(lenient.values()) == {2}, "and must not fail anywhere else"


def test_a_dictation_item_never_ships_its_sentence_to_the_client(db):
    """For dictation the sentence *is* the answer.

    Criterion 17 says no expected answer reaches the client before submission.
    Every other exercise shows Polish in the prompt; this one must not, or the
    learner reads what they were supposed to hear.
    """
    from pl.api import _serialise

    for item in db.scalars(
        select(Item).where(Item.exercise_type == "listening_dictation")
    ):
        payload = _serialise(db, item)
        assert item.expected_answer not in str(payload)
        assert payload["prompt"] == ""
        assert payload["options"] is None


# ---------------------------------------------- the scoring consequence
# Every test below asserts what `apply_diagnosis` *moved*, not just what the
# classifier called it. Three M2 features shipped scoring nothing because the
# tests stopped at the label.


def _score(db, user, item, submitted):
    """Grade a submission and report which cards it actually moved."""
    from pl.api import grade_item
    from pl.schedule import apply_diagnosis

    attempt = Attempt(
        user_id=user.id,
        item_id=item.id,
        submitted=submitted,
        created_at=datetime.now(UTC).replace(tzinfo=None),
    )
    db.add(attempt)
    db.flush()
    diagnosis = grade_item(db, item, submitted)
    applied = apply_diagnosis(
        db, user.id, item, diagnosis, attempt.id, start_of_user_day(SETTINGS)
    )
    db.commit()
    return diagnosis, applied


def test_choosing_the_wrong_aspect_fails_the_rule_card(db, user):
    """The design's highest-value grammar node has to produce a signal.

    `ExpectedSlot.aspect_partner` is what step 5 needs to tell an aspect error
    from a vocabulary one. Unset, the classifier returns LEXICAL — and LEXICAL
    under a grammar node scores nothing, so the exercise teaches the learner
    they were wrong and teaches the scheduler nothing at all.
    """
    from pl.domain import ErrorClass
    from pl.schedule import PATTERN

    item = db.scalar(select(Item).where(Item.exercise_type == "aspect_choice"))
    assert item is not None
    wrong = next(o for o in item.options_json if o != item.expected_answer)

    diagnosis, applied = _score(db, user, item, wrong)
    assert diagnosis.error_class is ErrorClass.ASPECT_WRONG
    assert applied.get(PATTERN) == 1, "the rule card must take the failure"


def test_misspelling_any_word_of_a_dictation_fails_it(db, user):
    """Dictation exists to test spelling, at every position.

    A mismatch away from the target position was classified LEXICAL, which is
    unscored under a grammar node — so a learner could misspell two words of a
    three-word sentence and move nothing.

    The item is chosen rather than taken first: the misspelling has to be a
    *dropped diacritic* to be an orthographic error at all, so the sentence
    needs a non-target word that has one. Mangling `brat` any other way is a
    lexical error and would test the opposite of what this claims to.
    """
    from pl.domain import ErrorClass
    from pl.grade.classify import _ASCII_FOLD
    from pl.models import ItemSlot

    for item in db.scalars(
        select(Item).where(Item.exercise_type == "listening_dictation")
    ):
        slots = list(
            db.scalars(
                select(ItemSlot)
                .where(ItemSlot.item_id == item.id)
                .order_by(ItemSlot.slot_index)
            )
        )
        target_index = next(
            i for i, s in enumerate(slots) if s.target_form_id is not None
        )
        candidates = [
            i
            for i, s in enumerate(slots)
            if i != target_index and any(c in _ASCII_FOLD for c in s.expected_surface)
        ]
        if candidates:
            break
    else:
        pytest.skip("no dictation sentence has a diacritic away from the target")

    tokens = [s.expected_surface for s in slots]
    position = candidates[0]
    tokens[position] = _drop_a_diacritic(tokens[position])
    diagnosis, applied = _score(db, user, item, " ".join(tokens))

    assert diagnosis.error_class is ErrorClass.ORTHOGRAPHY, (
        f"misspelling {slots[position].expected_surface!r} at position "
        f"{position} (target is {target_index})"
    )
    assert applied and set(applied.values()) == {1}, "spelling must fail here"


def _drop_a_diacritic(word: str) -> str:
    """Replace the first Polish letter with its ASCII base — a keyboard slip."""
    from pl.grade.classify import _ASCII_FOLD

    for index, char in enumerate(word):
        if char in _ASCII_FOLD:
            return word[:index] + _ASCII_FOLD[char] + word[index + 1 :]
    raise AssertionError(f"{word!r} has no diacritic to drop")


def test_every_error_class_has_an_explanation():
    """Acceptance criterion 5, over the whole vocabulary rather than a sample.

    Two classes became reachable with multi-slot items and neither had an arm,
    so both fell through to a sentence naming a form the learner got right.
    """
    from pl.domain import Diagnosis, ErrorClass, ExpectedSlot, Form as DomainForm
    from pl.grade import explain
    from pl.grade.explain import GENERIC_FALLBACK
    from pl.tags import parse

    slot = ExpectedSlot(
        expected=DomainForm(
            surface="kota", lemma="kot:Sm2", tag=parse("subst:sg:gen.acc:m2")
        ),
        paradigm=(),
    )
    missing = [
        str(cls)
        for cls in ErrorClass
        if explain(Diagnosis(error_class=cls, submitted="x", slot=slot))
        == GENERIC_FALLBACK.format(want="kota")
    ]
    assert not missing, f"classes with no explanation of their own: {missing}"


def test_introduction_does_not_stall_while_material_remains(db, user):
    """New material must keep coming until there is none left.

    Novelty is judged per referent-set. Skipping an item because *any* of its
    referents is started means the first item of a stratum claims the pattern
    for every other lexeme in it, and the queue dries up with most of the
    curriculum never offered. The invariant is exact: if a day introduces
    nothing, nothing introducible can remain.
    """
    from datetime import datetime as dt

    read_every_concept(db, user)

    from pl.models import NodeUnlock
    from pl.session import _item_referents, _sense_by_form, _started_referents

    for node in db.scalars(select(Node)):
        db.add(
            NodeUnlock(
                user_id=user.id,
                node_id=node.id,
                unlocked_at=dt.now(UTC).replace(tzinfo=None),
            )
        )
    db.commit()

    seen: set[int] = set()
    # Runs until introduction dries up of its own accord — the bound is a safety
    # stop against an infinite loop, not a measurement, and must stay well clear
    # of what the curriculum actually needs. A daily cap of ten cards over a few
    # hundred referents takes what it takes: 44 rounds at 82 lexemes. It was 40,
    # which stopped four rounds early the moment six nouns were added and made
    # this test report a stall the composer had not had.
    for _ in range(200):
        picked, stats = build_session(db, user.id, SETTINGS, limit=20)
        for item in picked:
            schedule_cards(db, user, item)
            seen.add(item.id)
        db.commit()
        _defer_everything(db)
        if stats["introduced"] == 0:
            break
    else:
        raise AssertionError(
            "introduction never dried up in 200 rounds; the safety stop is now "
            "the thing under test, which it must never be"
        )

    started = _started_referents(db, user.id)
    sense_by_form = _sense_by_form(db)
    stranded = [
        item.id
        for item in db.scalars(select(Item))
        if (refs := _item_referents(db.get(Node, item.node_id), item, sense_by_form))
        and not (refs <= started)
    ]
    total = db.scalar(select(func.count()).select_from(Item))
    assert not stranded, (
        f"introduction stalled with {len(stranded)} of {total} items never "
        f"offered (reached {len(seen)})"
    )


def test_an_unread_lesson_withholds_new_material_and_nothing_else(db, user):
    """The gate, and the line it does not cross.

    A concept is taught before it is drilled, so a node whose lesson is unread
    introduces nothing. What it must not do is hold work the learner has already
    started: a card they own keeps coming back through the debt queue, because
    losing new material is a gate and losing your own history is a punishment.
    """
    from datetime import datetime as dt

    from pl import concepts
    from pl.models import NodeUnlock

    node = db.scalar(select(Node).where(Node.key == "N03"))
    db.add(
        NodeUnlock(
            user_id=user.id,
            node_id=node.id,
            unlocked_at=dt.now(UTC).replace(tzinfo=None),
        )
    )
    db.commit()

    # The learner has met the words this node inflects, or the word-first rule
    # withholds its items whatever the lesson says, and the gate is untested.
    from pl.models import Sense
    from pl.schedule import LEXICAL, card_for

    items = list(db.scalars(select(Item).where(Item.node_id == node.id)))
    for candidate in items:
        if candidate.target_form_id is None:
            continue
        lexeme_id = db.get(Form, candidate.target_form_id).lexeme_id
        sense = db.scalar(select(Sense.id).where(Sense.lexeme_id == lexeme_id))
        if sense is not None:
            card_for(db, user.id, LEXICAL, sense)

    # A card of that node, already met. Back-dated first so today's budget is
    # whole, then made due — the debt segment is the half of this the gate must
    # not touch.
    item = items[0]
    schedule_cards(db, user, item)
    db.commit()
    _defer_everything(db)
    for card in db.scalars(select(Card).where(Card.user_id == user.id)):
        if card.form_id == item.target_form_id or card.pattern_id == item.pattern_id:
            card.due_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
    db.commit()

    picked, stats = build_session(db, user.id, SETTINGS, limit=20)
    introduced = picked[len(picked) - stats["introduced_items"]:]
    assert not [i for i in introduced if i.node_id == node.id], (
        "the lesson is unread, so this node must introduce nothing"
    )
    assert item.id in {i.id for i in picked}, (
        "a card the learner already holds was withheld — that is not the gate"
    )

    concepts.mark_read(db, user.id, concepts.concept_for_node("N03")["key"])
    read_every_concept(db, user)
    picked, stats = build_session(db, user.id, SETTINGS, limit=20)
    introduced = picked[len(picked) - stats["introduced_items"]:]
    assert [i for i in introduced if i.node_id == node.id], (
        "the lesson is read, so the node may now introduce"
    )


def test_grammar_introduces_a_noun_only_after_its_meaning(db, user):
    """A word is met as a word before it is met as an ending.

    Grammar items are built for every noun, and a vocabulary node can open well
    after the grammar nodes that inflect its words. Without this the learner is
    asked "Write the Polish for 'fridge'" before ever having seen `lodówka`: with
    the themed vocabulary nodes opening after N06, 52 of the 116 words a learner
    met over ninety simulated days arrived that way, and 51 never through their
    meaning item at all.

    Every node is unlocked, so every pool competes and nothing but the rule keeps
    a grammar item for an unmet noun out of the session.
    """
    from datetime import datetime as dt

    read_every_concept(db, user)

    from pl.models import NodeLexeme, NodeUnlock
    from pl.schedule import LEXICAL

    for node in db.scalars(select(Node)):
        db.add(
            NodeUnlock(
                user_id=user.id,
                node_id=node.id,
                unlocked_at=dt.now(UTC).replace(tzinfo=None),
            )
        )
    db.commit()

    sense_by_form = _sense_by_form(db)
    taught_words = set(
        db.scalars(
            select(Form.id)
            .join(NodeLexeme, NodeLexeme.lexeme_id == Form.lexeme_id)
            .join(Node, Node.id == NodeLexeme.node_id)
            .where(Node.type == "vocabulary")
        )
    )

    checked = 0
    for _ in range(12):
        met = {ref for ref in _started_referents(db, user.id) if ref[0] == LEXICAL}
        picked, _ = build_session(db, user.id, SETTINGS, limit=20)
        for item in picked:
            node = db.get(Node, item.node_id)
            word = (LEXICAL, sense_by_form.get(item.target_form_id))
            if node.type == "vocabulary":
                met.add(word)
            elif item.target_form_id in taught_words:
                checked += 1
                assert word in met, (
                    f"{item.prompt!r} ({node.key}) introduces "
                    f"{item.expected_answer!r} before its meaning item"
                )
            schedule_cards(db, user, item)
        db.commit()
        _defer_everything(db)

    assert checked, "no grammar item for a taught noun was served; nothing was tested"


def test_a_stratum_only_downstream_words_populate_is_refused(db):
    """The word-first rule has a deadlock in it, and the build must refuse it.

    A stratum counts toward its node's gate from the moment it exists. If every
    noun populating it is taught by a vocabulary node that opens only *after*
    that grammar node is mastered, its items wait for a meaning card that waits
    for the node that waits for them. `Verified:` nine masculine personal nouns
    in V02 gave N05 five such strata, N05 was never mastered on any seed, and a
    flawless learner's streak fell from 60 days to 36.
    """
    from pl.models import NodeLexeme

    n05 = db.scalar(select(Node).where(Node.key == "N05"))
    stratum = db.scalar(select(Pattern).where(Pattern.node_id == n05.id))
    v02 = db.scalar(select(Node).where(Node.key == "V02"))
    downstream_word = db.scalar(
        select(models.Lexeme.lemma)
        .join(NodeLexeme, NodeLexeme.lexeme_id == models.Lexeme.id)
        .where(NodeLexeme.node_id == v02.id)
    )

    with pytest.raises(AssertionError, match="unreachable"):
        frames.assert_every_stratum_is_reachable(
            db, {(stratum.rule_key, stratum.paradigm_class): {downstream_word}}
        )


def test_introduction_opens_a_new_stratum_before_another_form_of_an_old_one(db, user):
    """A node is mastered through its pattern cards, not its forms.

    Another noun in a stratum the learner has already started costs a card from
    the daily ten and moves no gate; an item in a stratum they have not met
    creates the pattern card the gate counts. With the vocabulary trebled, the
    second kind was being crowded out by the first: measured to 150 days, N08
    stopped opening at all at 85% accuracy, and a flawless learner's N09 and N10
    went with it.
    """
    from datetime import datetime as dt

    read_every_concept(db, user)

    from pl.models import NodeUnlock
    from pl.schedule import PATTERN

    # Only the node under test, so the daily budget is not spread across fifteen
    # pools and this test measures the ordering rather than the round-robin.
    node = db.scalar(select(Node).where(Node.key == "N04"))
    db.add(
        NodeUnlock(
            user_id=user.id,
            node_id=node.id,
            unlocked_at=dt.now(UTC).replace(tzinfo=None),
        )
    )
    db.commit()
    items = list(db.scalars(select(Item).where(Item.node_id == node.id)))
    strata = {i.pattern_id for i in items if i.pattern_id is not None}
    assert len(strata) >= 2, "this node cannot show the preference"
    started_pattern = sorted(strata)[0]

    # The learner has met every word this node inflects, or the word-first rule
    # holds all of them back and nothing here is about ordering.
    from pl.models import Sense
    from pl.schedule import LEXICAL, card_for

    for item in items:
        if item.target_form_id is None:
            continue
        lexeme_id = db.get(Form, item.target_form_id).lexeme_id
        sense = db.scalar(select(Sense.id).where(Sense.lexeme_id == lexeme_id))
        if sense is not None:
            card_for(db, user.id, LEXICAL, sense)
    db.commit()
    seed_item = next(i for i in items if i.pattern_id == started_pattern)
    schedule_cards(db, user, seed_item)
    db.commit()
    # A card created now is due now, and the debt segment would serve it before
    # anything is introduced — this is about what introduction chooses.
    _defer_everything(db)

    picked, stats = build_session(db, user.id, SETTINGS, limit=20)
    introduced = picked[len(picked) - stats["introduced_items"]:]
    from_node = [i for i in introduced if i.node_id == node.id and i.pattern_id]
    assert from_node, "nothing was introduced from the node under test"
    first = from_node[0]
    assert first.pattern_id != started_pattern, (
        f"introduction offered another form of the stratum already started "
        f"({first.prompt!r} / {first.expected_answer!r}) while "
        f"{len(strata) - 1} unstarted strata waited"
    )
    assert PATTERN


def _back_date_attempts(db, days: int):
    """Move the answer history back, leaving every card's schedule where it is.

    The cooldown is measured from when a card was last advanced, so a test for it
    must move time without also making the card due.
    """
    for attempt in db.scalars(select(Attempt)):
        attempt.created_at -= timedelta(days=days)
    db.commit()


def test_an_early_answer_advances_the_schedule_at_most_every_three_days(db, user):
    """A card met daily never earns a long interval, and mastery reads intervals.

    Any item scores the cards it touches, due or not, and a wide stratum's pattern
    card is touched almost every day: at 105 items it was rated 30 times in 36
    days, and at 85% accuracy its stability stalled at 3.2 against the seven-day
    mastery bar while a quiet card in the same node reached 21.5. One learner in
    twelve never finished the accusative because of it.
    """
    from pl import schedule
    from pl.schedule import EARLY_REVIEW_COOLDOWN_DAYS, MORPH

    item = db.scalar(
        select(Item)
        .join(Node, Node.id == Item.node_id)
        .where(Node.type == "grammar", Item.target_form_id.is_not(None))
    )
    card = schedule.cards_for_item(db, user.id, item)[MORPH]
    card.due_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30)
    db.commit()

    answer(db, user, item)
    assert card.reps == 1, "the first answer should have advanced the schedule"

    _back_date_attempts(db, 1)
    answer(db, user, item)
    assert card.reps == 1, (
        "a card answered again the next day, while not due, advanced anyway — "
        "this is what keeps a busy card's interval at zero"
    )

    _back_date_attempts(db, EARLY_REVIEW_COOLDOWN_DAYS)
    answer(db, user, item)
    assert card.reps == 2, "after the cooldown an early answer counts again"

    # A due card is always scored, cooldown or not.
    _back_date_attempts(db, 1)
    card.due_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
    db.commit()
    answer(db, user, item)
    assert card.reps == 3, "a card that is actually due must always be scored"


def _gloss_for(db, lemma: str, prompt: str) -> str | None:
    for item in db.scalars(select(Item).where(Item.prompt == prompt)):
        if item.target_form_id is None:
            continue
        if db.get(Form, item.target_form_id).lexeme_id == _lexeme_id(db, lemma):
            return item.gloss
    return None


def _lexeme_id(db, lemma: str) -> int:
    return db.scalar(select(models.Lexeme.id).where(models.Lexeme.lemma == lemma))


def test_a_place_takes_its_own_preposition_for_direction_too(db):
    """`Idę na targ`, never `Idę do targu` — and the reverse for `w` places.

    Which preposition a place takes is lexical, and it governs direction as well
    as location: the course built `Idę do poczty` and `Idę do targu`, which a
    Polish speaker does not say. One field on the lexeme decides both.
    """
    for lemma in ("targ", "poczta", "balkon", "uniwersytet", "wieś"):
        assert _gloss_for(db, lemma, "Idę na ___.") is not None, (
            f"{lemma} takes na for place but has no direction item"
        )
        assert _gloss_for(db, lemma, "Idę do ___.") is None, (
            f"{lemma} takes na for place, so do + genitive is the wrong direction"
        )
    for lemma in ("sklep", "apteka", "kino"):
        assert _gloss_for(db, lemma, "Idę do ___.") is not None
        assert _gloss_for(db, lemma, "Idę na ___.") is None


def test_the_english_gloss_carries_the_article_its_noun_needs(db):
    """Polish has no articles, so every one the course prints is authored.

    Without the distinction the frames printed "I like cat" for every count noun
    and "I have the coffee" for every mass one.
    """
    assert _gloss_for(db, "kot:Sm2", "Lubię ___.") == "I like a cat."
    assert _gloss_for(db, "kawa", "Lubię ___.") == "I like coffee."
    assert _gloss_for(db, "jabłko", "Lubię ___.") == "I like an apple."
    assert _gloss_for(db, "kot:Sm2", "Widzę ___.") == "I see the cat."
    assert _gloss_for(db, "kawa", "Nie mam ___.") == "I do not have coffee."
    assert _gloss_for(db, "kot:Sm2", "Nie mam ___.") == "I do not have a cat."


def test_a_relation_is_mine_where_english_needs_a_possessive(db):
    """"I like the brother" is not English, and Polish offers no possessive.

    `Lubię brata` carries none, so the English gloss supplies one until the
    course teaches `mój` — everywhere except where English counts relations
    instead: "I have a brother", "I do not have a brother".
    """
    assert _gloss_for(db, "brat", "Lubię ___.") == "I like my brother."
    assert _gloss_for(db, "brat", "To jest ___.") == "This is my brother."
    assert _gloss_for(db, "brat", "To jest dom ___.") == "This is my brother's house."
    assert _gloss_for(db, "brat", "Mam ___.") == "I have a brother."
    assert _gloss_for(db, "brat", "Nie mam ___.") == "I do not have a brother."
    # Not everyone is a relation.
    assert _gloss_for(db, "lekarz", "Lubię ___.") == "I like a doctor (m)."


def test_the_indefinite_article_follows_the_sound_not_the_letter(db):
    """"an university" is what a rule reading the first letter writes.

    English chooses `a` or `an` by sound, and `university` starts with a
    consonant sound. A lexeme can say so, beside `mass:` and `relation:`.
    """
    assert _gloss_for(db, "uniwersytet", "To jest ___.") == "This is a university."
    assert _gloss_for(db, "jabłko", "To jest ___.") == "This is an apple."


def test_a_frame_can_be_kept_off_a_word_it_would_mislead_on(db):
    """`Mam dziewczynę` is heard as "I have a girlfriend"."""
    for lemma in ("chłopak", "dziewczyna"):
        for prompt in ("Mam ___.", "Nie mam ___."):
            assert _gloss_for(db, lemma, prompt) is None, (
                f"{lemma} still has {prompt!r}, which the gloss cannot say honestly"
            )
        # Still taught everywhere the reading is plain.
        assert _gloss_for(db, lemma, "Widzę ___.") is not None


def _reload_with(monkeypatch, lexemes):
    """Re-read the curriculum with an edited lexeme list, the way an author would."""
    real = ingest._load

    def fake(name):
        return lexemes if name == "lexemes.yaml" else real(name)

    monkeypatch.setattr(ingest, "_load", fake)


def test_moving_a_word_between_vocabulary_nodes_reaches_an_existing_database(
    db, monkeypatch
):
    """An edit to `vocabulary_node` must survive a rebuild, not half of one.

    `ingest_nodes` only ever added `NodeLexeme` rows and an item is identified
    without its node, so a moved word kept its old membership, gained the new
    one, and left its meaning item where it was. Nothing reported it. Move
    enough words into V01 that way and its gate counts senses whose only meaning
    item sits behind N06 — V01 is the root, so the course stops at its first
    node with every row well-formed.
    """
    from pl.models import NodeLexeme

    lexemes = [dict(e) for e in ingest._load("lexemes.yaml")]
    moved = next(e for e in lexemes if e.get("vocabulary_node") == "V02")
    del moved["vocabulary_node"]
    _reload_with(monkeypatch, lexemes)

    ingest.ingest_all(db)
    frames.build(db)

    lexeme = db.scalar(select(models.Lexeme).where(models.Lexeme.lemma == moved["lemma"]))
    owners = sorted(
        key
        for (key,) in db.execute(
            select(Node.key)
            .join(NodeLexeme, NodeLexeme.node_id == Node.id)
            .where(NodeLexeme.lexeme_id == lexeme.id, Node.type == "vocabulary")
        )
    )
    assert owners == ["V01"], f"{moved['lemma']} is owned by {owners}"

    meaning = [
        db.get(Node, i.node_id).key
        for i in db.scalars(select(Item).where(Item.pattern_id.is_(None)))
        if i.target_form_id is not None
        and db.get(Form, i.target_form_id).lexeme_id == lexeme.id
        and i.prompt.startswith("Which word means")
    ]
    assert meaning == ["V01"], f"its meaning item is under {meaning}"


def test_every_gating_word_can_be_met_under_the_node_that_gates_on_it(db):
    """A vocabulary node's gate and its items must name the same words.

    A sense counted by a node the learner cannot meet it through is a gate with
    no key: only a meaning item creates a lexical card, and introduction offers
    items from the node they belong to.
    """
    from pl.models import NodeLexeme, Sense

    for node in db.scalars(select(Node).where(Node.type == "vocabulary")):
        gating = {
            sense_id
            for (sense_id,) in db.execute(
                select(Sense.id)
                .join(NodeLexeme, NodeLexeme.lexeme_id == Sense.lexeme_id)
                .where(NodeLexeme.node_id == node.id)
            )
        }
        met = set()
        for item in db.scalars(select(Item).where(Item.node_id == node.id)):
            if item.target_form_id is None:
                continue
            lexeme_id = db.get(Form, item.target_form_id).lexeme_id
            sense = db.scalar(select(Sense.id).where(Sense.lexeme_id == lexeme_id))
            met.add(sense)
        assert gating <= met, (
            f"{node.key} gates on {len(gating - met)} word(s) it offers no item for"
        )


def test_the_build_itself_refuses_an_unreachable_stratum(db, monkeypatch):
    """The guard has to be wired into the build, not merely importable.

    Deleting the call left the suite green, including with the nine masculine
    personal nouns whose strata under N05 nothing can reach — the deadlock the
    guard exists for.
    """
    lexemes = [dict(e) for e in ingest._load("lexemes.yaml")]
    lexemes.append(
        {"lemma": "ojciec", "gloss": "father", "theme": "people", "vocabulary_node": "V02"}
    )
    _reload_with(monkeypatch, lexemes)

    ingest.ingest_all(db)
    with pytest.raises(AssertionError, match="unreachable"):
        frames.build(db)


def schedule_cards(db, user, item):
    from pl import schedule

    schedule.cards_for_item(db, user.id, item)


def _defer_everything(db):
    """Push every card far into the future, and move yesterday's introductions
    into yesterday so the next call gets a fresh daily budget."""
    later = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=90)
    for card in db.scalars(select(Card)):
        card.due_at = later
        if card.created_at is not None:
            card.created_at -= timedelta(days=1)
    db.commit()


def test_no_dictation_is_offered_without_a_synthesiser(db, monkeypatch):
    """A listening item is unanswerable without a voice and must not be offered.

    Asserted on the guard itself rather than by driving `build_session`, because
    a dictation item is **currently unreachable through every segment**: it
    shares both referents with the cloze item built from the same sentence, so
    introduction always sees it as started, and the debt path serves the first
    item for a due form, which is that cloze. A test driven through the composer
    would pass while asserting nothing. The guard is applied at all three
    segments and is what will matter once dictation becomes reachable.

    Runs on every platform, unlike the rest of the audio tests.
    """
    from pl import audio
    from pl.domain import AUDIBLE
    from pl.session import offerable

    dictation = db.scalar(
        select(Item).where(Item.exercise_type.in_(tuple(AUDIBLE)))
    )
    cloze = db.scalar(select(Item).where(Item.exercise_type == "cloze"))
    assert dictation is not None and cloze is not None

    monkeypatch.setattr(audio, "available", lambda: True)
    assert offerable(dictation) and offerable(cloze)

    monkeypatch.setattr(audio, "available", lambda: False)
    assert not offerable(dictation), "a listening item was offerable with no voice"
    assert offerable(cloze), "a written item must be unaffected"


def test_every_exercise_type_built_is_a_type_the_learner_can_meet(db, user, monkeypatch):
    """Replaces `test_listening_items_are_currently_unreachable`.

    26 dictation items, 131 free translations and 58 prep drills were built,
    graded correctly, and never served. Not by any rule anyone chose: a card is
    shared by every exercise built on the same form or stratum, the draw offered
    whichever had the lowest id, and ids follow build order — every sentence's
    cloze is generated before its dictation. **Build order was silently deciding
    the curriculum.**

    The test this replaces asserted the defect and was written to be deleted on
    the day it broke. It never broke, and that is the more useful lesson: it
    deferred every card before each session, so it never built a backlog, and the
    debt queue is the only segment that can offer a *second* exercise for a form
    the learner already knows. It was asserting the defect through the one path
    that could not have shown the fix.
    """
    from datetime import datetime as dt

    read_every_concept(db, user)

    from pl import audio
    from pl.models import NodeUnlock

    monkeypatch.setattr(audio, "available", lambda: True)
    for node in db.scalars(select(Node)):
        db.add(
            NodeUnlock(
                user_id=user.id,
                node_id=node.id,
                unlocked_at=dt.now(UTC).replace(tzinfo=None),
            )
        )
    db.commit()

    rng = random.Random(5)
    met: set[str] = set()
    # Ninety days, because the curriculum is 2.7 times the size it was at forty
    # and dictation is reachable only through the debt queue — as a second
    # exercise for a form the learner already holds. Every other type is offered
    # by day eight; dictation lands on day 84 under this suite's FSRS fuzz seed,
    # and on day 58 or 64 under others, so the window carries headroom for a
    # figure that moves with the fuzz as well as with the content.
    for _ in range(90):
        picked, _ = build_session(db, user.id, SETTINGS, limit=20)
        for item in picked:
            met.add(item.exercise_type)
            correct = rng.random() < 0.85
            answer(db, user, item, None if correct else _wrong_form(db, item, rng))
        advance_one_day(db)

    built = set(db.scalars(select(Item.exercise_type).distinct()))
    assert built - met == set(), (
        f"built but never offered in ninety days: {sorted(built - met)}"
    )


# ------------------------------------------------- pacing, measured not assumed
# `scripts/journey_sim.py` is the instrument; these are the floors it establishes.
# The bounds are deliberately far below what the composer reaches today. They
# exist to catch the class of regression, not to pin a number that will move.


def _clear_debt(db):
    """Nothing due any more, but still the same day.

    Deliberately does not touch `created_at`: this models the learner finishing a
    round and pressing "Another round", which is the exact situation criterion 15
    is about.
    """
    later = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=2)
    for card in db.scalars(select(Card)):
        card.due_at = later
    db.commit()


def test_remediation_does_not_starve_new_material(db, user):
    """The middle segment must not be allowed to eat the whole session.

    This is the test whose absence let the product ship unusable with the suite
    green. Every other longitudinal test answers *correctly* — which leaves the
    error table empty, `weakest_node` returning None, and the remediation segment
    never executing at all. The defect lived in a branch no test entered.

    One wrong answer turns it on. Composed second, as the design reads, it filled
    the queue to `limit` from the weakest node every single day, so
    `len(picked) < limit` was never true again and introduction stopped for good
    on day two. Measured on the code this replaces: twenty distinct items over
    thirty days, every one of them multiple choice, no grammar, nothing unlocked,
    while the learner answered several hundred questions.
    """
    from pl.session import weakest_node

    read_every_concept(db, user)

    rng = random.Random(7)
    seen: set[int] = set()
    introduced_after_the_first_day = 0

    for day in range(1, 31):
        picked, stats = build_session(db, user.id, SETTINGS, limit=20)
        if day > 1:
            introduced_after_the_first_day += stats["introduced_items"]
        for item in picked:
            seen.add(item.id)
            correct = rng.random() < 0.85
            answer(db, user, item, None if correct else _wrong_form(db, item, rng))
        evaluate_unlocks(db, user.id)
        advance_one_day(db)

    # The premise. Without errors there is no weakest node, remediation never
    # runs, and everything below would pass while asserting nothing.
    assert weakest_node(db, user.id) is not None, (
        "no error events were recorded: the remediation segment never executed, "
        "so this test proved nothing"
    )
    assert introduced_after_the_first_day > 0, (
        "introduction stopped after day one — remediation is taking the session"
    )
    assert len(seen) > 40, (
        f"a learner studying every day for a month met {len(seen)} distinct items"
    )


def test_the_daily_cap_is_not_re_granted_by_asking_again(db, user):
    """Criterion 15: honoured "even when the learner keeps asking for more".

    The review page's "Another round" button reloads the page, which rebuilds the
    session. A cap enforced per *call* hands out a fresh ten every time it is
    pressed, and the debt spike the cap exists to prevent arrives three days
    later anyway.
    """
    from pl.session import DAILY_NEW_CAP

    read_every_concept(db, user)

    introduced = 0
    for _ in range(6):
        picked, stats = build_session(db, user.id, SETTINGS, limit=20)
        introduced += stats["introduced"]
        for item in picked:
            answer(db, user, item)
        _clear_debt(db)

    assert introduced > 0, "nothing was introduced at all; the test proves nothing"
    assert introduced <= DAILY_NEW_CAP, (
        f"{introduced} new cards were introduced in a single day against a cap "
        f"of {DAILY_NEW_CAP} — the cap is being re-granted per call"
    )


def _spend_the_day(db, user) -> None:
    """Take ordinary sessions until the day's new-card budget is gone."""
    for _ in range(6):
        picked, stats = build_session(db, user.id, SETTINGS, limit=20)
        if stats["introduced"] == 0:
            return
        for item in picked:
            answer(db, user, item)
        _clear_debt(db)
    pytest.fail("the day's budget never ran out")


def test_an_extra_round_brings_new_words_once_the_day_is_spent(db, user):
    """The learner asked for more, and more now includes new words.

    "Another round" rebuilt the day's session, which by then had nothing new to
    give — the cap was spent — so it re-served the words just met, and because a
    card moves once a day those answers changed nothing. An extra round is an
    explicit request: up to `EXTRA_ROUND_NEW` new cards however many the day has
    had, and review for the rest. The owner chose no daily limit, so a second
    extra round brings more again.
    """
    from pl.session import EXTRA_ROUND_NEW

    read_every_concept(db, user)
    _spend_the_day(db, user)
    for round_number in (1, 2):
        picked, stats = build_session(db, user.id, SETTINGS, limit=10, extra=True)
        assert 0 < stats["introduced"] <= EXTRA_ROUND_NEW, f"extra round {round_number}"
        assert stats["introduced_items"] < len(picked), "the rest of the round is review"
        for item in picked:
            answer(db, user, item)
        _clear_debt(db)


def test_an_extra_round_waits_while_reviews_are_overdue(db, user):
    """Criterion 9's bound holds for extra rounds too: nothing new on a backlog."""
    from pl.models import Attempt
    from pl.session import DEBT_TOLERANCE

    read_every_concept(db, user)
    picked, _ = build_session(db, user.id, SETTINGS, limit=20)
    for item in picked:
        answer(db, user, item)
    # Answered yesterday and overdue today — debt, not today's settled work.
    yesterday = timedelta(days=1)
    for attempt in db.scalars(select(Attempt).where(Attempt.user_id == user.id)):
        attempt.created_at -= yesterday
    cards = list(db.scalars(select(Card).where(Card.user_id == user.id)))
    assert len(cards) > DEBT_TOLERANCE, "not enough cards to make a backlog"
    for card in cards:
        card.created_at -= yesterday
        card.due_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
    db.commit()

    # Room to spare after the overdue cards, so the bound — not a full round —
    # is what keeps new material out.
    picked, stats = build_session(db, user.id, SETTINGS, limit=40, extra=True)
    assert len(picked) < 40, "the backlog filled the round; the bound went untested"
    assert stats["introduced"] == 0, "an extra round introduced on top of a backlog"


def test_an_extra_round_reviews_what_a_review_would_still_move(db, user):
    """Review that keeps words alive, not review that repeats today.

    A card moves once a day, and not again within `EARLY_REVIEW_COOLDOWN_DAYS`,
    so re-serving the words just met is practice the schedule never sees. An
    extra round's review half prefers words met before that window — and it
    exists without mistakes: ordinary remediation draws on the weakest node,
    which a learner who has answered everything correctly does not have.

    The words met a week ago are deliberately the *later* batch, with the higher
    item ids: taken in id order, today's words would come first, so only the
    preference can put last week's ahead of them.
    """
    from pl import schedule
    from pl.models import Attempt

    read_every_concept(db, user)
    first, _ = build_session(db, user.id, SETTINGS, limit=20)
    for item in first:
        answer(db, user, item)
    _clear_debt(db)
    today = {card.id for card in db.scalars(select(Card).where(Card.user_id == user.id))}
    for card in db.scalars(select(Card).where(Card.id.in_(today))):
        card.created_at -= timedelta(days=1)  # a fresh budget for the second batch
    db.commit()
    last_attempt = db.scalar(select(func.max(Attempt.id)))

    second, _ = build_session(db, user.id, SETTINGS, limit=20)
    for item in second:
        answer(db, user, item)
    _clear_debt(db)
    week = timedelta(days=7)
    for attempt in db.scalars(select(Attempt).where(Attempt.id > last_attempt)):
        attempt.created_at -= week
    older = set()
    for card in db.scalars(
        select(Card).where(Card.user_id == user.id, Card.id.not_in(today))
    ):
        card.created_at -= week
        older.add(card.id)
    db.commit()
    assert older, "the second session met no new words; the test proves nothing"

    picked, stats = build_session(db, user.id, SETTINGS, limit=10, extra=True)
    review = picked[: len(picked) - stats["introduced_items"]]
    assert review, "an extra round with no review in it"
    for item in review:
        cards = schedule.cards_for_item(db, user.id, item).values()
        assert any(card.id in older for card in cards), (
            f"{item.prompt!r} repeats a word met today while words from last week "
            f"were waiting for a review that would count"
        )


# ------------------------------------------------------- the pattern-card draw


def _stratum_with_several_lexemes(db):
    """A pattern whose items cover more than one lexeme, or there is nothing to
    rotate between and the test would pass vacuously."""
    for pattern in db.scalars(select(Pattern)):
        items = list(db.scalars(select(Item).where(Item.pattern_id == pattern.id)))
        lexemes = {
            db.get(Form, i.target_form_id).lexeme_id
            for i in items
            if i.target_form_id is not None
        }
        if len(lexemes) >= 2:
            return pattern, items, lexemes
    raise AssertionError("no stratum carries two lexemes")


def test_a_pattern_card_draws_only_on_vocabulary_the_learner_has_met(db, user):
    """§"Pattern cards are stratified": the draw is intersected with what the
    learner knows.

    A rule card drawn from the whole stratum tests the locative on a noun the
    learner has never seen — and the routing table scores that failure against
    the *rule*, so the learner is marked down on grammar for a vocabulary gap
    and the rule card's schedule is corrupted by it.
    """
    from pl import schedule
    from pl.schedule import PATTERN
    from pl.session import _items_for_card

    pattern, items, lexemes = _stratum_with_several_lexemes(db)
    card = schedule.card_for(db, user.id, PATTERN, pattern.id)
    db.commit()

    assert _items_for_card(db, card, set()) == [], (
        "a pattern card drew items when the learner had met none of its lexemes"
    )

    one = sorted(lexemes)[0]
    drawn = _items_for_card(db, card, {one})
    assert drawn, "meeting a lexeme in the stratum drew nothing"
    for item in drawn:
        assert db.get(Form, item.target_form_id).lexeme_id == one, (
            "the draw reached outside the lexemes the learner has met"
        )


def test_a_pattern_card_does_not_test_the_same_word_every_time(db, user):
    """An unordered draw returns insertion order for the life of the account.

    The debt loop takes the first offerable item, so the rule card is tested on
    one noun forever while claiming — and being credited for — generalisation
    across its whole stratum. That claim is what makes "80% of the node's pattern
    cards" a meaningful threshold, so a frozen draw quietly falsifies the gate.
    """
    from pl import schedule
    from pl.schedule import PATTERN
    from pl.session import _items_for_card

    pattern, items, lexemes = _stratum_with_several_lexemes(db)
    card = schedule.card_for(db, user.id, PATTERN, pattern.id)
    db.commit()

    first = _items_for_card(db, card, lexemes)[0]
    answer(db, user, first)
    second = _items_for_card(db, card, lexemes)[0]

    assert second.id != first.id, (
        f"the stratum served {first.expected_answer!r} twice running, with "
        f"{len(lexemes)} lexemes available to draw from"
    )


def _backlog(db, user, size: int) -> None:
    """Leave exactly `size` cards overdue, defer the rest, and make today new.

    Back-dates the attempt history as well as the cards. A backlog is work left
    over from *previous* days, and a card whose schedule already advanced today
    is not debt — it has had its turn and answering it again cannot move it. Left
    dated today, these cards would be filtered out of the debt queue and the
    helper would build a backlog of nothing.
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    cards = list(db.scalars(select(Card).where(Card.user_id == user.id)))
    assert len(cards) >= size, f"only {len(cards)} cards exist; need {size}"
    for index, card in enumerate(cards):
        card.due_at = now - timedelta(hours=1) if index < size else now + timedelta(days=30)
        # Yesterday's introductions, so today's budget starts unspent.
        if card.created_at is not None:
            card.created_at -= timedelta(days=1)
    for attempt in db.scalars(select(Attempt).where(Attempt.user_id == user.id)):
        attempt.created_at -= timedelta(days=1)
    db.commit()


def test_a_small_backlog_does_not_stop_the_curriculum_opening(db, user):
    """Criterion 9's bound is a measured number, not a principle.

    "Introduce nothing while anything is overdue" is the stricter-sounding rule
    and it is the one that stops the course opening. A learner at 85% accuracy is
    rarely at zero due cards and almost never at zero twice running, so
    introduction fired roughly one day in four: 123 distinct items and **one**
    node unlocked over ninety simulated days, averaged over four seeds, with two
    of six exercise types ever served. At a tolerance of five the same learner
    reaches 224 items and four nodes on every seed.

    Asserted at the boundary rather than by driving ninety days — that is what
    `scripts/journey_sim.py` is for, and a test that took ninety days to fail
    would tell nobody which line broke it.
    """
    read_every_concept(db, user)
    from pl.session import DEBT_TOLERANCE

    first, _ = build_session(db, user.id, SETTINGS, limit=20)
    for item in first:
        answer(db, user, item)

    _backlog(db, user, DEBT_TOLERANCE)
    _, at_the_bound = build_session(db, user.id, SETTINGS, limit=20)
    assert at_the_bound["debt_total"] == DEBT_TOLERANCE
    assert at_the_bound["introduced"] > 0, (
        f"a backlog of {DEBT_TOLERANCE} — the tolerance itself — stopped "
        f"introduction; the comparison is off by one"
    )

    _backlog(db, user, DEBT_TOLERANCE + 1)
    _, past_the_bound = build_session(db, user.id, SETTINGS, limit=20)
    assert past_the_bound["debt_total"] == DEBT_TOLERANCE + 1
    assert past_the_bound["introduced"] == 0, (
        "the bound does not bind: new material was introduced with a backlog "
        "past the tolerance, so debt can grow without ever blocking novelty"
    )


def test_an_untouched_stratum_does_not_always_offer_the_same_exercise(db, user):
    """Practice count cannot separate items the learner has never met.

    Everything unmet ties at zero and falls through to the id — and ids follow
    build order, where every sentence's cloze is generated before its dictation
    and every stratum's cloze items before its prep drills. A pattern card with
    eighty untouched candidates therefore offered the same cloze at every review,
    which is how 26 dictation items, 131 free translations and 58 prep drills
    came to be unreachable by construction rather than by any rule.

    Measured over ninety simulated days: without the rotation the learner meets
    three of six exercise types and no listening item at all; with it, five.
    """
    from pl import schedule
    from pl.schedule import PATTERN
    from pl.session import _items_for_card

    pattern, items, lexemes = _stratum_with_several_lexemes(db)
    assert len(items) > 2, "too small a stratum to prove anything"
    card = schedule.card_for(db, user.id, PATTERN, pattern.id)
    db.commit()

    # Nothing has been answered, so practice count separates none of them.
    offered = set()
    for reps in range(6):
        card.reps = reps
        db.commit()
        offered.add(_items_for_card(db, card, lexemes)[0].id)

    assert len(offered) > 1, (
        "the stratum offered the same item at every review count — the draw is "
        "pinned to build order, and whatever was generated first wins forever"
    )


# ------------------------------------------------------------ the mastery gate


def test_a_four_stratum_node_lets_one_weak_class_be_carried():
    """The design's own worked example, finally true of the code.

    §"Pattern cards are stratified" says of the four-stratum accusative node:
    *"80% means three of four, and the learner may carry one weak paradigm class
    forward while the other three are solid."* Three of four is 0.75, so
    `mastered / n >= 0.8` demanded four of four. The example described behaviour
    the formula never delivered, and no fraction can deliver it — there is no
    granularity between "all" and "not all" below five strata, and four of the
    eleven grammar nodes are narrower than that.
    """
    from pl.session import strata_needed

    assert strata_needed(4) == 3, "the design's own example: three of four"
    assert strata_needed(3) == 2
    # The value the whole N03 content fix turns on: a second paradigm class must
    # *widen* a one-stratum node, not narrow it. Under the fraction alone this is
    # 2, which would re-close N03 and the six nodes behind it with every test
    # still green.
    assert strata_needed(2) == 1, "a second stratum must widen the node"
    assert strata_needed(1) == 1, "a node is not mastered by mastering nothing"
    # Wide nodes are untouched: the fraction is already the binding constraint.
    assert strata_needed(5) == 4
    assert strata_needed(8) == 7
    assert strata_needed(28) == 23


def test_a_rule_card_advances_once_a_day_however_often_its_rule_comes_up(db, user):
    """A pattern card is shared by every item in its stratum.

    A session holding ten items of one rule reviewed that rule's card ten times,
    minutes apart, and FSRS grows stability from the interval actually elapsed —
    so those were ten intervals of nearly zero. Measured over ninety days one
    card took 447 reviews and stalled at 6.11 stability, under the seven-day
    bar, while a low-traffic card in the same node reached 112 on eight reviews.
    The busiest cards were the least able to master, which is the opposite of
    what the schedule is for.

    The attempt and its error events are still recorded — remediation still sees
    everything the learner got wrong. Only the schedule is left alone.
    """
    from pl.models import Review
    from pl.schedule import PATTERN

    pattern, items, _ = _stratum_with_several_lexemes(db)
    drilled = items[:4]
    assert len(drilled) == 4, "need several items in one stratum to prove anything"

    for item in drilled:
        answer(db, user, item)

    card = db.scalar(
        select(Card).where(
            Card.user_id == user.id,
            Card.population == PATTERN,
            Card.pattern_id == pattern.id,
        )
    )
    assert card is not None, "four items of this rule created no rule card"

    reviews = db.scalar(
        select(func.count()).select_from(Review).where(Review.card_id == card.id)
    )
    assert reviews == 1, (
        f"the rule card was advanced {reviews} times in one day — its stability "
        f"is being set by how often the rule comes up, not by recall"
    )

    # The evidence is still there; only the scheduling is suppressed. This is
    # the stated reason the suppression is safe — remediation reads `error_event`
    # to find the weakest node — so it is asserted rather than assumed.
    assert db.scalar(
        select(func.count()).select_from(Attempt).where(Attempt.user_id == user.id)
    ) == 4, "attempts must still be recorded for every answer"

    from pl.models import ErrorEvent

    errors_before = db.scalar(select(func.count()).select_from(ErrorEvent))
    answer(db, user, drilled[0], submitted="zzzzzz")
    assert db.scalar(select(func.count()).select_from(ErrorEvent)) == errors_before + 1, (
        "a wrong answer on a card already advanced today recorded no error event, "
        "so remediation cannot see the mistake the learner just made"
    )

    # Cards that are *not* shared still move once each.
    forms = {i.target_form_id for i in drilled}
    assert len(forms) > 1, "these items share a form, so they prove nothing"


def test_the_universal_rules_still_reach_every_noun(db):
    """Theme gating narrows the curriculum; it does not leave holes in it.

    A rule's strata are derived from the frames that populate them, so gating a
    frame cannot leave a stratum empty — it removes the stratum instead, and the
    node then teaches fewer paradigm classes than the lexeme set contains, with
    nothing failing anywhere. Gate every accusative frame to `people` and the
    accusative nodes quietly stop teaching neuter and masculine-inanimate
    endings: measured, N04 drops from three strata to two and N05 from four to
    two, and the build reports success.

    These rules are the ones that genuinely apply to any noun — you can name,
    like, lack or think about anything — so each keeps one ungated frame, and
    every noun must reach every one of them. That is what makes the gates on the
    *other* frames safe to add, and safe to keep adding as the lexeme set grows.
    """
    from pl.models import Form, Lexeme

    universal = {
        "NOM_SG": "name it",
        "NOM_PREDICATE": "point at it",
        "ACC_AFTER_TRANSITIVE_VERB": "like it",
        "GEN_NEGATION": "lack it",
        "LOC_PREPOSITION": "think about it",
    }
    reached: dict[str, set[int]] = {rule: set() for rule in universal}
    for item, pattern, form in db.execute(
        select(Item, Pattern, Form)
        .join(Pattern, Pattern.id == Item.pattern_id)
        .join(Form, Form.id == Item.target_form_id)
    ):
        if pattern.rule_key in reached:
            reached[pattern.rule_key].add(form.lexeme_id)

    nouns = {lx.id: lx.lemma for lx in db.scalars(select(Lexeme).where(Lexeme.pos == "subst"))}
    missing = {
        f"{rule} ({why})": sorted(nouns[i] for i in nouns.keys() - reached[rule])
        for rule, why in universal.items()
        if nouns.keys() - reached[rule]
    }
    assert not missing, (
        "theme gating has cut nouns out of a rule that applies to every noun: "
        f"{missing}"
    )


def test_removing_content_is_reported_because_the_build_never_deletes(db):
    """The build is an upsert, so *removing* content does nothing to a database
    that already has it.

    Gate a frame away from a theme and every sentence it used to make is still
    stored, still served, and the build still reports success — the 121 items
    the theme gates removed were all still live in the working database
    afterwards. Deleting them automatically is not the answer: an item may
    already carry attempts and error events, and a content edit is not a reason
    to rewrite what the learner did. So the build says so, and the operator
    decides.
    """
    from pl.content.ingest import stale_items

    assert stale_items(db) == [], "a freshly built curriculum has stale items"

    node = db.scalar(select(Node))
    db.add(
        Item(
            node_id=node.id,
            exercise_type="cloze",
            prompt="Kupuję ___.",
            expected_answer="kościół",  # "I am buying the church" — gated away
            source="template",
        )
    )
    db.commit()

    stale = stale_items(db)
    assert [i.expected_answer for i in stale] == ["kościół"], (
        "an item the current frames cannot produce went unreported, so removing "
        "content silently does nothing to an existing database"
    )


def test_a_preposition_agrees_with_the_word_that_follows_it(db):
    """`w` becomes `we` before a cluster it cannot be said against.

    Polish writes `we wsi`, not `w wsi`. Every M1 locative happened to be
    safe — `w szkole`, `w domu`, `w Krakowie` — so the template could carry a
    bare `w` and nothing revealed it. Adding one noun whose locative is `wsi`
    produced `Jestem w wsi`, which is simply wrong, and the build reported
    success: the expected surface is looked up and so cannot be wrong, but the
    *frame around it* is authored, and nothing was checking that.

    The trigger is the following word, not the frame, so it cannot be written
    into `frames.yaml` — one template has to yield both `w szkole` and `we wsi`.
    """
    from pl.content.frames import euphonic

    assert euphonic("Jestem w ___.", "wsi") == "Jestem we ___."
    assert euphonic("Jestem w ___.", "Wrocławiu") == "Jestem we ___."
    assert euphonic("Jestem w ___.", "szkole") == "Jestem w ___."
    assert euphonic("Idę z ___.", "stołem") == "Idę ze ___."
    assert euphonic("Idę z ___.", "siostrą") == "Idę z ___."
    # `do` and `na` have no syllabic form and must be left alone.
    assert euphonic("Idę do ___.", "wsi") == "Idę do ___."

    # The assertions above pin the rule; this pins that item construction
    # actually applies it, which is where the bug lived. A crude scan for " z s"
    # is not the check: `Idę z siostrą` is correct, because `si` is a consonant
    # and a vowel, not a cluster.
    built = {}
    for item in db.scalars(select(Item)):
        # "Jestem " rather than "Jestem w": a lexeme may override the preposition
        # entirely, and those items are exactly the ones worth checking.
        if item.prompt and item.prompt.startswith(("Jestem ", "Idę z")):
            rendered = item.prompt.replace("___", item.expected_answer)
            wanted = euphonic(item.prompt, item.expected_answer).replace(
                "___", item.expected_answer
            )
            built[rendered] = wanted

    wrong = sorted(got for got, want in built.items() if got != want)
    assert not wrong, f"built without applying the rule: {wrong}"
    assert "Jestem w szkole." in built, "the rule fired where it should not have"
    assert "Idę z siostrą." in built, "`z siostrą` is correct and must be left alone"
    # `wieś` is what exposed this rule, and no longer demonstrates it: its place
    # preposition is `na`, so the w/we question never arises for it now. The rule
    # is still right and still load-bearing for the next such noun — Polish has
    # plenty — which is why the unit assertions above carry the proof and this
    # corpus check only guards the wiring.
    assert "Jestem na wsi." in built, "the lexical override did not reach the build"


def test_a_place_takes_the_preposition_its_own_word_governs(db):
    """Which preposition a place takes is lexical, and no rule derives it.

    `w szkole` but `na uniwersytecie`; `w mieście` but `na wsi`. Nothing about
    the noun's shape, gender or paradigm predicts it — it is a fact about the
    word, so it lives on the word. Both were built wrong until a native speaker
    read them, which is the point: the "looked up, not written" guarantee covers
    the *form*, and every word around the form is still authored.

    Distinct from `euphonic`, which chooses between `w` and `we` for the same
    preposition on phonological grounds. One is which preposition; the other is
    how to say it. `wieś` needs both answers and they disagree — `we wsi` is the
    right way to say the wrong preposition.
    """
    from pl.content.frames import place_preposition

    assert place_preposition("Jestem w ___.", "uniwersytecie", "na") == "Jestem na ___."
    assert place_preposition("Jestem w ___.", "wsi", "na") == "Jestem na ___."
    # No override: the phonological rule still gets its say.
    assert place_preposition("Jestem w ___.", "wsi", None) == "Jestem we ___."
    assert place_preposition("Jestem w ___.", "szkole", None) == "Jestem w ___."

    built = {
        item.prompt.replace("___", item.expected_answer)
        for item in db.scalars(select(Item))
        if item.prompt and item.prompt.startswith("Jestem ")
    }
    assert "Jestem na uniwersytecie." in built
    assert "Jestem na wsi." in built
    assert "Jestem w szkole." in built, "the override leaked onto a noun that takes w"
    assert not [s for s in built if s.startswith("Jestem w uniwersytecie")]


def test_remediation_never_introduces_what_the_learner_has_not_met(db, user):
    """Criterion 15, at the segment that used to have no budget at all.

    `DAILY_NEW_CAP` is named for cards and the budget is read from
    `card.created_at`, but only the introduction segment ever consulted it —
    while *every* segment creates cards, because answering an item creates the
    cards it scores. Remediation drew from the weakest node regardless of what
    the learner had met, so it introduced material through a segment that
    answers to neither the cap nor criterion 9's gate. Measured over ninety
    simulated days: up to **17** cards in a day against a cap of 10, exceeded on
    10 of 540 learner-days.

    The weakest node is made a *grammar* node on purpose. Left to itself the
    weakest node is V01, whose items the learner has met in full — so remediation
    has nothing new to offer and the defect cannot appear, which is why a
    naturalistic run reproduces it only by luck.
    """
    from datetime import datetime as dt

    from pl.models import NodeUnlock

    for node in db.scalars(select(Node)):
        db.add(
            NodeUnlock(
                user_id=user.id,
                node_id=node.id,
                unlocked_at=dt.now(UTC).replace(tzinfo=None),
            )
        )
    db.commit()

    # Meet part of one grammar node, getting most of it wrong so it becomes the
    # weakest by error rate while most of its items remain unmet.
    grammar = db.scalar(select(Node).where(Node.key == "N01"))
    items = list(db.scalars(select(Item).where(Item.node_id == grammar.id).limit(8)))
    assert len(items) == 8, "N01 is too small for this test"
    for index, item in enumerate(items):
        answer(db, user, item, submitted=None if index == 0 else "zzzzzz")
    advance_one_day(db)

    weak = weakest_node(db, user.id)
    assert weak is not None and weak.key == grammar.key, (
        f"expected {grammar.key} to be weakest, got {weak.key if weak else None}"
    )
    unmet = [
        item
        for item in db.scalars(select(Item).where(Item.node_id == weak.id))
        if _item_referents(weak, item, _sense_by_form(db)) - _started_referents(db, user.id)
    ]
    assert unmet, "every item of the weakest node is already met; nothing to prove"

    started = _started_referents(db, user.id)
    sense_by_form = _sense_by_form(db)
    picked, stats = build_session(db, user.id, SETTINGS, limit=20)

    remediated = picked[stats["debt_served"] : len(picked) - stats["introduced_items"]]
    smuggled = [
        item.expected_answer
        for item in remediated
        if _item_referents(db.get(Node, item.node_id), item, sense_by_form) - started
    ]
    assert not smuggled, (
        f"remediation introduced {len(smuggled)} unmet referents past the daily "
        f"cap and criterion 9's gate: {smuggled[:5]}"
    )
    # Remediation contributing *nothing* here is the correct outcome, not a
    # vacuous test: every item of the weakest node the learner has met is already
    # in the debt queue, so there is nothing left to re-drill. The scenario is
    # live because unmet items exist and the session left room for them — the
    # only reason they did not arrive is the restriction under test.
    assert len(picked) < 20, (
        "the session filled up, so remediation was never put to the choice"
    )


def test_the_debt_queue_prefers_an_item_that_introduces_nothing(db, user):
    """A due card must be served; which of its items serves it is free.

    A form card is shared by every exercise built on that form — 93 of 257 forms
    here have items under more than one rule — and the draw is ordered
    least-practised-first, which prefers exactly the items the learner has never
    met. Those are the ones carrying an unmet second referent, so serving one
    creates a card the daily cap never authorised, through the one segment that
    cannot have a budget because debt must be served.

    This was the residue left after remediation stopped introducing: the day's
    count still read 11 or 12 against a limit of 10, all of it arriving through
    debt. Preferring a candidate that introduces nothing took it to exactly 10,
    with zero days over the cap across 540 measured learner-days.
    """
    from collections import defaultdict

    from pl.schedule import MORPH, PATTERN, card_for

    spans = defaultdict(set)
    for item in db.scalars(select(Item)):
        if item.target_form_id and item.pattern_id:
            spans[item.target_form_id].add(item.pattern_id)
    form_id = next(f for f, patterns in spans.items() if len(patterns) > 1)
    candidates = list(
        db.scalars(
            select(Item)
            .where(Item.target_form_id == form_id, Item.pattern_id.isnot(None))
            .order_by(Item.id)
        )
    )
    met, unmet = candidates[0], next(
        i for i in candidates if i.pattern_id != candidates[0].pattern_id
    )

    # The learner has met one of them, and so holds its form card and its rule
    # card. The other's rule is still unmet.
    answer(db, user, met)
    started = _started_referents(db, user.id)
    assert (PATTERN, unmet.pattern_id) not in started, "both rules are already met"

    # Only that form card is due, and its turn today is over, so it is real debt.
    later = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30)
    for card in db.scalars(select(Card).where(Card.user_id == user.id)):
        card.due_at = later
    card_for(db, user.id, MORPH, form_id).due_at = datetime.now(UTC).replace(
        tzinfo=None
    ) - timedelta(hours=1)
    for attempt in db.scalars(select(Attempt).where(Attempt.user_id == user.id)):
        attempt.created_at -= timedelta(days=1)
    db.commit()

    sense_by_form = _sense_by_form(db)
    started = _started_referents(db, user.id)
    picked, stats = build_session(db, user.id, SETTINGS, limit=20)
    assert stats["debt_served"] == 1, f"expected one debt item, got {stats['debt_served']}"

    served = picked[0]
    introduced = _item_referents(db.get(Node, served.node_id), served, sense_by_form) - started
    assert not introduced, (
        f"the debt queue served {served.expected_answer!r} ({served.prompt!r}), "
        f"which drags in {sorted(introduced)} — when an item for the same due "
        f"card introduced nothing at all"
    )
