"""The course on a phone: `pl.device` answers as the local server does, and a
content update keeps the learner's progress.

Everything here runs in CPython against SQLite files. `pl.device` is the
reference the phone's JavaScript port is held to, in `test_parity.py`. What only
a phone can show — IndexedDB, the Home Screen, start-up time — is checked by
hand; see the static-site design's Validation Required.
"""

from __future__ import annotations

import json
import random
import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from pl import api, audio, concepts, device, models, morph
from pl import db as database
from pl.content import frames, ingest
from pl.models import AppUser, Item

#: The committed word list: what a phone grades with. It covers every token of
#: the built content, which queueing a reordered answer depends on.
WORD_LIST = Path(__file__).resolve().parent.parent / "publish" / "lexicon.json"


def _build(path: Path) -> None:
    """Upsert the curriculum into the database at `path`, as the content build does."""
    engine = create_engine(f"sqlite:///{path}", future=True)
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True, expire_on_commit=False)()
    try:
        ingest.ingest_all(session)
        frames.build(session)
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(scope="module")
def ledgers(tmp_path_factory):
    """An older ledger missing one noun, and the next one: the whole course
    upserted onto a copy of it — how the content build grows the published
    ledger. The newer one also carries a nullable content column the code does
    not know, as a ledger built by newer code would.

    The noun is one no lesson's table names, as the content build requires of
    any ledger it writes; without it the lesson could not be rendered."""
    root = tmp_path_factory.mktemp("ledgers")
    older, newer = root / "older.db", root / "newer.db"

    real = ingest._load

    def missing_one(name):
        rows = real(name)
        if name != "lexemes.yaml":
            return rows
        partners = {r["aspect_partner"] for r in rows if r.get("aspect_partner")}
        tabled = {
            spec["lexeme"]
            for concept in concepts.all_concepts()
            for spec in concept.get("tables", [])
        }
        victim = next(
            r for r in rows
            if r.get("pos", "subst") == "subst" and r["lemma"] not in partners | tabled
        )
        return [r for r in rows if r is not victim]

    ingest._load = missing_one
    try:
        _build(older)
    finally:
        ingest._load = real
    shutil.copy2(older, newer)
    _build(newer)
    with sqlite3.connect(newer) as connection:
        connection.execute("ALTER TABLE lexeme ADD COLUMN note VARCHAR(64)")
    return older, newer


@pytest.fixture
def phone(tmp_path, monkeypatch):
    """A phone's database file, with `pl.db` pointed at it as `device.js` does."""
    _point(monkeypatch, tmp_path / "polish.db")
    shutil.copy2(WORD_LIST, tmp_path / "lexicon.json")
    yield tmp_path
    morph.use_lexicon(None)
    database.engine.dispose()


def _point(monkeypatch, path: Path) -> None:
    engine = create_engine(f"sqlite:///{path}", future=True)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(
        database,
        "SessionLocal",
        sessionmaker(bind=engine, expire_on_commit=False, future=True),
    )


def _start(phone: Path, ledger: Path | None, content_hash: str, tz: str = "Europe/Warsaw"):
    device.start(
        content=str(ledger) if ledger else None,
        content_hash=content_hash,
        lexicon=str(phone / "lexicon.json"),
        tz=tz,
    )


def _call(method: str, url: str, body: dict | str | None = None):
    text = body if isinstance(body, str) or body is None else json.dumps(body)
    status, answer = device.handle(method, url, text)
    return status, json.loads(answer)


def _rows(path: Path, table: str) -> list[tuple]:
    with sqlite3.connect(path) as connection:
        return connection.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall()


def _session_items() -> list[dict]:
    """The day's items, reading the lesson first as the page does: a new learner's
    first session is a lesson, because a concept is taught before it is drilled."""
    for _ in range(3):
        status, session = _call("GET", "/api/session?limit=20")
        assert status == 200
        if session["items"]:
            return session["items"]
        assert session["lesson"], "no items and no lesson to read"
        assert _call("POST", f"/api/concepts/{session['lesson']['key']}/read")[0] == 200
    raise AssertionError("still no items after reading the lessons")


def _expected(item_id: int) -> str:
    db = database.session()
    try:
        return db.get(Item, item_id).expected_answer
    finally:
        db.close()


# ------------------------------------------------------------ the tables


def test_every_table_is_either_content_or_the_learners():
    """An unclassified table would be silently dropped by an update or silently
    kept stale by it — so a new table has to be put in one list or the other."""
    content, learner = set(device.CONTENT_TABLES), set(device.LEARNER_TABLES)
    assert not content & learner
    assert set(models.Base.metadata.tables) == content | learner


# ------------------------------------------------------------ install and update


def test_a_new_phone_installs_content_and_creates_its_own_learner(phone, ledgers):
    older, _ = ledgers
    _start(phone, older, "older")

    db_file = phone / "polish.db"
    assert _rows(db_file, "item") == _rows(older, "item")
    # The ledger holds the build's learner (`ingest_all` calls `ensure_user`); a
    # phone never reads learner tables from it, and makes its own.
    db = database.session()
    try:
        users = db.scalars(select(AppUser)).all()
        assert len(users) == 1
        assert users[0].settings_json["tz"] == "Europe/Warsaw"
    finally:
        db.close()
    assert device.installed_hash() == "older"


def test_a_timezone_the_phone_cannot_name_falls_back_to_utc(phone, ledgers):
    older, _ = ledgers
    _start(phone, older, "older", tz="Mars/Olympus_Mons")
    db = database.session()
    try:
        assert db.scalar(select(AppUser)).settings_json["tz"] == "UTC"
    finally:
        db.close()


def test_an_update_keeps_the_learners_progress_and_brings_the_new_items(phone, ledgers):
    """Criterion 3. The learner answers under the older ledger; the newer one is
    installed; every learner row survives and still points at the same content,
    and the new items are there to answer."""
    older, newer = ledgers
    _start(phone, older, "older")
    db_file = phone / "polish.db"

    for served in _session_items()[:6]:
        status, _ = _call(
            "POST", "/api/submit",
            {"item_id": served["id"], "answer": _expected(served["id"]), "latency_ms": 900},
        )
        assert status == 200

    # A reordered sentence is queued for the owner, as a learner-made row inside
    # a content table.
    with sqlite3.connect(db_file) as connection:
        item_id, answer = connection.execute(
            "SELECT id, expected_answer FROM item"
            " WHERE exercise_type = 'free_translation' LIMIT 1"
        ).fetchone()
    reordered = " ".join(reversed(answer.rstrip(".").split()))
    assert _call("POST", "/api/submit", {"item_id": item_id, "answer": reordered})[0] == 200
    assert _call("POST", "/api/session/complete")[0] == 200
    with sqlite3.connect(db_file) as connection:
        queued = connection.execute(
            "SELECT count(*) FROM item_variant WHERE source = 'queued'"
        ).fetchone()[0]
    assert queued == 1, "the reordered answer was not queued, so the test proves nothing"

    before = {table: _rows(db_file, table) for table in device.LEARNER_TABLES}
    assert before["attempt"] and before["card"] and before["review"]
    pointers = _pointers(db_file)

    _start(phone, newer, "newer")

    assert {table: _rows(db_file, table) for table in device.LEARNER_TABLES} == before
    assert _pointers(db_file) == pointers, "a learner row now points at other content"
    with sqlite3.connect(db_file) as connection:
        assert connection.execute(
            "SELECT count(*) FROM item_variant WHERE source = 'queued'"
        ).fetchone()[0] == 0, "queued answers go with the content table they sit in"
        columns = {row[1] for row in connection.execute("PRAGMA table_info(lexeme)")}
    assert "note" in columns, "content tables take the ledger's own definitions"

    new_items = {row[0] for row in _rows(newer, "item")} - {row[0] for row in _rows(older, "item")}
    assert new_items, "the newer ledger adds nothing, so the test proves nothing"
    fresh = min(new_items)
    status, graded = _call("POST", "/api/submit", {"item_id": fresh, "answer": _expected(fresh)})
    assert status == 200 and graded["correct"]
    assert device.installed_hash() == "newer"


def _pointers(db_file: Path) -> dict:
    """What each learner row points at, by the content's own key rather than id."""
    with sqlite3.connect(db_file) as connection:
        return {
            "attempt": connection.execute(
                "SELECT attempt.id, item.exercise_type, item.prompt, item.expected_answer"
                " FROM attempt JOIN item ON item.id = attempt.item_id ORDER BY attempt.id"
            ).fetchall(),
            "card": connection.execute(
                "SELECT card.id, form.morph_tag, fl.lemma, sl.lemma,"
                " pattern.rule_key, pattern.paradigm_class"
                " FROM card"
                " LEFT JOIN form ON form.id = card.form_id"
                " LEFT JOIN lexeme fl ON fl.id = form.lexeme_id"
                " LEFT JOIN sense ON sense.id = card.sense_id"
                " LEFT JOIN lexeme sl ON sl.id = sense.lexeme_id"
                " LEFT JOIN pattern ON pattern.id = card.pattern_id"
                " ORDER BY card.id"
            ).fetchall(),
            "error_event": connection.execute(
                "SELECT error_event.id, node.key FROM error_event"
                " JOIN node ON node.id = error_event.node_id ORDER BY error_event.id"
            ).fetchall(),
        }


def test_the_same_hash_again_installs_nothing(phone, ledgers):
    older, _ = ledgers
    _start(phone, older, "older")
    with sqlite3.connect(phone / "polish.db") as connection:
        connection.execute(
            "INSERT INTO item_variant (item_id, accepted_answer, source)"
            " VALUES (1, 'sentinel', 'queued')"
        )
    _start(phone, older, "older")
    assert any(row[2] == "sentinel" for row in _rows(phone / "polish.db", "item_variant")), (
        "an unchanged hash reinstalled the content"
    )


# ------------------------------------------------------------ the page's calls


def _api_routes() -> set[tuple[str, str]]:
    return {
        (method, route.path)
        for route in api.app.routes
        if getattr(route, "path", "").startswith("/api/")
        for method in route.methods
    }


def test_every_api_route_of_the_server_is_answered_on_a_phone(phone, ledgers):
    """Audio included: the phone answers it with the text its own voice speaks.
    A refusal is an answer; only the router's own 404 and 405 are not."""
    older, _ = ledgers
    _start(phone, older, "older")
    for method, path in sorted(_api_routes()):
        url = path.replace("{key}", "CASES").replace("{item_id}", "1").replace("{index}", "0")
        body = {"item_id": 1, "answer": "x"} if path == "/api/submit" else None
        _, answer = _call(method, url, body)
        assert answer.get("detail") not in ("Not Found", "Method Not Allowed"), (
            f"{method} {path} is not answered on a phone"
        )


def test_what_the_phone_does_not_know_it_answers_as_the_server_does(phone, ledgers):
    older, _ = ledgers
    _start(phone, older, "older")
    assert _call("GET", "/api/nothing") == (404, {"detail": "Not Found"})
    assert _call("POST", "/api/graph") == (405, {"detail": "Method Not Allowed"})


@contextmanager
def _pointed_at(path: Path):
    with pytest.MonkeyPatch.context() as patch:
        _point(patch, path)
        try:
            yield
        finally:
            database.engine.dispose()


def _both(phone: Path, method: str, url: str, body: dict | str | None = None):
    """The same call through the server and through the phone, each against its
    own copy of the same database, with the same randomness."""
    source = phone / "polish.db"
    for name in ("server.db", "phone.db"):
        shutil.copy2(source, phone / name)
    with _pointed_at(phone / "server.db"):
        random.seed(20260923)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(api, "SESSION_ORDER", random.Random(7))
            response = TestClient(api.app).request(
                method, url,
                content=body if isinstance(body, str) else None,
                json=None if isinstance(body, str) else body,
            )
            server = (response.status_code, response.json())
    with _pointed_at(phone / "phone.db"):
        random.seed(20260923)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(device, "ORDER", random.Random(7))
            answered = _call(method, url, body)
    return server, answered


@pytest.fixture
def learner(phone, ledgers):
    """A phone that has installed content, answered one item and read nothing."""
    older, _ = ledgers
    _start(phone, older, "older")
    first = _session_items()[0]["id"]
    assert _call("POST", "/api/submit", {"item_id": first, "answer": _expected(first)})[0] == 200
    database.engine.dispose()
    return phone


def test_a_phone_speaks_only_what_the_server_would_play(learner):
    """Criterion 7: a play button's text, under the audio routes' guards — a
    listening item's sentence, an option by its position, and nothing else."""
    with sqlite3.connect(learner / "polish.db") as connection:
        listening, sentence = connection.execute(
            "SELECT id, expected_answer FROM item WHERE exercise_type = 'listening_dictation'"
            " ORDER BY id LIMIT 1"
        ).fetchone()
        choice, options = connection.execute(
            "SELECT id, options_json FROM item WHERE exercise_type = 'mcq' ORDER BY id LIMIT 1"
        ).fetchone()
    options = json.loads(options)
    audio.use_device_voice(True)
    try:
        assert _call("GET", f"/api/audio/{listening}?speed=slow") == (200, {"text": sentence})
        assert _call("GET", f"/api/audio/{choice}/option/1") == (200, {"text": options[1]})
        assert _call("GET", f"/api/audio/{choice}") == (404, {"detail": "no audio for this item"})
        assert _call("GET", f"/api/audio/{listening}/option/0") == (404, {"detail": "no such option"})
        assert _call("GET", f"/api/audio/{choice}/option/{len(options)}")[0] == 404
        assert _call("GET", "/api/audio/one")[0] == 422
        audio.use_device_voice(False)
        assert _call("GET", f"/api/audio/{listening}") == (
            503, {"detail": "no speech synthesiser on this machine"}
        )
    finally:
        audio.use_device_voice(None)


def test_a_phone_without_a_voice_shows_no_play_button(learner):
    """Criterion 7: without a Polish voice no item carries sound, so dictation is
    not offered and options have no play button; with one, options do."""
    audio.use_device_voice(False)
    try:
        items = _session_items()
        assert items and not any(i["has_audio"] or i["options_audio"] for i in items)
        assert not any(i["exercise_type"] == "listening_dictation" for i in items)
        audio.use_device_voice(True)
        assert any(i["options_audio"] for i in _session_items())
    finally:
        audio.use_device_voice(None)


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("GET", "/api/session?limit=20"),
        ("GET", "/api/session?limit=10&extra=1"),
        ("GET", "/api/graph"),
        ("GET", "/api/concepts"),
        ("GET", "/api/concepts/CASES"),
        ("GET", "/api/concepts/NO_SUCH_THING"),
        ("POST", "/api/concepts/NO_SUCH_THING/read"),
        ("POST", "/api/concepts/INSTRUMENTAL/read"),
        ("POST", "/api/concepts/VOCAB_GENDER/read"),
        ("POST", "/api/session/complete"),
    ],
)
def test_a_phone_answers_exactly_as_the_server(learner, method, url):
    """Criterion 1: the same status and the same body, refusals included."""
    server, phone = _both(learner, method, url)
    assert phone == server


def test_a_graded_answer_is_the_same_on_a_phone(learner):
    with sqlite3.connect(learner / "polish.db") as connection:
        item_id, answer = connection.execute(
            "SELECT id, expected_answer FROM item WHERE exercise_type = 'cloze' LIMIT 1"
        ).fetchone()
    for submitted in (answer, answer + "x", "zzzz"):
        server, phone = _both(
            learner, "POST", "/api/submit",
            {"item_id": item_id, "answer": submitted, "latency_ms": 1200},
        )
        assert phone == server, submitted
    server, phone = _both(learner, "POST", "/api/submit", {"item_id": 10**9, "answer": "x"})
    assert phone == server == (404, {"detail": "no such item"})


@pytest.mark.parametrize(
    "body",
    ['{"answer": "x"}', '{"item_id": "one", "answer": "x"}', '{"item_id": 1, "answer": 5}', "not json"],
)
def test_malformed_input_is_refused_as_the_server_refuses_it(learner, body):
    """Compared by status alone: FastAPI's 422 body lists pydantic's errors,
    which the page never reads."""
    server, phone = _both(learner, "POST", "/api/submit", body)
    assert phone[0] == server[0] == 422
    server, phone = _both(learner, "GET", "/api/session?limit=many")
    assert phone[0] == server[0] == 422
