"""The six-step error classifier — the subsystem the whole product rests on.

`classify` is a pure function of `(ExpectedSlot, str)`. Steps 1 to 4 read only the
paradigm the slot carries; step 5 is the single point that consults the open
vocabulary, because *is this a form of some other lexeme* cannot be answered from
one lexeme's paradigm.

**Two orderings are load-bearing and neither is arbitrary.**

Step 2 precedes step 3, so a dropped diacritic that lands on another real form of
the same lexeme is diagnosed as the case error it is: `matka` for `matkę` is the
nominative, not a typo. Reverse them and a learner who has not learnt the
accusative is told they are correct.

Step 3 precedes step 5, so a string that is *both* an ASCII fold of the expected
form and a real form of some unrelated lexeme is read as the spelling slip it
almost certainly is. This is not hypothetical: `sie` analyses as a plural
adjective and `robie` as the dative of `roba`, so under the reverse order the
commonest keyboard artefact in the language would be reported as a vocabulary
error.

Step 3 itself is **directional**. An ASCII fold in the learner's typing direction
(`ę` typed as `e`) is orthographic; its reverse (`o` typed as `ó`) never is,
because no keyboard produces `ó` by accident. That asymmetry is what keeps the
`stół → stołu` and `Kraków → Krakowie` vowel alternations — the morphology this
product exists to teach — from being written off as typos.
"""

from __future__ import annotations

from typing import Final

from pl import morph
from pl.domain import Diagnosis, ErrorClass, ExpectedSlot, Form
from pl.tags import MorphTag

#: Polish letters and the ASCII base a learner without a Polish keyboard types
#: instead. Applied in one direction only — see the module docstring.
_ASCII_FOLD: Final[dict[str, str]] = {
    "ą": "a",
    "ć": "c",
    "ę": "e",
    "ł": "l",
    "ń": "n",
    "ó": "o",
    "ś": "s",
    "ź": "z",
    "ż": "z",
}

#: True homophone confusions, applied in both directions. Unlike the folds these
#: encode no grammatical alternation, so confusing them is always a spelling
#: error: `ó` and `u` are both /u/, `ż` and `rz` both /ʐ/.
_HOMOPHONES: Final[tuple[tuple[str, str], ...]] = (
    ("ż", "rz"),
    ("u", "ó"),
    ("h", "ch"),
)

#: Attribute precedence for deciding which difference names the error. Case
#: outranks number, which outranks gender: a learner who chose the wrong case has
#: made a more fundamental mistake than one who agreed the wrong way.
_PRIORITY: Final[tuple[str, ...]] = (
    "case",
    "number",
    "gender",
    "aspect",
    "person",
    "degree",
)

_ATTR_TO_CLASS: Final[dict[str, ErrorClass]] = {
    "case": ErrorClass.CASE_WRONG,
    "number": ErrorClass.NUMBER_WRONG,
    "gender": ErrorClass.GENDER_AGREEMENT,
    "aspect": ErrorClass.ASPECT_WRONG,
}

#: Maximum edits between a non-word and the expected form for the submission to
#: count as an attempt at that cell rather than as noise. Tuned against the
#: golden corpus; it trades CASE_RIGHT_FORM_WRONG against UNANALYSABLE.
MAX_ATTEMPT_DISTANCE: Final = 2


def normalise(text: str) -> str:
    """Collapse whitespace and case. Diacritics are deliberately untouched.

    Whether a diacritic difference is orthographic or morphological depends on
    the paradigm, so it is a question for the classifier and cannot be settled
    here. Folding diacritics at this point would make `matka` compare equal to
    `matkę` and silently pass a case error.
    """
    return " ".join(text.split()).casefold()


def _fold(text: str) -> str:
    return "".join(_ASCII_FOLD.get(ch, ch) for ch in text)


def _is_learner_direction_fold(submitted: str, expected: str) -> bool:
    """True when `submitted` is `expected` with Polish letters typed as ASCII.

    Requires every difference to run Polish-to-ASCII. A single character going
    the other way — `o` typed as `ó` — disqualifies the whole string, because
    that is an assertion about the vowel rather than a missing keystroke.
    """
    if _fold(submitted) != _fold(expected):
        return False
    return all(
        s == e or (e in _ASCII_FOLD and s == _ASCII_FOLD[e])
        for s, e in zip(submitted, expected, strict=True)
    )


def _homophone_variants(text: str, depth: int) -> set[str]:
    """`text` plus everything reachable by at most `depth` homophone swaps."""
    seen = {text}
    frontier = {text}
    for _ in range(depth):
        nxt: set[str] = set()
        for word in frontier:
            for a, b in _HOMOPHONES:
                for src, dst in ((a, b), (b, a)):
                    start = 0
                    while (i := word.find(src, start)) != -1:
                        nxt.add(word[:i] + dst + word[i + len(src) :])
                        start = i + 1
        nxt -= seen
        seen |= nxt
        frontier = nxt
    return seen


def _is_orthographic(submitted: str, expected: str) -> bool:
    """True when the difference is spelling, not grammar."""
    return any(
        _is_learner_direction_fold(variant, expected)
        for variant in _homophone_variants(submitted, depth=2)
    )


def _distance(a: str, b: str) -> int:
    """Levenshtein distance."""
    if a == b:
        return 0
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (ca != cb),
                )
            )
        previous = current
    return previous[-1]


def _first_difference(observed: MorphTag, expected: MorphTag) -> str | None:
    """The highest-precedence attribute on which the two tags fail to intersect."""
    for attr in _PRIORITY:
        if not observed.agrees_on(attr, expected):
            return attr
    return None


def _best_candidate(candidates: list[Form], expected: MorphTag) -> Form:
    """The most charitable reading of an ambiguous surface.

    Ranked by fewest disagreements, then by agreeing on case. The tiebreak
    matters: `nowego` is both an accusative of the wrong gender and a genitive of
    the right one, and crediting the case the learner chose is both kinder and
    more informative than calling it a case error.
    """

    def rank(form: Form) -> tuple[int, int]:
        diffs = sum(
            1 for attr in _PRIORITY if not form.tag.agrees_on(attr, expected)
        )
        case_wrong = 0 if form.tag.agrees_on("case", expected) else 1
        return (diffs, case_wrong)

    return min(candidates, key=rank)


def _is_animacy_error(slot: ExpectedSlot, observed: MorphTag) -> bool:
    """True when a masculine accusative was built from the wrong animacy rule.

    The accusative borrows the genitive for m1 and m2 and the nominative for m3,
    so the same misconception appears with opposite signs: a nominative offered
    for an animate noun, or a genitive offered for an inanimate one.
    """
    if "acc" not in slot.tag.case:
        return False
    animacy = slot.masculine_animacy
    if animacy == "animate":
        return "nom" in observed.case
    if animacy == "inanimate":
        return "gen" in observed.case
    return False


def classify(slot: ExpectedSlot, submitted: str) -> Diagnosis:
    """Diagnose `submitted` against the paradigm cell `slot` requires."""
    text = normalise(submitted)
    expected_surface = normalise(slot.expected.surface)

    def result(
        error_class: ErrorClass,
        observed: Form | None = None,
        attr: str | None = None,
    ) -> Diagnosis:
        return Diagnosis(
            error_class=error_class,
            submitted=submitted,
            slot=slot,
            observed=observed,
            differing_attr=attr,
        )

    # 1 — the expected surface, exactly.
    if text == expected_surface:
        return result(ErrorClass.CORRECT, slot.expected)

    # 2 — another form of the same lexeme. Restricted to the expected part of
    # speech: a post-prepositional adjectival sharing a surface with a nominative
    # is a different kind of thing, not a competing reading of the same cell.
    candidates = [
        f for f in slot.with_surface(text) if f.tag.pos == slot.tag.pos
    ]
    if candidates:
        if any(f.tag.matches(slot.tag) for f in candidates):
            return result(ErrorClass.CORRECT, candidates[0])
        observed = _best_candidate(candidates, slot.tag)
        attr = _first_difference(observed.tag, slot.tag)
        if attr == "case" and _is_animacy_error(slot, observed.tag):
            return result(ErrorClass.ANIMACY, observed, attr)
        if attr is not None:
            return result(
                _ATTR_TO_CLASS.get(attr, ErrorClass.CASE_RIGHT_FORM_WRONG),
                observed,
                attr,
            )
        # A real form of this lexeme, same part of speech, disagreeing on nothing
        # ranked — a different surface for the same cell.
        return result(ErrorClass.CASE_RIGHT_FORM_WRONG, observed)

    # 3 — spelling, in the learner's typing direction only.
    if _is_orthographic(text, expected_surface):
        return result(ErrorClass.ORTHOGRAPHY)

    known = morph.analyses(text)

    # 4 — not a word, but close enough to be an attempt at this cell. The branch
    # the specification omits, and the majority case for a learner producing
    # morphology: a wrong ending yields no analysis at all, so there are no tags
    # to compare and everything downstream has nothing to work with.
    if not known and _distance(text, expected_surface) <= MAX_ATTEMPT_DISTANCE:
        return result(ErrorClass.CASE_RIGHT_FORM_WRONG)

    # 5 — a form of some other lexeme.
    if known:
        if slot.aspect_partner is not None:
            partner = slot.aspect_partner.split(":", 1)[0]
            if any(f.base_lemma == partner for f in known):
                return result(ErrorClass.ASPECT_WRONG, known[0], "aspect")
        return result(ErrorClass.LEXICAL, known[0])

    # 6 — not a word, and too far away to guess at.
    return result(ErrorClass.UNANALYSABLE)
