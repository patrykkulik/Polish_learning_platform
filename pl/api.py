"""JSON API and the review page.

Grading runs here, never in the browser. Client-side grading would leak the
expected answer and would suppress the error events the whole remediation loop
depends on — the learner's weakest node is computed from what they actually got
wrong, and a client that grades itself can decline to report.

The API is JSON-first and the page is a thin consumer of it, so replacing the
page with a PWA later replaces the page and not the backend.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select

from pl import db as database
from pl import session as composer
from pl import streak as streaks
from pl import tags
from pl.content import ingest
from pl.domain import ExpectedSlot
from pl.domain import Form as DomainForm
from pl.grade import classify, explain
from pl.models import AppUser, Attempt, Form, Item, Lexeme, Node
from pl.schedule import apply_diagnosis

HERE = Path(__file__).resolve().parent

app = FastAPI(title="Polish Learning Platform")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


class Submission(BaseModel):
    item_id: int
    answer: str
    latency_ms: int | None = None


def _user(db) -> AppUser:
    user = db.scalar(select(AppUser).limit(1))
    if user is None:
        user = ingest.ensure_user(db)
        db.commit()
    return user


def expected_slot(db, item: Item) -> ExpectedSlot:
    """Build the grader's input from stored rows.

    The M1 producer of an `ExpectedSlot`. M0's producer is `morph.forms`; both
    hand the classifier the same value, which is why the moat needed no change
    when a database appeared underneath it.
    """
    target = db.get(Form, item.target_form_id)
    if target is None:
        raise HTTPException(404, "item has no target form")
    lexeme = db.get(Lexeme, target.lexeme_id)
    rows = db.scalars(select(Form).where(Form.lexeme_id == lexeme.id)).all()

    paradigm = tuple(
        DomainForm(surface=r.surface, lemma=lexeme.lemma, tag=tags.parse(r.morph_tag))
        for r in rows
    )
    expected = next(
        f for f in paradigm
        if f.surface == target.surface and f.tag.raw == target.morph_tag
    )
    return ExpectedSlot(expected=expected, paradigm=paradigm)


def _serialise(db, item: Item) -> dict:
    """What the client is allowed to see before it answers.

    Deliberately omits `expected_answer` and every accepted variant. For multiple
    choice the options go out unlabelled — the correct one is not marked.
    """
    node = db.get(Node, item.node_id)
    return {
        "id": item.id,
        "exercise_type": item.exercise_type,
        "prompt": item.prompt,
        "gloss": item.gloss,
        "options": item.options_json,
        "node": {"key": node.key, "title": node.title, "type": node.type},
    }


@app.get("/", response_class=HTMLResponse)
def page(request: Request):
    return templates.TemplateResponse(request, "session.html", {})


@app.get("/api/session")
def get_session(limit: int = 20):
    db = database.session()
    try:
        user = _user(db)
        items, stats = composer.build_session(db, user.id, user.settings_json, limit)
        return {
            "items": [_serialise(db, i) for i in items],
            "stats": stats,
            "progress": streaks.progress(db, user.id, user.settings_json),
        }
    finally:
        db.close()


@app.post("/api/submit")
def submit(payload: Submission):
    db = database.session()
    try:
        user = _user(db)
        item = db.get(Item, payload.item_id)
        if item is None:
            raise HTTPException(404, "no such item")

        attempt = Attempt(
            user_id=user.id,
            item_id=item.id,
            submitted=payload.answer,
            latency_ms=payload.latency_ms,
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
        db.add(attempt)
        db.flush()

        diagnosis = classify(expected_slot(db, item), payload.answer)
        applied = apply_diagnosis(db, user.id, item, diagnosis, attempt.id)
        db.commit()

        return {
            "correct": diagnosis.is_correct,
            "error_class": str(diagnosis.error_class),
            "message": explain(diagnosis),
            "expected": item.expected_answer,
            # Which cards this one answer moved, and which it deliberately did
            # not. The distinction is the whole design; surfacing it makes the
            # scheduling legible instead of magic.
            "scored": {k: int(v) for k, v in applied.items()},
            "progress": streaks.progress(db, user.id, user.settings_json),
        }
    finally:
        db.close()


@app.post("/api/session/complete")
def complete(items_completed: int = 0):
    """End of session: evaluate unlocks, then the streak."""
    db = database.session()
    try:
        user = _user(db)
        unlocked = composer.evaluate_unlocks(db, user.id)
        row = streaks.record_activity(
            db, user.id, user.settings_json, items_completed
        )
        return {
            "unlocked": unlocked,
            "streak": row.current,
            "progress": streaks.progress(db, user.id, user.settings_json),
        }
    finally:
        db.close()


@app.get("/api/graph")
def graph():
    """The skill graph with per-node mastery, for the progress view."""
    db = database.session()
    try:
        user = _user(db)
        out = []
        for node in db.scalars(select(Node)):
            out.append(
                {
                    "key": node.key,
                    "title": node.title,
                    "type": node.type,
                    "unlocked": composer.is_unlocked(db, user.id, node),
                    "explanation": node.explanation_md,
                }
            )
        return {"nodes": out}
    finally:
        db.close()
