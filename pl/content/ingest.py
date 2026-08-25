"""Load the curriculum: lexemes, their paradigms, the node graph, the strata.

Idempotent — running it twice changes nothing — so the content build can be
re-run after a data edit without dropping learner state. Cards reference `form`,
`sense` and `pattern`, never `item`, so regenerating items never orphans a
schedule.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from pl import morph
from pl.models import (
    AppUser,
    Form,
    Lexeme,
    Node,
    NodeLexeme,
    NodePrereq,
    Pattern,
    Sense,
    Streak,
)

DATA = Path(__file__).resolve().parent.parent.parent / "data"

#: The cases a paradigm class is computed over. M1 teaches the nominative and the
#: accusative, so those are the cells whose endings decide whether two lexemes
#: drill the same thing.
#:
#: Extending the curriculum means recomputing this and re-ingesting: adding the
#: genitive splits `sklep`/`chleb` and `kawa`/`książka`, which are one class here
#: and must not stay one class then. That re-ingest is the known cost of deriving
#: the stratum rather than hand-assigning it, and it is cheap — one function and
#: a rebuild — where a wrong hand-assignment would be invisible.
STRATIFICATION_CASES = ("nom", "acc")

_ANIMACY = {"m1": "animate", "m2": "animate", "m3": "inanimate"}


def _load(name: str):
    return yaml.safe_load((DATA / name).read_text(encoding="utf-8"))


def ingest_lexemes(db: Session) -> dict[str, Lexeme]:
    """Create lexemes, their full paradigms, and their glosses."""
    out: dict[str, Lexeme] = {}
    for rank, entry in enumerate(_load("lexemes.yaml"), start=1):
        lemma = entry["lemma"]
        existing = db.scalar(select(Lexeme).where(Lexeme.lemma == lemma))
        if existing is not None:
            out[lemma] = existing
            continue

        paradigm = morph.forms(lemma)
        if not paradigm:
            raise LookupError(f"{lemma!r} generated no forms")
        nominal = [f for f in paradigm if f.tag.pos == "subst"]
        gender = sorted(nominal[0].tag.gender)[0]

        lexeme = Lexeme(
            lemma=lemma,
            pos="subst",
            gender=gender,
            animacy=_ANIMACY.get(gender),
            paradigm_class=morph.paradigm_class(lemma, STRATIFICATION_CASES),
            frequency_rank=rank,
        )
        db.add(lexeme)
        db.flush()

        # Only nominal cells are stored. The peripheral entries `generate`
        # attaches to a lemma — abbreviations, participial adverbs — are not
        # inflected forms of it and no curriculum cell is ever drawn from one.
        seen: set[str] = set()
        for form in nominal:
            if form.tag.raw in seen:
                continue
            seen.add(form.tag.raw)
            db.add(
                Form(
                    lexeme_id=lexeme.id,
                    surface=form.surface,
                    morph_tag=form.tag.raw,
                )
            )
        db.add(Sense(lexeme_id=lexeme.id, en_gloss=entry["gloss"]))
        out[lemma] = lexeme
    db.flush()
    return out


def ingest_nodes(db: Session, lexemes: dict[str, Lexeme]) -> dict[str, Node]:
    """Create the node graph, its prerequisites, and its pattern strata."""
    entries = _load("nodes.yaml")
    nodes: dict[str, Node] = {}

    for entry in entries:
        node = db.scalar(select(Node).where(Node.key == entry["key"]))
        if node is None:
            node = Node(
                key=entry["key"],
                type=entry["type"],
                title=entry["title"],
                cefr_level=entry.get("cefr_level"),
                explanation_md=entry.get("explanation_md"),
            )
            db.add(node)
            db.flush()
        nodes[entry["key"]] = node

    for entry in entries:
        node = nodes[entry["key"]]
        for prereq in entry.get("prereqs", []):
            link = db.get(NodePrereq, (node.id, nodes[prereq].id))
            if link is None:
                db.add(NodePrereq(node_id=node.id, prereq_node_id=nodes[prereq].id))

        genders = entry.get("genders")
        rule_key = entry.get("rule_key")

        # A grammar node's lexemes are those whose gender it covers; a vocabulary
        # node owns the whole set.
        eligible = [
            lex
            for lex in lexemes.values()
            if genders is None or lex.gender in genders
        ]
        if entry["type"] == "vocabulary":
            eligible = list(lexemes.values())

        for lex in eligible:
            if db.get(NodeLexeme, (node.id, lex.id)) is None:
                db.add(NodeLexeme(node_id=node.id, lexeme_id=lex.id))

        # One pattern per (rule, paradigm class) actually present among the
        # node's lexemes. Creating strata that no lexeme populates would put an
        # unreachable card in the unlock gate's denominator.
        if rule_key and genders:
            for cls in sorted({lex.paradigm_class for lex in eligible}):
                exists = db.scalar(
                    select(Pattern).where(
                        Pattern.rule_key == rule_key,
                        Pattern.paradigm_class == cls,
                    )
                )
                if exists is None:
                    db.add(
                        Pattern(node_id=node.id, rule_key=rule_key, paradigm_class=cls)
                    )
    db.flush()
    return nodes


def ensure_user(db: Session, email: str | None = None) -> AppUser:
    """The single path-A learner. `user_id` is threaded from the first row."""
    user = db.scalar(select(AppUser).limit(1))
    if user is None:
        user = AppUser(
            email=email,
            created_at=datetime.now(UTC),
            settings_json={"tz": "Europe/London", "daily_goal_items": 20},
        )
        db.add(user)
        db.flush()
        db.add(Streak(user_id=user.id, freezes=2))
        db.flush()
    return user


def ingest_all(db: Session) -> dict[str, Node]:
    lexemes = ingest_lexemes(db)
    nodes = ingest_nodes(db, lexemes)
    ensure_user(db)
    db.commit()
    return nodes


def main() -> None:
    """Build the whole curriculum into the configured database.

    Idempotent, so it is safe to re-run after editing any data file.
    """
    from pl import db as database
    from pl.content import frames

    database.create_all()
    session = database.session()
    try:
        ingest_all(session)
        items = frames.build(session)
        print(f"curriculum built: {len(items)} new items")
    finally:
        session.close()


if __name__ == "__main__":  # uv run python -m pl.content.ingest
    main()
