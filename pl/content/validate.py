"""Automated validation of candidate sentences — the pipeline's second stage.

The specification's pipeline is generation → **validation** → human review → TTS
→ published, and it budgets this stage to reject 30–50% before a person sees
anything, because the reviewer's time is the scarce resource.

Every check here is a morphological or arithmetic fact about the candidate, so
none of it needs a language model. That matters more than it looks: the stage
that decides whether generated content is *usable* is exactly the stage that
must not share a failure mode with the generator. A validator that asked a model
whether a model's output was good would agree with itself.

The generator is deliberately not this module's problem. Candidates arrive as
strings from wherever — an API, a file, a person — and are judged the same way.
"""

from __future__ import annotations

from enum import StrEnum

from pl import morph
from pl.grade.classify import normalise


class Rejection(StrEnum):
    """Why a candidate was refused. Each maps to a fixable authoring mistake."""

    #: The form the item exists to drill does not occur in the sentence.
    TARGET_ABSENT = "TARGET_ABSENT"
    #: A content word outside the learner's unlocked set. The sentence would
    #: test vocabulary the learner has never met, and route the resulting error
    #: to the grammar node it was supposed to teach.
    OUT_OF_VOCABULARY = "OUT_OF_VOCABULARY"
    #: A token Morfeusz cannot analyse — a typo, or not Polish.
    UNANALYSABLE = "UNANALYSABLE"
    #: Longer than the level admits.
    TOO_LONG = "TOO_LONG"
    #: Too close to something already in the corpus.
    NEAR_DUPLICATE = "NEAR_DUPLICATE"


#: A1 sentences are short. The bound is a level decision, not a technical one.
MAX_TOKENS = 8


def tokenise(text: str) -> list[str]:
    """Words only. Punctuation carries no morphology to check."""
    return [
        token.strip(".,!?;:—–\"'()„”")
        for token in normalise(text).split()
        if token.strip(".,!?;:—–\"'()„”")
    ]


def validate(
    text: str,
    *,
    target: str,
    allowed: set[str],
    max_tokens: int = MAX_TOKENS,
    corpus: set[str] | None = None,
) -> list[Rejection]:
    """Every reason to refuse `text`, or an empty list.

    Returns all problems rather than the first, so an author fixing a sentence
    sees everything wrong with it in one pass instead of playing whack-a-mole
    through repeated builds.

    `allowed` holds base lemmas: the curriculum's lexemes plus the function words
    a natural sentence cannot avoid.
    """
    problems: list[Rejection] = []
    tokens = tokenise(text)

    if normalise(target) not in tokens:
        problems.append(Rejection.TARGET_ABSENT)

    if len(tokens) > max_tokens:
        problems.append(Rejection.TOO_LONG)

    unanalysable = False
    out_of_vocabulary = False
    for token in tokens:
        readings = morph.analyses(token)
        if not readings:
            unanalysable = True
            continue
        # A token is in scope if *any* of its readings is, because the analyser
        # is ambiguous and the sentence author meant one of them. Demanding that
        # every reading be in scope would reject `mamy` for being a form of
        # `mama` when it was written as a form of `mieć`.
        if not any(reading.base_lemma in allowed for reading in readings):
            out_of_vocabulary = True

    if unanalysable:
        problems.append(Rejection.UNANALYSABLE)
    if out_of_vocabulary:
        problems.append(Rejection.OUT_OF_VOCABULARY)

    if corpus is not None and _is_near_duplicate(tokens, corpus):
        problems.append(Rejection.NEAR_DUPLICATE)

    return problems


def _is_near_duplicate(tokens: list[str], corpus: set[str]) -> bool:
    """True when the corpus already holds something this close.

    Compared as token *sets*, so a sentence that only reorders or swaps one word
    of an existing one is caught. Exact-match deduplication would let a corpus
    fill up with trivial variants and call it coverage.
    """
    candidate = set(tokens)
    for existing in corpus:
        other = set(tokenise(existing))
        if not other:
            continue
        overlap = len(candidate & other) / max(len(candidate), len(other))
        if overlap >= 0.8:
            return True
    return False
