"""The teaching surface: what the learner reads, and what the build refuses.

The prose is authored and only a reader can judge it. Everything else here is a
fact the build can check — that a table is made of real forms, that a locked
concept carries no prose, and that a learner who was already mid-course is not
made to read their way back to material they have been answering for weeks.
"""

from __future__ import annotations

import warnings
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from pl import concepts, models
from pl.content import frames, ingest
from pl.models import AppUser, ConceptRead, Node, NodeUnlock

warnings.filterwarnings("ignore", category=DeprecationWarning)


@pytest.fixture
def engine():
    """A fresh curriculum per test.

    Function-scoped for the rule `tests/test_journey.py` states: learner state
    must not cross a test boundary. `concepts.reconcile` sweeps every learner in
    the database, so with a shared engine the "reconciled into nothing" tests
    held only because an earlier test had already stamped the learners it left
    behind — run the pair alone and it returned 2 where it asserted 0. These are
    the tests standing in for the defect that would swallow `CASES`, so a green
    that depends on file order is that defect back.
    """
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    ingest.ingest_all(session)
    frames.build(session)
    session.close()
    return engine


@pytest.fixture
def db(engine):
    session = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    yield session
    session.close()


@pytest.fixture
def user(db):
    row = AppUser(
        created_at=datetime.now(UTC).replace(tzinfo=None),
        settings_json={"tz": "UTC", "daily_goal_items": 10},
    )
    db.add(row)
    db.commit()
    return row


def _unlock(db, user, *keys):
    for key in keys:
        node = db.scalar(select(Node).where(Node.key == key))
        db.add(
            NodeUnlock(
                user_id=user.id,
                node_id=node.id,
                unlocked_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
    db.commit()


# ------------------------------------------------------------------ the data


def test_the_build_accepts_the_concepts_as_authored(db):
    """Every node named exists, and every table cell is a real form.

    The assertion criterion 16 makes for items, applied to the pages that explain
    them: a table is *looked up*, never written, so it cannot be wrong unless
    SGJP is.
    """
    concepts.validate(db)


def test_a_table_cell_that_is_not_a_real_form_is_refused(db):
    with pytest.raises(AssertionError, match="not in the set"):
        concepts.table(db, {"lexeme": "niesłowo", "cases": ["nom"]})


def test_a_case_the_lexeme_does_not_have_is_refused(db):
    """A table with a hole in it is a lesson that teaches the hole."""
    with pytest.raises(AssertionError, match="no sg"):
        concepts.table(db, {"lexeme": "czytać", "cases": ["nom"]})


def test_the_animacy_table_shows_one_form_in_two_rows(db):
    """The point of the lesson, and the reason it is generated rather than typed.

    `kota` is one `subst:sg:gen.acc:m2` cell. "The accusative borrows the
    genitive" is not an assertion about Polish here — it is the same surface
    appearing twice, which a reader can see.
    """
    table = concepts.table(db, {"lexeme": "kot:Sm2", "cases": ["nom", "gen", "acc"]})
    by_case = {row["case"]: row["surfaces"] for row in table["rows"]}
    assert by_case["nom"] == ["kot"]
    assert by_case["gen"] == by_case["acc"] == ["kota"]

    # And an inanimate masculine does not do it, or there is nothing to teach.
    sklep = concepts.table(db, {"lexeme": "sklep", "cases": ["nom", "gen", "acc"]})
    rows = {row["case"]: row["surfaces"] for row in sklep["rows"]}
    assert rows["nom"] == rows["acc"] == ["sklep"]
    assert rows["gen"] != rows["acc"]


def test_every_case_row_carries_the_question_that_finds_it(db):
    """`kogo? czego?` is how the case is named in a Polish classroom."""
    table = concepts.table(db, {"lexeme": "kawa", "cases": ["nom", "gen", "acc"]})
    questions = {row["case"]: row["questions"] for row in table["rows"]}
    assert questions == {"nom": "kto? co?", "gen": "kogo? czego?", "acc": "kogo? co?"}


def test_the_question_is_given_in_english_as_well(db):
    """A beginner cannot read `kogo? czego?` yet, and that column is the lesson.

    The owner asked for it at the first table they saw: a column of Polish
    questions teaches nothing to someone still learning what the words mean.
    """
    table = concepts.table(db, {"lexeme": "kawa", "cases": ["nom", "gen", "acc"]})
    glosses = {row["case"]: row["gloss"] for row in table["rows"]}
    assert glosses == {"nom": "who? what?", "gen": "of whom? of what?", "acc": "whom? what?"}


def test_a_case_missing_its_english_question_is_refused_by_name(db, monkeypatch):
    """Named, in the build and on the live path alike.

    `_load` picks up an edit to the file without a restart and without the build
    running, so a dropped `gloss:` would otherwise surface as a bare `KeyError`
    inside `/api/session` — the learner could not study at all, and the log would
    not say which case. And the build must not depend on some table happening to
    list every case: the seven-case table in `CASES` is the only reason it would.
    """
    real = concepts._load()
    gen = {k: v for k, v in real["cases"]["gen"].items() if k != "gloss"}
    monkeypatch.setattr(
        concepts, "_load", lambda: {**real, "cases": {**real["cases"], "gen": gen}}
    )
    with pytest.raises(AssertionError, match=r"'gen'.*'gloss'"):
        concepts.table(db, {"lexeme": "kawa", "cases": ["gen"]})

    # A case no table shows: only a check of the whole map can catch this one.
    unshown = {"polish": "x", "english": "x", "questions": "x?"}
    monkeypatch.setattr(
        concepts, "_load", lambda: {**real, "cases": {**real["cases"], "unshown": unshown}}
    )
    with pytest.raises(AssertionError, match=r"'unshown'.*'gloss'"):
        concepts.validate(db)


def _shown(db, key: str) -> dict[str, dict[str, list[str]]]:
    """What a concept's authored tables put in front of the learner."""
    rendered = concepts.render(db, concepts.by_key(key))
    return {
        t["lexeme"]: {row["case"]: row["surfaces"] for row in t["rows"]}
        for t in rendered["tables"]
    }


def test_each_table_shows_the_ending_its_caption_names(db):
    """The forms under the captions the owner review rewrote, pinned.

    `validate` refuses an empty cell and nothing else, so a table pointed at the
    wrong lexeme — or a paradigm that changed under one — still builds. One of
    these captions shipped disagreeing with its own table with the suite green.
    """
    genitive = _shown(db, "GENITIVE")
    assert genitive["kawa"]["gen"] == ["kawy"], "hard stem: -y"
    assert genitive["kuchnia"]["gen"] == ["kuchni"], "soft stem: -i"
    assert genitive["książka"]["gen"] == ["książki"], "k takes -i by spelling"

    noc = _shown(db, "ACCUSATIVE")["noc"]
    assert noc["nom"] == noc["acc"] == ["noc"], "nothing to swap, so nothing moves"


def test_the_first_lesson_shows_no_case_table(db):
    """Every row of a declension table names a case and asks its question —
    `mianownik`, *kto? co?* — and the first lesson comes before any case is
    taught. The owner found it confused more than it helped, so gender is shown
    with an example table in the prose instead: word, meaning, gender."""
    assert concepts.render(db, concepts.by_key("VOCAB_GENDER"))["tables"] == []


@pytest.mark.parametrize(
    "field",
    ["title", "summary", "heading", "caption"],
)
def test_markdown_in_a_field_shown_as_typed_is_refused(db, monkeypatch, field):
    """Only a section body is Markdown; the other four fields are escaped.

    A backtick in a caption reaches the learner as a backtick — eight captions
    did exactly that until the owner flagged them — and the house style for
    prose, a backtick around every Polish word, makes the next one likely.
    """
    concept = {
        "key": "MARKED",
        "title": "Plain",
        "summary": "Plain",
        "introduced_by": "N11",
        "sections": [{"heading": "Plain", "body": "`kawa` is fine here"}],
        "tables": [{"lexeme": "kawa", "caption": "Plain", "cases": ["nom"]}],
    }
    marked = "the ending is `-a`"
    if field == "heading":
        concept["sections"][0]["heading"] = marked
    elif field == "caption":
        concept["tables"][0]["caption"] = marked
    else:
        concept[field] = marked
    _with_concepts(monkeypatch, [concept])
    with pytest.raises(AssertionError, match=rf"'MARKED'.*{field}"):
        concepts.validate(db)


def test_a_concept_with_markdown_only_in_its_body_is_accepted(db, monkeypatch):
    _with_concepts(
        monkeypatch,
        [
            {
                "key": "PLAIN",
                "title": "Plain",
                "summary": "Plain",
                "introduced_by": "N11",
                "sections": [{"heading": "Plain", "body": "`kawa` and *emphasis*"}],
            }
        ],
    )
    concepts.validate(db)


def _with_concepts(monkeypatch, extra: list[dict]) -> None:
    """Author extra concepts for one test, the way an edit to the file would."""
    real = concepts._load()
    monkeypatch.setattr(
        concepts, "_load", lambda: {**real, "concepts": [*real["concepts"], *extra]}
    )


def test_a_concept_naming_a_node_the_graph_lacks_is_refused(db, monkeypatch):
    _with_concepts(monkeypatch, [{"key": "GHOST", "title": "x", "introduced_by": "N99"}])
    with pytest.raises(AssertionError, match="graph does not contain"):
        concepts.validate(db)


def test_a_concept_declared_twice_is_refused(db, monkeypatch):
    _with_concepts(monkeypatch, [{"key": "CASES", "title": "again", "introduced_by": "N01"}])
    with pytest.raises(AssertionError, match="more than once"):
        concepts.validate(db)


def test_a_reading_whose_concept_is_gone_is_reported_not_deleted(db, user, capsys):
    """The learner's history is not the build's to delete."""
    db.add(
        ConceptRead(
            user_id=user.id,
            concept_key="RETIRED",
            read_at=datetime.now(UTC).replace(tzinfo=None),
        )
    )
    db.commit()
    concepts.validate(db)
    assert "RETIRED" in capsys.readouterr().out
    assert db.get(ConceptRead, (user.id, "RETIRED")) is not None


def test_a_concept_taught_again_before_it_is_introduced_is_refused(db, monkeypatch):
    """A gate with no lesson behind it.

    `also_taught_in` nodes are gated while the concept is unread, but the lesson
    is only offered once `introduced_by` opens. A node that could open first
    would introduce nothing, with no lesson offered and nothing raised — the
    deadlock `frames.assert_every_stratum_is_reachable` refuses for strata.
    """
    _with_concepts(
        monkeypatch,
        [{"key": "BACKWARDS", "title": "x", "introduced_by": "N08", "also_taught_in": ["N03"]}],
    )
    with pytest.raises(AssertionError, match="BACKWARDS"):
        concepts.validate(db)


# ------------------------------------------------------------------ the gate


def test_a_concept_opens_with_the_node_that_introduces_it(db, user):
    assert concepts.is_open(db, user.id, concepts.by_key("VOCAB_GENDER")), (
        "V01 has no prerequisites, so its concept is open from the first session"
    )
    assert not concepts.is_open(db, user.id, concepts.by_key("GENITIVE"))
    _unlock(db, user, "N08")
    assert concepts.is_open(db, user.id, concepts.by_key("GENITIVE"))


def test_an_unread_concept_gates_every_node_that_teaches_it(db, user):
    """One concept, three nodes: the genitive is taught at N08, N09 and N10."""
    gated = concepts.gated_node_ids(db, user.id)
    keys = {
        key
        for (key,) in db.execute(select(Node.key).where(Node.id.in_(gated)))
    }
    assert {"N08", "N09", "N10"} <= keys
    assert "N06" not in keys, "a function node introduces no concept of its own"

    concepts.mark_read(db, user.id, "GENITIVE")
    after = {
        key
        for (key,) in db.execute(
            select(Node.key).where(Node.id.in_(concepts.gated_node_ids(db, user.id)))
        )
    }
    assert not {"N08", "N09", "N10"} & after


def test_one_lesson_is_offered_at_a_time_in_node_order(db, user):
    _unlock(db, user, "N01", "N03")
    first = concepts.lesson_for(db, user.id)
    assert first["key"] == "VOCAB_GENDER", "V01 comes before N01 in the graph"
    concepts.mark_read(db, user.id, first["key"])
    assert concepts.lesson_for(db, user.id)["key"] == "CASES"


def test_a_lesson_carries_its_tables_resolved(db, user):
    lesson = concepts.render(db, concepts.by_key("CASES"))
    assert lesson["sections"], "a lesson with no prose teaches nothing"
    assert lesson["tables"] and lesson["tables"][0]["rows"]


def test_reading_is_recorded_once(db, user):
    concepts.mark_read(db, user.id, "CASES")
    concepts.mark_read(db, user.id, "CASES")
    rows = db.scalars(
        select(ConceptRead).where(ConceptRead.user_id == user.id)
    ).all()
    assert len(rows) == 1


# --------------------------------------------------------------- reconciling


def test_a_learner_mid_course_is_recorded_as_already_taught(db, user):
    """They have been answering N08 for weeks; the gate must not take it away."""
    _unlock(db, user, "N01", "N02", "N03", "N08")
    assert concepts.reconcile(db) > 0

    read = concepts.read_keys(db, user.id)
    assert {"VOCAB_GENDER", "CASES", "ACCUSATIVE", "GENITIVE"} <= read, (
        "V01 has no unlock row and would be missed by a rule reading the latch "
        "table alone — a learner who knows every word in it would be handed its "
        "lesson"
    )
    assert "INSTRUMENTAL" not in read, "N07 is not unlocked; that lesson is still owed"
    assert not concepts.gated_node_ids(db, user.id) & {
        node_id
        for (node_id,) in db.execute(select(Node.id).where(Node.key == "N08"))
    }


def test_a_new_learner_is_reconciled_into_nothing(db, user):
    """The defect the first draft of the design would have shipped.

    `evaluate_unlocks` never latches a node without prerequisites, so a learner
    who has just unlocked N01 holds exactly one latch row and no readings — and a
    rule that fired there would mark `CASES` read at the moment it was written
    for. Reconciliation runs in the build, once, and only for a learner who was
    already mid-course.
    """
    assert concepts.reconcile(db) == 0
    assert concepts.read_keys(db, user.id) == set()
    assert concepts.lesson_for(db, user.id)["key"] == "VOCAB_GENDER"


def test_a_later_build_does_not_reconcile_a_learner_who_has_simply_not_read_yet(
    db, user
):
    """The same defect, one build later, which the readings alone cannot prevent.

    A learner created after this shipped is stamped on their first build with
    nothing recorded. They then study for three weeks and unlock N01 — holding a
    latch row and no readings, which is indistinguishable from a learner who was
    mid-course when the concepts arrived. Guarding on the readings would hand
    them `CASES` as already read on the next rebuild; the stamp is what does not.
    """
    concepts.reconcile(db)
    _unlock(db, user, "N01")

    assert concepts.reconcile(db) == 0
    assert "CASES" not in concepts.read_keys(db, user.id)
    assert concepts.lesson_for(db, user.id) is not None


def test_reconciling_twice_records_nothing_further(db, user):
    _unlock(db, user, "N01")
    assert concepts.reconcile(db) > 0
    before = concepts.read_keys(db, user.id)
    assert concepts.reconcile(db) == 0
    assert concepts.read_keys(db, user.id) == before
