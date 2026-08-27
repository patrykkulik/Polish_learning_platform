"""JSON API and the review page.

Grading runs here, never in the browser. Client-side grading would leak the
expected answer and would suppress the error events the whole remediation loop
depends on — the learner's weakest node is computed from what they actually got
wrong, and a client that grades itself can decline to report.

The API is JSON-first and the page is a thin consumer of it, so replacing the
page with a PWA later replaces the page and not the backend.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import case, func, select

from pl import db as database
from pl import session as composer
from pl import streak as streaks
from pl import tags
from pl import audio
from pl.content import ingest
from pl.domain import AUDIBLE, MULTI_SLOT, ExpectedSlot
from pl.domain import Form as DomainForm
from pl.grade import classify, classify_sentence, explain
from pl.models import (
    AppUser,
    Attempt,
    Card,
    Form,
    Item,
    ItemSlot,
    Lexeme,
    Node,
    Review,
)
from pl.schedule import apply_diagnosis, cards_for_item, ratings_for

log = logging.getLogger(__name__)

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
    # Without this the classifier's step 5 cannot tell an aspect error from a
    # vocabulary one, so ASPECT_WRONG is unreachable at runtime and the aspect
    # exercise scores nothing when the learner gets it wrong.
    partner = (
        db.get(Lexeme, lexeme.aspect_partner_id)
        if lexeme.aspect_partner_id is not None
        else None
    )
    return ExpectedSlot(
        expected=expected,
        paradigm=paradigm,
        aspect_partner=partner.lemma if partner else None,
    )


def grade_item(db, item: Item, submitted: str):
    """Grade one submission, whichever kind of item it is.

    Single-slot items hold their whole expectation in `target_form_id`.
    Multi-slot items hold one `item_slot` row per token, and the position
    carrying the inflected target is graded by the same six-step classifier —
    so a case error inside a sentence is still diagnosed as a case error.
    """
    if item.exercise_type not in MULTI_SLOT:
        return classify(expected_slot(db, item), submitted)

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
    return classify_sentence(
        [s.expected_surface for s in slots],
        target_index,
        expected_slot(db, item),
        submitted,
    )


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
        # Whether the client should offer a player. Never the audio itself, and
        # never the text it renders — for dictation, the sentence IS the answer.
        "has_audio": item.exercise_type in AUDIBLE and audio.available(),
        "node": {"key": node.key, "title": node.title, "type": node.type},
    }


@app.get("/api/audio/{item_id}")
def item_audio(item_id: int, speed: str = "normal"):
    """Synthesised speech for one item, cached after the first request.

    Deliberately a separate endpoint rather than a field on the item: the
    session payload must never carry the sentence for a dictation item, because
    the sentence is the answer. The learner gets a URL that returns sound.
    """
    if not audio.available():
        raise HTTPException(503, "no speech synthesiser on this machine")
    db = database.session()
    try:
        item = db.get(Item, item_id)
        if item is None or item.exercise_type not in AUDIBLE:
            raise HTTPException(404, "no audio for this item")
        try:
            path = audio.synthesise(item.expected_answer, speed=speed)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except (subprocess.SubprocessError, OSError) as exc:
            # A voice that vanished, an unwritable cache, a wedged child. The
            # client cannot act on any of them, but it must be able to tell
            # "no audio here" from "this item is broken" — and the cause has to
            # reach the log, because the browser silently swallows a failed
            # play() and the learner just sees a button that does nothing.
            log.exception("synthesis failed for item %s", item_id)
            raise HTTPException(503, "speech synthesis failed") from exc
        return FileResponse(path, media_type="audio/mp4")
    finally:
        db.close()


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

        diagnosis = grade_item(db, item, payload.answer)
        applied = apply_diagnosis(db, user.id, item, diagnosis, attempt.id)
        db.commit()

        # Populations the routing table *would* have scored but whose card had
        # already advanced today (see `ONE_REVIEW_PER_DAY`). Reporting these as
        # "untouched" alongside the ones the table deliberately leaves alone
        # would collapse two different facts into one word — "this exercise does
        # not test that" and "this counted, and the schedule moves once a day".
        rated = ratings_for(item, diagnosis.error_class)
        counted_earlier = sorted(
            population
            for population in cards_for_item(db, user.id, item)
            if population in rated and population not in applied
        )

        return {
            "correct": diagnosis.is_correct,
            "error_class": str(diagnosis.error_class),
            "message": explain(diagnosis),
            "expected": item.expected_answer,
            # Which cards this one answer moved, and which it deliberately did
            # not. The distinction is the whole design; surfacing it makes the
            # scheduling legible instead of magic.
            "scored": {k: int(v) for k, v in applied.items()},
            "counted_earlier": counted_earlier,
            "progress": streaks.progress(db, user.id, user.settings_json),
        }
    finally:
        db.close()


#: Round numbers worth naming, spaced so that early encouragement is frequent and
#: later ones are rare enough to mean something.
RETAINED_MILESTONES = (10, 25, 50, 100, 250, 500)
VOCABULARY_MILESTONES = (10, 25, 50, 100, 250)
STREAK_MILESTONES = (3, 7, 14, 30, 60, 100)


def _standing(value: int, thresholds: tuple[int, ...]) -> dict:
    reached = max((t for t in thresholds if value >= t), default=0)
    return {
        "value": value,
        "reached": reached,
        "next": next((t for t in thresholds if value < t), None),
    }


def _milestones_reached(db, user_id: int, streak_current: int) -> dict:
    """Where the learner stands against the next round number.

    Deliberately *where they stand* rather than *what they crossed today*.
    Announcing a crossing needs a column recording which milestones have already
    been announced, and a milestone announced twice is worse than one stated
    plainly — it tells the learner the number is decorative. What this returns is
    true every time it is rendered, which is the property worth having.

    Three separate counts rather than one score, for the same reason `progress`
    returns retention beside the streak: they answer different questions, and a
    single number would hide whichever one is currently bad.
    """
    retained = db.scalar(
        select(func.count())
        .select_from(Card)
        .where(
            Card.user_id == user_id,
            Card.stability_max >= composer.MASTERY_STABILITY_DAYS,
        )
    )
    return {
        "retained": _standing(retained, RETAINED_MILESTONES),
        "vocabulary": _standing(
            len(composer.known_lexemes(db, user_id)), VOCABULARY_MILESTONES
        ),
        "streak": _standing(streak_current, STREAK_MILESTONES),
    }


@app.post("/api/session/complete")
def complete():
    """End of session: evaluate unlocks, then the streak.

    Takes no count. How many items were answered today is a fact the server
    already holds in `attempt`; accepting it as a query parameter let the client
    award itself a streak by typing a number into the URL.
    """
    db = database.session()
    try:
        user = _user(db)
        newly = composer.evaluate_unlocks(db, user.id)
        row = streaks.record_activity(db, user.id, user.settings_json)
        return {
            # The node itself, not its primary key. Unlocking is the one moment
            # in the loop where the course visibly opens up, and it was reaching
            # the learner as the string "N01" — the database's name for the
            # thing, which tells them nothing about what they just earned.
            "unlocked": [
                {
                    "key": node.key,
                    "title": node.title,
                    "type": node.type,
                    "explanation": node.explanation_md,
                }
                for key in newly
                if (node := db.scalar(select(Node).where(Node.key == key)))
            ],
            "streak": row.current,
            "milestones": _milestones_reached(db, user.id, row.current),
            "progress": streaks.progress(db, user.id, user.settings_json),
        }
    finally:
        db.close()


def _retention_curve(db, user_id: int, days: int = 30) -> list[dict]:
    """Share of reviews the learner actually recalled, by day.

    The figure that survives the streak breaking. A streak counts appearances; a
    retention curve counts what stayed learned, which is the thing the product
    claims to deliver.

    `Again` is the only failure. A `Hard` is a recall — it is what a dropped
    diacritic scores, and the learner who wrote `robie` for `robię` had the
    grammar and missed the keyboard. Counting that as forgetting would make the
    curve a measure of typing.
    """
    since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
    rows = db.execute(
        select(
            func.date(Attempt.created_at),
            func.count(Review.id),
            func.sum(case((Review.rating > 1, 1), else_=0)),
        )
        .join(Review, Review.attempt_id == Attempt.id)
        .where(Attempt.user_id == user_id, Attempt.created_at >= since)
        .group_by(func.date(Attempt.created_at))
        .order_by(func.date(Attempt.created_at))
    ).all()
    return [
        {"day": str(day), "reviews": int(total), "recalled": int(recalled or 0)}
        for day, total, recalled in rows
    ]


@app.get("/progress", response_class=HTMLResponse)
def progress_page(request: Request):
    return templates.TemplateResponse(request, "progress.html", {})


@app.get("/api/graph")
def graph():
    """The skill graph with per-node mastery, for the progress view.

    Each node carries two different figures on purpose. `mastered` is the unlock
    gate's own answer, read off a high-water mark, so it never falls. `mastery`
    is current retrievability averaged across the node's strata, so it decays
    between sessions — criterion 14 asks for exactly that split, and showing the
    gate's figure as the progress bar would draw a line that can only go up.
    """
    db = database.session()
    try:
        user = _user(db)
        out = []
        for node in db.scalars(select(Node)):
            detail = composer.node_mastery(db, user.id, node)
            out.append(
                {
                    "key": node.key,
                    "title": node.title,
                    "type": node.type,
                    "unlocked": composer.is_unlocked(db, user.id, node),
                    "mastered": detail["strata"] > 0
                    and composer.is_mastered(db, user.id, node),
                    #: Decays with time. The progress bar.
                    "mastery": round(detail["retention"], 4),
                    "strata": detail["strata"],
                    "started": detail["started"],
                    "mastered_strata": detail["mastered"],
                    "explanation": node.explanation_md,
                }
            )

        met = composer.known_lexemes(db, user.id)
        figures = streaks.progress(db, user.id, user.settings_json)
        return {
            "nodes": out,
            "vocabulary": {
                "met": len(met),
                "total": db.scalar(select(func.count()).select_from(Lexeme)),
            },
            "retention_curve": _retention_curve(db, user.id),
            "milestones": _milestones_reached(db, user.id, figures["streak"]),
            "progress": figures,
        }
    finally:
        db.close()
