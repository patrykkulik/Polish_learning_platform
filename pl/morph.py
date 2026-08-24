"""The only module that imports `morfeusz2`.

Everything the grader needs from the analyser passes through here, so if the
in-process decision ever reverses — a Morfeusz instance turning out not to be
thread-safe, or too heavy to hold per worker — this is the single file that
changes and nothing downstream notices.

Two operations matter. `analyse` returns the segmentation lattice, used only to
ask whether a submitted string is a word at all. `forms` returns a lexeme's full
paradigm by synthesis, which is what populates `ExpectedSlot` and therefore what
the error classifier actually searches.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import morfeusz2

from pl.domain import Form
from pl.tags import IGN, MorphTag, parse

_instance: morfeusz2.Morfeusz | None = None


def _morfeusz() -> morfeusz2.Morfeusz:
    """Lazily construct the shared instance.

    Loading the dictionary is the expensive part, so one instance is reused. It
    is created on first use rather than at import so that `pl.tags` and
    `pl.domain` stay importable without paying for it.
    """
    global _instance
    if _instance is None:
        _instance = morfeusz2.Morfeusz(generate=True)
    return _instance


@dataclass(frozen=True, slots=True)
class Edge:
    """One interpretation spanning `start` to `end` in the segmentation lattice."""

    start: int
    end: int
    form: Form


def _to_form(interp: tuple) -> Form:
    surface, lemma, tag, labels, _qualifiers = interp
    return Form(surface=surface, lemma=lemma, tag=parse(tag), labels=tuple(labels))


def analyse(text: str) -> tuple[Edge, ...]:
    """Analyse `text` into its segmentation lattice.

    The result is a DAG, not a token list: a span may carry several
    interpretations (`mamy` is both `mieć` and `mama`), and segmentation itself
    is ambiguous (`zrobiłbym` splits into `zrobił` + `by` + `m`). Grading never
    disambiguates it — it asks whether a matching path exists.
    """
    return tuple(
        Edge(start=start, end=end, form=_to_form(interp))
        for start, end, interp in _morfeusz().analyse(text)
    )


def analyses(token: str) -> tuple[Form, ...]:
    """Every interpretation of a single token, `ign` readings excluded.

    Empty means the string is not a Polish word. Note that Morfeusz never returns
    an empty list of its own accord: an unanalysable string comes back with one
    `ign` interpretation, which this filters out.
    """
    return tuple(
        edge.form for edge in analyse(token) if not edge.form.tag.is_unknown
    )


def is_known(token: str) -> bool:
    """True when `token` has at least one real analysis."""
    return bool(analyses(token))


@lru_cache(maxsize=2048)
def forms(lemma: str) -> tuple[Form, ...]:
    """The full paradigm of `lemma`, by synthesis.

    `lemma` may be a citation form (`kot`) or a full Morfeusz identifier
    (`kot:Sm2`). A citation form shared by several lexemes returns all of them,
    so callers that care about the distinction — the accusative of the animal
    against the accusative of the colloquial personal noun — should pass the
    identifier.
    """
    base = lemma.split(":", 1)[0]
    generated = _morfeusz().generate(base)
    out = [_to_form(interp) for interp in generated]
    if ":" in lemma:
        out = [f for f in out if f.lemma == lemma]
    return tuple(out)


def cell(lemma: str, **features: str) -> Form:
    """The paradigm cell of `lemma` matching every requested feature value.

    Requested values are matched against the tag's value *sets*, so asking for
    `case="acc"` finds `subst:sg:gen.acc:m2` — the syncretic cell that makes the
    masculine animate accusative what it is.

    A `pos` key filters on part of speech rather than on a feature, which is how
    a caller pins down an infinitive: `aspect="perf"` alone matches every
    perfective form in the paradigm, participles and gerunds included.

    Raises LookupError when nothing matches, or when the request is ambiguous
    across genuinely different surfaces.
    """
    wanted_pos = features.pop("pos", None)
    hits = [
        f
        for f in forms(lemma)
        if (wanted_pos is None or f.tag.pos == wanted_pos)
        and all(f.tag.has(attr, value) for attr, value in features.items())
    ]
    if not hits:
        raise LookupError(f"no cell of {lemma!r} matches {features}")
    surfaces = {f.surface for f in hits}
    if len(surfaces) > 1:
        raise LookupError(
            f"{features} is ambiguous for {lemma!r}: {sorted(surfaces)}"
        )
    return hits[0]


#: Parts of speech `generate` attaches to a lemma that are not inflected forms of
#: it, and do not analyse back to it:
#:
#:   brev   abbreviations — `dom` yields `d`, which reads as a Roman numeral
#:   adja   adjectival-prefix forms — `duży` yields `dużo`, lemmatised elsewhere
#:   pacta  participial adverbs — `czytać` yields `czytająco`, which Morfeusz
#:          generates but cannot analyse at all
#:
#: No curriculum cell is ever drawn from one of these, so excluding them narrows
#: the round-trip assertion to the forms content is actually built from.
NON_INFLECTIONAL: frozenset[str] = frozenset({"brev", "adja", "pacta"})


def paradigm_roundtrips(lemma: str) -> tuple[str, ...]:
    """Inflected surfaces of `lemma` that do not re-analyse back to it.

    Empty means the round trip holds. Asserted before any content is generated
    from a paradigm: an item built on a surface the analyser cannot recognise
    could never be graded correctly, whatever the learner typed.

    Peripheral, non-inflectional entries are excluded — see NON_INFLECTIONAL.
    """
    base = lemma.split(":", 1)[0]
    broken = []
    for form in forms(lemma):
        if form.tag.pos in NON_INFLECTIONAL:
            continue
        lemmas = {f.base_lemma for f in analyses(form.surface)}
        if base not in lemmas:
            broken.append(form.surface)
    return tuple(dict.fromkeys(broken))


__all__ = [
    "Edge",
    "IGN",
    "MorphTag",
    "analyse",
    "analyses",
    "cell",
    "forms",
    "is_known",
    "paradigm_roundtrips",
]
