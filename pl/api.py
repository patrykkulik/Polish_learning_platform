"""The local app over HTTP: the JSON routes, the pages, and speech.

The JSON routes are `pl.course` behind FastAPI. Grading runs there — on this
server for the local app, and on the phone for the static site, where
`pl.device` calls the same functions in-process. Served from here, the page gets
what `course._serialise` allows and nothing more, so the answer key stays off it
and the error events the remediation loop depends on are recorded where the page
cannot decline to report them.

The API is JSON-first and the page is a thin consumer of it, so replacing the
page with a PWA later replaces the page and not the backend.
"""

from __future__ import annotations

import logging
import random
import subprocess
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.exc import OperationalError

from pl import audio
from pl import course
from pl import db as database
# The tests patch the composer through this name, and import the rest from here,
# as does `scripts/journey_sim.py`. They live in `pl.course` now.
from pl import session as composer  # noqa: F401
from pl.course import (  # noqa: F401
    RETAINED_MILESTONES,
    STREAK_MILESTONES,
    _serialise,
    _standing,
    expected_slot,
    grade_item,
)
from pl.domain import AUDIBLE
from pl.models import Item

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent

app = FastAPI(title="Polish Learning Platform")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")

#: The order a session is served in. A generator of its own, because FSRS draws
#: its interval fuzz from the global one — shuffling there would shift every
#: schedule computed after it.
SESSION_ORDER = random.Random()

#: What the learner is told when the database is older than the code.
REBUILD_NEEDED = (
    "This database was made by an older version of the app. Stop the server, "
    "run: uv run python -m pl.content.ingest — then start it again."
)


@app.exception_handler(OperationalError)
def _database_older_than_the_code(request: Request, exc: OperationalError):
    """Name the one command that fixes a schema the build has not caught up with.

    `create_all` runs only in the content build, never here, so a table or
    column added since the database was built is simply absent until the build
    is re-run — and the first route to touch it raised SQLite's `no such table`,
    which the page could only report as "the server returned 500". Any other
    database error is not this, and still fails as one.
    """
    if str(exc.orig).startswith(("no such table", "no such column")):
        return JSONResponse(status_code=503, content={"detail": REBUILD_NEEDED})
    raise exc


@app.exception_handler(course.Refused)
def _refused(request: Request, exc: course.Refused):
    """A request the course declined, answered as `HTTPException` answered it."""
    return JSONResponse(status_code=exc.status, content={"detail": exc.detail})


class Submission(BaseModel):
    item_id: int
    answer: str
    latency_ms: int | None = None


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
        return _speak(item.expected_answer, speed, f"item {item_id}")
    finally:
        db.close()


@app.get("/api/audio/{item_id}/option/{index}")
def option_audio(item_id: int, index: int, speed: str = "normal"):
    """One choice option, spoken, so the learner hears the word while choosing.

    The option is taken from the item by position, never as text from the
    request: the endpoint can say what the screen already shows and nothing
    else — least of all a dictation sentence, which has no options.
    """
    if not audio.available():
        raise HTTPException(503, "no speech synthesiser on this machine")
    db = database.session()
    try:
        item = db.get(Item, item_id)
        options = (item.options_json or []) if item is not None else []
        if not 0 <= index < len(options):
            raise HTTPException(404, "no such option")
        return _speak(options[index], speed, f"option {index} of item {item_id}")
    finally:
        db.close()


def _speak(text: str, speed: str, what: str) -> FileResponse:
    """Synthesise `text`, or say which of two things went wrong."""
    try:
        path = audio.synthesise(text, speed=speed)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (subprocess.SubprocessError, OSError) as exc:
        # A voice that vanished, an unwritable cache, a wedged child. The
        # client cannot act on any of them, but it must be able to tell
        # "no audio here" from "this item is broken" — and the cause has to
        # reach the log, because the browser silently swallows a failed
        # play() and the learner just sees a button that does nothing.
        log.exception("synthesis failed for %s", what)
        raise HTTPException(503, "speech synthesis failed") from exc
    return FileResponse(path, media_type="audio/mp4")


@app.get("/", response_class=HTMLResponse)
def page(request: Request):
    return templates.TemplateResponse(request, "session.html", {})


@app.get("/api/session")
def get_session(limit: int = 20, extra: bool = False):
    """See `course.get_session`. The shuffle uses `SESSION_ORDER`, read here at
    call time, so a test that seeds it still controls the order."""
    return course.get_session(limit, extra, order=SESSION_ORDER)


@app.post("/api/submit")
def submit(payload: Submission):
    return course.submit(payload.item_id, payload.answer, payload.latency_ms)


@app.post("/api/session/complete")
def complete():
    return course.complete()


@app.get("/progress", response_class=HTMLResponse)
def progress_page(request: Request):
    return templates.TemplateResponse(request, "progress.html", {})


@app.get("/grammar", response_class=HTMLResponse)
def grammar_page(request: Request):
    return templates.TemplateResponse(request, "grammar.html", {})


@app.get("/grammar/{key}", response_class=HTMLResponse)
def grammar_concept_page(request: Request, key: str):
    return templates.TemplateResponse(request, "grammar.html", {})


@app.get("/api/concepts")
def list_concepts():
    return course.list_concepts()


@app.get("/api/concepts/{key}")
def get_concept(key: str):
    return course.get_concept(key)


@app.post("/api/concepts/{key}/read")
def read_concept(key: str):
    return course.read_concept(key)


@app.get("/api/graph")
def graph():
    return course.graph()
