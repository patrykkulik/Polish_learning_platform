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
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pl import morph
from pl.models import (
    AppUser,
    Form,
    Item,
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


def rule_stratification() -> dict[str, tuple[str, ...]]:
    """The cases each rule's strata are computed over.

    A stratum exists to make difficulty homogeneous **for one rule**, so the
    cases it is computed over must be that rule's own. One global key is wrong
    in both directions at once: too coarse for the locative, where `sklep → w
    sklepie` and `dom → w domu` are different things to learn, and far too fine
    for the accusative, where folding the locative in would split seven strata
    into twenty-seven.

    The second half matters more than the first. Strata are content, and the
    unlock gate counts them — so re-partitioning a rule the learner has already
    mastered silently revokes a node they earned. Keying each rule to its own
    cases means adding a case to the curriculum cannot disturb any rule that
    does not teach it.
    """
    out: dict[str, tuple[str, ...]] = {}
    for entry in _load("nodes.yaml"):
        rule = entry.get("rule_key")
        if rule:
            out[rule] = tuple(entry.get("stratify_cases", STRATIFICATION_CASES))
    return out


def rule_nodes() -> dict[str, list[tuple[dict, str]]]:
    """rule_key -> [(match spec, node key)].

    One rule can span several nodes: the accusative splits three ways because
    the ending depends on gender and animacy, and all three share
    `ACC_AFTER_TRANSITIVE_VERB`. A node selects its share by gender, or by part
    of speech where gender is not a property the lexeme has — aspect is a verb
    distinction and verbs are not gendered in the infinitive.
    """
    out: dict[str, list[tuple[dict, str]]] = {}
    for entry in _load("nodes.yaml"):
        rule = entry.get("rule_key")
        if not rule:
            continue
        spec = {k: entry[k] for k in ("genders", "pos") if k in entry}
        out.setdefault(rule, []).append((spec, entry["key"]))
    return out


def node_key_for(rule: str, lexeme, mapping: dict) -> str | None:
    """Which node of `rule` claims `lexeme`, if any."""
    for spec, key in mapping.get(rule, []):
        if "pos" in spec and lexeme.pos != spec["pos"]:
            continue
        if "genders" in spec and lexeme.gender not in spec["genders"]:
            continue
        return key
    return None


def lexeme_themes() -> dict[str, set[str]]:
    """lemma -> its themes, for frames that only make sense with some of the set.

    A *set*, because a noun can honestly belong to more than one. `dom` is a
    venue you go to and a home you own; with one theme apiece, gating "Mam ___"
    away from venues to stop `Mam kino` also stops `Mam dom`, and gating it the
    other way admits both. Single-theme lexemes stay written as `theme:`.
    """
    themes: dict[str, set[str]] = {}
    for entry in _load("lexemes.yaml"):
        listed = entry.get("themes") or [entry.get("theme", "")]
        themes[entry["lemma"]] = {t for t in listed if t}
    return themes


def _upsert_forms(db: Session, lexeme: Lexeme, cells) -> None:
    """Store the paradigm, keyed on the cell the design's unique constraint names.

    Only inflected cells are stored. The peripheral entries `generate` attaches
    to a lemma — abbreviations, participial adverbs — are not inflections of it
    and no curriculum cell is ever drawn from one.
    """
    existing = {
        row.morph_tag: row
        for row in db.scalars(select(Form).where(Form.lexeme_id == lexeme.id))
    }
    wanted = {cell.tag.raw for cell in cells}
    for cell in cells:
        row = existing.get(cell.tag.raw)
        if row is None:
            existing[cell.tag.raw] = Form(
                lexeme_id=lexeme.id, surface=cell.surface, morph_tag=cell.tag.raw
            )
            db.add(existing[cell.tag.raw])
        else:
            row.surface = cell.surface

    # An upsert that only ever adds leaves cells the generator has stopped
    # producing — after a dictionary update, or a change to what counts as
    # inflectional — sitting in the paradigm the grader reads. A wrong answer
    # then matches a form of the right lexeme and is graded as merely the wrong
    # cell, which is the one diagnosis it cannot be.
    #
    # An item pointing at such a cell is a different problem: deleting the row
    # would orphan the item, so that is reported rather than resolved. Silently
    # keeping it is what this exists to stop.
    for tag, row in existing.items():
        if tag in wanted:
            continue
        referenced = db.scalar(
            select(func.count()).select_from(Item).where(Item.target_form_id == row.id)
        )
        if referenced:
            raise AssertionError(
                f"{lexeme.lemma!r} no longer has a {tag!r} cell, but {referenced} "
                f"item(s) still expect it. Rebuild the items for this lexeme."
            )
        db.delete(row)


def _upsert_sense(db: Session, lexeme: Lexeme, gloss: str) -> None:
    sense = db.scalar(select(Sense).where(Sense.lexeme_id == lexeme.id))
    if sense is None:
        db.add(Sense(lexeme_id=lexeme.id, en_gloss=gloss))
    else:
        sense.en_gloss = gloss


def ingest_lexemes(db: Session) -> dict[str, Lexeme]:
    """Create *or update* lexemes, their full paradigms, and their glosses.

    An upsert, not an insert-if-absent. `paradigm_class` is derived from
    STRATIFICATION_CASES, so extending the curriculum re-partitions every
    stratum — and an ingest that short-circuits on a lemma it has seen before
    leaves stale classes in place, builds new strata from them, and reports a
    successful build either way.
    """
    out: dict[str, Lexeme] = {}
    for rank, entry in enumerate(_load("lexemes.yaml"), start=1):
        lemma = entry["lemma"]
        paradigm = morph.forms(lemma)
        if not paradigm:
            raise LookupError(f"{lemma!r} generated no forms")

        pos = entry.get("pos", "subst")
        cells = [f for f in paradigm if f.tag.pos not in morph.NON_INFLECTIONAL]
        headwords = [f for f in paradigm if f.tag.pos == pos]
        if not headwords:
            raise LookupError(f"{lemma!r} generated no {pos!r} forms")

        gender = sorted(headwords[0].tag.gender)[0] if headwords[0].tag.gender else None
        aspect = sorted(headwords[0].tag.values("aspect"))
        stratify_over = entry.get("stratify_cases", STRATIFICATION_CASES)

        lexeme = db.scalar(select(Lexeme).where(Lexeme.lemma == lemma))
        if lexeme is None:
            lexeme = Lexeme(lemma=lemma)
            db.add(lexeme)
        lexeme.pos = pos
        lexeme.gender = gender
        lexeme.animacy = _ANIMACY.get(gender) if gender else None
        lexeme.aspect = aspect[0] if aspect else None
        lexeme.paradigm_class = morph.paradigm_class(
            lemma, tuple(stratify_over), pos=pos
        )
        lexeme.frequency_rank = rank
        db.flush()

        _upsert_forms(db, lexeme, cells)
        _upsert_sense(db, lexeme, entry["gloss"])
        out[lemma] = lexeme

    _link_aspect_partners(db, out)
    _assert_nothing_was_withdrawn(db, out)
    db.flush()
    return out


def _assert_nothing_was_withdrawn(db: Session, current: dict[str, Lexeme]) -> None:
    """Report lexemes the database holds and the curriculum no longer declares.

    Deliberately not a deletion. A lexeme owns forms, forms are named by items,
    and items carry the learner's cards and attempt history — so removing it
    quietly discards review history for a decision that was only meant to change
    what gets taught next. Keeping it quietly is worse than either: its items
    stay offerable, and its strata stay in the unlock denominators of nodes it
    was withdrawn from, where nothing can now satisfy them.

    So it is neither, and the operator decides.
    """
    stale = sorted(
        row.lemma
        for row in db.scalars(select(Lexeme))
        if row.lemma not in current
    )
    if stale:
        raise AssertionError(
            f"{len(stale)} lexeme(s) withdrawn from the curriculum are still in "
            f"the database: {stale}. Their items remain offerable and their "
            f"strata still count towards unlocking. Remove them and the cards "
            f"that depend on them deliberately, or restore the declarations."
        )


def _link_aspect_partners(db: Session, lexemes: dict[str, Lexeme]) -> None:
    """Point each verb at its partner, both ways.

    Aspect is taught from the first verb, never bolted on later, so a verb whose
    partner is absent from the lexeme set is an authoring error rather than
    something to tolerate — the learner would meet an imperfective with no
    perfective to contrast it against.
    """
    # Cleared before relinking, because the declaration can be *removed*. Left
    # as it was, a verb goes on being contrasted against a partner the
    # curriculum no longer pairs it with — and `aspect_partner` is what the
    # ASPECT_WRONG diagnosis reads, so the stale link outlives the decision to
    # drop it in the one place a learner would notice.
    for lexeme in lexemes.values():
        lexeme.aspect_partner_id = None

    for entry in _load("lexemes.yaml"):
        partner_lemma = entry.get("aspect_partner")
        if partner_lemma is None:
            continue
        lexeme = lexemes[entry["lemma"]]
        partner = lexemes.get(partner_lemma)
        if partner is None:
            raise LookupError(
                f"{entry['lemma']!r} names aspect partner {partner_lemma!r}, "
                f"which is not in the lexeme set"
            )
        lexeme.aspect_partner_id = partner.id


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
            # Only the parts of speech this node actually has items for. A
            # vocabulary node gates on the senses of its lexemes, so adopting a
            # lexeme no item covers puts an unmasterable referent straight into
            # the denominator — twelve verbs would push V01's 80% out of reach.
            wanted = entry.get("pos", "subst")
            eligible = [lex for lex in lexemes.values() if lex.pos == wanted]

        for lex in eligible:
            if db.get(NodeLexeme, (node.id, lex.id)) is None:
                db.add(NodeLexeme(node_id=node.id, lexeme_id=lex.id))

        # One pattern per (rule, paradigm class) actually present among the
        # node's lexemes. Creating strata that no lexeme populates would put an
        # unreachable card in the unlock gate's denominator.
        # Patterns are *not* created here. A stratum only earns a place in the
        # unlock gate's denominator if some item can exercise it, and whether
        # one can depends on the frames — which additionally restrict by theme.
        # Deriving strata from the node's gender list alone manufactures cards
        # the learner can never be shown, and a node containing one of those can
        # never be mastered. See frames.ensure_patterns.
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


def stale_items(db: Session) -> list:
    """Items in this database that the current data files no longer produce.

    The build is an upsert and never deletes, which is right — an item may
    already carry attempts and error events, and editing a frame is not a reason
    to rewrite what the learner did. But it means *removing* content has no
    effect on a database that already has it: gate a frame away from a theme and
    every sentence it used to make is still sitting there, still being served.

    Detected by building the curriculum from scratch in memory and diffing the
    item keys, so the check runs the real build path rather than a second
    implementation of it that would drift from the first.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from pl import models
    from pl.content import frames

    engine = create_engine("sqlite://", future=True)
    models.Base.metadata.create_all(engine)
    canonical = sessionmaker(bind=engine, future=True, expire_on_commit=False)()
    try:
        ingest_all(canonical)
        frames.build(canonical)
        produced = {
            (i.exercise_type, i.prompt, i.expected_answer)
            for i in canonical.scalars(select(models.Item))
        }
    finally:
        canonical.close()

    return [
        item
        for item in db.scalars(select(models.Item))
        if (item.exercise_type, item.prompt, item.expected_answer) not in produced
    ]


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
        from sqlalchemy import func

        from pl.models import Item, Lexeme, Pattern

        counts = dict(
            session.execute(
                select(Item.source, func.count(Item.id)).group_by(Item.source)
            ).all()
        )
        print(f"curriculum built: {len(items)} new items")
        print(
            f"  lexemes {session.scalar(select(func.count()).select_from(Lexeme))}"
            f"  strata {session.scalar(select(func.count()).select_from(Pattern))}"
            f"  items {session.scalar(select(func.count()).select_from(Item))}"
            f"  (template {counts.get('template', 0)},"
            f" generated {counts.get('generated', 0)})"
        )

        stale = stale_items(session)
        if stale:
            print(
                f"\n  {len(stale)} items are still stored but are no longer produced"
                f" by the current frames and lexemes."
            )
            for item in stale[:5]:
                print(f"    {item.prompt or '(free translation)'} -> {item.expected_answer}")
            if len(stale) > 5:
                print(f"    ... and {len(stale) - 5} more")
            print(
                "  Nothing was deleted: these may carry attempts and error events,\n"
                "  and a content edit is not a reason to rewrite the learner's history.\n"
                "  They will still be served. Delete the database and rebuild to drop\n"
                "  them, or keep them deliberately."
            )
    finally:
        session.close()


if __name__ == "__main__":  # uv run python -m pl.content.ingest
    main()
