"""The M0 kill-gate, plus the named acceptance criteria it does not by itself cover.

If `test_golden_corpus_meets_the_gate` cannot be made to pass, the product's
central claim — that a machine can tell a learner *which grammatical decision*
they got wrong — is false, and the right response is to stop rather than to
loosen the threshold.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

import pytest
import yaml

from pl import morph
from pl.domain import ErrorClass, ExpectedSlot
from pl.grade import classify, classify_sentence, explain

GOLDEN = Path(__file__).parent / "data" / "golden.yaml"

MIN_CASES = 60
MIN_PER_CLASS = 5
MIN_OVERALL_ACCURACY = 0.95
MIN_CLASS_ACCURACY = 0.80


def _load() -> list[dict]:
    return yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))


def _slot(case: dict) -> ExpectedSlot:
    lemma = case["lemma"]
    expected = morph.cell(lemma, **dict(case["cell"]))
    return ExpectedSlot(
        expected=expected,
        paradigm=morph.forms(lemma),
        aspect_partner=case.get("partner"),
    )


CASES = _load()


def _run() -> list[tuple[dict, ErrorClass]]:
    return [(c, classify(_slot(c), c["submitted"]).error_class) for c in CASES]


# ------------------------------------------------------------------ the gate


def test_corpus_is_large_enough():
    assert len(CASES) >= MIN_CASES


def test_corpus_covers_every_class_the_classifier_can_produce():
    counts = Counter(c["expect"] for c in CASES)
    thin = {k: n for k, n in counts.items() if n < MIN_PER_CLASS}
    assert not thin, f"classes below {MIN_PER_CLASS} cases: {thin}"


def test_case_ids_are_unique():
    dupes = [k for k, n in Counter(c["id"] for c in CASES).items() if n > 1]
    assert not dupes, f"duplicate ids: {dupes}"


def test_golden_corpus_meets_the_gate():
    """Acceptance criterion 4: >= 95% overall, and >= 80% within every class."""
    results = _run()
    misses = [(c["id"], c["expect"], got) for c, got in results if got != c["expect"]]

    overall = 1 - len(misses) / len(results)
    per_class: dict[str, list[bool]] = defaultdict(list)
    for case, got in results:
        per_class[case["expect"]].append(got == case["expect"])

    report = "\n".join(f"    {i}: expected {e}, got {g}" for i, e, g in misses)
    below = {
        cls: round(sum(hits) / len(hits), 3)
        for cls, hits in sorted(per_class.items())
        if sum(hits) / len(hits) < MIN_CLASS_ACCURACY
    }

    assert overall >= MIN_OVERALL_ACCURACY, (
        f"overall {overall:.1%} < {MIN_OVERALL_ACCURACY:.0%}\n{report}"
    )
    assert not below, (
        f"classes below {MIN_CLASS_ACCURACY:.0%}: {below}\n{report}"
    )


# ------------------------------------------- criteria the corpus alone misses


def test_animacy_is_not_reported_as_a_generic_case_error():
    """Acceptance criterion 6."""
    slot = _slot({"lemma": "kot:Sm2", "cell": {"number": "sg", "case": "acc"}})
    assert classify(slot, "kot").error_class is ErrorClass.ANIMACY


def test_dropped_ogonek_landing_on_a_real_form_is_a_case_error():
    """Acceptance criterion 7 — why step 2 precedes step 3."""
    slot = _slot({"lemma": "matka", "cell": {"number": "sg", "case": "acc"}})
    assert classify(slot, "matka").error_class is ErrorClass.CASE_WRONG


def test_dropped_ogonek_landing_on_no_form_is_a_spelling_slip():
    """Acceptance criterion 8."""
    slot = _slot({"lemma": "matka", "cell": {"number": "sg", "case": "acc"}})
    assert classify(slot, "matke").error_class is ErrorClass.ORTHOGRAPHY


def test_reverse_fold_is_never_orthographic():
    """Criterion 4b: the alternation this product exists to teach.

    `o` typed as `ó` is not a keyboard artefact, so it must fall through to the
    paradigm branch and fail the grammar card.
    """
    slot = _slot({"lemma": "Kraków", "cell": {"number": "sg", "case": "loc"}})
    assert classify(slot, "Krakówie").error_class is ErrorClass.CASE_RIGHT_FORM_WRONG


def test_forward_fold_on_the_same_letter_is_orthographic():
    """The other direction of the same pair, to show the rule is not blanket."""
    slot = _slot({"lemma": "Kraków", "cell": {"number": "sg", "case": "nom"}})
    assert classify(slot, "Krakow").error_class is ErrorClass.ORTHOGRAPHY


def test_wrong_case_and_wrong_ending_are_different_diagnoses():
    """The distinction the card routing table is built on.

    `sklepie` is a well-formed locative — the learner can inflect this noun and
    chose the wrong case. `sklepa` is not a word — the learner chose the genitive
    correctly and built it with the wrong paradigm's ending.
    """
    slot = _slot({"lemma": "sklep", "cell": {"number": "sg", "case": "gen"}})
    assert classify(slot, "sklepie").error_class is ErrorClass.CASE_WRONG
    assert classify(slot, "sklepa").error_class is ErrorClass.CASE_RIGHT_FORM_WRONG


def test_normalisation_touches_whitespace_and_case_only():
    slot = _slot({"lemma": "sklep", "cell": {"number": "sg", "case": "acc"}})
    assert classify(slot, "  SKLEP \n").error_class is ErrorClass.CORRECT


# ------------------------------------------------------------- explanations


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_every_diagnosis_produces_a_message(case):
    """Acceptance criterion 5, part one: no diagnosis renders as empty."""
    message = explain(classify(_slot(case), case["submitted"]))
    assert message and message.strip().endswith((".", "!"))


def test_message_names_the_grammatical_decision_not_the_strings():
    """Acceptance criterion 5, part two."""
    slot = _slot({"lemma": "kot:Sm2", "cell": {"number": "sg", "case": "acc"}})
    message = explain(classify(slot, "kot"))
    assert "animate" in message
    assert "genitive" in message
    assert "expected" not in message.lower()


def test_case_error_message_names_both_cases():
    slot = _slot({"lemma": "sklep", "cell": {"number": "sg", "case": "gen"}})
    message = explain(classify(slot, "sklepie"))
    assert "locative" in message and "genitive" in message


# ---------------------------------------------- the multi-slot classifier

# `classify_sentence` had no cases at all: every branch it owns — absence,
# transposition, a non-target misspelling, a surplus constituent — was reachable
# only through a full dictation item, and none of them was ever asserted.
#
# Kept here as a table rather than in golden.yaml because the corpus there is
# the M0 kill-gate and its arithmetic is defined over single-slot cases; mixing
# these in would move the denominators the gate is expressed in.

SENTENCE_CASES = [
    # id, expected tokens, target lemma, cell, target index, submitted, expect
    (
        "sentence-correct",
        ["widzę", "kota"], "kot:Sm2", {"number": "sg", "case": "acc"}, 1,
        "Widzę kota.", ErrorClass.CORRECT,
    ),
    (
        "sentence-correct-ignores-case-and-punctuation",
        ["widzę", "kota"], "kot:Sm2", {"number": "sg", "case": "acc"}, 1,
        "widzę kota", ErrorClass.CORRECT,
    ),
    (
        "sentence-missing-constituent",
        ["widzę", "kota"], "kot:Sm2", {"number": "sg", "case": "acc"}, 1,
        "Widzę.", ErrorClass.MISSING_CONSTITUENT,
    ),
    (
        "sentence-word-order",
        ["brat", "czyta", "książkę"], "książka", {"number": "sg", "case": "acc"}, 2,
        "Książkę czyta brat.", ErrorClass.WORD_ORDER,
    ),
    (
        "sentence-target-case-wrong",
        ["widzę", "kota"], "kot:Sm2", {"number": "sg", "case": "acc"}, 1,
        "Widzę kotu.", ErrorClass.CASE_WRONG,
    ),
    (
        # m2: the accusative borrows the genitive, so the bare nominative is the
        # animacy error rather than a generic wrong case — and it stays that
        # inside a sentence, which is the point of routing the target position
        # through the full classifier.
        "sentence-target-nominative-for-animate-accusative",
        ["widzę", "kota"], "kot:Sm2", {"number": "sg", "case": "acc"}, 1,
        "Widzę kot.", ErrorClass.ANIMACY,
    ),
    (
        "sentence-target-lexical",
        ["widzę", "kota"], "kot:Sm2", {"number": "sg", "case": "acc"}, 1,
        "Widzę psa.", ErrorClass.LEXICAL,
    ),
    (
        "sentence-orthography-away-from-the-target",
        ["widzę", "kota"], "kot:Sm2", {"number": "sg", "case": "acc"}, 1,
        "Widze kota.", ErrorClass.ORTHOGRAPHY,
    ),
    (
        "sentence-lexical-away-from-the-target",
        ["widzę", "kota"], "kot:Sm2", {"number": "sg", "case": "acc"}, 1,
        "Mam kota.", ErrorClass.LEXICAL,
    ),
    (
        "sentence-surplus-constituent",
        ["widzę", "kota"], "kot:Sm2", {"number": "sg", "case": "acc"}, 1,
        "Widzę kota dzisiaj.", ErrorClass.LEXICAL,
    ),
]


@pytest.mark.parametrize(
    ("case_id", "expected", "lemma", "cell", "index", "submitted", "want"),
    SENTENCE_CASES,
    ids=[c[0] for c in SENTENCE_CASES],
)
def test_sentence_cases(case_id, expected, lemma, cell, index, submitted, want):
    slot = _slot({"lemma": lemma, "cell": cell})
    got = classify_sentence(expected, index, slot, submitted).error_class
    assert got is want, f"{case_id}: expected {want}, got {got}"


def test_a_surplus_constituent_is_not_called_a_word_order_error():
    """It was, and nothing was reordered.

    The sentence matched at every position and the learner typed extra words —
    an insertion, not a transposition — so `explain` rendered a word-order
    lesson to someone who had not misordered anything.
    """
    slot = _slot({"lemma": "kot:Sm2", "cell": {"number": "sg", "case": "acc"}})
    got = classify_sentence(["widzę", "kota"], 1, slot, "Widzę kota dzisiaj.")
    assert got.error_class is not ErrorClass.WORD_ORDER


def test_every_class_a_sentence_can_produce_is_routed():
    """The same guarantee the single-slot table has, for the multi-slot path."""
    from pl.schedule import ROUTING

    for case in SENTENCE_CASES:
        assert case[6] in ROUTING, f"{case[0]} produces {case[6]}, which routes nowhere"
