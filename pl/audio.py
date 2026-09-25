"""Polish speech synthesis, cached to disk.

The design specifies neural TTS with Polish voices at two speeds — natural and
0.8× — synthesised once, cached, and served. It names Azure Speech because that
matched the surrounding toolchain, not because anything downstream depends on
the vendor. The requirement is *a* Polish voice, and macOS ships one.

That distinction matters: audio was recorded as "blocked on Azure" for a while
and was never blocked on anything except a synthesiser nobody had looked for.

**This module is the only place that knows how speech is produced**, the same
containment `pl.morph` gives the analyser. Moving to a different engine — a
cloud voice for deployment, or a cross-platform one for Linux — changes this
file and nothing else.

The design's caveat still stands: TTS prosody is sufficient for comprehension
training and not for prosody modelling, which is why §5.3 defers human recording
to B1 rather than pretending otherwise.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import uuid
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

#: macOS's speech synthesiser. Present on every Mac; absent everywhere else,
#: which `available()` reports rather than discovering at request time.
ENGINE = "say"

#: The Polish voice. `say -v '?'` lists what is installed.
VOICE = "Zosia"

#: Words per minute. The design asks for natural and 0.8×; `say`'s default is
#: about 175, so 140 is the slow pass. Speeds are named rather than numeric so a
#: different engine can honour the intent without matching the units.
RATES: dict[str, int] = {"normal": 175, "slow": 140}

DEFAULT_CACHE = Path(__file__).resolve().parent.parent / "audio-cache"

#: A synthesis of an eight-token A1 sentence takes well under a second. The
#: bound exists because `item_audio` is a sync endpoint on the shared worker
#: pool: a child that never returns holds a worker, and enough of them stop
#: grading and session composition too, not just audio.
TIMEOUT_SECONDS = 15


def _installed_voices() -> set[str]:
    """Voice names the engine reports. Isolated so tests can substitute it."""
    try:
        listing = subprocess.run(
            [ENGINE, "-v", "?"],
            check=True,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not list %s voices: %s", ENGINE, exc)
        return set()
    return {
        line.split()[0]
        for line in listing.stdout.splitlines()
        if line.strip()
    }


#: On a phone, whether it has a Polish voice of its own; None everywhere else,
#: where the local synthesiser is probed. See `use_device_voice`.
_device_voice: bool | None = None


def use_device_voice(present: bool | None) -> None:
    """Speak with the device's own voice: `present` says whether it has one.

    A phone speaks through the browser, not through `say`, so what it can say
    is decided by its voice list and never by probing this machine. None
    returns to the probe.
    """
    global _device_voice
    _device_voice = present
    available.cache_clear()


@lru_cache(maxsize=1)
def available() -> bool:
    """Whether speech can be produced on this machine at all.

    Checks for the **voice**, not merely the binary. `Zosia` is an optional
    download on macOS, so "say exists" is the most likely false positive there:
    it renders a player, then fails inside a request where the client swallows
    the error and the learner gets a button that does nothing forever.

    On a phone the answer is the phone's, set by `use_device_voice`.

    Memoised because it spawns a process; `available.cache_clear()` resets it.
    """
    if _device_voice is not None:
        return _device_voice
    if shutil.which(ENGINE) is None:
        return False
    if VOICE not in _installed_voices():
        log.warning("%s is installed but the %r voice is not", ENGINE, VOICE)
        return False
    return True


def cache_key(text: str, speed: str) -> str:
    """Stable identity for one rendering.

    Voice and rate are part of the key, not just the text: changing either must
    produce a new file rather than serving a stale one that no longer matches
    what the learner is being asked to hear.
    """
    material = f"{ENGINE}|{VOICE}|{RATES[speed]}|{text}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def synthesise(
    text: str, *, speed: str = "normal", cache_dir: Path | None = None
) -> Path:
    """Render `text` to a cached audio file and return its path.

    Synthesis is the slow part, so a hit returns immediately and the file is
    written exactly once. AAC in an M4A container, which every browser plays.
    """
    if speed not in RATES:
        raise ValueError(
            f"unknown speed {speed!r}; expected one of {sorted(RATES)}"
        )
    if not available():
        raise RuntimeError(
            f"cannot synthesise speech here: {ENGINE!r} or the {VOICE!r} voice "
            f"is unavailable"
        )

    directory = cache_dir or DEFAULT_CACHE
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{cache_key(text, speed)}.m4a"
    if path.exists():
        return path

    # Written to a temporary name and moved into place, so an interrupted run
    # cannot leave a truncated file that the cache would then serve forever.
    #
    # The name is unique **per attempt**, not per cache key: two concurrent
    # requests for the same text would otherwise write the same staging file,
    # interleave, and both rename — permanently poisoning a content-keyed entry
    # that nothing revalidates on read. `Path.replace` is atomic on POSIX, so
    # unique names make concurrency merely wasteful. The `.m4a` suffix stays
    # because `say` infers the container from the extension.
    staging = path.with_name(f"{path.stem}.{os.getpid()}.{uuid.uuid4().hex}.m4a")
    try:
        subprocess.run(
            [
                ENGINE,
                "-v", VOICE,
                "-r", str(RATES[speed]),
                "--data-format=aac",
                "-o", str(staging),
                # Ends option parsing: a sentence beginning with `-` would
                # otherwise be read as a flag.
                "--",
                text,
            ],
            check=True,
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
        )
        staging.replace(path)
    except subprocess.CalledProcessError as exc:
        log.error(
            "%s failed for %r (rc=%s): %s",
            ENGINE, text[:60], exc.returncode,
            (exc.stderr or b"").decode(errors="replace").strip(),
        )
        raise
    except subprocess.TimeoutExpired:
        log.error("%s timed out after %ss for %r", ENGINE, TIMEOUT_SECONDS, text[:60])
        raise
    finally:
        staging.unlink(missing_ok=True)
    return path
