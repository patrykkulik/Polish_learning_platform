"""What the learner reads, and the gate that makes them read it.

A *concept* is the teaching unit, and it is deliberately not a node: the genitive
is three nodes and one idea, and the case system spans every node. A concept
names the node that introduces it and the nodes that also teach it, so `N09` and
`N10` point at the genitive `N08` introduced rather than explaining it again.

Prose is authored in `data/concepts.yaml`. The endings are not: a table names a
lexeme and the cases to show, and the surfaces are read out of the stored
paradigm — the guarantee acceptance criterion 16 gives every item, extended to
the pages that explain them. It also makes the tables honest about syncretism:
`kota` is one `subst:sg:gen.acc:m2` cell, so it appears in both rows, which is
what "the accusative borrows the genitive" means.

Only the *reading* is state. Concepts themselves are content, and content is
regenerable — the same reason a card references `form`, `sense` and `pattern` and
never `item`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from pl import tags
from pl.models import AppUser, ConceptRead, Form, Lexeme, Node, NodePrereq, NodeUnlock

DATA = Path(__file__).resolve().parent.parent / "data"


#: The parsed file and the modification time it was parsed at.
_CACHE: dict = {}


def _load() -> dict:
    """The authored concepts, parsed once per edit of the file.

    A single session request reached this up to six times — once to gate, once
    to choose a lesson, and once per table rendered — each a full pure-Python
    YAML parse. Keyed on the file's modification time, so an author's edit is
    still picked up without a restart; callers must not mutate what it returns.
    """
    path = DATA / "concepts.yaml"
    stamp = path.stat().st_mtime_ns
    if _CACHE.get("stamp") != stamp:
        _CACHE["data"] = yaml.safe_load(path.read_text(encoding="utf-8"))
        _CACHE["stamp"] = stamp
    return _CACHE["data"]


def cases() -> dict[str, dict]:
    """Case metadata: the Polish name, and the question that finds it."""
    return _load()["cases"]


def all_concepts() -> list[dict]:
    """Every concept, in the order the file declares them."""
    return _load()["concepts"]


def by_key(key: str) -> dict | None:
    return next((c for c in all_concepts() if c["key"] == key), None)


def concept_for_node(node_key: str) -> dict | None:
    """The concept a node needs read before it may introduce anything.

    A node that introduces a concept needs that one; a node that merely teaches
    it again needs the same one, which by then is read. A node named by neither —
    `N06` composes what its prerequisites taught — needs nothing.
    """
    for concept in all_concepts():
        if node_key == concept["introduced_by"]:
            return concept
        if node_key in concept.get("also_taught_in", []):
            return concept
    return None


# ------------------------------------------------------------------- reading


def read_keys(db: Session, user_id: int) -> set[str]:
    return set(
        db.scalars(select(ConceptRead.concept_key).where(ConceptRead.user_id == user_id))
    )


def mark_read(db: Session, user_id: int, key: str) -> None:
    """Idempotent: a learner who presses the button twice has read it once."""
    if db.get(ConceptRead, (user_id, key)) is not None:
        return
    db.add(
        ConceptRead(
            user_id=user_id,
            concept_key=key,
            read_at=datetime.now(UTC).replace(tzinfo=None),
        )
    )
    db.commit()


def is_open(db: Session, user_id: int, concept: dict) -> bool:
    """A concept opens when the node that introduces it does."""
    from pl.session import is_unlocked

    node = db.scalar(select(Node).where(Node.key == concept["introduced_by"]))
    return node is not None and is_unlocked(db, user_id, node)


def gated_node_ids(db: Session, user_id: int) -> set[int]:
    """Nodes that may introduce nothing yet, because their concept is unread.

    Read by the introduction segment and by nothing else. Debt and remediation
    never consult it: a learner who leaves a lesson unread loses new material
    from that node and keeps everything they have already met, which is the
    difference between a gate and a punishment.
    """
    read = read_keys(db, user_id)
    gated: set[int] = set()
    for concept in all_concepts():
        if concept["key"] in read:
            continue
        keys = [concept["introduced_by"], *concept.get("also_taught_in", [])]
        for node_id in db.scalars(select(Node.id).where(Node.key.in_(keys))):
            gated.add(node_id)
    return gated


def lesson_for(db: Session, user_id: int) -> dict | None:
    """The lesson this session should open with, if any.

    The first unread concept whose node is open, in node order. One at a time,
    not one a day: acknowledging re-fetches the session, which offers the next
    unread concept until none remain, so a learner returning to three unlocked
    concepts reads them in order in one sitting. `journey_sim` reads them the
    same way, or the instrument and the product would diverge the first time two
    concepts open together.
    """
    read = read_keys(db, user_id)
    order = {node.key: node.id for node in db.scalars(select(Node))}
    candidates = [
        concept
        for concept in all_concepts()
        if concept["key"] not in read and is_open(db, user_id, concept)
    ]
    candidates.sort(key=lambda c: order.get(c["introduced_by"], 0))
    return render(db, candidates[0]) if candidates else None


# ------------------------------------------------------------------ rendering


def _surfaces(db: Session, lexeme: Lexeme, case: str, number: str) -> list[str]:
    """Every stored surface of `lexeme` whose tag contains `case`.

    Containment, not equality, for the same reason `frames._cell` uses it: `kota`
    is tagged `gen.acc`, one interpretation covering two cases. Equality would
    empty the accusative row for exactly the nouns `ANIMACY` exists to teach.
    """
    found: list[str] = []
    for form in db.scalars(select(Form).where(Form.lexeme_id == lexeme.id)):
        tag = tags.parse(form.morph_tag)
        if tag.pos != "subst":
            continue
        if number in tag.number and case in tag.case and form.surface not in found:
            found.append(form.surface)
    return found


def table(db: Session, spec: dict) -> dict:
    """One declension table, read out of the paradigm.

    Raises if the lexeme or a cell is missing: a table with a hole in it is a
    lesson that teaches the hole.
    """
    lexeme = db.scalar(select(Lexeme).where(Lexeme.lemma == spec["lexeme"]))
    if lexeme is None:
        raise AssertionError(
            f"table names lexeme {spec['lexeme']!r}, which is not in the set"
        )
    meta = cases()
    number = spec.get("number", "sg")
    rows = []
    for case in spec["cases"]:
        surfaces = _surfaces(db, lexeme, case, number)
        if not surfaces:
            raise AssertionError(
                f"{spec['lexeme']!r} has no {number} {case!r} form, so the table "
                f"in this concept cannot be built"
            )
        rows.append(
            {
                "case": case,
                "polish": meta[case]["polish"],
                "english": meta[case]["english"],
                "questions": meta[case]["questions"],
                "surfaces": surfaces,
            }
        )
    return {"lexeme": spec["lexeme"], "caption": spec.get("caption", ""), "rows": rows}


def render(db: Session, concept: dict) -> dict:
    """A concept with its tables resolved — everything a lesson page needs."""
    return {
        "key": concept["key"],
        "title": concept["title"],
        "summary": concept.get("summary", ""),
        "introduced_by": concept["introduced_by"],
        "sections": concept.get("sections", []),
        "tables": [table(db, spec) for spec in concept.get("tables", [])],
    }


def index(db: Session, user_id: int) -> list[dict]:
    """Every concept and its state, for the grammar index.

    A concept the learner has not reached carries its title and nothing else: the
    index shows the shape of the course, and the explanation opens when the
    concept does.
    """
    read = read_keys(db, user_id)
    out = []
    for concept in all_concepts():
        open_ = is_open(db, user_id, concept)
        out.append(
            {
                "key": concept["key"],
                "title": concept["title"],
                "introduced_by": concept["introduced_by"],
                "open": open_,
                "read": concept["key"] in read,
                "summary": concept["summary"] if open_ else "",
            }
        )
    return out


# --------------------------------------------------------------- build checks


def validate(db: Session) -> None:
    """Refuse a concept the learner could not be taught from.

    Runs in the content build, which is the only place that sees both the
    authored file and the built paradigm. Three failures, and the third is
    reported rather than repaired: a reading whose concept no longer exists is
    the learner's history, and deleting it silently is not this function's call —
    the build already refuses a withdrawn lexeme the same way.
    """
    keys = [c["key"] for c in all_concepts()]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    if duplicates:
        raise AssertionError(f"concepts.yaml declares {duplicates} more than once")

    nodes = {node.key: node.id for node in db.scalars(select(Node))}
    keys_by_id = {node_id: key for key, node_id in nodes.items()}
    parents: dict[int, set[int]] = {}
    for link in db.scalars(select(NodePrereq)):
        parents.setdefault(link.node_id, set()).add(link.prereq_node_id)

    def upstream(node_id: int) -> set[str]:
        seen: set[int] = set()
        stack = list(parents.get(node_id, ()))
        while stack:
            current = stack.pop()
            if current not in seen:
                seen.add(current)
                stack.extend(parents.get(current, ()))
        return {keys_by_id[i] for i in seen}

    for concept in all_concepts():
        named = [concept["introduced_by"], *concept.get("also_taught_in", [])]
        missing = [key for key in named if key not in nodes]
        if missing:
            raise AssertionError(
                f"concept {concept['key']!r} names node(s) {missing}, which the "
                f"graph does not contain"
            )
        # A node that teaches a concept again is gated until it is read, but the
        # lesson is only offered once `introduced_by` opens. If the node could
        # open first it would introduce nothing, with no lesson offered and
        # nothing raised — the deadlock `frames.assert_every_stratum_is_reachable`
        # refuses for strata, refused here for the same reason.
        #
        # Upstream is sufficient but not necessary. N04 teaches the accusative
        # N03 introduces, and they are siblings: both need only N01. Mastery is a
        # high-water mark, so every node upstream of N04 is mastered by the time
        # it opens — which means everything N03 needs is too, and the same
        # `evaluate_unlocks` pass latches both. What must hold is that the
        # introducing node needs nothing the teaching node does not already have.
        introducer = nodes[concept["introduced_by"]]
        needs = {keys_by_id[i] for i in parents.get(introducer, ())}
        early = [
            key
            for key in concept.get("also_taught_in", [])
            if concept["introduced_by"] not in (ahead := upstream(nodes[key]))
            and not needs <= ahead
        ]
        if early:
            raise AssertionError(
                f"concept {concept['key']!r} is taught again at {early}, which can "
                f"open before {concept['introduced_by']!r} introduces it — those "
                f"nodes would be gated with no lesson ever offered"
            )
        for spec in concept.get("tables", []):
            table(db, spec)

    stale = sorted(
        key
        for (key,) in db.execute(select(ConceptRead.concept_key).distinct())
        if key not in keys
    )
    if stale:
        print(
            f"  note: {len(stale)} concept reading(s) name a concept that no "
            f"longer exists: {stale}. The lesson will be offered again."
        )


def reconcile(db: Session) -> int:
    """Record what a learner mid-course has already been taught, once each.

    A learner who was answering `N08` items before this existed must not have
    them withheld to read a page about them. Run in the build, on a database that
    already holds their history — never at request time, because
    `evaluate_unlocks` never latches a node without prerequisites, so `V01` has
    no `node_unlock` row and a learner who has just unlocked `N01` is
    indistinguishable from one who has read nothing.

    The stamp is what makes "once" true, and it is not the readings. Guarding on
    "this database holds no readings" puts the same defect back a build later: a
    new learner who has unlocked `N01` and read nothing yet would be reconciled
    on the next rebuild, and `CASES` would be marked read at exactly the moment
    it was written for. Every learner is stamped, whether or not anything was
    recorded for them, so a learner created after this ships is never a candidate
    again.
    """
    stamped = 0
    for user in db.scalars(select(AppUser)):
        settings = dict(user.settings_json or {})
        if settings.get("concepts_reconciled_at"):
            continue

        recorded = 0
        # Read through `is_unlocked`, not off the latch table: a node without
        # prerequisites is open to everyone and is never written there, so V01
        # would be missed and a learner who has met every word in it would be
        # handed its lesson. A learner with no latch rows at all is new — they
        # are stamped and nothing is recorded, so the gate teaches them properly.
        from pl.session import is_unlocked

        mid_course = (
            db.scalar(
                select(NodeUnlock.node_id).where(NodeUnlock.user_id == user.id).limit(1)
            )
            is not None
        )
        if mid_course:
            for concept in all_concepts():
                node = db.scalar(
                    select(Node).where(Node.key == concept["introduced_by"])
                )
                if node is not None and is_unlocked(db, user.id, node):
                    mark_read(db, user.id, concept["key"])
                    recorded += 1

        settings["concepts_reconciled_at"] = (
            datetime.now(UTC).replace(tzinfo=None).isoformat()
        )
        user.settings_json = settings
        db.commit()
        stamped += recorded
    return stamped
