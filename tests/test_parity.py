"""Parity: the phone's JavaScript runtime against the Python it was ported from.

Each test builds one job, runs it through the Python here and through
`tests/js/replay.mjs` under Node, and compares the two by one rule: JSON and
timestamps parsed, floats within a relative 1e-12, everything else exactly,
and FSRS's own `card_id` left out. The floats that can differ at all are
FSRS's, whose `**` is the platform's `pow` in both languages.

Node is required. Without it these tests fail rather than skip, because a port
that drifts unnoticed is exactly what they exist to catch.
"""

from __future__ import annotations

import json
import math
import random
import shutil
import sqlite3
import subprocess
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from fsrs import Card, Rating, Scheduler
from fsrs.scheduler import DEFAULT_PARAMETERS
from test_classify import CASES, SENTENCE_CASES, _slot
from test_device import ledgers, phone  # noqa: F401 — fixtures: two ledgers, a phone's database

from pl import audio, concepts, course, device, morph, schedule, session
from pl import db as database
from pl.content import ingest
from pl.domain import MULTI_SLOT
from pl.grade import classify, classify_sentence, explain
from pl.models import Item
from scripts import journey_sim

ROOT = Path(__file__).resolve().parent.parent
JS_TESTS = ROOT / "tests" / "js"
WORD_LIST = ROOT / "publish" / "lexicon.json"
LEDGER = ROOT / "publish" / "content.db"


def _node_binary() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.fail("Node is not installed, and the parity tests need it: see the README.")
    return node


def _node(job: dict) -> dict:
    """Run `job` through replay.mjs and return what the JavaScript produced."""
    done = subprocess.run(
        [_node_binary(), str(JS_TESTS / "replay.mjs")],
        input=json.dumps(job),
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        pytest.fail(f"replay.mjs failed:\n{done.stderr}")
    return json.loads(done.stdout)


def assert_same(python, javascript, path: str = "$") -> None:
    """The parity rule, over already-parsed values."""
    if isinstance(python, float) or isinstance(javascript, float):
        assert isinstance(python, int | float) and isinstance(javascript, int | float), (
            f"{path}: {python!r} != {javascript!r}"
        )
        assert math.isclose(python, javascript, rel_tol=1e-12, abs_tol=0.0), (
            f"{path}: {python!r} != {javascript!r}"
        )
    elif isinstance(python, dict):
        assert isinstance(javascript, dict), f"{path}: {python!r} != {javascript!r}"
        assert python.keys() == javascript.keys(), (
            f"{path}: keys {sorted(python)} != {sorted(javascript)}"
        )
        for key in python:
            assert_same(python[key], javascript[key], f"{path}.{key}")
    elif isinstance(python, list | tuple):
        assert isinstance(javascript, list) and len(python) == len(javascript), (
            f"{path}: {python!r} != {javascript!r}"
        )
        for index, (mine, theirs) in enumerate(zip(python, javascript, strict=True)):
            assert_same(mine, theirs, f"{path}[{index}]")
    else:
        assert python == javascript, f"{path}: {python!r} != {javascript!r}"


# ------------------------------------------------------------------- grading


@pytest.fixture(scope="module")
def word_list() -> dict[str, list[list[str]]]:
    """The word list the site publishes, which phones grade with."""
    return json.loads(WORD_LIST.read_text(encoding="utf-8"))


def _form(form) -> list[str]:
    return [form.surface, form.lemma, form.tag.raw]


def _outcome(case_id: str, diagnosis) -> dict:
    return {
        "id": case_id,
        "error_class": str(diagnosis.error_class),
        "message": explain(diagnosis),
        "observed": _form(diagnosis.observed) if diagnosis.observed else None,
        "differing_attr": diagnosis.differing_attr,
    }


def test_the_grader_matches_the_python_on_the_golden_corpus(word_list):
    """Criterion 5: every case gets the same diagnosis and message, graded with
    the same word list. Paradigms come from Morfeusz, here, at test time."""
    cases: list[dict] = []
    python: list[dict] = []

    morph.use_lexicon(word_list)
    try:
        for case in CASES:
            slot = _slot(case)
            cases.append({"id": case["id"], "slot": slot, "submitted": case["submitted"]})
            python.append(_outcome(case["id"], classify(slot, case["submitted"])))
        for case_id, expected, lemma, cell, index, submitted, _want in SENTENCE_CASES:
            slot = _slot({"lemma": lemma, "cell": cell})
            sentence = {"expected": expected, "target_index": index}
            cases.append({"id": case_id, "slot": slot, "submitted": submitted, "sentence": sentence})
            diagnosis = classify_sentence(expected, index, slot, submitted)
            python.append(_outcome(case_id, diagnosis))
    finally:
        morph.use_lexicon(None)

    job_cases = [
        {
            "id": case["id"],
            "slot": {
                "expected": _form(case["slot"].expected),
                "paradigm": [_form(f) for f in case["slot"].paradigm],
                "aspect_partner": case["slot"].aspect_partner,
            },
            "submitted": case["submitted"],
            "sentence": case.get("sentence"),
        }
        for case in cases
    ]
    javascript = _node(
        {"kind": "grade", "lexicon": str(WORD_LIST), "cases": job_cases, "words": []}
    )
    assert_same(python, javascript["cases"])


def test_every_word_of_the_built_content_gets_morfeuszs_analyses_on_the_phone():
    """Criterion 5: the phone's word list answers for every built word exactly
    as Morfeusz does."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from scripts.build_site import content_surfaces

    engine = create_engine(f"sqlite:///file:{LEDGER}?mode=ro&uri=true")
    try:
        with Session(engine) as db:
            words = sorted(content_surfaces(db))
    finally:
        engine.dispose()

    morfeusz = {word: [[f.lemma, f.tag.raw] for f in morph.analyses(word)] for word in words}
    on_a_phone = _node({"kind": "grade", "lexicon": str(WORD_LIST), "cases": [], "words": words})
    assert on_a_phone["words"] == morfeusz


# ---------------------------------------------------------------------- FSRS

#: 22:00 UTC, so short gaps cross UTC midnight.
T0 = datetime(2026, 3, 1, 22, 0, tzinfo=UTC)
MICRO = timedelta(microseconds=1)
SECOND = timedelta(seconds=1)
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)

#: Retrievability is read at these offsets after every review, in microseconds:
#: at once, half a day, either side of a whole day, a week and a hundred days.
PROBES = [0, 12 * 3_600_000_000, 86_400_000_000 - 1, 86_400_000_000, 7 * 86_400_000_000,
          100 * 86_400_000_000]


def _sequence(
    sequence_id: str,
    reviews: list[tuple[Rating, timedelta]],
    *,
    parameters: list[float] | None = None,
    fuzz: bool = False,
    draws: list[float] | None = None,
) -> dict:
    """A card created at T0 and reviewed at T0 + each offset."""
    return {
        "id": sequence_id,
        "scheduler": {"parameters": parameters, "enable_fuzzing": fuzz},
        "draws": draws or [],
        # An explicit card_id spares fsrs its 1 ms sleep; it is never compared.
        "card": Card(card_id=1, due=T0).to_dict(),
        "reviews": [
            {"rating": int(rating), "at": (T0 + offset).isoformat()} for rating, offset in reviews
        ],
        "probes": PROBES,
    }


def _half_stability() -> float:
    """A stability the default scheduler turns into an interval of exactly an
    even number and a half, where Python's round and JavaScript's differ."""
    factor = Scheduler()._FACTOR
    for stability in (2.5, 4.5, 6.5, 8.5, 10.5, 12.5, 14.5, 16.5):
        if (stability / factor) * factor == stability:
            return stability
    raise AssertionError("no stability yields an interval of exactly a half")


def _random_sequence(seed: int, length: int) -> list[tuple[Rating, timedelta]]:
    """Ratings in every state, at gaps from seconds to a year, with microseconds.

    Kept inside a century or two of T0: the JavaScript's microsecond instants
    are exact only until 2255."""
    rng = random.Random(seed)
    at = timedelta(0)
    reviews = []
    for _ in range(length):
        reviews.append((rng.choice([Rating.Again, Rating.Hard, Rating.Good, Rating.Good, Rating.Easy]), at))
        at += timedelta(seconds=10 ** rng.uniform(1, 7.5), microseconds=rng.randrange(1_000_000))
    return reviews


def _sequences() -> list[dict]:
    good, hard, again, easy = Rating.Good, Rating.Hard, Rating.Again, Rating.Easy
    half = list(DEFAULT_PARAMETERS)
    half[3] = _half_stability()  # the initial stability of an Easy first review
    return [
        _sequence("learning-steps", [(good, 0 * DAY), (good, 10 * MINUTE),
                                     (good, 10 * MINUTE + 3 * DAY), (good, 10 * MINUTE + 12 * DAY)]),
        _sequence("hard-on-the-first-step", [(hard, 0 * DAY), (hard, 5 * MINUTE + 30 * SECOND),
                                             (good, 11 * MINUTE), (good, 21 * MINUTE),
                                             (hard, 21 * MINUTE + 2 * DAY)]),
        _sequence("again-while-learning", [(again, 0 * DAY), (again, MINUTE), (good, 2 * MINUTE),
                                           (easy, 12 * MINUTE)]),
        _sequence("lapses", [(good, 0 * DAY), (good, 10 * MINUTE), (again, 4 * DAY),
                             (good, 4 * DAY + 10 * MINUTE), (again, 10 * DAY),
                             (hard, 10 * DAY + 10 * MINUTE), (again, 10 * DAY + 20 * MINUTE),
                             (easy, 10 * DAY + 30 * MINUTE), (good, 30 * DAY)]),
        # 22:00, 22:10, then 01:10 the next day: three hours apart across UTC
        # midnight, which fsrs counts as zero days and ts-fsrs as one. Then one
        # microsecond either side of a whole day.
        _sequence("under-a-day-across-utc-midnight", [
            (good, 0 * DAY), (good, 10 * MINUTE), (good, 3 * HOUR + 10 * MINUTE),
            (hard, 3 * HOUR + 10 * MINUTE + DAY - MICRO), (good, 3 * HOUR + 10 * MINUTE + 2 * DAY - MICRO),
            (again, 3 * HOUR + 10 * MINUTE + 3 * DAY - MICRO), (good, 3 * HOUR + 20 * MINUTE + 3 * DAY),
        ]),
        _sequence("long-gaps", [(easy, 0 * DAY), (good, 400 * DAY), (again, 3400 * DAY),
                                (good, 3400 * DAY + 10 * MINUTE), (hard, 9000 * DAY)]),
        # A barely learnt card forgotten long after: the one case where the
        # short-term bound of the forgetting stability is the smaller.
        _sequence("forgotten-after-a-long-gap", [(again, 0 * DAY), (again, 300 * DAY),
                                                 (good, 300 * DAY + MINUTE), (hard, 700 * DAY),
                                                 (again, 2000 * DAY)]),
        _sequence("up-to-the-maximum-interval", [
            (easy, 0 * DAY), *[(easy, (4 ** n) * DAY) for n in range(1, 8)],
        ]),
        _sequence("interval-on-a-half", [(easy, 0 * DAY), (good, 5 * DAY)], parameters=half),
        *[_sequence(f"random-{seed}", _random_sequence(seed, 80)) for seed in (1, 2, 3)],
    ]


def _run_in_python(sequence: dict, monkeypatch) -> dict:
    options = {"enable_fuzzing": sequence["scheduler"]["enable_fuzzing"]}
    if sequence["scheduler"]["parameters"]:
        options["parameters"] = sequence["scheduler"]["parameters"]
    scheduler = Scheduler(**options)
    draws = iter(sequence["draws"])
    monkeypatch.setattr("fsrs.scheduler.random", lambda: next(draws))

    card = Card.from_dict(sequence["card"])
    steps = []
    for review in sequence["reviews"]:
        at = datetime.fromisoformat(review["at"])
        card, _log = scheduler.review_card(card, Rating(review["rating"]), review_datetime=at)
        steps.append({
            "card": card.to_dict(),
            "retrievability": [
                scheduler.get_card_retrievability(card, at + timedelta(microseconds=offset))
                for offset in sequence["probes"]
            ],
        })
    return {"id": sequence["id"], "steps": steps, "draws_left": sum(1 for _ in draws)}


def _parsed(result: dict) -> dict:
    """Timestamps parsed, and FSRS's own card_id left out."""
    for step in result["steps"]:
        card = step["card"]
        del card["card_id"]
        card["due"] = datetime.fromisoformat(card["due"])
        if card["last_review"] is not None:
            card["last_review"] = datetime.fromisoformat(card["last_review"])
    return result


def _assert_fsrs_parity(sequences: list[dict], monkeypatch) -> list[dict]:
    python = [_parsed(_run_in_python(s, monkeypatch)) for s in sequences]
    javascript = [_parsed(r) for r in _node({"kind": "fsrs", "sequences": sequences})["sequences"]]
    assert_same(python, javascript)
    return python


def test_fsrs_matches_fsrs_6_3_2_on_scripted_reviews(monkeypatch):
    """Criterion 6: the port schedules as fsrs 6.3.2 does, with fuzz off."""
    python = _assert_fsrs_parity(_sequences(), monkeypatch)
    by_id = {result["id"]: result for result in python}

    # The sequences reach what they are named for.
    steps = by_id["interval-on-a-half"]["steps"]
    assert steps[0]["card"]["due"] - T0 == timedelta(days=round(_half_stability()))
    assert round(_half_stability()) != math.floor(_half_stability() + 0.5)
    capped = by_id["up-to-the-maximum-interval"]["steps"]
    assert any(
        step["card"]["due"] - step["card"]["last_review"] == timedelta(days=36500) for step in capped
    )


def test_fuzz_draws_from_the_librarys_ranges(monkeypatch):
    """With fuzz on, the same draws give the same intervals: the port uses
    fsrs's own ranges and rounding. The draws include both ends."""
    draws = [0.0, 0.999999999, 0.5, 0.25, 0.75, 0.1, 0.9, 0.49999, 0.5000001, 0.33] * 4
    reviews = [(Rating.Good, 0 * DAY), (Rating.Good, 10 * MINUTE)]
    at = 10 * MINUTE
    for gap in (3, 5, 9, 16, 30, 70, 150, 400, 900, 2000):
        at += gap * DAY
        reviews.append((Rating.Good, at))
    sequence = _sequence("fuzzed", reviews, fuzz=True, draws=draws)
    python = _assert_fsrs_parity([sequence], monkeypatch)
    assert python[0]["draws_left"] < len(draws), "no interval was long enough to fuzz"


# ----------------------------------------------------------------- the clock


class _Clock(datetime):
    """`datetime` whose `now()` is `_Clock.at`, as `journey_sim.FakeDatetime`
    stands in for the simulation's."""

    at = datetime(2026, 1, 1, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):
        return cls.at.astimezone(tz) if tz is not None else cls.at.replace(tzinfo=None)


#: Every module that reads the clock.
CLOCKED = (session, schedule, course, concepts, ingest)


@pytest.fixture
def frozen_clock(monkeypatch):
    for module in CLOCKED:
        monkeypatch.setattr(module, "datetime", _Clock)
    yield _Clock
    _Clock.at = datetime(2026, 1, 1, tzinfo=UTC)


def _db_text(moment: datetime) -> str:
    """A naive UTC datetime as a DateTime column holds it."""
    return moment.strftime("%Y-%m-%d %H:%M:%S.%f")


#: America/Havana's clocks change at midnight: in 2026 they skip from 00:00 to
#: 01:00 on 8 March and repeat 00:00 on 1 November.
ZONES = ("Europe/Warsaw", "America/Los_Angeles", "Pacific/Auckland", "America/Havana")


def _instants(zone: str) -> list[datetime]:
    """Every twenty minutes for thirty hours either side of each of the zone's
    2026 clock changes, and three times a day through the year."""
    tz = ZoneInfo(zone)
    changes = []
    at = datetime(2026, 1, 1, tzinfo=UTC)
    while at.year == 2026:
        after = at + timedelta(minutes=30)
        if after.astimezone(tz).utcoffset() != at.astimezone(tz).utcoffset():
            changes.append(after)
        at = after
    assert len(changes) == 2, f"{zone} changes its clocks {len(changes)} times in 2026"
    instants = [change + timedelta(minutes=20 * k) for change in changes for k in range(-90, 91)]
    for day in range(365):
        midnight = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=day)
        instants += [midnight + timedelta(minutes=30), midnight + timedelta(hours=12),
                     midnight + timedelta(hours=23, minutes=30)]
    return instants


def test_the_learners_day_matches_the_python_in_four_zones(frozen_clock):
    """Criterion 6: today's date and the midnights either side of it, across
    daylight-saving changes in four zones."""
    frozen_clock.at = datetime(2026, 3, 8, 12, 0, tzinfo=UTC)
    # Midnight never happened in Havana that day: the day starts at 01:00.
    assert session.start_of_user_day({"tz": "America/Havana"}) == datetime(2026, 3, 8, 5, 0)

    cases, python = [], []
    for zone in ZONES:
        settings = {"tz": zone}
        for at in _instants(zone):
            frozen_clock.at = at
            cases.append({"zone": zone, "at": at.isoformat()})
            python.append({
                "zone": zone,
                "at": at.isoformat(),
                "today": session.user_today(settings).isoformat(),
                "start": _db_text(session.start_of_user_day(settings)),
                "end": _db_text(session.end_of_user_day(settings)),
            })
    assert_same(python, _node({"kind": "days", "cases": cases})["days"])


# --------------------------------------------------------------- the journey

ZONE = "Europe/Warsaw"
#: 08:00 in Warsaw. The clocks go forward on day 14.
JOURNEY_START = datetime(2026, 3, 16, 7, 0, tzinfo=UTC)
JOURNEY_DAYS = 64
#: One day, two, and five — more than the two freezes cover.
SKIPPED = {5, 11, 12, 40, 41, 42, 43, 44}
#: Answered, and never completed.
UNFINISHED = {20}
#: Back after a missed day, completed short of the goal: the absence is reckoned
#: that day and again the next, and must be paid for once.
SHORT_DAYS = {6}
EXTRA_ROUNDS = {3, 17, 33, 50}
#: The day the newer ledger is installed.
UPDATE_DAY = 30
#: Days the progress and grammar pages are opened.
PAGE_DAYS = {7, 14, 21, 28, 35, 49, 56, 63}
SECONDS_PER_ITEM = 45
#: Real words the course does not teach, and one that is no word at all.
OUT_OF_COURSE = ("samolotem", "komputer", "szczęśliwy", "Warszawa", "xyzzy")
ASCII = str.maketrans("ąćęłńóśźż", "acelnoszz")


class _Unshuffled(random.Random):
    def shuffle(self, x) -> None:
        pass


def _typo(text: str, rng: random.Random) -> str:
    """Polish letters typed as ASCII, or one letter doubled."""
    ascii_ = text.translate(ASCII)
    if ascii_ != text and rng.random() < 0.6:
        return ascii_
    i = rng.randrange(len(text))
    return text[:i] + text[i] + text[i:]


class _Journey:
    """A phone driven through `pl.device`, recording what the JavaScript replays."""

    def __init__(self, phone_dir: Path, ledger_files: dict[str, Path], clock) -> None:
        self.phone = phone_dir
        self.ledgers = ledger_files
        self.clock = clock
        self.events: list[dict] = []
        self.responses: list[dict] = []
        self.graded: list[tuple[str, str]] = []
        self.rng = random.Random(20260925)
        self.day = 0
        #: The kinds of wrong answer already given on purpose; see `answer`.
        self.forced: set[str] = set()

    def _at(self) -> str:
        return self.clock.at.isoformat()

    def start(self, ledger: str) -> None:
        self.events.append({"at": self._at(), "start": {"ledger": ledger, "hash": ledger, "zone": ZONE}})
        device.start(
            content=str(self.ledgers[ledger]),
            content_hash=ledger,
            lexicon=str(self.phone / "lexicon.json"),
            tz=ZONE,
        )

    def sql(self, text: str) -> None:
        self.events.append({"at": self._at(), "sql": text})
        with closing(sqlite3.connect(self.phone / "polish.db")) as connection:
            connection.execute(text)
            connection.commit()

    def request(self, method: str, url: str, body=None) -> dict:
        text = body if isinstance(body, str) or body is None else json.dumps(body)
        self.events.append({"at": self._at(), "request": [method, url, text]})
        status, answer = device.handle(method, url, text)
        parsed = json.loads(answer)
        self.responses.append({"status": status, "body": parsed})
        return parsed

    def _forced(self, item) -> str | None:
        """Three wrong answers given on purpose, the first time each is possible,
        so the journey compares them whatever path the learner takes: a
        dictation sentence with its Polish letters typed as ASCII, the other
        aspect, and — after the update, which drops earlier queued answers — a
        sentence in the wrong order."""
        words = item.expected_answer.split()
        others = [o for o in item.options_json or [] if o != item.expected_answer]
        ascii_ = item.expected_answer.translate(ASCII)
        wanted = {
            "dictation": item.exercise_type == "listening_dictation" and ascii_ != item.expected_answer,
            "aspect": item.exercise_type == "aspect_choice" and bool(others),
            "order": item.exercise_type in MULTI_SLOT and len(words) > 1 and self.day > UPDATE_DAY,
        }
        for kind, possible in wanted.items():
            if possible and kind not in self.forced:
                self.forced.add(kind)
                return {"dictation": ascii_, "aspect": others[0] if others else "",
                        "order": " ".join(reversed(words))}[kind]
        return None

    def answer(self, item_id: int) -> str:
        """Right 85% of the time; otherwise a real wrong form, a typo, a word
        the course does not teach, or a sentence in the wrong order. An aspect
        choice goes wrong by the other aspect, and dictation, which exists to
        test spelling, by a typo."""
        db = database.session()
        try:
            item = db.get(Item, item_id)
            forced = self._forced(item)
            if forced is not None:
                return forced
            if self.rng.random() < 0.85:
                return item.expected_answer
            kind = self.rng.random()
            words = item.expected_answer.split()
            if item.exercise_type == "aspect_choice":
                others = [o for o in item.options_json or [] if o != item.expected_answer]
                if others:
                    return self.rng.choice(others)
            if item.exercise_type == "listening_dictation":
                return _typo(item.expected_answer, self.rng)
            if item.exercise_type in MULTI_SLOT and len(words) > 1 and kind < 0.6:
                return " ".join(reversed(words))
            if kind < 0.55:
                return _typo(item.expected_answer, self.rng)
            if kind < 0.65:
                return self.rng.choice(OUT_OF_COURSE)
            return journey_sim.wrong_answer(db, item, self.rng)
        finally:
            db.close()

    def round(self, extra: bool = False, answers: int | None = None) -> None:
        """The review page's round: the session, each lesson it offers, and its
        items — all of them, or the first `answers`."""
        url = "api/session?limit=10&extra=1" if extra else "api/session?limit=20"
        served = self.request("GET", url)
        while served.get("lesson"):
            self.request("POST", f"api/concepts/{served['lesson']['key']}/read")
            served = self.request("GET", url)
        for item in served["items"][:answers]:
            self.clock.at += timedelta(seconds=SECONDS_PER_ITEM)
            # What the play buttons would say, before answering.
            if item["has_audio"]:
                self.request("GET", f"api/audio/{item['id']}?speed=slow")
            if item["options_audio"]:
                self.request("GET", f"api/audio/{item['id']}/option/{len(item['options']) - 1}")
            graded = self.request(
                "POST", "api/submit",
                {"item_id": item["id"], "answer": self.answer(item["id"]), "latency_ms": 4000},
            )
            self.graded.append((item["exercise_type"], graded.get("error_class")))

    def pages(self) -> None:
        self.request("GET", "api/graph")
        self.request("GET", "api/concepts")
        for concept in concepts.all_concepts():
            self.request("GET", f"api/concepts/{concept['key']}")


def _drive(journey: _Journey) -> None:
    journey.clock.at = JOURNEY_START - timedelta(hours=1)
    journey.start("older")
    for day in range(1, JOURNEY_DAYS + 1):
        journey.day = day
        journey.clock.at = JOURNEY_START + timedelta(days=day - 1)
        if day in SKIPPED:
            continue
        if day == UPDATE_DAY:
            journey.start("newer")
        journey.round(answers=3 if day in SHORT_DAYS else None)
        if day not in UNFINISHED:
            journey.request("POST", "api/session/complete")
        if day in EXTRA_ROUNDS:
            journey.round(extra=True)
            journey.request("POST", "api/session/complete")
        if day in PAGE_DAYS:
            journey.pages()

    journey.clock.at += timedelta(hours=1)
    # A GET that would create the missing streak row rolls it back.
    journey.sql("DELETE FROM streak")
    journey.request("GET", "api/session?limit=20")
    journey.request("GET", "api/graph")
    # A submit refused after its attempt was flushed leaves no attempt behind.
    with closing(sqlite3.connect(journey.phone / "polish.db")) as connection:
        (item_id,) = connection.execute(
            "SELECT id FROM item WHERE exercise_type = 'cloze' AND target_form_id IS NOT NULL"
            " ORDER BY id LIMIT 1"
        ).fetchone()
    journey.sql(f"UPDATE item SET target_form_id = NULL WHERE id = {item_id}")
    journey.request("POST", "api/submit", {"item_id": item_id, "answer": "kot"})
    # What the page could not have meant.
    journey.request("GET", "api/session?limit=abc")
    journey.request("GET", "api/session?extra=perhaps")
    journey.request("POST", "api/submit", "[]")
    journey.request("POST", "api/submit", {"item_id": True, "answer": "x"})
    journey.request("POST", "api/submit", {"item_id": "7", "answer": 3})
    journey.request("POST", "api/submit", {"item_id": 999999, "answer": "x"})
    journey.request("GET", "api/nothing")
    journey.request("DELETE", "api/graph")
    journey.request("POST", "api/concepts/NOPE/read")
    # Speech only for a listening item, and an option only by its position.
    journey.request("GET", f"api/audio/{item_id}")
    journey.request("GET", "api/audio/1/option/99")
    journey.request("GET", "api/audio/one")


#: Each learner table, and the queued answers, in primary-key order.
ROW_ORDER = {
    "app_user": "id",
    "card": "id",
    "node_unlock": "user_id, node_id",
    "concept_read": "user_id, concept_key",
    "attempt": "id",
    "review": "id",
    "error_event": "id",
    "streak": "user_id",
    "item_variant": "id",
}
JSON_COLUMNS = {("app_user", "settings_json"), ("card", "fsrs_state_json")}
TIME_COLUMNS = {
    ("app_user", "created_at"), ("card", "due_at"), ("card", "created_at"),
    ("attempt", "created_at"), ("node_unlock", "unlocked_at"), ("concept_read", "read_at"),
}


def _parsed_rows(rows: dict[str, list], columns: dict[str, list[str]]) -> dict[str, list[dict]]:
    """Rows by the parity rule: JSON and timestamps parsed, FSRS's card_id out."""
    out = {}
    for table, values in rows.items():
        parsed = []
        for row in values:
            record = dict(zip(columns[table], row, strict=True))
            for name, value in record.items():
                if value is None:
                    continue
                if (table, name) in JSON_COLUMNS:
                    value = json.loads(value)
                    if name == "fsrs_state_json":
                        del value["card_id"]
                        value["due"] = datetime.fromisoformat(value["due"])
                        if value["last_review"] is not None:
                            value["last_review"] = datetime.fromisoformat(value["last_review"])
                elif (table, name) in TIME_COLUMNS:
                    value = datetime.fromisoformat(value)
                record[name] = value
            parsed.append(record)
        out[table] = parsed
    return out


def test_a_journey_gives_the_same_responses_and_rows(ledgers, phone, frozen_clock, monkeypatch, tmp_path):
    """Criterion 6: sixty-four days through the Python reference, replayed through
    the JavaScript runtime, give the same responses and the same learner rows."""
    monkeypatch.setattr(schedule.SCHEDULER, "enable_fuzzing", False)
    monkeypatch.setattr(device, "ORDER", _Unshuffled())

    older, newer = ledgers
    journey = _Journey(phone, {"older": older, "newer": newer}, frozen_clock)
    audio.use_device_voice(True)
    try:
        _drive(journey)
    finally:
        audio.use_device_voice(None)

    database_file = phone / "polish.db"
    with closing(sqlite3.connect(database_file)) as connection:
        columns = {
            table: [c[1] for c in connection.execute(f'PRAGMA table_info("{table}")')]
            for table in ROW_ORDER
        }
        python_rows = {
            table: [list(r) for r in connection.execute(f'SELECT * FROM "{table}" ORDER BY {key}')]
            for table, key in ROW_ORDER.items()
        }

    authored = tmp_path / "concepts.json"
    authored.write_text(
        json.dumps(yaml.safe_load((ROOT / "data" / "concepts.yaml").read_text(encoding="utf-8"))),
        encoding="utf-8",
    )
    javascript = _node({
        "kind": "journey",
        "ledgers": {"older": str(older), "newer": str(newer)},
        "lexicon": str(phone / "lexicon.json"),
        "concepts": str(authored),
        "voice": True,
        "events": journey.events,
    })

    requests = [e["request"] for e in journey.events if "request" in e]
    assert len(javascript["responses"]) == len(journey.responses) == len(requests)
    for index, (request, mine, theirs) in enumerate(
        zip(requests, journey.responses, javascript["responses"], strict=True)
    ):
        try:
            assert_same(mine, theirs, f"response {index}")
        except AssertionError as error:
            raise AssertionError(f"{error}\n  request: {request}") from None
    assert_same(
        _parsed_rows(python_rows, columns), _parsed_rows(javascript["rows"], columns), "rows"
    )

    # The journey reached what it is there to compare.
    classes = {error_class for _, error_class in journey.graded}
    assert {"CORRECT", "ORTHOGRAPHY", "WORD_ORDER", "UNANALYSABLE", "ASPECT_WRONG"} <= classes, classes
    assert ("listening_dictation", "ORTHOGRAPHY") in journey.graded, "no dictation spelling slip"
    spoken = [r["body"] for r, (_, url, _) in zip(journey.responses, requests, strict=True)
              if url.startswith("api/audio/") and r["status"] == 200]
    assert spoken and all(set(body) == {"text"} for body in spoken), "nothing was spoken"
    queued = [r for r in _parsed_rows(python_rows, columns)["item_variant"] if r["source"] == "queued"]
    assert queued, "no reordered answer was queued"
    assert python_rows["node_unlock"], "nothing unlocked"
    assert not python_rows["streak"], "a rolled-back streak row was kept"


# ---------------------------------------------------------------- unit tests


def test_the_javascript_unit_tests_pass():
    """`node --test` over tests/js/, so `uv run pytest` runs them too."""
    done = subprocess.run(
        [_node_binary(), "--test", *sorted(str(p) for p in JS_TESTS.glob("*.test.mjs"))],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
