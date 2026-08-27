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
import re
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from pl import morph
from pl import tags
from pl.content.ingest import (
    lexeme_themes,
    node_key_for,
    rule_nodes,
    rule_stratification,
)
from pl.content.validate import validate
from pl.domain import MULTI_SLOT
from pl.grade.classify import normalise, tokenise
from pl.models import Form, Item, ItemSlot, Lexeme, Node, Pattern, Sense

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


def _match(cells: list[Form], spec: dict) -> Form | None:
    """The stored cell matching every feature in `spec`.

    Values are tested against the tag's value *sets*, the same containment the
    accusative depends on. Used where a frame names a cell by features rather
    than by case — an aspect frame wants `praet:sg:m1`, which has no case at all.
    """
    for form in cells:
        tag = tags.parse(form.morph_tag)
        if spec.get("pos") and tag.pos != spec["pos"]:
            continue
        if all(tag.has(k, v) for k, v in spec.items() if k != "pos"):
            return form
    return None


def _frame_target(cells: list[Form], frame: dict) -> Form | None:
    """The cell a frame asks for, however it asks."""
    if "cell" in frame:
        return _match(cells, frame["cell"])
    return _cell(cells, frame["case"])


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
    node_of = rule_nodes()
    nodes = {n.key: n for n in db.scalars(select(Node))}
    existing = {
        (p.rule_key, p.paradigm_class): p for p in db.scalars(select(Pattern))
    }
    created: list[Pattern] = []
    #: Every stratum the *current* stratification yields, whether or not a row
    #: for it already exists. Compared against the database at the end: seeding
    #: this from `existing` instead would make the comparison vacuous, since
    #: every stored row is in `existing` by construction.
    producible: set[tuple[str, str]] = set()

    for frame in _frames():
        rule = frame.get("rule_key")
        if not rule or frame.get("vocabulary"):
            continue
        admitted = frame.get("themes")
        for lexeme in db.scalars(select(Lexeme)):
            if admitted and not themes.get(lexeme.lemma, set()).intersection(admitted):
                continue
            if _frame_target(_cells(db, lexeme), frame) is None:
                continue
            if frame.get("aspect") and lexeme.aspect != frame["aspect"]:
                continue
            node_key = node_key_for(rule, lexeme, node_of)
            if node_key is None:
                continue
            stratum = morph.paradigm_class(
                lexeme.lemma, rule_cases[rule], pos=lexeme.pos
            )
            producible.add((rule, stratum))
            if (rule, stratum) in existing:
                continue
            pattern = Pattern(
                node_id=nodes[node_key].id, rule_key=rule, paradigm_class=stratum
            )
            db.add(pattern)
            existing[(rule, stratum)] = pattern
            created.append(pattern)

    # Authored sentences reach lexeme/rule combinations no frame covers — a
    # frame restricted by theme leaves gaps a hand-written sentence can fill.
    # They must create their strata too, or the sentence is silently dropped for
    # want of a pattern to hang on.
    node_of_rule = rule_nodes()
    for entry in yaml.safe_load((DATA / "sentences.yaml").read_text(encoding="utf-8")):
        rule = entry["rule_key"]
        lexeme = db.scalar(select(Lexeme).where(Lexeme.lemma == entry["lemma"]))
        if lexeme is None:
            raise LookupError(
                f"sentence {entry['text']!r} names lexeme {entry['lemma']!r}, "
                f"which is not in the lexeme set"
            )
        node_key = node_key_for(rule, lexeme, node_of_rule)
        if node_key is None:
            raise LookupError(
                f"sentence {entry['text']!r} uses rule {rule!r}, which claims no "
                f"node for a {lexeme.gender or lexeme.pos} lexeme"
            )
        stratum = morph.paradigm_class(
            lexeme.lemma, rule_cases[rule], pos=lexeme.pos
        )
        producible.add((rule, stratum))
        if (rule, stratum) in existing:
            continue
        pattern = Pattern(
            node_id=nodes[node_key].id, rule_key=rule, paradigm_class=stratum
        )
        db.add(pattern)
        existing[(rule, stratum)] = pattern
        created.append(pattern)

    db.flush()
    _assert_no_orphaned_strata(db, producible)
    return created


def _assert_no_orphaned_strata(
    db: Session, producible: set[tuple[str, str]]
) -> None:
    """Refuse a build that has re-partitioned an existing database's strata.

    `paradigm_class` is derived from a rule's `stratify_cases`, so editing that
    list renames every stratum it produces. On a fresh database that is simply
    the new partition. On one that already has content it is a migration: the
    old `Pattern` rows stay, still carrying items and still counted in their
    nodes' unlock denominators, where no newly-built item can ever satisfy them.

    That is exactly the permanently-unmasterable node this function was written
    to prevent, arriving on the *second* build instead of the first — and
    silently, because every individual row is well-formed.

    Reconciling automatically would mean repointing items at the new stratum and
    deciding what becomes of the cards scheduled against the old one. That is a
    judgement about the learner's history, so it is refused rather than guessed.
    """
    orphaned = sorted(
        (p.rule_key, p.paradigm_class)
        for p in db.scalars(select(Pattern))
        if (p.rule_key, p.paradigm_class) not in producible
    )
    if orphaned:
        raise AssertionError(
            f"the stratification has changed: {len(orphaned)} pattern(s) in the "
            f"database can no longer be produced by any lexeme — {orphaned}. "
            f"Their items are orphaned and their strata still count towards "
            f"unlocking, so they can never be mastered. Rebuild into a fresh "
            f"database, or migrate the affected items and cards deliberately."
        )


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
    noms = _noun_nominatives(db)

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
            if admitted and not themes.get(lexeme.lemma, set()).intersection(admitted):
                continue

            if frame.get("vocabulary"):
                item = _meaning_item(frame, lexeme, cells, gloss, nodes, noms, rng)
            elif frame["exercise_type"] == "free_translation":
                item = _free_item(frame, lexeme, cells, gloss, patterns, rule_cases)
            elif frame["exercise_type"] == "aspect_choice":
                item = _aspect_item(
                    db, frame, lexeme, cells, gloss, patterns, rule_cases
                )
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
    _build_slots(db, created)
    _assert_expected_answers_are_real_forms(db, created)
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


def _free_item(frame, lexeme, cells, gloss, patterns, rule_cases) -> Item | None:
    """A whole sentence the learner types out, one slot per token."""
    target = _cell(cells, frame["case"])
    if target is None:
        return None
    stratum = morph.paradigm_class(
        lexeme.lemma, rule_cases[frame["rule_key"]], pos=lexeme.pos
    )
    pattern = patterns.get((frame["rule_key"], stratum))
    if pattern is None:
        return None

    sentence = frame["sentence"].format(form=target.surface)
    return Item(
        node_id=pattern.node_id,
        pattern_id=pattern.id,
        exercise_type=frame["exercise_type"],
        prompt=frame["template"],
        gloss=frame["gloss"].format(gloss=gloss),
        expected_answer=sentence,
        target_form_id=target.id,
        options_json=None,
        source="template",
    )


def _build_slots(db: Session, items: list[Item]) -> None:
    """Give every multi-slot item one row per token position.

    Slots are derived from the frame's own components rather than by analysing
    the finished sentence: the analyser returns a lattice, and picking a path
    through it to decide what *we* meant would be inventing an answer we already
    know.
    """
    for item in items:
        if item.exercise_type not in MULTI_SLOT:
            continue
        target = db.get(Form, item.target_form_id)
        for index, token in enumerate(tokenise(item.expected_answer)):
            db.add(
                ItemSlot(
                    item_id=item.id,
                    slot_index=index,
                    expected_surface=token,
                    target_form_id=(
                        target.id if token == target.surface.casefold() else None
                    ),
                )
            )
    db.flush()


def _aspect_item(db, frame, lexeme, cells, gloss, patterns, rule_cases) -> Item | None:
    """Choose between a verb and its aspect partner, in one fixed cell.

    Both options are the *same* cell of the two partners, so the only thing the
    learner decides is aspect — not tense, not person, and not a form they have
    to build. The context word in the prompt carries the entire decision, which
    is what makes this the specification's highest-value grammar exercise.
    """
    if lexeme.aspect != frame["aspect"] or lexeme.aspect_partner_id is None:
        return None
    target = _match(cells, frame["cell"])
    partner_cells = list(
        db.scalars(select(Form).where(Form.lexeme_id == lexeme.aspect_partner_id))
    )
    alternative = _match(partner_cells, frame["cell"])
    if target is None or alternative is None:
        return None
    if target.surface == alternative.surface:
        return None  # nothing to choose between

    stratum = morph.paradigm_class(
        lexeme.lemma, rule_cases[frame["rule_key"]], pos=lexeme.pos
    )
    pattern = patterns.get((frame["rule_key"], stratum))
    if pattern is None:
        return None

    options = sorted({target.surface, alternative.surface})
    return Item(
        node_id=pattern.node_id,
        pattern_id=pattern.id,
        exercise_type=frame["exercise_type"],
        prompt=frame["template"],
        gloss=frame["gloss"].format(gloss=gloss.replace("to ", "").split(" (")[0]),
        expected_answer=target.surface,
        target_form_id=target.id,
        options_json=options,
        source="template",
    )


def _meaning_item(frame, lexeme, cells, gloss, nodes, noms, rng) -> Item | None:
    """Meaning recall, under the vocabulary node — the only M1 item scoring a
    lexical card."""
    # Nouns only, for the answer as well as the distractors. A verb's paradigm
    # contains participles and a participle carries case, so a plain nominative
    # lookup builds "which word means to read? — czytająca" and calls it
    # vocabulary.
    target = _match(cells, {"pos": "subst", "number": "sg", "case": "nom"})
    if target is None:
        return None

    pool = [surface for lex_id, surface in noms.items() if lex_id != lexeme.id]
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


def _noun_nominatives(db: Session) -> dict[int, str]:
    """Nominative singulars, for vocabulary distractors — **nouns only**.

    A verb's paradigm contains participles, and a participle carries case, so a
    plain nominative lookup over every lexeme happily returns `nieprzeczytana`
    and offers it as a candidate answer to "which word means coffee?". The
    distractor pool has to be drawn from the part of speech the question asks
    about.

    Computed per build rather than cached at module level. The cache this
    replaces was keyed on lexeme id and never cleared, so a second database in
    the same process — every test after the first — drew its distractors from a
    previous database's lexemes.
    """
    out: dict[int, str] = {}
    for lexeme in db.scalars(select(Lexeme).where(Lexeme.pos == "subst")):
        cells = list(db.scalars(select(Form).where(Form.lexeme_id == lexeme.id)))
        cell = _match(cells, {"pos": "subst", "number": "sg", "case": "nom"})
        if cell is not None:
            out[lexeme.id] = cell.surface
    return out


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
        if form is None:
            raise AssertionError(f"item {item.prompt!r} names a missing form")

        if item.exercise_type in MULTI_SLOT:
            # The answer is a sentence, so the check is that the inflected
            # target genuinely occurs in it as a whole token — not that the
            # whole answer is one paradigm cell.
            if form.surface.casefold() not in tokenise(item.expected_answer):
                raise AssertionError(
                    f"item {item.gloss!r} expects {item.expected_answer!r}, "
                    f"which does not contain the form {form.surface!r}"
                )
        elif form.surface != item.expected_answer:
            raise AssertionError(
                f"item {item.prompt!r} expects {item.expected_answer!r}, "
                f"which is not the surface of form {item.target_form_id}"
            )


def _allowed_lemmas() -> set[str]:
    """The curriculum's lexemes plus the function words a sentence cannot avoid."""
    lexemes = {
        entry["lemma"].split(":", 1)[0]
        for entry in yaml.safe_load(
            (DATA / "lexemes.yaml").read_text(encoding="utf-8")
        )
    }
    function_words = set(
        yaml.safe_load((DATA / "function_words.yaml").read_text(encoding="utf-8"))
    )
    return lexemes | function_words


def _sentences() -> list[dict]:
    """The authored corpus. Separate so a test can substitute one."""
    return yaml.safe_load((DATA / "sentences.yaml").read_text(encoding="utf-8"))


def _blank(text: str, target: str) -> str:
    """Replace the target **token** with a blank.

    A plain `str.replace` was wrong twice over. It is case-sensitive, and targets
    are stored as paradigm cells — which are lower-case — so a sentence-initial
    target matched nothing and the sentence was returned verbatim, showing the
    learner the answer. And it matches substrings, so blanking `ma` in `Mama ma
    kota.` produced `M___ma ma kota.`

    Raising on a miss is the point: the failure mode being closed is a prompt
    that silently contains its own answer, so there is no safe fall-through.
    """
    wanted = normalise(target)
    out: list[str] = []
    blanked = False
    for piece in re.split(r"(\w+)", text, flags=re.UNICODE):
        if not blanked and normalise(piece) == wanted and piece.strip():
            out.append("___")
            blanked = True
        else:
            out.append(piece)
    if not blanked:
        raise AssertionError(
            f"target {target!r} does not occur as a whole token in {text!r}, "
            f"so the prompt would have been shown with the answer still in it"
        )
    return "".join(out)


def build_sentence_items(db: Session) -> list[Item]:
    """Authored sentences become contextual cloze items — after validation.

    This is the specification's pipeline with the generation stage run offline
    and committed rather than called at build time. Everything downstream is
    unchanged, which is what `item.source` exists to make true: a sentence that
    arrived from a language model and one that was written by hand are graded,
    scheduled and remediated identically.

    Validation is a **build error**, not a warning. A sentence containing a word
    the learner has never met is not a slightly worse exercise — the error it
    provokes is routed to the grammar node it was supposed to teach, so the
    remediation loop learns the wrong lesson from it.
    """
    allowed = _allowed_lemmas()
    rule_cases = rule_stratification()
    patterns = {
        (p.rule_key, p.paradigm_class): p for p in db.scalars(select(Pattern))
    }
    existing = {
        (i.exercise_type, i.prompt, i.expected_answer)
        for i in db.scalars(select(Item))
    }
    created: list[Item] = []
    #: Accepted sentences, each mapped to the target it drills — the duplicate
    #: check needs both to tell a paradigm drill from a clone.
    seen: dict[str, str] = {}

    for entry in _sentences():
        problems = validate(
            entry["text"],
            target=entry["target"],
            allowed=allowed,
            corpus=seen,
        )
        if problems:
            raise AssertionError(
                f"sentence {entry['text']!r} rejected: "
                f"{[str(p) for p in problems]}"
            )
        seen[entry["text"]] = entry["target"]

        lexeme = db.scalar(select(Lexeme).where(Lexeme.lemma == entry["lemma"]))
        if lexeme is None:
            raise LookupError(f"{entry['lemma']!r} is not in the lexeme set")
        target = _cell(_cells(db, lexeme), entry["case"])
        if target is None or target.surface != entry["target"]:
            raise AssertionError(
                f"sentence {entry['text']!r} declares target {entry['target']!r}, "
                f"but the {entry['case']} of {entry['lemma']} is "
                f"{target.surface if target else None!r}"
            )

        stratum = morph.paradigm_class(
            lexeme.lemma, rule_cases[entry["rule_key"]], pos=lexeme.pos
        )
        pattern = patterns.get((entry["rule_key"], stratum))
        if pattern is None:
            # ensure_patterns walks these same sentences, so a missing pattern
            # means the two disagree — which is a bug, not a sentence to skip.
            raise AssertionError(
                f"sentence {entry['text']!r} has no pattern for "
                f"({entry['rule_key']}, {stratum})"
            )

        prompt = _blank(entry["text"], entry["target"])
        key = ("cloze", prompt, target.surface)
        if key in existing:
            continue
        existing.add(key)

        item = Item(
            node_id=pattern.node_id,
            pattern_id=pattern.id,
            exercise_type="cloze",
            prompt=prompt,
            gloss=entry["gloss"],
            expected_answer=target.surface,
            target_form_id=target.id,
            options_json=None,
            # The seam the design declared for exactly this: content that came
            # from the generation pipeline rather than from a frame.
            source="generated",
        )
        db.add(item)
        created.append(item)

        # The same sentence, heard rather than read. Dictation is the only
        # exercise where spelling is the lesson, and it needs no extra content:
        # the sentence is already validated and already sliced into slots.
        dictation_key = ("listening_dictation", "", entry["text"])
        if dictation_key not in existing:
            existing.add(dictation_key)
            dictation = Item(
                node_id=pattern.node_id,
                pattern_id=pattern.id,
                exercise_type="listening_dictation",
                prompt="",
                gloss="Listen, and write what you hear.",
                expected_answer=entry["text"],
                target_form_id=target.id,
                options_json=None,
                source="generated",
            )
            db.add(dictation)
            created.append(dictation)

    db.flush()
    _build_slots(db, created)
    # The same criterion-16 assertion `build_items` makes. Redundant today —
    # the checks above already imply it for both shapes this builder emits —
    # but the guarantee must not depend on which builder produced a row, or a
    # later change to either path drops it silently.
    _assert_expected_answers_are_real_forms(db, created)
    return created


def build(db: Session) -> list[Item]:
    """The whole build, or none of it.

    Neither half commits. `build_items` used to, so a corpus error raised inside
    `build_sentence_items` left a database holding template items, the strata
    `ensure_patterns` had already created for sentences that were then rejected,
    and no sentence items — a state indistinguishable downstream from a build
    that had simply finished. The rollback is the caller's, and a caller that
    lets the exception escape gets one from the session's own context.
    """
    items = build_items(db) + build_sentence_items(db)
    db.commit()
    return items
