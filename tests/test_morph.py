"""The analyser wrapper: synthesis, analysis, and the round trip between them."""

from __future__ import annotations

import pytest

from pl import morph

#: The lexemes M1 teaches across the nominative and accusative, plus the two
#: adjectives and the aspect pairs the classifier needs to exercise every class.
M1_LEXEMES = [
    "sklep",
    "dom",
    "stół",
    "samochód",
    "kot:Sm2",
    "pies",
    "ptak",
    "kawa",
    "książka",
    "matka",
    "córka",
    "gazeta",
    "okno",
    "krzesło",
    "Kraków",
    "duży",
    "nowy",
    "dobry",
    "mały",
    "robić",
    "zrobić",
    "czytać",
    "przeczytać",
]


@pytest.mark.parametrize("lemma", M1_LEXEMES)
def test_every_generated_surface_reanalyses_to_its_lemma(lemma):
    """Acceptance criterion 3.

    Content is generated from the paradigm, so a surface the analyser cannot
    recognise would produce an item that can never be graded correctly. Asserted
    over the whole M1 lexeme set before any content is authored from it.
    """
    assert morph.paradigm_roundtrips(lemma) == ()


@pytest.mark.parametrize("lemma", M1_LEXEMES)
def test_every_tag_in_the_m1_paradigms_parses(lemma):
    """Acceptance criterion 2, over real data rather than a handful of examples."""
    assert morph.forms(lemma), f"{lemma} generated no forms"


def test_unknown_words_yield_no_analyses():
    """Morfeusz never returns an empty list — a non-word comes back as `ign`.

    The specification's step 4 is guarded on the submission "analysing as
    nothing", which is only true after the `ign` reading is filtered out.
    """
    assert morph.analyses("sklepa") == ()
    assert morph.analyses("Krakówie") == ()
    assert not morph.is_known("matke")


def test_real_words_are_known_even_when_they_look_like_typos():
    """`sie` and `robie` are real forms, which is why step 3 precedes step 5."""
    assert morph.is_known("sie")
    assert morph.is_known("robie")


def test_cell_finds_a_syncretic_cell_by_one_of_its_values():
    assert morph.cell("kot:Sm2", number="sg", case="acc").surface == "kota"
    assert morph.cell("sklep", number="sg", case="acc").surface == "sklep"


def test_cell_applies_the_alternation():
    assert morph.cell("Kraków", number="sg", case="loc").surface == "Krakowie"
    assert morph.cell("stół", number="sg", case="gen").surface == "stołu"


def test_cell_can_pin_a_part_of_speech():
    """`aspect` alone matches participles and gerunds too."""
    assert morph.cell("zrobić", pos="inf", aspect="perf").surface == "zrobić"


def test_cell_rejects_an_ambiguous_request():
    with pytest.raises(LookupError, match="ambiguous"):
        morph.cell("zrobić", aspect="perf")


def test_cell_rejects_an_unmatched_request():
    with pytest.raises(LookupError, match="no cell"):
        morph.cell("sklep", number="sg", case="nosuchcase")


def test_full_lemma_id_selects_one_lexeme():
    """`kot` is two lexemes: the animal (m2) and a colloquial personal noun (m1)."""
    animal = {f.lemma for f in morph.forms("kot:Sm2")}
    assert animal == {"kot:Sm2"}
    assert len({f.lemma for f in morph.forms("kot")}) > 1


def test_analyse_returns_a_lattice_with_ambiguity():
    """`mamy` is both a verb and a noun; grading never has to choose."""
    lemmas = {e.form.base_lemma for e in morph.analyse("mamy")}
    assert {"mieć", "mama"} <= lemmas


def test_analyse_splits_clitics_across_lattice_nodes():
    edges = morph.analyse("zrobiłbym")
    assert max(e.end for e in edges) == 3
    assert {e.form.surface for e in edges} == {"zrobił", "by", "m"}
