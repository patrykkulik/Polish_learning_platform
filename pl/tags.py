"""Morfeusz tag strings to structured, comparable feature records.

A Morfeusz tag is colon-separated, and **every position is a dot-separated set of
values**. `subst:sg:gen.acc:m1` is a single interpretation covering genitive *and*
accusative; `subst:sg.pl:nom.gen.dat.acc.inst.loc.voc:n:ncol` is one covering both
numbers and all seven cases. Syncretism is collapsed into the tag rather than
expanded into separate interpretations.

That is load-bearing, not a detail. `kota` carries exactly one nominal reading of
the animal lexeme, `subst:sg:gen.acc:m2`, so an expected accusative matches it
only under **set intersection**. Equality would report the correct answer to the
masculine-animate accusative — the first hard concept in the course — as a case
error.

The positional schema depends on the part of speech, and several parts of speech
have optional trailing positions (`subst` gains `ncol` for neuters, `prep` gains
vocalicity, `ppron3` gains accentability and post-prepositionality). A tag whose
part of speech or arity is not declared here raises rather than yielding a null
feature, so an unmodelled tag fails loudly at ingest instead of silently grading
something wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

# Part of speech -> (required attribute names, optional trailing attribute names).
#
# Derived by surveying Morfeusz output over the M1 lexeme set and a corpus of
# M1-shaped sentences, not from the published tagset description: `subst` and
# `prep` both turned out to have variable arity, which a fixed table would have
# mis-parsed. Parts of speech outside this table raise (see UnknownTagError).
_SCHEMA: Final[dict[str, tuple[tuple[str, ...], tuple[str, ...]]]] = {
    # nominal
    "subst": (("number", "case", "gender"), ("collectivity",)),
    "depr": (("number", "case", "gender"), ()),
    "adj": (("number", "case", "gender", "degree"), ()),
    "adja": ((), ()),
    # Post-prepositional adjectival, e.g. `adjp:gen`. Surveyed as arity 2 over
    # the M1 adjective set; the single position carries a case value.
    "adjp": (("case",), ()),
    "adv": ((), ("degree",)),
    "num": (("number", "case", "gender", "accommodability"), ("collectivity",)),
    "numcol": (("number", "case", "gender", "accommodability"), ()),
    # pronominal
    "ppron12": (("number", "case", "gender", "person"), ("accentability",)),
    "ppron3": (
        ("number", "case", "gender", "person"),
        ("accentability", "post_prepositionality"),
    ),
    "siebie": (("case",), ()),
    # verbal
    "fin": (("number", "person", "aspect"), ()),
    "bedzie": (("number", "person", "aspect"), ()),
    "impt": (("number", "person", "aspect"), ()),
    "aglt": (("number", "person", "aspect", "vocalicity"), ()),
    "praet": (("number", "gender", "aspect"), ("agglutination",)),
    "winien": (("number", "gender", "aspect"), ()),
    "inf": (("aspect",), ()),
    "imps": (("aspect",), ()),
    "pcon": (("aspect",), ()),
    "pant": (("aspect",), ()),
    "ger": (("number", "case", "gender", "aspect", "negation"), ()),
    "pact": (("number", "case", "gender", "aspect", "negation"), ()),
    "ppas": (("number", "case", "gender", "aspect", "negation"), ()),
    "pacta": ((), ()),
    "pred": ((), ()),
    # closed class and non-words
    "prep": (("case",), ("vocalicity",)),
    "conj": ((), ()),
    "comp": ((), ()),
    "qub": ((), ()),
    "part": ((), ("vocalicity",)),
    "interj": ((), ()),
    "burk": ((), ()),
    "brev": (("punctuation",), ()),
    "interp": ((), ()),
    "xxx": ((), ()),
    # `dom` reads as Roman numerals, and short strings can read as fragments.
    # Both are attribute-free and both surface when analysing ordinary words.
    "romandig": ((), ()),
    "frag": ((), ()),
    "ign": ((), ()),
}

#: The tag Morfeusz assigns to a string it cannot analyse. It does **not** return
#: an empty analysis list for unknown input — every token gets at least one
#: interpretation, and for a non-word that interpretation is `ign`.
IGN: Final = "ign"

#: Masculine genders that take the genitive form in the accusative singular.
#: SGJP is three-valued: m1 personal, m2 animate, m3 inanimate.
ANIMATE_MASCULINE: Final = frozenset({"m1", "m2"})
INANIMATE_MASCULINE: Final = "m3"


class UnknownTagError(ValueError):
    """A tag's part of speech or arity is absent from the declared schema."""


@dataclass(frozen=True, slots=True)
class MorphTag:
    """One Morfeusz interpretation, with each attribute held as a value set."""

    pos: str
    #: (attribute, values) pairs, in the order the tag declares them. A tuple
    #: rather than a dict so the record stays frozen and hashable.
    features: tuple[tuple[str, frozenset[str]], ...] = ()
    raw: str = field(default="", compare=False)

    def values(self, attr: str) -> frozenset[str]:
        """Values declared for `attr`, or the empty set if the tag omits it."""
        for name, vals in self.features:
            if name == attr:
                return vals
        return frozenset()

    def has(self, attr: str, value: str) -> bool:
        return value in self.values(attr)

    def agrees_on(self, attr: str, other: MorphTag) -> bool:
        """True when both tags declare `attr` and their value sets intersect.

        A tag that omits the attribute agrees vacuously — `inf:imperf` carries no
        case, and comparing it to a noun on case should not manufacture a
        difference.
        """
        mine, theirs = self.values(attr), other.values(attr)
        if not mine or not theirs:
            return True
        return bool(mine & theirs)

    def matches(self, other: MorphTag) -> bool:
        """True when the parts of speech agree and every shared attribute intersects."""
        if self.pos != other.pos:
            return False
        shared = {a for a, _ in self.features} & {a for a, _ in other.features}
        return all(self.agrees_on(a, other) for a in shared)

    def differing_attrs(self, other: MorphTag) -> tuple[str, ...]:
        """Shared attributes whose value sets do not intersect, in tag order."""
        theirs = {a for a, _ in other.features}
        return tuple(
            attr
            for attr, _ in self.features
            if attr in theirs and not self.agrees_on(attr, other)
        )

    @property
    def case(self) -> frozenset[str]:
        return self.values("case")

    @property
    def number(self) -> frozenset[str]:
        return self.values("number")

    @property
    def gender(self) -> frozenset[str]:
        return self.values("gender")

    @property
    def is_unknown(self) -> bool:
        return self.pos == IGN

    def __str__(self) -> str:
        return self.raw or self.pos


def parse(tag: str) -> MorphTag:
    """Parse a Morfeusz tag string into a MorphTag.

    Raises UnknownTagError if the part of speech is not declared, or if the tag
    carries more positions than the schema allows for it.
    """
    parts = tag.split(":")
    pos = parts[0]
    try:
        required, optional = _SCHEMA[pos]
    except KeyError:
        raise UnknownTagError(
            f"unknown part of speech {pos!r} in tag {tag!r}; "
            f"add it to pl.tags._SCHEMA once its positions are verified"
        ) from None

    attrs = parts[1:]
    if not len(required) <= len(attrs) <= len(required) + len(optional):
        expected = (
            f"{len(required)}"
            if not optional
            else f"{len(required)}-{len(required) + len(optional)}"
        )
        raise UnknownTagError(
            f"tag {tag!r} has {len(attrs)} attribute position(s); "
            f"schema for {pos!r} expects {expected}"
        )

    names = (*required, *optional)
    features = tuple(
        (names[i], frozenset(value.split("."))) for i, value in enumerate(attrs)
    )
    return MorphTag(pos=pos, features=features, raw=tag)
