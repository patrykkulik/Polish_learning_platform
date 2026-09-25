"""Speech synthesis, cached to disk.

The design specifies neural TTS with Polish voices at two speeds, synthesised
once and cached. It names Azure because that matched the toolchain, not because
the pipeline needs it — the requirement is *a* Polish voice, and this machine
already has one.

Most of these run everywhere. Only the three that actually invoke the
synthesiser are skipped without one, so a Linux run still exercises the cache
key, the speed validation and the availability logic rather than reporting green
having tested nothing.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

from pl import audio

needs_engine = pytest.mark.skipif(
    not audio.available(), reason="no local speech synthesiser"
)


@pytest.fixture(autouse=True)
def _clear_probe():
    """`available()` memoises its voice probe; tests must not inherit it, nor
    a phone's voice. Setting the flag clears the probe too."""
    audio.use_device_voice(None)
    yield
    audio.use_device_voice(None)


# ------------------------------------------------------- runs everywhere


def test_availability_requires_the_voice_not_just_the_binary(monkeypatch):
    """A Mac with `say` but without the Polish voice is the likely first run.

    `Zosia` is an optional download. Checking only for the binary reports
    available, renders a player, and then fails inside a request — where the
    client swallows it and the learner gets a button that does nothing.
    """
    monkeypatch.setattr(audio.shutil, "which", lambda _: "/usr/bin/say")
    monkeypatch.setattr(audio, "_installed_voices", lambda: {"Alex", "Daniel"})
    assert audio.available() is False

    audio.available.cache_clear()
    monkeypatch.setattr(audio, "_installed_voices", lambda: {audio.VOICE, "Alex"})
    assert audio.available() is True


def test_a_phones_own_voice_decides_whether_it_can_speak(monkeypatch):
    """A phone speaks through the browser, so its own voice list decides, never
    this machine's synthesiser — in either direction, and at once."""
    monkeypatch.setattr(audio.shutil, "which", lambda _: "/usr/bin/say")
    monkeypatch.setattr(audio, "_installed_voices", lambda: {audio.VOICE})
    assert audio.available() is True  # probed, and cached

    audio.use_device_voice(False)
    assert audio.available() is False
    audio.use_device_voice(True)
    monkeypatch.setattr(audio.shutil, "which", lambda _: None)
    assert audio.available() is True

    audio.use_device_voice(None)
    assert audio.available() is False  # the probe again: no binary now


def test_availability_is_false_without_the_binary(monkeypatch):
    monkeypatch.setattr(audio.shutil, "which", lambda _: None)
    assert audio.available() is False


def test_synthesising_without_an_engine_raises_a_clear_error(monkeypatch, tmp_path):
    """Never a `CalledProcessError` from the depths — the caller needs to be able
    to distinguish "this machine has no voice" from "this item is broken"."""
    monkeypatch.setattr(audio.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="cannot synthesise"):
        audio.synthesise("Widzę kota.", cache_dir=tmp_path)


def test_an_unknown_speed_is_refused(tmp_path):
    with pytest.raises(ValueError, match="speed"):
        audio.synthesise("Widzę kota.", speed="glacial", cache_dir=tmp_path)


def test_the_cache_key_covers_voice_rate_and_text():
    """Changing any of the three must produce a different file, not serve a
    stale one that no longer matches what the learner is asked to hear."""
    base = audio.cache_key("Widzę kota.", "normal")
    assert base != audio.cache_key("Widzę psa.", "normal")
    assert base != audio.cache_key("Widzę kota.", "slow")
    assert base == audio.cache_key("Widzę kota.", "normal")


def test_a_timeout_is_passed_to_the_synthesiser(monkeypatch, tmp_path):
    """An endpoint on the shared threadpool must not be able to block forever.

    `item_audio` is sync, so a wedged child holds an anyio worker; with no bound
    on how many can be consumed, grading stops responding too.
    """
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        # Honour the staging path the caller chose rather than assuming one:
        # the name is unique per attempt precisely so it cannot be predicted.
        target = argv[argv.index("-o") + 1]
        pathlib.Path(target).write_bytes(b"x" * 2000)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(audio.shutil, "which", lambda _: "/usr/bin/say")
    monkeypatch.setattr(audio, "_installed_voices", lambda: {audio.VOICE})
    monkeypatch.setattr(audio.subprocess, "run", fake_run)
    audio.synthesise("Widzę kota.", cache_dir=tmp_path)
    assert seen.get("timeout"), "no timeout passed to the child process"


# ------------------------------------------------- needs a real synthesiser


@needs_engine
def test_synthesis_produces_a_playable_file(tmp_path):
    path = audio.synthesise("Widzę kota.", cache_dir=tmp_path)
    assert path.exists()
    assert path.stat().st_size > 1000, "suspiciously small audio file"
    assert path.suffix == ".m4a"


@needs_engine
def test_the_same_text_is_synthesised_once(tmp_path):
    first = audio.synthesise("Widzę kota.", cache_dir=tmp_path)
    stamp = first.stat().st_mtime_ns
    again = audio.synthesise("Widzę kota.", cache_dir=tmp_path)
    assert again == first
    assert again.stat().st_mtime_ns == stamp, "re-synthesised instead of reusing"


@needs_engine
def test_the_two_speeds_are_different_files(tmp_path):
    normal = audio.synthesise("Widzę kota.", cache_dir=tmp_path)
    slow = audio.synthesise("Widzę kota.", speed="slow", cache_dir=tmp_path)
    assert normal != slow
    assert normal.exists() and slow.exists()


@needs_engine
def test_no_staging_files_are_left_behind(tmp_path):
    """The staging name is unique per attempt, so concurrent synthesis of the
    same text cannot interleave two writes into one cache entry."""
    audio.synthesise("Widzę kota.", cache_dir=tmp_path)
    assert not list(tmp_path.glob("*.partial*")), "staging debris left in the cache"
