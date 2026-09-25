"""Build the static site: the content ledger on the Mac, the site itself in CI.

    uv run python scripts/build_site.py content [--init]
    python scripts/build_site.py check-ids OLD NEW
    python scripts/build_site.py check-live SITE_URL
    python scripts/build_site.py assemble [--base /repo/] [--out _site]

`content` needs Morfeusz, so it runs on the owner's Mac. It builds the course
into `publish/content.db` — the ledger, committed — through the existing ingest,
and writes the word list a phone grades with and the dictionary's licence.

The ledger exists because learner rows point at content by id and ids follow
build order: a phone's cards and attempts are only as good as the promise that a
published id never names a different row. `check-ids` is that promise, checked.
`content` checks against the ledger it started from; CI checks against what is
live on Pages (`check-live`), which is what learners actually have.

`check-ids`, `check-live` and `assemble` need only the standard library and
PyYAML, so CI never installs Morfeusz.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Run as a file, a script sees its own folder and not the repo root, and the
# project is not installed into the venv; `content` imports `pl`.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DATA = ROOT / "data"
PACKAGE = ROOT / "pl"
PUBLISH = ROOT / "publish"
LEDGER = PUBLISH / "content.db"
LEXICON = PUBLISH / "lexicon.json"
NOTICE = PUBLISH / "NOTICE"

#: A published id may never name a different row, by the key the build itself
#: identifies rows by. These are every table learner rows point into — card to
#: form, sense and pattern; attempt to item; error_event and node_unlock to node
#: — plus lexeme, whose lemma the form and sense keys are made of.
NATURAL_KEYS = {
    "lexeme": "SELECT id, lemma FROM lexeme",
    "form": (
        "SELECT form.id, lexeme.lemma, form.morph_tag"
        " FROM form JOIN lexeme ON lexeme.id = form.lexeme_id"
    ),
    "sense": (
        "SELECT sense.id, lexeme.lemma"
        " FROM sense JOIN lexeme ON lexeme.id = sense.lexeme_id"
    ),
    "pattern": "SELECT id, rule_key, paradigm_class FROM pattern",
    "node": "SELECT id, key FROM node",
    "item": "SELECT id, exercise_type, prompt, expected_answer FROM item",
}

#: What the app folder holds, from `pl/static`: the runtime and `sql.js`.
APP = ("course", "vendor")

#: The markers `assemble` rewrites in each template.
BASE_TAG = '<base href="/">'
COMMON_SCRIPT = '<script src="static/common.js"></script>'

Fetch = Callable[[str], bytes]


# ------------------------------------------------------------------ the ledger


def check_ids(old: Path, new: Path) -> list[str]:
    """Every way `new` breaks a promise `old` made: an id gone, or renamed."""
    problems = []
    with closing(_read_only(old)) as before, closing(_read_only(new)) as after:
        for table, sql in NATURAL_KEYS.items():
            was = {row[0]: row[1:] for row in before.execute(sql)}
            now = {row[0]: row[1:] for row in after.execute(sql)}
            for id_, key in sorted(was.items()):
                if id_ not in now:
                    problems.append(f"{table} {id_} {key} is gone")
                elif now[id_] != key:
                    problems.append(f"{table} {id_} was {key}, now {now[id_]}")
    return problems


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def course_lemmas() -> list[str]:
    """The course's lexemes and the function words its sentences use.

    A function word that is not a string is skipped: PyYAML reads the unquoted
    `on` in `function_words.yaml` as `True`. "on" still reaches the word list
    through the content's own tokens.
    """
    import yaml

    lexemes = yaml.safe_load((DATA / "lexemes.yaml").read_text(encoding="utf-8"))
    function_words = yaml.safe_load(
        (DATA / "function_words.yaml").read_text(encoding="utf-8")
    )
    return [entry["lemma"] for entry in lexemes] + [
        word for word in function_words if isinstance(word, str)
    ]


def content_surfaces(db) -> set[str]:
    """Every token in the built content: prompts, answers, slots and options."""
    from sqlalchemy import select

    from pl.grade.classify import tokenise
    from pl.models import Item, ItemSlot

    texts: list[str] = []
    for item in db.scalars(select(Item)):
        texts += [item.prompt or "", item.expected_answer or ""]
        texts += list(item.options_json or [])
    texts += [slot.expected_surface for slot in db.scalars(select(ItemSlot))]
    return {token for text in texts for token in tokenise(text)}


def build_content(init: bool = False) -> None:
    """Upsert the course into the ledger, then write the word list and notice.

    Builds into a copy, so nothing is written until `check-ids` passes. Never
    passes `--drop-unanswered-stale`: nobody here can know what a learner on a
    phone has answered.
    """
    if not LEDGER.exists() and not init:
        raise SystemExit(
            "publish/content.db does not exist. Pass --init to start a ledger — once,\n"
            "before anything is published: every id it assigns is a promise to learners."
        )
    with tempfile.TemporaryDirectory() as scratch:
        building = Path(scratch) / "content.db"
        if LEDGER.exists():
            shutil.copy2(LEDGER, building)
        subprocess.run(
            [sys.executable, "-m", "pl.content.ingest"],
            cwd=ROOT,
            env={**os.environ, "DATABASE_URL": f"sqlite:///{building}"},
            check=True,
        )
        if LEDGER.exists():
            problems = check_ids(LEDGER, building)
            if problems:
                raise SystemExit(_id_report(problems))
        table = _lexicon_for(building)
        PUBLISH.mkdir(exist_ok=True)
        shutil.copy2(building, LEDGER)
    LEXICON.write_text(_dump_lexicon(table), encoding="utf-8")
    NOTICE.write_text(_notice(), encoding="utf-8")
    print(f"ledger {LEDGER.relative_to(ROOT)}, word list of {len(table)} surfaces, notice")


def _lexicon_for(ledger: Path) -> dict[str, list[list[str]]]:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from pl import morph

    engine = create_engine(f"sqlite:///{ledger}", future=True)
    try:
        with Session(engine) as db:
            surfaces = content_surfaces(db)
    finally:
        engine.dispose()
    return morph.lexicon(course_lemmas(), surfaces)


def _dump_lexicon(table: dict[str, list[list[str]]]) -> str:
    """One surface per line, so a content change reads as a diff."""
    lines = [
        f"{json.dumps(word, ensure_ascii=False)}: {json.dumps(readings, ensure_ascii=False)}"
        for word, readings in table.items()
    ]
    return "{\n" + ",\n".join(lines) + "\n}\n"


def _notice() -> str:
    from pl import morph

    return (
        "The inflected Polish forms in content.db and lexicon.json are derived from\n"
        "this dictionary, distributed with Morfeusz 2 (https://morfeusz.sgjp.pl) under\n"
        "the licence below.\n\n" + morph.dictionary_notice()
    )


def _id_report(problems: list[str]) -> str:
    shown = "\n".join(f"  {p}" for p in problems[:20])
    more = f"\n  ... and {len(problems) - 20} more" if len(problems) > 20 else ""
    return (
        f"{len(problems)} published id(s) would change meaning — every learner's cards\n"
        f"and attempts point at them:\n{shown}{more}"
    )


# ------------------------------------------------------------------ the site


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def check_live(site_url: str, fetch: Fetch = _fetch) -> list[str] | None:
    """`check-ids` against the ledger learners actually have: the live one.

    None when nothing is deployed yet — a 404 for `site.json`. Anything else
    that goes wrong raises, and fails the deploy: a check that cannot see what
    is live cannot vouch for what replaces it. `site.json` is asked for with a
    query no cache has seen, so a stale copy cannot stand in for the live one.
    """
    base = site_url.rstrip("/") + "/"
    try:
        names = json.loads(fetch(f"{base}site.json?t={time.time_ns()}"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    with tempfile.TemporaryDirectory() as scratch:
        live = Path(scratch) / "live.db"
        live.write_bytes(fetch(base + names["content"]))
        return check_ids(live, LEDGER)


def assemble(out: Path, base: str = "/") -> dict[str, str]:
    """Write the whole static site into `out`, and return `site.json`'s names.

    The app folder, the ledger, the word list and the lessons are named by their
    hashes: a page names the files it was built with, a stale page asks for files
    that no longer exist, and `site.json` — at a fixed name — says which are
    current. The app is a folder so that one deploy's modules only ever import
    each other.
    """
    import yaml

    base = f"/{base.strip('/')}/" if base.strip("/") else "/"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    concepts = yaml.safe_load((DATA / "concepts.yaml").read_text(encoding="utf-8"))
    names = {
        "app": _publish_app(out),
        "content": _publish(out, "content", ".db", LEDGER.read_bytes()),
        "lexicon": _publish(out, "lexicon", ".json", LEXICON.read_bytes()),
        "concepts": _publish(
            out, "concepts", ".json", json.dumps(concepts, ensure_ascii=False).encode("utf-8")
        ),
    }
    shutil.copytree(PACKAGE / "static", out / "static", ignore=shutil.ignore_patterns(*APP))
    shutil.copy2(NOTICE, out / "NOTICE.txt")
    (out / "site.json").write_text(json.dumps(names, indent=2) + "\n", encoding="utf-8")

    pages = {
        "index.html": "session.html",
        "progress/index.html": "progress.html",
        "grammar/index.html": "grammar.html",
    }
    for concept in concepts["concepts"]:
        pages[f"grammar/{concept['key']}/index.html"] = "grammar.html"
    for target, template in pages.items():
        html = (PACKAGE / "templates" / template).read_text(encoding="utf-8")
        path = out / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_page(html, base, names), encoding="utf-8")
    return names


def _publish(out: Path, stem: str, suffix: str, data: bytes) -> str:
    name = f"{stem}.{hashlib.sha256(data).hexdigest()[:12]}{suffix}"
    (out / name).write_bytes(data)
    return name


def _publish_app(out: Path) -> str:
    """The phone's runtime — `pl/static/course/` and `sql.js` with its licence —
    in a folder named by the hash of its paths and contents."""
    files = sorted(
        path for folder in APP for path in (PACKAGE / "static" / folder).rglob("*") if path.is_file()
    )
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(PACKAGE / "static").as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
    name = f"app.{digest.hexdigest()[:12]}"
    for path in files:
        target = out / name / path.relative_to(PACKAGE / "static")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    return name


def _page(html: str, base: str, stamp: dict[str, str]) -> str:
    """A template as the static site serves it.

    The base becomes the site's path, the manifest makes it a Home Screen app,
    and `device.js` follows `common.js`, carrying the names of the files this
    deploy was built with.
    """
    if html.count(BASE_TAG) != 1 or html.count(COMMON_SCRIPT) != 1:
        raise ValueError("a template has lost its <base> or its common.js script")
    html = html.replace(
        BASE_TAG,
        f'<base href="{base}">\n<link rel="manifest" href="static/manifest.webmanifest">',
    )
    attributes = " ".join(f'data-{key}="{value}"' for key, value in sorted(stamp.items()))
    return html.replace(
        COMMON_SCRIPT, f'{COMMON_SCRIPT}\n<script src="static/device.js" {attributes}></script>'
    )


# ------------------------------------------------------------------ command line


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    content = commands.add_parser("content", help="upsert the course into the ledger")
    content.add_argument("--init", action="store_true", help="start a new ledger")
    ids = commands.add_parser("check-ids", help="fail if NEW breaks an id in OLD")
    ids.add_argument("old", type=Path)
    ids.add_argument("new", type=Path)
    live = commands.add_parser("check-live", help="check-ids against the live site")
    live.add_argument("site_url")
    site = commands.add_parser("assemble", help="write the static site")
    site.add_argument("--base", default="/", help="the site's path, e.g. /repo/")
    site.add_argument("--out", type=Path, default=ROOT / "_site")
    args = parser.parse_args(argv)

    if args.command == "content":
        build_content(init=args.init)
    elif args.command == "check-ids":
        problems = check_ids(args.old, args.new)
        if problems:
            print(_id_report(problems), file=sys.stderr)
            return 1
        print("every published id still names the same row")
    elif args.command == "check-live":
        problems = check_live(args.site_url)
        if problems is None:
            print("nothing is deployed yet, so no published id can break")
        elif problems:
            print(_id_report(problems), file=sys.stderr)
            return 1
        else:
            print("every live id still names the same row")
    elif args.command == "assemble":
        names = assemble(args.out, args.base)
        print(f"site written to {args.out}: {', '.join(names.values())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
