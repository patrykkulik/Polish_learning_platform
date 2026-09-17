"""Session composition and the node unlock gate.

The learner is served **debt, then remediation, then new**, and nothing new is
introduced while the backlog is deep. That ordering is what makes the streak mean
retention rather than novelty: a learner with forty due cards cannot bank a day
by starting a fresh lesson.

"Deep" rather than "non-empty" — see `DEBT_TOLERANCE`. Demanding a completely
clear queue reads as the stricter, more honest rule, and measured over ninety
days it stops the curriculum opening at all.

The segments are *composed* in a different order — debt, new, remediation — and
`build_session` explains why. In short: remediation wants the whole session and
has no natural stopping point, so it is composed last, against what is left.

The unlock gate reads `stability_max`, never current stability. FSRS schedules a
card to fall due exactly when its retrievability decays to 0.9, so "retrievability
>= 0.9" is a statement about being up to date, not about knowing anything — gate on
it and four days off would re-lock everything downstream. Stability is what does
not move while the learner sleeps.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pl.models import (
    Attempt,
    Card,
    ErrorEvent,
    Form,
    Item,
    Node,
    NodeLexeme,
    NodePrereq,
    NodeUnlock,
    Pattern,
    Review,
    Sense,
)
from pl import audio, concepts, schedule
from pl.domain import AUDIBLE
from pl.schedule import LEXICAL, MORPH, PATTERN, Rating, populations_for

#: Days of retention a card must once have reached to count toward mastery.
MASTERY_STABILITY_DAYS = 7.0
#: Fraction of a node's strata that must clear it.
MASTERY_FRACTION = 0.8
#: Strata that may remain unmastered whatever the fraction works out to.
#:
#: This is the design's own worked example, finally implemented. §"Pattern cards
#: are stratified" says of the four-stratum accusative node: *"80% means three of
#: four, and the learner may carry one weak paradigm class forward while the
#: other three are solid."* Three of four is 0.75, so the fraction alone has
#: always demanded four of four. The example described behaviour the formula
#: never delivered.
#:
#: A fraction cannot deliver it, either: `mastered / n >= 0.8` is 1-of-1, 2-of-2,
#: 3-of-3 and 4-of-4, because there is no granularity between "all" and "not all"
#: until five strata — and four of the eleven grammar nodes are narrower than
#: that, three of them (N03, N04, N05) on the critical path to everything else.
#: Carrying one weak class forward has to be said in strata to be sayable at all.
MASTERY_ALLOWED_SHORTFALL = 1
#: Successful reviews, and the calendar span they must cover.
MASTERY_MIN_REVIEWS = 3
MASTERY_MIN_SPAN_DAYS = 7

#: New cards introduced per day. Without a cap, one enthusiastic evening creates
#: a debt spike three days later that reads as punishment for engagement.
#:
#: Also the default daily goal, in items. With nothing due a session is
#: introduction only, and since every introduced item costs at least one card, a
#: clean day offers at most this many items — fewer when the first item of a
#: stratum costs two. The goal stood at twenty, which a learner who had caught up
#: entirely could never meet: a flawless learner advanced the streak on 2 days of
#: 90. At ten, 56. Raising the cap to match a goal of twenty was measured and
#: rejected: 13 days, and a backlog of 29 to 38 against a twenty-item session.
DAILY_NEW_CAP = 10

#: Criterion 9's bound: how much debt may remain and still admit new material.
#:
#: Zero is the criterion as originally written — introduce only on a day that
#: starts completely clear — and measurement is what argued it down. A learner at
#: 85% accuracy is rarely at zero and almost never at zero on consecutive days,
#: so introduction fired about one day in four: 123 distinct items and **one**
#: node unlocked over ninety simulated days, averaged across four seeds, with
#: only two of the six exercise types ever served.
#:
#: At five, the same learner reaches 224 items and four nodes on every seed, and
#: meets four exercise types. Five rather than ten because ten lets the backlog
#: reach 27 against a twenty-item session — more than one sitting can clear, and
#: criterion 12 makes clearing it the condition for the streak. A bound that
#: quietly puts the streak out of reach is not a kindness.
#:
#: Re-measure with `scripts/journey_sim.py` before moving this.
DEBT_TOLERANCE = 5
#: Total session length is deliberately **not** capped at M1 — see the design's
#: Optional hardening. Path A has one learner who can simply stop.


def user_today(settings: dict) -> date:
    tz = ZoneInfo(settings.get("tz", "UTC"))
    return datetime.now(tz).date()


def end_of_user_day(settings: dict) -> datetime:
    tz = ZoneInfo(settings.get("tz", "UTC"))
    local = datetime.now(tz)
    midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time())
    return midnight.replace(tzinfo=tz).astimezone(UTC).replace(tzinfo=None)


def start_of_user_day(settings: dict) -> datetime:
    """The other end of the learner's day, in the same UTC-naive frame.

    Anything counted "today" — cards introduced, items answered — is counted
    between this and `end_of_user_day`, so the two boundaries cannot drift apart.
    """
    tz = ZoneInfo(settings.get("tz", "UTC"))
    local = datetime.now(tz)
    midnight = datetime.combine(local.date(), datetime.min.time())
    return midnight.replace(tzinfo=tz).astimezone(UTC).replace(tzinfo=None)


# ------------------------------------------------------------------ unlocking


def _prereqs(db: Session, node_id: int) -> list[Node]:
    ids = db.scalars(
        select(NodePrereq.prereq_node_id).where(NodePrereq.node_id == node_id)
    ).all()
    return [db.get(Node, i) for i in ids]


def is_unlocked(db: Session, user_id: int, node: Node) -> bool:
    """A node with no prerequisites is open from the start; everything else is
    latched by a row that is written once and never deleted."""
    if not _prereqs(db, node.id):
        return True
    return db.get(NodeUnlock, (user_id, node.id)) is not None


def _gating_refs(db: Session, node: Node) -> tuple[str, list[int]]:
    """The referents mastery is measured over, and their population.

    Content-fixed, not learner-fixed. The denominator counts the node's *strata*,
    not the cards that happen to exist — a stratum the learner has not met yet
    counts as unmastered rather than shrinking the denominator. Counting existing
    cards instead would let a node unlock at 3/3 and re-lock at 3/4 the moment a
    fourth card was lazily created.
    """
    if node.type == "grammar":
        ids = db.scalars(select(Pattern.id).where(Pattern.node_id == node.id)).all()
        return PATTERN, list(ids)
    if node.type == "vocabulary":
        lex_ids = db.scalars(
            select(NodeLexeme.lexeme_id).where(NodeLexeme.node_id == node.id)
        ).all()
        ids = db.scalars(
            select(Sense.id).where(Sense.lexeme_id.in_(lex_ids))
        ).all()
        return LEXICAL, list(ids)
    return "", []


def _card_is_mastered(db: Session, card: Card | None) -> bool:
    """Retained for a week, genuinely recalled, and known for a week.

    All three conditions are read from *this card's* review history:

    - `card.reps` counts every review and `card.lapses` counts `Again` only in
      the Review state, so `reps - lapses` is not the number of successful
      recalls and must not stand in for it.
    - The calendar span is a property of the card, not of the account. Measured
      account-wide it passes unconditionally once the learner is a week old, and
      the gate silently stops gating from then on.
    """
    if card is None or card.stability_max < MASTERY_STABILITY_DAYS:
        return False

    successes, first_success = db.execute(
        select(func.count(Review.id), func.min(Attempt.created_at))
        .join(Attempt, Attempt.id == Review.attempt_id)
        .where(Review.card_id == card.id, Review.rating >= int(Rating.Good))
    ).one()

    if successes < MASTERY_MIN_REVIEWS or first_success is None:
        return False
    return (datetime.now(UTC).replace(tzinfo=None) - first_success) >= timedelta(
        days=MASTERY_MIN_SPAN_DAYS
    )


def strata_needed(n: int) -> int:
    """How many of a node's `n` strata must be mastered for the node to be.

    Stated as a count rather than as `mastered / n >= FRACTION`, because a
    shortfall is not expressible as a fraction on a narrow node — and it is the
    narrow nodes that need it. Never below one: a node is not mastered by
    mastering nothing, whatever the arithmetic says.
    """
    by_fraction = min(k for k in range(n + 1) if k / n >= MASTERY_FRACTION)
    return max(1, min(by_fraction, n - MASTERY_ALLOWED_SHORTFALL))


def is_mastered(db: Session, user_id: int, node: Node) -> bool:
    """Mastery, evaluated over the population appropriate to the node's type.

    A function node introduces no competency of its own — it composes earlier
    nodes — so it is mastered exactly when its prerequisites are.
    """
    if node.type == "function":
        return all(is_mastered(db, user_id, p) for p in _prereqs(db, node.id))

    population, ref_ids = _gating_refs(db, node)
    if not ref_ids:
        raise AssertionError(
            f"node {node.key!r} of type {node.type!r} has no gating referents; "
            f"its unlock condition is undefined"
        )

    column = {LEXICAL: Card.sense_id, MORPH: Card.form_id, PATTERN: Card.pattern_id}[
        population
    ]
    mastered = 0
    for ref_id in ref_ids:
        card = db.scalar(
            select(Card).where(
                Card.user_id == user_id,
                Card.population == population,
                column == ref_id,
            )
        )
        if _card_is_mastered(db, card):
            mastered += 1
    return mastered >= strata_needed(len(ref_ids))


def node_mastery(db: Session, user_id: int, node: Node) -> dict:
    """The node's progress as the *display* sees it (criterion 14).

    Deliberately a different number from `is_mastered`, and the difference is the
    criterion. The gate reads `stability_max`, a monotone high-water mark, so a
    node once unlocked never re-locks (criterion 13). The display reads current
    retrievability, which decays while the learner sleeps and recovers when they
    review. A single number cannot do both jobs: drawn from the gate it is a bar
    that can only rise, which tells a learner who has not studied in a month that
    they still know everything; drawn from retrievability it would re-lock half
    the graph after a holiday.

    `retention` is averaged over every stratum, not merely the started ones — a
    node the learner has barely begun should read as barely begun, and dividing
    by what they have started would show a full bar after one good answer.
    """
    if node.type == "function":
        # No competency of its own; it composes the nodes beneath it.
        parts = [node_mastery(db, user_id, p) for p in _prereqs(db, node.id)]
        strata = sum(p["strata"] for p in parts)
        return {
            "strata": strata,
            "started": sum(p["started"] for p in parts),
            "mastered": sum(p["mastered"] for p in parts),
            "retention": (
                sum(p["retention"] * p["strata"] for p in parts) / strata
                if strata
                else 0.0
            ),
        }

    population, ref_ids = _gating_refs(db, node)
    if not ref_ids:
        return {"strata": 0, "started": 0, "mastered": 0, "retention": 0.0}

    column = {LEXICAL: Card.sense_id, MORPH: Card.form_id, PATTERN: Card.pattern_id}[
        population
    ]
    started = mastered = 0
    retained = 0.0
    for ref_id in ref_ids:
        card = db.scalar(
            select(Card).where(
                Card.user_id == user_id,
                Card.population == population,
                column == ref_id,
            )
        )
        if card is None:
            continue
        started += 1
        retained += schedule.retrievability(card)
        if _card_is_mastered(db, card):
            mastered += 1
    return {
        "strata": len(ref_ids),
        "started": started,
        "mastered": mastered,
        "retention": retained / len(ref_ids),
    }


def evaluate_unlocks(db: Session, user_id: int) -> list[str]:
    """Latch any node whose prerequisites are now all mastered.

    Run at session end, never per review — a node unlocking mid-session would
    inject material into a session already composed.
    """
    newly: list[str] = []
    for node in db.scalars(select(Node)):
        if is_unlocked(db, user_id, node):
            continue
        prereqs = _prereqs(db, node.id)
        if prereqs and all(
            is_unlocked(db, user_id, p) and is_mastered(db, user_id, p)
            for p in prereqs
        ):
            db.add(
                NodeUnlock(
                    user_id=user_id,
                    node_id=node.id,
                    unlocked_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )
            newly.append(node.key)
    db.commit()
    return newly


# --------------------------------------------------------------- composition


def settled_today(db: Session, user_id: int, settings: dict) -> set[int]:
    """Cards that have already had their turn during the learner's day.

    One definition, read by the composer and by the streak, so "still due" means
    the same thing to the queue the learner works through and to the condition
    that rewards them for emptying it. They disagreed once and the streak became
    unearnable; a single helper is what stops that recurring.

    A card has had its turn when an item that may score it was answered today —
    **whether or not the answer scored it.** `kot` for `kota` fails the rule and,
    by criterion 11, leaves the form card alone; the form card was due, and the
    composer serves one item per due card, so reading only what *advanced* left
    it owed with nothing left in the session to clear it. One wrong case cost the
    day's streak: over ninety simulated days at 85% accuracy the streak failed on
    debt about 55 days in 90, and advanced on a median 25.5. Counting the turn
    instead, it advances on 77.5, with the same items met and nodes opened. The
    card is due again tomorrow, which is where the routing table put it.

    Empty when `ONE_REVIEW_PER_DAY` is off, because then answering a card again
    does move it and it is genuinely still due.
    """
    if not schedule.ONE_REVIEW_PER_DAY:
        return set()
    since = start_of_user_day(settings)
    settled = schedule.cards_advanced_since(db, user_id, since)
    answered = db.scalars(
        select(Item).where(
            Item.id.in_(
                select(Attempt.item_id).where(
                    Attempt.user_id == user_id, Attempt.created_at >= since
                )
            )
        )
    ).all()
    if not answered:
        return settled
    sense_by_form = _sense_by_form(db)
    turned: set[tuple[str, int]] = set()
    for item in answered:
        turned |= _item_referents(db.get(Node, item.node_id), item, sense_by_form)
    for card in db.scalars(select(Card).where(Card.user_id == user_id)):
        if (card.population, card.sense_id or card.form_id or card.pattern_id) in turned:
            settled.add(card.id)
    return settled


def known_lexemes(db: Session, user_id: int) -> set[int]:
    """The lexemes the learner holds a lexical or morph card for.

    §"Pattern cards are stratified" specifies that a pattern card draws from
    `{lexemes in the stratum} ∩ {lexemes the learner has met}`. Drawing from the
    whole stratum tests the rule on vocabulary the learner has never seen, and
    the routing table then scores that failure against the *rule* — the learner
    is marked down on the genitive for not knowing a noun.
    """
    known: set[int] = set()
    known.update(
        db.scalars(
            select(Form.lexeme_id)
            .join(Card, Card.form_id == Form.id)
            .where(Card.user_id == user_id, Card.population == MORPH)
        )
    )
    known.update(
        db.scalars(
            select(Sense.lexeme_id)
            .join(Card, Card.sense_id == Sense.id)
            .where(Card.user_id == user_id, Card.population == LEXICAL)
        )
    )
    return known


def _least_practised_first(
    db: Session, user_id: int, items: list[Item], rotate: int = 0
) -> list[Item]:
    """Least-practised first, and among equals a different one each time.

    A stratum holds roughly one item per lexeme in it. An unordered select
    returns them in insertion order and the debt loop takes the first offerable
    one, so the rule card is tested on the same noun at every review for the
    life of the account: the card claims generalisation across its stratum and
    measures a single word.

    Practice count alone does not fix that, and the reason is worth recording.
    It only separates items the learner has *already met*; everything unmet ties
    at zero and falls through to the id, and ids follow build order — every
    sentence's cloze is built before its dictation, and every stratum's cloze
    items before its prep drills. A pattern card with eighty untouched
    candidates therefore served the same cloze forever, and 26 dictation items,
    131 free translations and 58 prep drills were unreachable *by construction*
    rather than by any rule anyone wrote down.

    `rotate` is the card's own review count, so the starting point among equals
    advances each time the card comes round. Deterministic — no seed, so the
    composer stays testable — but no longer a fixed preference for whatever was
    generated first.
    """
    if len(items) < 2:
        return items
    counts = dict(
        db.execute(
            select(Attempt.item_id, func.count(Attempt.id))
            .where(
                Attempt.user_id == user_id,
                Attempt.item_id.in_([i.id for i in items]),
            )
            .group_by(Attempt.item_id)
        ).all()
    )
    ordered = sorted(items, key=lambda i: (counts.get(i.id, 0), i.id))
    fewest = counts.get(ordered[0].id, 0)
    equals = [i for i in ordered if counts.get(i.id, 0) == fewest]
    rest = [i for i in ordered if counts.get(i.id, 0) != fewest]
    if rotate and len(equals) > 1:
        offset = rotate % len(equals)
        equals = equals[offset:] + equals[:offset]
    return equals + rest


def _items_for_card(
    db: Session, card: Card, known: set[int] | None = None
) -> list[Item]:
    """The items that could be served for one due card.

    `known` is the learner's met lexemes, from `known_lexemes`. `None` means "do
    not restrict", which is what the callers that are not composing a session
    want.
    """
    if card.population == MORPH:
        items = list(
            db.scalars(select(Item).where(Item.target_form_id == card.form_id))
        )
    elif card.population == PATTERN:
        items = list(db.scalars(select(Item).where(Item.pattern_id == card.pattern_id)))
        if known is not None:
            # An empty intersection means the card is not scheduled at all, which
            # is what the design asks for: there is nothing honest to test it on.
            items = [i for i in items if _lexeme_of_item(db, i) in known]
    else:
        sense = db.get(Sense, card.sense_id)
        stmt = (
            select(Item)
            .join(Node, Node.id == Item.node_id)
            .where(Node.type == "vocabulary", Item.pattern_id.is_(None))
        )
        items = [
            i
            for i in db.scalars(stmt)
            if _lexeme_of_item(db, i) == sense.lexeme_id
        ]
    # Ordered for every population, not just pattern cards. A form card is shared
    # by every exercise built on that form — the cloze, the dictation, the whole
    # sentence — and serving whichever has the lowest id means the learner meets
    # one of them and never the others, however many were built.
    return _least_practised_first(db, card.user_id, items, rotate=card.reps)


def _lexeme_of_item(db: Session, item: Item) -> int | None:
    from pl.models import Form

    if item.target_form_id is None:
        return None
    form = db.get(Form, item.target_form_id)
    return form.lexeme_id if form else None


def _started_referents(db: Session, user_id: int) -> set[tuple[str, int]]:
    """Every (population, referent) the learner already holds a card for.

    Keyed on the referent rather than on one referent *column*: a lexical card
    carries `sense_id` and leaves `form_id` null, so reading `form_id` alone
    reports every vocabulary item as never-seen forever.
    """
    started: set[tuple[str, int]] = set()
    for card in db.scalars(select(Card).where(Card.user_id == user_id)):
        ref = card.sense_id or card.form_id or card.pattern_id
        if ref is not None:
            started.add((card.population, ref))
    return started


def _sense_by_form(db: Session) -> dict[int, int]:
    """form id -> the sense of its lexeme, so a vocabulary item can name its card."""
    rows = db.execute(
        select(Form.id, Sense.id).join(Sense, Sense.lexeme_id == Form.lexeme_id)
    ).all()
    return {form_id: sense_id for form_id, sense_id in rows}


def _forms_of_taught_words(db: Session) -> set[int]:
    """Every form of a word some vocabulary node teaches.

    The word-first rule applies to these and nothing else. Verbs have senses but
    no meaning items, so keying the rule on "has a sense" would stop every aspect
    item from ever being introduced.
    """
    return set(
        db.scalars(
            select(Form.id)
            .join(NodeLexeme, NodeLexeme.lexeme_id == Form.lexeme_id)
            .join(Node, Node.id == NodeLexeme.node_id)
            .where(Node.type == "vocabulary")
        )
    )


def _item_referents(
    node: Node, item: Item, sense_by_form: dict[int, int]
) -> set[tuple[str, int]]:
    """The cards this item would score, as (population, referent) pairs.

    Mirrors `schedule.cards_for_item` without creating anything — the same
    intersection of the routing table with what the node type permits.
    """
    allowed = populations_for(node)
    refs: set[tuple[str, int]] = set()
    if MORPH in allowed and item.target_form_id is not None:
        refs.add((MORPH, item.target_form_id))
    if PATTERN in allowed and item.pattern_id is not None:
        refs.add((PATTERN, item.pattern_id))
    if LEXICAL in allowed and item.target_form_id is not None:
        sense_id = sense_by_form.get(item.target_form_id)
        if sense_id is not None:
            refs.add((LEXICAL, sense_id))
    return refs


def weakest_node(db: Session, user_id: int, days: int = 14) -> Node | None:
    """The node with the highest error *rate* over the trailing window.

    A rate rather than a count, or the node with the most items always wins.
    """
    since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
    rows = db.execute(
        select(ErrorEvent.node_id, func.count(ErrorEvent.id))
        .join(Attempt, Attempt.id == ErrorEvent.attempt_id)
        .where(Attempt.user_id == user_id, Attempt.created_at >= since)
        .group_by(ErrorEvent.node_id)
    ).all()
    if not rows:
        return None

    best, best_rate = None, 0.0
    for node_id, errors in rows:
        total = db.scalar(
            select(func.count())
            .select_from(Attempt)
            .join(Item, Item.id == Attempt.item_id)
            .where(
                Attempt.user_id == user_id,
                Attempt.created_at >= since,
                Item.node_id == node_id,
            )
        )
        rate = errors / total if total else 0.0
        if rate > best_rate:
            best, best_rate = node_id, rate
    return db.get(Node, best) if best else None


def offerable(item: Item) -> bool:
    """Whether this item can actually be answered on this machine.

    A listening item without a synthesiser is not merely degraded — it is
    unanswerable. The learner sees an empty box under "Listen, and write what
    you hear", and because a dictation item shares its pattern card with the
    cloze items built from the same sentence, the forced failure rates a card
    the learner is otherwise mastering and returns through the debt queue,
    which is served first.
    """
    return item.exercise_type not in AUDIBLE or audio.available()


def _introduction_budget(db: Session, user_id: int, settings: dict) -> int:
    """How many new cards today still allows.

    Counted from `card.created_at` rather than from a per-call counter. The
    learner can rebuild the session as often as they like — the review page's
    "Another round" button does exactly that — and a counter that lives inside
    one call grants a fresh ten every time it is pressed, which is criterion 15
    failing in the one situation it names.
    """
    introduced = db.scalar(
        select(func.count())
        .select_from(Card)
        .where(
            Card.user_id == user_id,
            Card.created_at >= start_of_user_day(settings),
        )
    )
    return max(0, DAILY_NEW_CAP - (introduced or 0))


def build_session(
    db: Session, user_id: int, settings: dict, limit: int = 20
) -> tuple[list[Item], dict]:
    """Compose the day's queue: debt, then remediation, then new.

    The three segments are *composed* in the order debt, new, remediation, and
    *served* in the order debt, remediation, new. That is not a contradiction and
    it is the whole point: remediation has an appetite for the entire session and
    no natural stopping point, so it is composed last, against whatever room the
    other two left. Composed second, as it reads, it fills the queue to `limit`
    from the weakest node every single day, `len(picked) < limit` is never true
    again after day one, and introduction stops permanently on day two — the
    learner then answers the same twenty items forever, meets no grammar, and
    unlocks nothing. Raising `limit` only moves which twenty.
    """
    horizon = end_of_user_day(settings)
    debt_items: list[Item] = []
    debt_introduced = 0
    remedial: list[Item] = []
    new_items: list[Item] = []
    seen: set[int] = set()

    def total() -> int:
        return len(debt_items) + len(remedial) + len(new_items)

    def add(bucket: list[Item], item: Item) -> bool:
        if item.id in seen or total() >= limit:
            return False
        seen.add(item.id)
        bucket.append(item)
        return True

    # The day's new-card budget, and what the learner already holds. Computed for
    # the whole composition rather than inside the introduction segment, because
    # every segment can create cards and the cap is named for the day, not for
    # one segment of it.
    budget = _introduction_budget(db, user_id, settings)
    started = _started_referents(db, user_id)
    sense_by_form = _sense_by_form(db)
    taught_words = _forms_of_taught_words(db)
    # A concept is taught before it is drilled. A node whose lesson is unread
    # introduces nothing new; its due cards still come back through the debt
    # segment, which never consults this — losing new material is a gate, and
    # losing work already started would be a punishment.
    gated = concepts.gated_node_ids(db, user_id)

    # 1 — debt. A card whose schedule has already advanced today is not debt: it
    # has had its turn, and under `ONE_REVIEW_PER_DAY` answering it again cannot
    # move it. Counting it would leave the learner with a due figure that no
    # amount of work brings down, and — because criterion 12 requires the day's
    # debt cleared — a streak that cannot be earned on any day they meet new
    # material. FSRS puts a new card's first steps minutes apart, so that is
    # every productive day.
    settled = settled_today(db, user_id, settings)
    due = [
        card
        for card in db.scalars(
            select(Card)
            .where(Card.user_id == user_id, Card.due_at <= horizon)
            .order_by(Card.due_at)
        )
        if card.id not in settled
    ]
    known = known_lexemes(db, user_id)
    for card in due:
        candidates = [i for i in _items_for_card(db, card, known) if offerable(i)]
        # A due card must be served, but which of its items serves it is free —
        # and a form card is shared by several. Preferring one that introduces
        # nothing keeps the debt queue from dragging an unmet referent along and
        # creating a card the daily cap never authorised. This is the residue
        # that remained after remediation stopped introducing: the cap read 11 or
        # 12 against a limit of 10, every one of them arriving through debt.
        candidates.sort(
            key=lambda i: bool(
                _item_referents(db.get(Node, i.node_id), i, sense_by_form) - started
            )
        )
        for item in candidates:
            if add(debt_items, item):
                # Charged if it introduced after all, so the budget the other
                # segments read is the truth about the day.
                introduced_by_debt = _item_referents(
                    db.get(Node, item.node_id), item, sense_by_form
                ) - started
                started |= introduced_by_debt
                debt_introduced += len(introduced_by_debt)
                break
    debt_total = len(due)
    debt_served = len(debt_items)

    # 2 — new, while the backlog is small enough to bear it. Claims its room
    # before remediation is allowed to ask for any.
    introduced = 0
    budget = max(0, budget - debt_introduced)
    if len(due) <= DEBT_TOLERANCE:
        # Round-robin across nodes rather than draining them in turn. Node order
        # is arbitrary, and taking one node at a time means the first unlocked
        # node swallows the whole daily cap — a learner would spend day one on
        # vocabulary alone and never reach the grammar the course is *for*.
        node_items = [
            (node, list(db.scalars(select(Item).where(Item.node_id == node.id)).all()))
            for node in db.scalars(select(Node))
            if is_unlocked(db, user_id, node) and node.id not in gated
        ]
        # Start the round-robin somewhere new each time. Round-robin alone is not
        # enough to be fair: every round begins at the first node, and a budget of
        # ten cards is spent by the sixth or seventh, so nodes late in the list
        # are never reached at all. N12's aspect items sat unoffered for forty
        # simulated days for exactly this reason — not because anything blocked
        # them, but because `node.id` decided who ate first. Offset by how much
        # the learner already holds, so it advances with them and stays
        # deterministic.
        if node_items:
            offset = len(started) % len(node_items)
            node_items = node_items[offset:] + node_items[:offset]
        # Two passes over the same pools. The first takes only items that open a
        # stratum the learner has not met; the second is the ordinary draw.
        #
        # A node is mastered through its pattern cards, so an item in an unstarted
        # stratum creates the card the gate counts, while another noun in a
        # stratum already started spends one of the day's ten cards and moves no
        # gate. One pass in id order lets the second kind crowd out the first as
        # soon as the vocabulary is large: `Verified:` with 110 nouns added, N08
        # stopped opening at all within 150 days at 85% accuracy, and a flawless
        # learner lost N09, N10 and N11 with it. Two passes, and the same learner
        # opens eleven nodes against nine, with N08 on day 99.5 against the 109
        # the smaller vocabulary managed.
        #
        # Ordering *within* a node is not enough, and was measured: the
        # round-robin still gives every vocabulary pool its turn, so N08 landed on
        # day 118.5 rather than 99.5. The preference has to hold across nodes.
        for openers_only in (True, False):
            pools = [(node, iter(items)) for node, items in node_items]
            while pools and introduced < budget and total() < limit:
                progressed = False
                for entry in list(pools):
                    node, pool = entry
                    if introduced >= budget or total() >= limit:
                        break
                    for item in pool:
                        if not offerable(item):
                            continue
                        refs = _item_referents(node, item, sense_by_form)
                        if openers_only and not any(
                            population == PATTERN
                            for population, _ in refs - started
                        ):
                            continue
                        # A word before its endings. Grammar items exist for every
                        # noun, and a themed vocabulary node opens after the grammar
                        # that inflects its words — so without this the learner is
                        # asked to write the Polish for "fridge" before ever seeing
                        # `lodówka`. With V02 and V03 opening after N06, 52 of the
                        # 116 words a learner met over ninety simulated days arrived
                        # that way, 51 of them never through their meaning item.
                        if (
                            node.type != "vocabulary"
                            and item.target_form_id in taught_words
                            and (LEXICAL, sense_by_form.get(item.target_form_id))
                            not in started
                        ):
                            continue
                        # Skip only when the item is entirely old. A pattern card is
                        # shared by every lexeme in its stratum, so testing for *any*
                        # overlap lets the first item of a stratum claim it for all
                        # the others — the queue then dries up having offered a small
                        # fraction of the curriculum, with the rest reachable only by
                        # chance through remediation.
                        if refs and refs <= started:
                            continue
                        # The budget is counted in cards, which is what DAILY_NEW_CAP
                        # names and what the day actually costs: the first item of a
                        # stratum introduces both a form and a rule, later ones only
                        # a form. An item that does not fit waits for tomorrow rather
                        # than being allowed to overshoot the cap.
                        cost = len(refs - started) or 1
                        if cost > budget - introduced:
                            continue
                        if item.id in seen:
                            # Already claimed by the debt segment this session. Not a
                            # reason to give up on this pool: `add` reports "full" and
                            # "already taken" with the same False, and treating the
                            # second as the first ends introduction for the day with
                            # budget and room still unspent — handing the room to
                            # remediation instead. The remediation segment carries a
                            # comment about this exact trap; this loop fell into it.
                            continue
                        if add(new_items, item):
                            started |= refs
                            introduced += cost
                            progressed = True
                        break
                    else:
                        pools.remove(entry)
                # Every remaining pool held only items too expensive for what is left
                # of the budget. Without this the outer loop spins on them forever.
                if not progressed:
                    break

    # 3 — remediation, filling whatever debt and introduction left over.
    if total() < limit:
        weak = weakest_node(db, user_id)
        if weak is not None:
            for item in db.scalars(select(Item).where(Item.node_id == weak.id)):
                if not offerable(item):
                    continue
                # `add` returns False both when the session is full and when the
                # item is already picked. Only the first is a reason to stop —
                # treating the second as one ends remediation on its first
                # overlap with the debt queue, which is the common case.
                if total() >= limit:
                    break
                # Remediation re-drills what the learner got wrong. It does not
                # introduce, and an item from the weakest node whose referents
                # they have never met is not remediation wearing a different hat
                # — it is new material arriving through a segment that answers to
                # no budget and no gate. That is how a cap named for ten cards a
                # day passed seventeen, and how new referents reached the learner
                # on days criterion 9's bound had shut introduction entirely.
                #
                # Charging it to the daily budget instead was tried and is not
                # enough: it still admits new material when the gate is shut.
                if _item_referents(weak, item, sense_by_form) - started:
                    continue
                add(remedial, item)

    return debt_items + remedial + new_items, {
        "debt_total": debt_total,
        "debt_served": debt_served,
        #: Cards, not items — the unit DAILY_NEW_CAP is spent in.
        "introduced": introduced,
        "introduced_items": len(new_items),
    }
