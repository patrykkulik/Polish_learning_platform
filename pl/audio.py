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
import shutil
import subprocess
from pathlib import Path

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


def available() -> bool:
    """Whether speech can be produced on this machine at all.

    Checked up front so the app can offer a session without audio rather than
    failing a request halfway through one.
    """
    return shutil.which(ENGINE) is not None


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
            f"{ENGINE!r} is not on PATH; this machine cannot synthesise speech"
        )

    directory = cache_dir or DEFAULT_CACHE
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{cache_key(text, speed)}.m4a"
    if path.exists():
        return path

    # Written to a temporary name and moved into place, so an interrupted run
    # cannot leave a truncated file that the cache would then serve forever.
    # The staging name keeps the `.m4a` suffix: `say` infers the container from
    # the extension and refuses anything it does not recognise.
    staging = path.with_name(f"{path.stem}.partial.m4a")
    subprocess.run(
        [
            ENGINE,
            "-v", VOICE,
            "-r", str(RATES[speed]),
            "--data-format=aac",
            "-o", str(staging),
            text,
        ],
        check=True,
        capture_output=True,
    )
    staging.replace(path)
    return path
