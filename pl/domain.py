"""Frozen value types shared by the grader.

`ExpectedSlot` is the important one. It carries the expected lexeme's **whole
paradigm** alongside the cell under test, which is what lets `pl.grade.classify`
stay a pure function of `(ExpectedSlot, str)`: steps 2 to 4 of the classifier all
read the paradigm, and none of them needs a database or an analyser call. Only
step 5 — *is this a form of some other lexeme* — reaches for the open vocabulary.

The paradigm has two producers and one consumer. At M0 it comes straight from
`pl.morph.forms`; at M1 it comes from a `form` table query. Passing it by value
rather than abstracting over its source keeps the classifier testable from a
plain YAML fixture, which is what makes the M0 gate possible with no schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pl.tags import ANIMATE_MASCULINE, INANIMATE_MASCULINE, MorphTag


class ErrorClass(StrEnum):
    """The diagnosis vocabulary. Each value routes to a different card outcome.

    `CASE_WRONG` and `CASE_RIGHT_FORM_WRONG` are not two shades of the same
    error: the first fails the pattern card and leaves the morphological card
    untouched, the second fails the morphological card while the pattern card
    *passes*, because choosing the genitive and then building it wrong means the
    government rule was applied correctly.
    """

    CORRECT = "CORRECT"
    CASE_WRONG = "CASE_WRONG"
    CASE_RIGHT_FORM_WRONG = "CASE_RIGHT_FORM_WRONG"
    GENDER_AGREEMENT = "GENDER_AGREEMENT"
    NUMBER_WRONG = "NUMBER_WRONG"
    ASPECT_WRONG = "ASPECT_WRONG"
    ANIMACY = "ANIMACY"
    ORTHOGRAPHY = "ORTHOGRAPHY"
    LEXICAL = "LEXICAL"
    #: Terminal class. The submission is not a word and is too far from the
    #: expected form to diagnose. Absent from the specification's taxonomy, which
    #: has no terminal case — without one the classifier is forced to assert a
    #: label for input it does not understand.
    UNANALYSABLE = "UNANALYSABLE"
    #: Multi-slot only; unreachable until items carry more than one blank.
    WORD_ORDER = "WORD_ORDER"
    MISSING_CONSTITUENT = "MISSING_CONSTITUENT"


@dataclass(frozen=True, slots=True)
class Form:
    """One inflected surface with its analysis."""

    surface: str
    #: Full Morfeusz lemma identifier, which carries a homonym discriminator:
    #: `kot:Sm1` (colloquial, personal) and `kot:Sm2` (the animal) are distinct
    #: lexemes sharing a citation form.
    lemma: str
    tag: MorphTag
    labels: tuple[str, ...] = ()

    @property
    def base_lemma(self) -> str:
        return self.lemma.split(":", 1)[0]


@dataclass(frozen=True, slots=True)
class ExpectedSlot:
    """A single blank: the form required, and the paradigm it was drawn from."""

    expected: Form
    paradigm: tuple[Form, ...]
    #: Full lemma of the expected lexeme's aspect partner, when it has one.
    #: Supplied by the caller; M0 has no lexicon to look it up in.
    aspect_partner: str | None = None

    @property
    def lemma(self) -> str:
        return self.expected.lemma

    @property
    def tag(self) -> MorphTag:
        return self.expected.tag

    def with_surface(self, surface: str) -> tuple[Form, ...]:
        """Forms of this lexeme whose surface is `surface`, case-insensitively."""
        folded = surface.casefold()
        return tuple(f for f in self.paradigm if f.surface.casefold() == folded)

    @property
    def masculine_animacy(self) -> str | None:
        """`animate`, `inanimate`, or None when the expected form is not a
        masculine noun.

        Drives the `ANIMACY` diagnosis, which needs the three-way SGJP split:
        accusative borrows the genitive for m1 and m2, and the nominative for m3.
        """
        if self.expected.tag.pos != "subst":
            return None
        genders = self.expected.tag.gender
        if genders & ANIMATE_MASCULINE:
            return "animate"
        if INANIMATE_MASCULINE in genders:
            return "inanimate"
        return None


@dataclass(frozen=True, slots=True)
class Diagnosis:
    """What the grader concluded, in terms a remediation rule can route on."""

    error_class: ErrorClass
    submitted: str
    slot: ExpectedSlot
    #: The real form the learner produced, when they produced one.
    observed: Form | None = None
    #: The attribute that decided the diagnosis, for `explain` to render.
    differing_attr: str | None = None

    @property
    def is_correct(self) -> bool:
        return self.error_class is ErrorClass.CORRECT
