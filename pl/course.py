"""What the course does for its learner, with no web framework involved.

Every JSON route the pages call is a function here, taking plain arguments and
returning plain data. `pl.api` serves them over HTTP on the local server, and
`pl.device` answers the page's calls with them in-process — the reference for
the phone, which runs `pl/static/course/course.js`, a port of this module that
`tests/test_parity.py` holds to it. One implementation of the course's rules, so
what a phone runs is what the tests and `journey_sim` measure.

Grading runs here, on whichever of the two the learner is using. On the local
server that keeps the answer key off the page. On a phone the answer key is
already on the device — design v1 called that a non-issue for one learner, who
could only be leaking answers to themselves.

Collaborators are reached through their modules — `composer.build_session`,
`database.session()`, `audio.available()` — and never imported by name. The
tests replace them on the module, and a name bound here would slip past every
one of those patches.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from pl import audio
from pl import concepts as teaching
from pl import db as database
from pl import session as composer
from pl import streak as streaks
from pl import tags
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
from pl.schedule import apply_diagnosis, card_advanced_since, cards_for_item, ratings_for


class Refused(Exception):
    """A request the course declines, with the HTTP status it amounts to.

    `pl.api` answers it with that status and `{"detail": ...}`, exactly as the
    `HTTPException` these functions used to raise; `pl.device` answers the same
    way on a phone. Nothing here knows about HTTP beyond the number.
    """

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


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
        raise Refused(404, "item has no target form")
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
        # Whether each option can be heard. Safe where the sentence is not: the
        # options are already on screen, so hearing one gives nothing away.
        "options_audio": bool(item.options_json) and audio.available(),
        "node": {"key": node.key, "title": node.title, "type": node.type},
    }


def speech_text(item_id: int, index: int | None = None) -> dict:
    """What a play button says: a listening item's sentence, or one option.

    The local server synthesises the sound itself; a phone speaks this text in
    its own voice. The guards are the audio routes': a sentence only for a
    listening item, and an option only by its position on the item, so a phone
    says nothing the page could not already ask the server to say.
    """
    if not audio.available():
        raise Refused(503, "no speech synthesiser on this machine")
    db = database.session()
    try:
        item = db.get(Item, item_id)
        if index is None:
            if item is None or item.exercise_type not in AUDIBLE:
                raise Refused(404, "no audio for this item")
            return {"text": item.expected_answer}
        options = (item.options_json or []) if item is not None else []
        if not 0 <= index < len(options):
            raise Refused(404, "no such option")
        return {"text": options[index]}
    finally:
        db.close()


def get_session(limit: int = 20, extra: bool = False, *, order: random.Random) -> dict:
    """The day's session, or with `extra` an extra round the learner asked for:
    up to `EXTRA_ROUND_NEW` new cards whatever the day has had, review for the
    rest. Reloading the page is not asking — only the review page's "Another
    round" button sends it.

    `order` shuffles what is served. Each caller owns its generator, because
    FSRS draws its interval fuzz from the global one."""
    db = database.session()
    try:
        user = _user(db)
        items, stats = composer.build_session(
            db, user.id, user.settings_json, limit, extra=extra
        )
        # Served mixed, not in the blocks the composer builds it in — review
        # first and new material last read as five old words and then five new
        # ones. Shuffled here, on the way out, so the composition and every
        # measurement made on it stay exactly as they were.
        served = list(items)
        order.shuffle(served)
        return {
            "items": [_serialise(db, i) for i in served],
            "stats": stats,
            # At most one, for the node this session would otherwise introduce
            # from next. The concept is taught before it is drilled, and the
            # client re-fetches once it is acknowledged — so the lesson and the
            # material it explains land in the same sitting.
            "lesson": teaching.lesson_for(db, user.id),
            "progress": streaks.progress(db, user.id, user.settings_json),
        }
    finally:
        db.close()


def submit(item_id: int, answer: str, latency_ms: int | None = None) -> dict:
    db = database.session()
    try:
        user = _user(db)
        item = db.get(Item, item_id)
        if item is None:
            raise Refused(404, "no such item")

        attempt = Attempt(
            user_id=user.id,
            item_id=item.id,
            submitted=answer,
            latency_ms=latency_ms,
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
        db.add(attempt)
        db.flush()

        diagnosis = grade_item(db, item, answer)
        applied = apply_diagnosis(
            db, user.id, item, diagnosis, attempt.id,
            composer.start_of_user_day(user.settings_json),
        )
        db.commit()

        # Populations the routing table *would* have scored but that this answer
        # did not move, split by why. Reporting them as "untouched" alongside the
        # ones the table deliberately leaves alone would collapse different facts
        # into one word — and so would reporting them together:
        #
        # - `counted_earlier`: the card already advanced today (see
        #   `ONE_REVIEW_PER_DAY`). True to say it counted.
        # - `cooled_down`: the card is not due and advanced within
        #   `EARLY_REVIEW_COOLDOWN_DAYS`, on an earlier day. Nothing was recorded
        #   today, so "counted earlier today" would be false — and remediation
        #   serves exactly these cards, so it was the common case.
        day_start = composer.start_of_user_day(user.settings_json)
        rated = ratings_for(item, diagnosis.error_class)
        counted_earlier: list[str] = []
        cooled_down: list[str] = []
        for population, card in sorted(cards_for_item(db, user.id, item).items()):
            if population not in rated or population in applied:
                continue
            if card_advanced_since(db, card, day_start):
                counted_earlier.append(population)
            else:
                cooled_down.append(population)

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
            "cooled_down": cooled_down,
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


def complete() -> dict:
    """End of session: evaluate unlocks, then the streak.

    Takes no count. How many items were answered today is a fact the course
    already holds in `attempt`; accepting it from the client let the client
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


def _retention_curve(db, user_id: int, settings: dict, days: int = 30) -> list[dict]:
    """Share of reviews the learner actually recalled, by day.

    The figure that survives the streak breaking. A streak counts appearances; a
    retention curve counts what stayed learned, which is the thing the product
    claims to deliver.

    `Again` is the only failure. A `Hard` is a recall — it is what a dropped
    diacritic scores, and the learner who wrote `robie` for `robię` had the
    grammar and missed the keyboard. Counting that as forgetting would make the
    curve a measure of typing.

    Bucketed by the learner's **local** day, not by the UTC date. Grouping in SQL
    on the stored UTC-naive column is cheaper and wrong for anyone west of
    Greenwich: an evening session in Los Angeles is past midnight UTC, so one
    sitting is split across two bars and the count of active days is inflated.
    Thirty days of one learner's reviews is small enough to bucket in Python,
    which also keeps the query portable.
    """
    tz = ZoneInfo(settings.get("tz", "UTC"))
    since = composer.start_of_user_day(settings) - timedelta(days=days - 1)
    rows = db.execute(
        select(Attempt.created_at, Review.rating)
        .join(Review, Review.attempt_id == Attempt.id)
        .where(Attempt.user_id == user_id, Attempt.created_at >= since)
    ).all()

    buckets: dict[str, list[int]] = {}
    for created_at, rating in rows:
        local_day = created_at.replace(tzinfo=UTC).astimezone(tz).date()
        tally = buckets.setdefault(str(local_day), [0, 0])
        tally[0] += 1
        if rating > 1:
            tally[1] += 1
    return [
        {"day": day, "reviews": total, "recalled": recalled}
        for day, (total, recalled) in sorted(buckets.items())
    ]


def list_concepts() -> dict:
    db = database.session()
    try:
        return {"concepts": teaching.index(db, _user(db).id)}
    finally:
        db.close()


def get_concept(key: str) -> dict:
    """A concept the learner has reached, in full; one they have not, by name.

    The index shows the shape of the course and the explanation opens when the
    concept does, so a locked concept carries no prose here either — a page that
    withholds what its own API hands out is not a gate.
    """
    db = database.session()
    try:
        user = _user(db)
        concept = teaching.by_key(key)
        if concept is None:
            raise Refused(404, "no such concept")
        state = {
            "key": concept["key"],
            "title": concept["title"],
            "read": key in teaching.read_keys(db, user.id),
        }
        if not teaching.is_open(db, user.id, concept):
            return {**state, "open": False}
        return {**state, "open": True, **teaching.render(db, concept)}
    finally:
        db.close()


def read_concept(key: str) -> dict:
    """Acknowledge a lesson. Writes no card, no review and no attempt.

    Reading is not answering: the daily goal counts items answered, and a goal a
    page of prose can satisfy is not a goal.
    """
    db = database.session()
    try:
        user = _user(db)
        concept = teaching.by_key(key)
        if concept is None:
            raise Refused(404, "no such concept")
        if not teaching.is_open(db, user.id, concept):
            raise Refused(403, "this concept has not opened yet")
        teaching.mark_read(db, user.id, key)
        return {"key": key, "read": True}
    finally:
        db.close()


def _mastered_for_display(db, user_id: int, node, detail: dict) -> bool:
    """`is_mastered`, but a progress page must not 500 on an undefined gate.

    `is_mastered` raises for a node with no gating referents, deliberately: an
    unlock condition nobody can evaluate is a bug worth stopping for. The guard
    here reads `strata > 0`, which holds for a leaf node — but a *function* node
    has no strata of its own and recurses into its prerequisites, so the guard
    passes while the recursion raises on whichever prerequisite is empty. The
    whole page then 500s instead of one row reading "not yet".

    Zero-stratum nodes became newly reachable when theme gating arrived: gating
    removes a stratum rather than emptying it, so a rule can narrow to nothing
    while the build still reports success.
    """
    if detail["strata"] <= 0 and node.type != "function":
        return False
    try:
        return composer.is_mastered(db, user_id, node)
    except AssertionError:
        # A prerequisite has no gating referents. Undefined is not mastered.
        return False


def graph() -> dict:
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
                    "mastered": _mastered_for_display(db, user.id, node, detail),
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
            "retention_curve": _retention_curve(db, user.id, user.settings_json),
            "milestones": _milestones_reached(db, user.id, figures["streak"]),
            "progress": figures,
        }
    finally:
        db.close()
