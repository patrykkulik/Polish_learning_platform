"""The learner's journey, end to end, on a database with no history.

Every test in `test_loop.py` pokes one function with hand-built state. That is
why three defects that make the product unusable shipped with the suite green:
the code paths they live in are never executed, or are executed against a
fixture that has already drifted into a state where they cannot fail.

These tests use a clean database per test and drive the loop through the same
calls the API makes, so a learner who cannot progress produces a red test.
"""

from __future__ import annotations

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
from pl.session import build_session, evaluate_unlocks

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
    apply_diagnosis(db, user.id, item, diagnosis, attempt.id)
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
    applied = apply_diagnosis(db, user.id, item, diagnosis, attempt.id)
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
    for _ in range(40):
        picked, stats = build_session(db, user.id, SETTINGS, limit=20)
        for item in picked:
            schedule_cards(db, user, item)
            seen.add(item.id)
        db.commit()
        _defer_everything(db)
        if stats["introduced"] == 0:
            break

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
    later = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=90)
    for card in db.scalars(select(Card)):
        card.due_at = later
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


def test_listening_items_are_currently_unreachable(db, user, monkeypatch):
    """Records a live defect so it is not rediscovered as a surprise.

    52 dictation items are built and none can ever be served. Introduction skips
    them because they share both referents with the cloze from the same
    sentence; debt serves the lowest-id item for a due form, which is that
    cloze. This test asserts the *current* behaviour — when dictation is made
    reachable it will fail, and that failure is the signal to delete it.
    """
    from datetime import datetime as dt

    from pl import audio
    from pl.domain import AUDIBLE
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

    offered = 0
    for _ in range(12):
        picked, _ = build_session(db, user.id, SETTINGS, limit=20)
        offered += sum(1 for i in picked if i.exercise_type in AUDIBLE)
        for item in picked:
            answer(db, user, item)
        _defer_everything(db)

    assert offered == 0, (
        "listening items are now reachable — delete this test and assert the "
        "behaviour you want instead"
    )
