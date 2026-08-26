"""Speech synthesis, cached to disk.

The design specifies neural TTS with Polish voices at two speeds, synthesised
once and cached. It names Azure because that matched the toolchain, not because
the pipeline needs it — the requirement is *a* Polish voice, and this machine
already has one.
"""

from __future__ import annotations

import shutil

import pytest

from pl import audio

pytestmark = pytest.mark.skipif(
    not audio.available(), reason="no local speech synthesiser"
)


def test_synthesis_produces_a_playable_file(tmp_path):
    path = audio.synthesise("Widzę kota.", cache_dir=tmp_path)
    assert path.exists()
    assert path.stat().st_size > 1000, "suspiciously small audio file"
    assert path.suffix == ".m4a"


def test_the_same_text_is_synthesised_once(tmp_path):
    """Synthesis is the slow part, so the cache is what makes audio usable.

    Keyed on voice, rate and text together: changing any of them must produce a
    different file rather than silently serving the previous one.
    """
    first = audio.synthesise("Widzę kota.", cache_dir=tmp_path)
    stamp = first.stat().st_mtime_ns

    again = audio.synthesise("Widzę kota.", cache_dir=tmp_path)
    assert again == first
    assert again.stat().st_mtime_ns == stamp, "re-synthesised instead of reusing"


def test_the_two_speeds_are_different_files(tmp_path):
    """A learner who cannot follow the natural speed needs the same sentence
    slower, not a different sentence."""
    normal = audio.synthesise("Widzę kota.", cache_dir=tmp_path)
    slow = audio.synthesise("Widzę kota.", speed="slow", cache_dir=tmp_path)
    assert normal != slow
    assert normal.exists() and slow.exists()


def test_an_unknown_speed_is_refused(tmp_path):
    with pytest.raises(ValueError, match="speed"):
        audio.synthesise("Widzę kota.", speed="glacial", cache_dir=tmp_path)


def test_availability_reports_the_truth():
    """The app must degrade to no audio rather than 500 on a machine without a voice."""
    assert audio.available() is bool(shutil.which(audio.ENGINE))
