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
from pl.session import build_session, evaluate_unlocks, start_of_user_day

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
    first, stats = build_session(db, user.id, SETTINGS, limit=10)
    assert stats["introduced"] > 0, "no new material was introduced at all"

    for item in first:
        answer(db, user, item)
    _let_time_pass(db)

    second, _ = build_session(db, user.id, SETTINGS, limit=10)
    repeated = {i.id for i in first} & {i.id for i in second}
    assert not repeated, f"{len(repeated)} answered items were re-offered as new"


def test_remediation_fills_a_session_that_debt_leaves_short(db, user):
    """The weakest node should top up a session that debt alone does not fill.

    Debt claims one item per due card, so the items it picks and the items
    remediation wants overlap heavily. If the remediation loop cannot tell "this
    session is full" from "I have already seen this one", it stops on the first
    overlap and the middle segment of the composition contributes nothing at all.
    """
    first, _ = build_session(db, user.id, SETTINGS, limit=6)
    for item in first:
        answer(db, user, item, submitted="zzzzzz")  # wrong, so a node gets weak

    picked, stats = build_session(db, user.id, SETTINGS, limit=20)
    assert stats["debt_served"] > 0, "expected the wrong answers to create debt"
    assert len(picked) > stats["debt_served"], (
        "remediation added nothing beyond the debt queue"
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
    for _ in range(40):
        picked, _ = build_session(db, user.id, SETTINGS, limit=20)
        for item in picked:
            met.add(item.exercise_type)
            correct = rng.random() < 0.85
            answer(db, user, item, None if correct else _wrong_form(db, item, rng))
        advance_one_day(db)

    built = set(db.scalars(select(Item.exercise_type).distinct()))
    assert built - met == set(), (
        f"built but never offered in forty days: {sorted(built - met)}"
    )


# ------------------------------------------------- pacing, measured not assumed
# `scripts/journey_sim.py` is the instrument; these are the floors it establishes.
# The bounds are deliberately far below what the composer reaches today. They
# exist to catch the class of regression, not to pin a number that will move.


def _wrong_form(db, item, rng):
    """Another cell of the same paradigm — a mistake, not gibberish.

    Nonsense classifies as `UNANALYSABLE` and routes to different cards than the
    errors learners actually make, so the debt it produces would not resemble a
    learner's debt. Debt is what criterion 9 blocks new material on, which makes
    this the difference between simulating a learner and simulating a cat.
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
