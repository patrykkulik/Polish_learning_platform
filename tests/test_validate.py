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
    import yaml

    from pl.content.frames import DATA, _allowed_lemmas

    allowed = _allowed_lemmas()
    corpus: set[str] = set()
    rejected: list[tuple[str, list[str]]] = []

    for entry in yaml.safe_load((DATA / "sentences.yaml").read_text(encoding="utf-8")):
        problems = validate(
            entry["text"], target=entry["target"], allowed=allowed, corpus=corpus
        )
        if problems:
            rejected.append((entry["text"], [str(p) for p in problems]))
        corpus.add(entry["text"])

    assert not rejected, f"committed sentences fail validation: {rejected}"
