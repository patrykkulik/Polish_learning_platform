"""The automated validation stage of the content pipeline.

The specification puts this between generation and human review, and budgets it
to reject 30–50% before a person sees anything. It needs no LLM: every check is
a morphological or arithmetic fact about the candidate sentence, which is why it
can be built and trusted long before any generator exists to feed it.
"""

from __future__ import annotations

import warnings

from pl.content.validate import Rejection, validate

warnings.filterwarnings("ignore", category=DeprecationWarning)

ALLOWED = {"kot", "brat", "książka", "czytać", "park"}


def test_a_clean_sentence_passes():
    assert validate("Brat czyta książkę.", target="książkę", allowed=ALLOWED) == []


def test_a_sentence_missing_its_target_form_is_rejected():
    """The item exists to drill one form; a sentence without it drills nothing."""
    problems = validate("Brat czyta.", target="książkę", allowed=ALLOWED)
    assert Rejection.TARGET_ABSENT in problems


def test_out_of_vocabulary_content_words_are_rejected():
    """`samochód` is real Polish and not in this learner's set.

    A sentence the learner cannot read is not a morphology exercise, it is a
    vocabulary ambush — and the error it produces would be routed to the grammar
    node it was supposed to teach.
    """
    problems = validate(
        "Brat czyta książkę w samochodzie.", target="książkę", allowed=ALLOWED
    )
    assert Rejection.OUT_OF_VOCABULARY in problems


def test_an_unanalysable_token_is_rejected():
    problems = validate(
        "Brat czyta książkę qwertz.", target="książkę", allowed=ALLOWED
    )
    assert Rejection.UNANALYSABLE in problems


def test_an_over_long_sentence_is_rejected():
    long_one = "Brat czyta książkę i brat czyta książkę i brat czyta książkę."
    problems = validate(
        long_one, target="książkę", allowed=ALLOWED, max_tokens=8
    )
    assert Rejection.TOO_LONG in problems


def test_the_authored_corpus_passes_its_own_validator():
    """Every committed sentence survives the stage that guards the pipeline.

    The generation stage here is authoring rather than a model, but the
    validation stage is the same one either would face — and it must be the
    thing that decides, not the author's confidence. Four sentences were
    rejected on first run: two used words outside the curriculum, one named a
    lexeme that did not exist, and one was a near-duplicate of another.
    """
    from pl.content.frames import _allowed_lemmas, _sentences

    allowed = _allowed_lemmas()
    corpus: dict[str, str] = {}
    rejected: list[tuple[str, list[str]]] = []

    for entry in _sentences():
        problems = validate(
            entry["text"], target=entry["target"], allowed=allowed, corpus=corpus
        )
        if problems:
            rejected.append((entry["text"], [str(p) for p in problems]))
        corpus[entry["text"]] = entry["target"]

    assert not rejected, f"committed sentences fail validation: {rejected}"


def test_a_reordered_sentence_is_a_near_duplicate():
    """The check has to fire at the lengths this level actually uses.

    A ratio over token sets cannot reach 0.8 below five tokens: a three-token
    sentence differing by one word scores 0.667, a four-token one 0.75. The
    stage was accepting template clones and reporting a clean build.
    """
    corpus = {"Brat czyta książkę.": "książkę"}
    assert Rejection.NEAR_DUPLICATE in validate(
        "Książkę czyta brat.", target="książkę", allowed=ALLOWED, corpus=corpus
    )


def test_a_one_word_swap_is_a_near_duplicate():
    corpus = {"Brat czyta książkę.": "książkę"}
    assert Rejection.NEAR_DUPLICATE in validate(
        "Kot czyta książkę.", target="książkę", allowed=ALLOWED | {"kot"}, corpus=corpus
    )


def test_a_genuinely_different_sentence_is_not_a_duplicate():
    """The check must not reject everything — that would be as useless."""
    corpus = {"Brat czyta książkę.": "książkę"}
    assert Rejection.NEAR_DUPLICATE not in validate(
        "Kot jest w parku.",
        target="parku",
        allowed=ALLOWED | {"kot", "być", "w"},
        corpus=corpus,
    )


def test_a_repeated_word_does_not_shrink_its_own_denominator():
    """Compared as multisets: a set lets a repeated token inflate the ratio."""
    corpus = {"Brat czyta książkę i brat czyta książkę.": "książkę"}
    problems = validate(
        "Brat czyta książkę.", target="książkę", allowed=ALLOWED, corpus=corpus
    )
    assert Rejection.NEAR_DUPLICATE not in problems


def test_a_substitution_drill_is_not_a_duplicate():
    """The same frame with a different target is what a case drill *is*.

    `Nie mam czasu` and `Nie mam książki` teach two different genitives. A rule
    that rejected the second would make paradigm coverage impossible: at three
    to five tokens there are not enough distinct frames in an A1 vocabulary to
    give every noun its own.
    """
    corpus = {"Nie mam czasu.": "czasu"}
    assert Rejection.NEAR_DUPLICATE not in validate(
        "Nie mam książki.",
        target="książki",
        allowed=ALLOWED | {"nie", "mieć", "czas"},
        corpus=corpus,
    )
