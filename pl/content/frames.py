"""Items are frames x lexemes, resolved against the stored paradigm.

Nine authored frames over 44 lexemes yields several hundred items with no manual
review, because no expected answer is ever *written* — each one is looked up in a
generated paradigm. An item can only be wrong if SGJP is wrong, which is a very
different risk from a reviewer being tired.

Acceptance criterion 16 is asserted here at build time rather than trusted:
every generated item's expected surface must be a real form of its lexeme.
"""

from __future__ import annotations

import random
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from pl import morph
from pl.content.ingest import lexeme_themes, rule_node_map, rule_stratification
from pl.models import Form, Item, Lexeme, Node, Pattern, Sense

DATA = Path(__file__).resolve().parent.parent.parent / "data"

#: Fixed so a rebuild produces the same distractors. Item identity is otherwise
#: unstable across builds, and unstable identity churns nothing at M1 but would
#: churn `item_variant` rows later.
SEED = 20260824


def _frames() -> list[dict]:
    return yaml.safe_load((DATA / "frames.yaml").read_text(encoding="utf-8"))


def _cells(db: Session, lexeme: Lexeme) -> list[Form]:
    return list(db.scalars(select(Form).where(Form.lexeme_id == lexeme.id)))


def _cell(cells: list[Form], case: str, number: str = "sg") -> Form | None:
    """The singular cell whose tag *contains* `case`.

    Containment, not equality: `kota` is tagged `subst:sg:gen.acc:m2` — one
    interpretation covering both cases — so an accusative lookup that tested for
    equality would find nothing for exactly the nouns N05 exists to teach.
    """
    for form in cells:
        parts = form.morph_tag.split(":")
        if len(parts) < 3:
            continue
        if number in parts[1].split(".") and case in parts[2].split("."):
            return form
    return None


def ensure_patterns(db: Session) -> list[Pattern]:
    """Create exactly the strata that some frame can actually populate.

    Derived from frames × lexemes rather than from a node's gender list, because
    a stratum is counted in the unlock gate's denominator whether or not any
    item exercises it. One unpopulatable stratum makes its node permanently
    unmasterable, and everything downstream of that node unreachable — silently,
    because the gate only raises when a node has *no* strata at all.
    """
    rule_cases = rule_stratification()
    themes = lexeme_themes()
    node_of = rule_node_map()
    nodes = {n.key: n for n in db.scalars(select(Node))}
    existing = {
        (p.rule_key, p.paradigm_class): p for p in db.scalars(select(Pattern))
    }
    created: list[Pattern] = []

    for frame in _frames():
        rule = frame.get("rule_key")
        if not rule or frame.get("vocabulary"):
            continue
        admitted = frame.get("themes")
        for lexeme in db.scalars(select(Lexeme)):
            if admitted and themes.get(lexeme.lemma) not in admitted:
                continue
            if _cell(_cells(db, lexeme), frame["case"]) is None:
                continue
            node_key = node_of.get((rule, lexeme.gender))
            if node_key is None:
                continue
            stratum = morph.paradigm_class(
                lexeme.lemma, rule_cases[rule], pos=lexeme.pos
            )
            if (rule, stratum) in existing:
                continue
            pattern = Pattern(
                node_id=nodes[node_key].id, rule_key=rule, paradigm_class=stratum
            )
            db.add(pattern)
            existing[(rule, stratum)] = pattern
            created.append(pattern)

    db.flush()
    return created


def build_items(db: Session) -> list[Item]:
    """Generate every item. Idempotent: existing items are left alone."""
    rng = random.Random(SEED)
    ensure_patterns(db)
    lexemes = list(db.scalars(select(Lexeme)))
    patterns = {
        (p.rule_key, p.paradigm_class): p for p in db.scalars(select(Pattern))
    }
    nodes = {n.key: n for n in db.scalars(select(Node))}
    glosses = {
        s.lexeme_id: s.en_gloss for s in db.scalars(select(Sense))
    }
    # Item identity includes the exercise type. Without it a multiple-choice
    # frame sharing a template and case with a cloze frame — which is exactly
    # what a form-selection MCQ is — collides with it and is dropped.
    rule_cases = rule_stratification()
    themes = lexeme_themes()

    preexisting = {
        (i.exercise_type, i.prompt, i.expected_answer)
        for i in db.scalars(select(Item))
    }
    added: dict[tuple[str, str, str], str] = {}
    created: list[Item] = []

    for frame in _frames():
        for lexeme in lexemes:
            cells = _cells(db, lexeme)
            gloss = glosses.get(lexeme.id, lexeme.lemma)

            # A frame may only make sense for part of the lexeme set. "Jestem
            # sklepem" inflects correctly and means nothing, and a drill the
            # learner cannot read as a sentence is a worse drill.
            admitted = frame.get("themes")
            if admitted and themes.get(lexeme.lemma) not in admitted:
                continue

            if frame.get("vocabulary"):
                item = _meaning_item(frame, lexeme, cells, gloss, nodes, lexemes,
                                     glosses, rng)
            else:
                item = _form_item(
                    frame, lexeme, cells, gloss, patterns, rng, rule_cases
                )

            if item is None:
                continue

            key = (item.exercise_type, item.prompt, item.expected_answer)
            # Two frames colliding is an authoring bug and must be loud. A
            # collision with a row already in the database is just idempotency,
            # and must stay silent — the two are indistinguishable if you only
            # track one set, which is how an entire frame went missing before.
            if key in added:
                raise AssertionError(
                    f"frame {frame['key']!r} produces an item identical to one "
                    f"from frame {added[key]!r}: {key}. Give one of them a "
                    f"distinct template, case, or exercise type."
                )
            if key in preexisting:
                continue

            added[key] = frame["key"]
            db.add(item)
            created.append(item)

    db.flush()
    _assert_expected_answers_are_real_forms(db, created)
    db.commit()
    return created


def _form_item(frame, lexeme, cells, gloss, patterns, rng, rule_cases) -> Item | None:
    target = _cell(cells, frame["case"])
    if target is None:
        return None

    # The stratum is computed over this rule's cases, not over the lexeme's
    # global class — see ingest.rule_stratification.
    stratum = morph.paradigm_class(
        lexeme.lemma, rule_cases[frame["rule_key"]], pos=lexeme.pos
    )
    pattern = patterns.get((frame["rule_key"], stratum))
    if pattern is None:
        return None

    options = None
    if frame["exercise_type"] == "mcq":
        distractors = []
        for case in frame.get("distractor_cases", []):
            cell = _cell(cells, case)
            if cell is not None and cell.surface != target.surface:
                distractors.append(cell.surface)
        # A stratum whose cells are syncretic yields too few distinct wrong
        # answers to make a choice meaningful; skip rather than pad with
        # repeats. m3 nouns have identical nominative and accusative, so this
        # legitimately drops some of them.
        if len(set(distractors)) < 2:
            return None
        options = sorted({target.surface, *distractors[:3]})
        rng.shuffle(options)

    return Item(
        node_id=pattern.node_id,
        pattern_id=pattern.id,
        exercise_type=frame["exercise_type"],
        prompt=frame["template"],
        gloss=frame["gloss"].format(gloss=gloss),
        expected_answer=target.surface,
        target_form_id=target.id,
        options_json=options,
        source="template",
    )


def _meaning_item(frame, lexeme, cells, gloss, nodes, lexemes, glosses, rng) -> Item | None:
    """Meaning recall, under the vocabulary node — the only M1 item scoring a
    lexical card."""
    target = _cell(cells, "nom")
    if target is None:
        return None

    pool = [
        _nominative(l, glosses)
        for l in lexemes
        if l.id != lexeme.id
    ]
    pool = [p for p in pool if p]
    if len(pool) < frame.get("distractor_lexemes", 3):
        return None
    distractors = rng.sample(pool, frame.get("distractor_lexemes", 3))
    options = sorted({target.surface, *distractors})
    rng.shuffle(options)

    return Item(
        node_id=nodes["V01"].id,
        pattern_id=None,
        exercise_type="mcq",
        prompt=frame["template"].format(gloss=gloss),
        gloss=None,
        expected_answer=target.surface,
        target_form_id=target.id,
        options_json=options,
        source="template",
    )


_NOM_CACHE: dict[int, str] = {}


def _nominative(lexeme: Lexeme, glosses) -> str | None:
    return _NOM_CACHE.get(lexeme.id)


def _prime_nominatives(db: Session) -> None:
    for lexeme in db.scalars(select(Lexeme)):
        cells = list(db.scalars(select(Form).where(Form.lexeme_id == lexeme.id)))
        cell = _cell(cells, "nom")
        if cell is not None:
            _NOM_CACHE[lexeme.id] = cell.surface


def _assert_expected_answers_are_real_forms(db: Session, items: list[Item]) -> None:
    """Acceptance criterion 16.

    Content is regenerable, so this runs on every build rather than once. An item
    whose expected answer is not a cell of its lexeme could never be answered
    correctly, and would present to the learner as the grader being broken.
    """
    for item in items:
        if item.target_form_id is None:
            raise AssertionError(f"item {item.prompt!r} has no target form")
        form = db.get(Form, item.target_form_id)
        if form is None or form.surface != item.expected_answer:
            raise AssertionError(
                f"item {item.prompt!r} expects {item.expected_answer!r}, "
                f"which is not the surface of form {item.target_form_id}"
            )


def build(db: Session) -> list[Item]:
    _prime_nominatives(db)
    return build_items(db)
