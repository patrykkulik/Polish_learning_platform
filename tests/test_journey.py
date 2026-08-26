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
from pl.models import Attempt, Card, Item, Node, Pattern
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
    day = 0
    for day in range(1, horizon + 1):
        picked, _ = build_session(db, user.id, SETTINGS, limit=20)
        for item in picked:
            answer(db, user, item)
        unlocked += evaluate_unlocks(db, user.id)
        if "N01" in unlocked:
            break
        advance_one_day(db)

    assert "N01" in unlocked, (
        f"a learner answering everything correctly every day for {horizon} days "
        f"never unlocked the first grammar node; unlocked={unlocked}"
    )
    assert day < horizon, f"unlocked only on the final day ({day}); bound is too tight"


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
