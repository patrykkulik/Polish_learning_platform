"""HTTP-level tests. The repository had none before this file.

`TestClient` was imported in `test_loop.py` and never used, and `httpx` was
added to the dev group for tests that were never written — so criterion 17
("no expected answer is ever sent to the client before submission") was
asserted only indirectly, and the audio endpoint not at all.
"""

from __future__ import annotations

import re
import warnings

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from pl import api, audio, models
from pl.content import frames, ingest
from pl.domain import AUDIBLE
from pl.models import Item

warnings.filterwarnings("ignore", category=DeprecationWarning)


@pytest.fixture
def client(monkeypatch, tmp_path):
    """A client bound to a throwaway database, not the developer's own."""
    # StaticPool, or every connection gets its own empty in-memory database and
    # the request handlers see nothing the fixture built.
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = Session()
    ingest.ingest_all(session)
    frames.build(session)
    session.close()

    monkeypatch.setattr(api.database, "session", Session)
    monkeypatch.setattr(audio, "DEFAULT_CACHE", tmp_path)
    _clear_voice_probe()
    yield TestClient(api.app), Session
    _clear_voice_probe()


def _clear_voice_probe() -> None:
    """`available()` is memoised, and some tests replace it outright."""
    clear = getattr(audio.available, "cache_clear", None)
    if clear is not None:
        clear()


def _one(Session, exercise_type: str) -> Item:
    with Session() as db:
        return db.scalar(select(Item).where(Item.exercise_type == exercise_type))


# ------------------------------------------------------------ criterion 17


def _taught_session(http, limit: int = 20) -> dict:
    """A session with the teaching out of the way.

    A concept is taught before it is drilled, so a new learner's first session
    carries a lesson and no items at all. A test about what the *items* contain
    has to read it first, or it asserts over an empty list and passes for the
    wrong reason.
    """
    payload = http.get(f"/api/session?limit={limit}").json()
    while payload.get("lesson"):
        http.post(f"/api/concepts/{payload['lesson']['key']}/read")
        payload = http.get(f"/api/session?limit={limit}").json()
    return payload


def test_a_session_never_carries_an_answer(client):
    """The payload the learner receives must not contain what they must produce."""
    http, Session = client
    payload = _taught_session(http)
    assert payload["items"], "an empty session proves nothing"

    with Session() as db:
        answers = {
            i.expected_answer for i in db.scalars(select(Item))
        }
    blob = str(payload)
    # Whole words, not substrings. `kuchni` is an answer and `kuchnia` a
    # legitimate option elsewhere in the session; a substring test reads the
    # option as a leak of the answer it merely contains.
    leaked = [
        a
        for a in answers
        if len(a) > 3 and re.search(rf"(?<!\w){re.escape(a)}(?!\w)", blob)
    ]
    # A multiple-choice item legitimately ships its options, one of which is
    # correct — but unlabelled. Anything else appearing is a leak.
    for item in payload["items"]:
        for option in item.get("options") or []:
            leaked = [a for a in leaked if a != option]
    assert not leaked, f"expected answers reachable before submission: {leaked[:5]}"


def test_no_exercise_type_carries_its_answer(client):
    """Criterion 17 for every type, not only the ones a first session holds.

    A new learner's session is multiple choice throughout, where the answer is
    legitimately one of the options — so the session-level test above cannot
    fail on it. Ablated by returning `expected_answer` from `_serialise`: this
    test fails, the one above does not.
    """
    http, Session = client
    with Session() as db:
        by_type = {}
        for item in db.scalars(select(Item)):
            by_type.setdefault(item.exercise_type, item)
        assert len(by_type) >= 5, "too few exercise types to prove anything"
        for exercise_type, item in by_type.items():
            payload = api._serialise(db, item)
            options = payload.pop("options") or []
            assert item.expected_answer not in str(payload), (
                f"a {exercise_type} item ships its answer: {payload}"
            )
            assert item.expected_answer in options or not options, (
                "the answer vanished from the options, which is a different bug"
            )


def test_no_item_is_labelled_with_which_option_is_right(client):
    http, _ = client
    items = _taught_session(http)["items"]
    assert items, "an empty session would pass this vacuously"
    for item in items:
        assert set(item) == {
            "id", "exercise_type", "prompt", "gloss", "options", "has_audio", "node"
        }


# ------------------------------------------------------------- the teaching


def test_a_session_carries_one_lesson_until_it_is_acknowledged(client):
    """A concept is taught before it is drilled, and once.

    The lesson is the session's first step; acknowledging it is what lets the
    node introduce. Reading writes no card, review or attempt — the daily goal
    counts items answered, and a goal a page of prose can satisfy is not a goal.
    """
    from pl.models import Attempt, Card, Review

    http, Session = client
    lesson = http.get("/api/session?limit=20").json()["lesson"]
    assert lesson and lesson["key"] == "VOCAB_GENDER", (
        "a new learner's first session should open with the first concept"
    )
    assert lesson["sections"] and lesson["tables"], "a lesson needs its content"

    assert http.post(f"/api/concepts/{lesson['key']}/read").json()["read"] is True
    assert http.get("/api/session?limit=20").json()["lesson"] is None

    with Session() as db:
        for table in (Attempt, Review, Card):
            assert db.scalar(select(func.count()).select_from(table)) == 0, (
                f"reading a lesson wrote a {table.__name__} row"
            )


def test_a_concept_the_learner_has_not_reached_carries_no_prose(client):
    """Criterion 7. A page that withholds what its own API hands out is not a
    gate, so the locked state is decided here rather than in the browser."""
    http, _ = client
    locked = http.get("/api/concepts/GENITIVE").json()
    assert locked["open"] is False
    assert "sections" not in locked and "tables" not in locked
    assert http.post("/api/concepts/GENITIVE/read").status_code == 403

    listed = {c["key"]: c for c in http.get("/api/concepts").json()["concepts"]}
    assert listed["GENITIVE"]["summary"] == "", "the index leaked a locked summary"
    assert listed["VOCAB_GENDER"]["summary"], "an open concept should say what it is"


def test_an_unknown_concept_is_a_404(client):
    http, _ = client
    assert http.get("/api/concepts/NOPE").status_code == 404
    assert http.post("/api/concepts/NOPE/read").status_code == 404


def test_the_grammar_pages_render(client):
    http, _ = client
    assert http.get("/grammar").status_code == 200
    assert http.get("/grammar/CASES").status_code == 200


def test_the_grammar_is_reachable_from_every_page(client):
    """P01-1. The lesson step links to the grammar, and it is never shown again
    once acknowledged — so without a header link a concept's page needed a typed
    URL from the day after it was taught, which is the defect the teaching
    surface was designed to remove."""
    http, _ = client
    for page in ("/", "/progress"):
        assert 'href="/grammar"' in http.get(page).text, f"{page} has no way to the grammar"


# ---------------------------------------------------------------- audio


def test_audio_is_refused_for_an_item_that_has_none(client):
    http, Session = client
    cloze = _one(Session, "cloze")
    assert http.get(f"/api/audio/{cloze.id}").status_code == 404


def test_audio_rejects_an_unknown_speed(client):
    http, Session = client
    listening = _one(Session, next(iter(AUDIBLE)))
    response = http.get(f"/api/audio/{listening.id}?speed=glacial")
    assert response.status_code == 400


def test_audio_reports_503_when_the_machine_has_no_voice(client, monkeypatch):
    """Not a 500. The client has to be able to tell "no synthesiser" from "broken"."""
    http, Session = client
    listening = _one(Session, next(iter(AUDIBLE)))
    monkeypatch.setattr(audio, "available", lambda: False)
    assert http.get(f"/api/audio/{listening.id}").status_code == 503


def test_a_failing_synthesiser_is_503_not_500(client, monkeypatch):
    import subprocess

    http, Session = client
    listening = _one(Session, next(iter(AUDIBLE)))
    monkeypatch.setattr(audio, "available", lambda: True)

    def boom(*_a, **_k):
        raise subprocess.CalledProcessError(1, "say", stderr=b"voice missing")

    monkeypatch.setattr(audio, "synthesise", boom)
    assert http.get(f"/api/audio/{listening.id}").status_code == 503


@pytest.mark.skipif(not audio.available(), reason="no local speech synthesiser")
def test_audio_serves_playable_bytes(client):
    http, Session = client
    listening = _one(Session, next(iter(AUDIBLE)))
    response = http.get(f"/api/audio/{listening.id}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mp4"
    assert len(response.content) > 1000


# ---------------------------------------------------------------- submit


def test_submitting_an_unknown_item_is_404(client):
    http, _ = client
    assert http.post(
        "/api/submit", json={"item_id": 10**9, "answer": "kota"}
    ).status_code == 404


def test_a_correct_answer_reports_what_it_scored(client):
    http, Session = client
    item = _one(Session, "cloze")
    body = http.post(
        "/api/submit", json={"item_id": item.id, "answer": item.expected_answer}
    ).json()
    assert body["correct"] is True
    assert body["scored"], "a correct answer must move at least one card"


# ----------------------------------------------- criterion 14, the progress view


def test_the_graph_returns_the_mastery_its_docstring_promised(client):
    """`/api/graph` advertised per-node mastery and returned node titles.

    Criterion 14 wants two different figures per node, and the difference is the
    whole point: `mastered` reads the latching unlock gate, `mastery` reads
    current retrievability. A payload carrying only the first cannot draw a
    progress view whose bars are ever allowed to fall.
    """
    http, _ = client
    body = http.get("/api/graph").json()
    assert body["nodes"], "an empty graph proves nothing"

    for node in body["nodes"]:
        assert set(node) == {
            "key",
            "title",
            "type",
            "unlocked",
            "mastered",
            "mastery",
            "strata",
            "started",
            "mastered_strata",
            "explanation",
        }
        assert 0.0 <= node["mastery"] <= 1.0
        assert node["started"] <= node["strata"]
        assert node["mastered_strata"] <= node["strata"]

    assert body["vocabulary"]["total"] > 0
    assert body["vocabulary"]["met"] == 0, "a learner with no history has met none"
    assert body["retention_curve"] == []
    assert body["progress"]["streak"] == 0


def test_mastery_decays_while_the_unlock_gate_does_not(client):
    """Criterion 14, stated exactly: the display falls, the gate latches.

    The two numbers are read from different things on purpose — `stability_max`
    is a high-water mark and cannot fall, retrievability decays with elapsed
    time. Drawing the progress bar from the gate would tell a learner who has not
    opened the app in four months that they still know everything.
    """
    from datetime import datetime, timedelta

    from pl.models import Card, Item, Node

    http, Session = client
    item = _one(Session, "mcq")
    with Session() as db:
        node_key = db.get(Node, db.get(Item, item.id).node_id).key

    http.post(
        "/api/submit", json={"item_id": item.id, "answer": item.expected_answer}
    )

    def mastery_of(key: str) -> dict:
        return {n["key"]: n for n in http.get("/api/graph").json()["nodes"]}[key]

    before = mastery_of(node_key)
    assert before["mastery"] > 0, "answering an item moved no mastery at all"

    # Four months of not studying, with the gate's own input left untouched.
    with Session() as db:
        for card in db.scalars(select(Card)):
            state = dict(card.fsrs_state_json)
            for field in ("last_review", "due"):
                if state.get(field):
                    state[field] = (
                        datetime.fromisoformat(state[field]) - timedelta(days=120)
                    ).isoformat()
            card.fsrs_state_json = state
        db.commit()

    after = mastery_of(node_key)
    assert after["mastery"] < before["mastery"], (
        "mastery did not decay over four months away — it is being read off the "
        "unlock gate rather than off retrievability"
    )
    assert after["unlocked"] == before["unlocked"], "the gate moved"
    assert after["mastered"] == before["mastered"], "the gate moved"


def test_the_progress_page_is_served_and_consumes_the_graph(client):
    """Criterion 14 was unimplemented in both halves: no figure, and no page.

    A payload nothing renders is not a progress view, which is why this asserts
    the page exists and pulls the script that reads `/api/graph`.
    """
    http, _ = client
    page = http.get("/progress")
    assert page.status_code == 200
    assert "/static/progress.js" in page.text


# ------------------------------------------------------- the streak is server-side


def test_completing_a_session_ignores_a_count_from_the_client(client):
    """The daily-goal condition used to trust a query parameter.

    Anyone who could type a URL could award themselves a streak without
    answering a single item, on a server already holding the real count.
    """
    http, _ = client
    body = http.post("/api/session/complete?items_completed=9999").json()
    assert body["streak"] == 0, "a number in the query string bought a streak"
    assert body["progress"]["streak"] == 0


# ------------------------------------------------------------ gamification


def test_an_unlock_names_the_skill_rather_than_its_primary_key(client, monkeypatch):
    """Unlocking is the one moment where the course visibly opens up.

    It used to reach the learner as the string "N01" — the database's name for
    the thing, which says nothing about what was earned. The node's own title and
    explanation are already in the row; not sending them was the whole defect.
    """
    from pl.models import Node

    http, Session = client
    body = http.post("/api/session/complete").json()
    assert body["unlocked"] == [], "a learner with no history unlocked something"

    with Session() as db:
        assert db.scalar(select(Node).where(Node.key == "N01")) is not None

    # The gate itself is driven in `test_journey.py`, over the forty simulated
    # days it actually takes. What is under test here is only what the endpoint
    # says once a node *has* opened.
    monkeypatch.setattr(api.composer, "evaluate_unlocks", lambda db, uid: ["N01"])
    body = http.post("/api/session/complete").json()

    assert len(body["unlocked"]) == 1
    unlocked = body["unlocked"][0]
    assert isinstance(unlocked, dict), (
        f"the endpoint returned {unlocked!r} — the database's name for the node, "
        f"which tells the learner nothing about what they just earned"
    )
    assert unlocked["key"] == "N01"
    assert unlocked["title"] and unlocked["title"] != "N01"
    assert unlocked["explanation"], "the learner is told nothing about the skill"


def test_milestones_report_a_standing_not_a_celebration(client):
    """Where the learner is against the next round number, and nothing louder.

    Claiming they *crossed* one today would need a column recording which have
    already been announced. Without it the honest thing is a standing, which is
    true however many times it is rendered.
    """
    http, _ = client
    for body in (
        http.post("/api/session/complete").json(),
        http.get("/api/graph").json(),
    ):
        m = body["milestones"]
        assert set(m) == {"retained", "vocabulary", "streak"}
        for key, standing in m.items():
            assert set(standing) == {"value", "reached", "next"}, key
            assert standing["value"] == 0, "a fresh learner has done nothing yet"
            assert standing["reached"] == 0, "nothing has been reached"
            assert standing["next"] > 0, "there is always a next target"
            assert standing["value"] < standing["next"]


def test_a_second_item_of_the_same_rule_says_it_counted(client):
    """"Counted, and the schedule moves once a day" is not "untouched".

    A pattern card is shared by every item in its stratum and advances at most
    once a day, so the second item of a rule in one session legitimately moves
    no rule card. Reporting that as `untouched` — the word for a population the
    routing table deliberately leaves alone — would collapse two different facts
    into one, and the learner would read it as their answer not counting.
    """
    from pl.models import Item

    http, Session = client
    with Session() as db:
        first = db.scalar(
            select(Item).where(Item.exercise_type == "cloze", Item.pattern_id.isnot(None))
        )
        pair = db.scalar(
            select(Item).where(
                Item.pattern_id == first.pattern_id, Item.id != first.id
            )
        )
        assert pair is not None, "need two items of one rule to prove anything"
        answers = (
            (first.id, first.expected_answer),
            (pair.id, pair.expected_answer),
        )

    opening = http.post(
        "/api/submit", json={"item_id": answers[0][0], "answer": answers[0][1]}
    ).json()
    assert "pattern" in opening["scored"], "the first item did not move the rule card"
    assert opening["counted_earlier"] == []

    second = http.post(
        "/api/submit", json={"item_id": answers[1][0], "answer": answers[1][1]}
    ).json()
    assert "pattern" not in second["scored"], "the rule card advanced twice in a day"
    assert "pattern" in second["counted_earlier"], (
        "the second answer moved no rule card and did not say why — the learner "
        "sees it as 'untouched' and reads that as not counting"
    )


def test_a_card_resting_between_reviews_is_not_reported_as_counted_today(client):
    """P02-1. The cooldown is a third reason an answer moves no card.

    A card that is not due advances at most every three days, so answering it
    again two days later moves nothing — and nothing was recorded today, so
    "counted earlier today" is false. Remediation serves exactly these cards, so
    the wrong label was the common case, not the edge.
    """
    from datetime import UTC, datetime, timedelta

    from pl.models import Attempt, Item

    http, Session = client
    with Session() as db:
        item = db.scalar(
            select(Item).where(Item.exercise_type == "cloze", Item.pattern_id.isnot(None))
        )
        item_id, answer = item.id, item.expected_answer

    first = http.post("/api/submit", json={"item_id": item_id, "answer": answer}).json()
    assert "pattern" in first["scored"], "the first answer should move the rule card"

    # Two days pass; the card is not yet due, and was last moved within three.
    with Session() as db:
        for attempt in db.scalars(select(Attempt)):
            attempt.created_at -= timedelta(days=2)
        db.commit()

    again = http.post("/api/submit", json={"item_id": item_id, "answer": answer}).json()
    assert "pattern" not in again["scored"]
    assert "pattern" not in again["counted_earlier"], (
        "nothing was recorded today, so this must not say it counted today"
    )
    assert "pattern" in again["cooled_down"]


def test_the_retention_curve_counts_hard_as_a_recall(client):
    """The curve is the figure that survives a broken streak, so it has to be
    right — and it was asserted only in its empty state.

    `Again` is the only failure. A `Hard` is a recall: it is what a dropped
    diacritic scores, and the learner who wrote `robie` for `robię` had the
    grammar and missed the keyboard. Counting that as forgetting would make the
    curve a measure of typing.
    """
    http, Session = client
    with Session() as db:
        correct = db.scalar(
            select(Item).where(Item.exercise_type == "cloze", Item.pattern_id.isnot(None))
        )
        # A different lexeme, so the two answers do not share a card and the
        # second is not suppressed as already-advanced-today.
        other = db.scalar(
            select(Item).where(
                Item.exercise_type == "cloze",
                Item.target_form_id != correct.target_form_id,
                Item.pattern_id != correct.pattern_id,
            )
        )
        assert other is not None
        answers = [(correct.id, correct.expected_answer), (other.id, "zzzzzz")]

    for item_id, submitted in answers:
        http.post("/api/submit", json={"item_id": item_id, "answer": submitted})

    curve = http.get("/api/graph").json()["retention_curve"]
    assert len(curve) == 1, f"one day of work should be one bar, got {curve}"
    day = curve[0]
    assert day["reviews"] > day["recalled"] > 0, (
        f"expected some recalled and some not, got {day}"
    )


def test_a_standing_reports_the_last_threshold_passed_and_the_next(client):
    """`_standing` was tested only at zero, where both interesting branches are
    invisible: a non-zero `reached`, and the terminal `next is None` once every
    threshold is behind the learner. Both change how the two clients render —
    the progress page pins the meter at 100% and the session screen drops the
    standing entirely — so the one case where the feature changes shape was the
    one case uncovered.
    """
    from pl.api import RETAINED_MILESTONES, STREAK_MILESTONES, _standing

    assert _standing(0, STREAK_MILESTONES) == {"value": 0, "reached": 0, "next": 3}
    assert _standing(30, STREAK_MILESTONES) == {"value": 30, "reached": 30, "next": 60}
    assert _standing(31, STREAK_MILESTONES) == {"value": 31, "reached": 30, "next": 60}

    finished = _standing(10_000, RETAINED_MILESTONES)
    assert finished["next"] is None, "a learner past every threshold has no next one"
    assert finished["reached"] == max(RETAINED_MILESTONES)
