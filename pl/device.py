"""The course on a phone: the page's API calls, answered in-process.

The executable spec of the phone's API. On the static site there is no server,
and no Python: `pl/static/device.js` restores the learner's database from the
browser's storage and hands every `request()` the page makes to
`pl/static/course/phone.js`, a port of this module, instead of the network.
`tests/test_parity.py` replays what this module answers through that port.
`handle` calls the same `pl.course` functions the local server's routes call,
and answers the way FastAPI would have: a status and a JSON body,
`{"detail": ...}` when the course refuses.

Everything a phone keeps is one SQLite file, the local schema unchanged: the
course's content, and one learner's progress. Content arrives as the published
ledger and replaces the content tables wholesale; the learner's tables are never
read from it. See `install`.
"""

from __future__ import annotations

import json
import random
import sqlite3
import traceback
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from pl import course, morph
from pl import db as database
from pl.content import ingest
from pl.models import AppUser

#: The course's content: replaced wholesale by each published ledger. Queued
#: answers live in `item_variant`, so a content update drops them — nothing on a
#: phone reads them, and the owner cannot see them there anyway.
CONTENT_TABLES = (
    "lexeme",
    "form",
    "sense",
    "node",
    "node_prereq",
    "node_lexeme",
    "pattern",
    "item",
    "item_slot",
    "item_variant",
)

#: The learner's own rows. Never read from a published ledger — which holds the
#: learner the content build creates, because `ingest_all` calls `ensure_user`.
LEARNER_TABLES = (
    "app_user",
    "card",
    "node_unlock",
    "concept_read",
    "attempt",
    "review",
    "error_event",
    "streak",
)

#: The order a session is served in. A generator of its own, like the server's
#: `SESSION_ORDER`, because FSRS draws its interval fuzz from the global one.
ORDER = random.Random()

#: Query-string values FastAPI (through pydantic) reads as a boolean.
_TRUE = frozenset({"1", "on", "t", "true", "y", "yes"})
_FALSE = frozenset({"0", "off", "f", "false", "n", "no"})


class _Invalid(Exception):
    """A request the page could not have meant: FastAPI answers these 422."""


def _database_file() -> Path:
    return Path(database.engine.url.database)


def _hash_file() -> Path:
    """Beside the database, not in it: a table would be neither content nor learner."""
    return _database_file().with_name("content.hash")


def installed_hash() -> str | None:
    """The hash of the ledger this phone last installed, or None on a new phone."""
    path = _hash_file()
    return path.read_text(encoding="utf-8").strip() if path.exists() else None


def install(ledger: str | Path) -> None:
    """Replace this phone's content tables with the published ledger's.

    One transaction. Each content table and its indexes are dropped and
    recreated from the ledger's own definitions, and its rows are copied with
    their ids — learner rows point at content by id, and the deploy's
    `check-ids` guarantees a published id never names a different row.

    `ATTACH` cannot run inside a transaction, so the ledger is attached first and
    detached after the commit. Nothing turns on SQLite's `foreign_keys`, so
    dropping a table the learner's rows point into is allowed.
    """
    connection = sqlite3.connect(_database_file(), isolation_level=None)
    try:
        connection.execute("ATTACH DATABASE ? AS shipped", (str(ledger),))
        try:
            connection.execute("BEGIN")
            try:
                for table in CONTENT_TABLES:
                    _replace(connection, table)
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        finally:
            connection.execute("DETACH DATABASE shipped")
    finally:
        connection.close()


def _replace(connection: sqlite3.Connection, table: str) -> None:
    row = connection.execute(
        "SELECT sql FROM shipped.sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if row is None:
        raise LookupError(f"the published ledger has no {table!r} table")
    # Dropping a table drops its indexes with it.
    connection.execute(f'DROP TABLE IF EXISTS main."{table}"')
    connection.execute(row[0])
    connection.execute(f'INSERT INTO main."{table}" SELECT * FROM shipped."{table}"')
    indexes = connection.execute(
        "SELECT sql FROM shipped.sqlite_master"
        " WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL",
        (table,),
    ).fetchall()
    for (sql,) in indexes:
        connection.execute(sql)


def start(*, content: str | None, content_hash: str, lexicon: str, tz: str) -> None:
    """Make this phone's database ready for the page, before any request.

    `content` is the published ledger's path when this phone has not installed
    that ledger yet, or None when it already has — `device.js` does not fetch it
    then. Install and update are the same step, and a new phone is only a phone
    that has installed nothing.

    Then the schema step the content build runs everywhere else, which adds any
    missing learner table or nullable column; then the learner, created once.
    The hash is recorded last, so an update interrupted before it simply runs
    again.
    """
    with open(lexicon, encoding="utf-8") as handle:
        morph.use_lexicon(json.load(handle))
    if content is not None and installed_hash() == content_hash:
        content = None
    if content is not None:
        install(content)
        # The pool may hold a connection that saw the old schema.
        database.engine.dispose()
    database.create_all()
    _ensure_learner(tz)
    if content is not None:
        _hash_file().write_text(content_hash, encoding="utf-8")


def _ensure_learner(tz: str) -> None:
    """Create this phone's one learner, in the phone's own timezone.

    `ensure_user` records `Europe/London`, the local app's learner. A phone's
    learner gets the zone the phone reports, or UTC for one `tzdata` does not
    know, so their day — the streak's unit — ends at their own midnight.
    """
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        tz = "UTC"
    db = database.session()
    try:
        if db.scalar(select(AppUser).limit(1)) is None:
            user = ingest.ensure_user(db)
            user.settings_json = {**(user.settings_json or {}), "tz": tz}
            db.commit()
    finally:
        db.close()


def handle(method: str, url: str, body: str | None = None) -> tuple[int, str]:
    """Answer one of the page's API calls: `(status, JSON text)`."""
    parts = urlsplit(url)
    segments = [unquote(s) for s in parts.path.strip("/").split("/")]
    query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
    try:
        route = _route(method.upper(), segments, query, body)
        if isinstance(route, int):
            return route, json.dumps({"detail": _REASONS[route]})
        return 200, json.dumps(route())
    except course.Refused as exc:
        return exc.status, json.dumps({"detail": exc.detail})
    except _Invalid as exc:
        return 422, json.dumps({"detail": str(exc)})
    except Exception as exc:
        # Printed so Safari's Web Inspector shows where; the page shows a message.
        traceback.print_exc()
        return 500, json.dumps(
            {"detail": f"The app hit an error on this phone ({type(exc).__name__})."}
        )


_REASONS = {404: "Not Found", 405: "Method Not Allowed"}


def _route(method: str, segments: list[str], query: dict, body: str | None):
    """The page's JSON routes, as `pl.api` declares them.

    Returns a call to make, or a status when nothing here answers — 404 for no
    such path, 405 for a known path asked with the wrong method, as FastAPI does.
    """
    match segments:
        case ["api", "session"]:
            wanted, call = "GET", lambda: course.get_session(
                _int(query, "limit", 20), _bool(query, "extra", False), order=ORDER
            )
        case ["api", "submit"]:
            wanted, call = "POST", lambda: course.submit(*_submission(body))
        case ["api", "session", "complete"]:
            wanted, call = "POST", course.complete
        case ["api", "graph"]:
            wanted, call = "GET", course.graph
        case ["api", "concepts"]:
            wanted, call = "GET", course.list_concepts
        case ["api", "concepts", key]:
            wanted, call = "GET", lambda: course.get_concept(key)
        case ["api", "concepts", key, "read"]:
            wanted, call = "POST", lambda: course.read_concept(key)
        # What a play button says, for the phone's own voice to speak: the text,
        # not a sound, and without the server's `speed`, which the voice applies.
        case ["api", "audio", item_id]:
            wanted, call = "GET", lambda: course.speech_text(_path_int(item_id, "item_id"))
        case ["api", "audio", item_id, "option", index]:
            wanted, call = "GET", lambda: course.speech_text(
                _path_int(item_id, "item_id"), _path_int(index, "index")
            )
        case _:
            return 404
    return call if method == wanted else 405


def _int(query: dict, name: str, default: int) -> int:
    if name not in query:
        return default
    try:
        return int(query[name])
    except ValueError:
        raise _Invalid(f"{name} must be an integer") from None


def _path_int(value: str, name: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise _Invalid(f"{name} must be an integer") from None


def _bool(query: dict, name: str, default: bool) -> bool:
    if name not in query:
        return default
    value = query[name].lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise _Invalid(f"{name} must be a boolean")


def _submission(body: str | None) -> tuple[int, str, int | None]:
    """What `pl.api.Submission` accepts: an item id, an answer, maybe a latency."""
    try:
        payload = json.loads(body or "")
    except ValueError:
        raise _Invalid("the body is not JSON") from None
    if not isinstance(payload, dict):
        raise _Invalid("the body must be an object")
    item_id = _as_int(payload.get("item_id"))
    answer = payload.get("answer")
    latency = payload.get("latency_ms")
    if item_id is None:
        raise _Invalid("item_id must be an integer")
    if not isinstance(answer, str):
        raise _Invalid("answer must be a string")
    if latency is not None:
        latency = _as_int(latency)
        if latency is None:
            raise _Invalid("latency_ms must be an integer")
    return item_id, answer, latency


def _as_int(value) -> int | None:
    """An integer as pydantic's lax mode reads one: an int, an integral float, or
    a string of digits. Never a boolean."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None
