"""Fixtures that must apply to the whole suite, not to one file.

There is exactly one thing here, and it is here rather than in `test_journey.py`
because the hazard it removes is cross-file by nature.
"""

from __future__ import annotations

import random

import pytest

#: Arbitrary, and fixed. Any value works; changing it re-rolls every FSRS
#: interval in the suite, so change it only deliberately.
FSRS_FUZZ_SEED = 20260827


@pytest.fixture(autouse=True)
def repeatable_fsrs_fuzz():
    """FSRS jitters every interval it computes, drawing on the *global* RNG.

    Left unseeded, a test's outcome depends on how much randomness the tests
    before it happened to consume — so the same test passes alone and fails in a
    full run, or the other way round, and neither result means anything. One of
    these tests did exactly that during development.

    Seeding keeps the fuzz switched on, so the behaviour under test stays
    production's, and makes the order irrelevant. It lives in `conftest.py`
    because a file-local `autouse` fixture reseeds for its own file and leaves
    every other file running on whatever state that file left behind — which
    swaps one order dependency for another and looks like a fix.
    """
    random.seed(FSRS_FUZZ_SEED)
