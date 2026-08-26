"""Render a Diagnosis as a sentence naming the grammatical decision.

Every message is built from the features the classifier compared, never from a
string difference between the two words. `"kot is animate, so its accusative
borrows the genitive: kota"` teaches the rule that generalises; `"expected kota,
got kot"` teaches this one word. The distinction is the entire reason the grader
parses instead of comparing strings, so it has to survive into what the learner
actually reads.
"""

from __future__ import annotations

from typing import Final

from pl.domain import Diagnosis, ErrorClass
from pl.tags import MorphTag

_CASES: Final[dict[str, str]] = {
    "nom": "nominative",
    "gen": "genitive",
    "dat": "dative",
    "acc": "accusative",
    "inst": "instrumental",
    "loc": "locative",
    "voc": "vocative",
}

_NUMBERS: Final[dict[str, str]] = {"sg": "singular", "pl": "plural"}

_GENDERS: Final[dict[str, str]] = {
    "m1": "masculine personal",
    "m2": "masculine animate",
    "m3": "masculine inanimate",
    "f": "feminine",
    "n": "neuter",
}

_ASPECTS: Final[dict[str, str]] = {
    "imperf": "imperfective",
    "perf": "perfective",
}

#: Case order for rendering a syncretic value set, so `gen.acc` always reads
#: "genitive or accusative" rather than in set iteration order.
_CASE_ORDER: Final = ("nom", "gen", "dat", "acc", "inst", "loc", "voc")

#: Last resort. Named so a test can assert no class reaches it — a message that
#: names a form the learner got right is worse than no message.
GENERIC_FALLBACK: Final = "The form needed here is {want}."


def _render(values: frozenset[str], names: dict[str, str], order=None) -> str:
    keys = order or list(names)
    present = [names[v] for v in keys if v in values]
    if not present:
        return "unspecified"
    if len(present) == 1:
        return present[0]
    return " or ".join((", ".join(present[:-1]), present[-1]))


def _case(tag: MorphTag) -> str:
    return _render(tag.case, _CASES, _CASE_ORDER)


def _number(tag: MorphTag) -> str:
    return _render(tag.number, _NUMBERS)


def _gender(tag: MorphTag) -> str:
    return _render(tag.gender, _GENDERS)


def _aspect(tag: MorphTag) -> str:
    return _render(tag.values("aspect"), _ASPECTS)


def explain(diagnosis: Diagnosis) -> str:
    """A learner-facing sentence for `diagnosis`."""
    slot = diagnosis.slot
    want = slot.expected.surface
    lemma = slot.expected.base_lemma
    expected_tag = slot.tag
    observed = diagnosis.observed

    match diagnosis.error_class:
        case ErrorClass.CORRECT:
            return "Correct."

        case ErrorClass.ANIMACY:
            if slot.masculine_animacy == "animate":
                return (
                    f"{lemma} is animate, so its accusative borrows the "
                    f"genitive: {want}."
                )
            return (
                f"{lemma} is inanimate, so its accusative is the same as the "
                f"nominative: {want}."
            )

        case ErrorClass.CASE_WRONG:
            got = _case(observed.tag) if observed else "another case"
            return (
                f"You used the {got}. This slot needs the "
                f"{_case(expected_tag)}: {want}."
            )

        case ErrorClass.CASE_RIGHT_FORM_WRONG:
            return (
                f"The {_case(expected_tag)} was the right choice — but "
                f"{lemma} does not build it that way: {want}."
            )

        case ErrorClass.NUMBER_WRONG:
            got = _number(observed.tag) if observed else "the wrong number"
            return (
                f"You used the {got}. This slot needs the "
                f"{_number(expected_tag)}: {want}."
            )

        case ErrorClass.GENDER_AGREEMENT:
            got = _gender(observed.tag) if observed else "the wrong gender"
            return (
                f"{lemma} has to agree with a {_gender(expected_tag)} noun "
                f"here, not a {got} one: {want}."
            )

        case ErrorClass.ASPECT_WRONG:
            partner = (slot.aspect_partner or "").split(":", 1)[0]
            got = f"{partner} is " if partner else "That is "
            return (
                f"{got}{_aspect(observed.tag) if observed else 'the wrong aspect'}. "
                f"This needs the {_aspect(expected_tag)} {lemma}: {want}."
            )

        case ErrorClass.ORTHOGRAPHY:
            return f"The grammar is right — check the spelling: {want}."

        case ErrorClass.WORD_ORDER:
            return (
                "Every word is right — the order is not. Polish word order is "
                "freer than English, but not free."
            )

        case ErrorClass.MISSING_CONSTITUENT:
            return "Something is missing. Every word you heard has to be there."

        case ErrorClass.LEXICAL:
            other = observed.base_lemma if observed else "another word"
            return f"That is a form of {other}. This slot needs {lemma}: {want}."

        case ErrorClass.UNANALYSABLE:
            return (
                f"That is not a Polish word. The {_case(expected_tag)} "
                f"of {lemma} is {want}."
            )

    return GENERIC_FALLBACK.format(want=want)
