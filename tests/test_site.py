"""The static site's build: the ledger's promise, the committed files, and what
`assemble` writes.

Nothing here touches the network. `check_live` and `assemble` take the fetch they
use, and the site is assembled against a stand-in lock; the real download is
made by CI, which checks it against `uv.lock` the same way.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import urllib.error
from pathlib import Path

import pytest
import yaml

from scripts import build_site

ACTIVITY = ("attempt", "card", "review", "error_event", "node_unlock", "concept_read")


def _copy(tmp_path: Path) -> Path:
    copy = tmp_path / "content.db"
    shutil.copy2(build_site.LEDGER, copy)
    return copy


def _edit(path: Path, *statements: str) -> None:
    with sqlite3.connect(path) as connection:
        for statement in statements:
            connection.execute(statement)


# ------------------------------------------------------------ check-ids


def test_an_extended_ledger_keeps_every_published_id(tmp_path):
    newer = _copy(tmp_path)
    _edit(
        newer,
        "INSERT INTO item (node_id, exercise_type, prompt, expected_answer, source)"
        " SELECT node_id, exercise_type, prompt || ' (new)', expected_answer, source"
        " FROM item ORDER BY id LIMIT 1",
    )
    assert build_site.check_ids(build_site.LEDGER, newer) == []


def test_an_id_that_names_another_row_fails(tmp_path):
    newer = _copy(tmp_path)
    _edit(newer, "UPDATE item SET prompt = prompt || ' (renamed)' WHERE id = 1")
    problems = build_site.check_ids(build_site.LEDGER, newer)
    assert len(problems) == 1 and problems[0].startswith("item 1 was")


def test_a_reordered_ledger_fails(tmp_path):
    """Two rows that swapped ids: every key is still present, under the wrong id."""
    newer = _copy(tmp_path)
    _edit(
        newer,
        "UPDATE pattern SET id = -1 WHERE id = 1",
        "UPDATE pattern SET id = 1 WHERE id = 2",
        "UPDATE pattern SET id = 2 WHERE id = -1",
    )
    problems = build_site.check_ids(build_site.LEDGER, newer)
    assert {p.split(" was ")[0] for p in problems} == {"pattern 1", "pattern 2"}


def test_a_shrunk_ledger_fails(tmp_path):
    newer = _copy(tmp_path)
    _edit(newer, "DELETE FROM form WHERE id = (SELECT max(id) FROM form)")
    problems = build_site.check_ids(build_site.LEDGER, newer)
    assert len(problems) == 1 and problems[0].endswith("is gone")


# ------------------------------------------------------------ check-live


def _serving(pages: dict[str, bytes | int]):
    """A fetch that answers from `pages`: bytes, or an HTTP status to fail with."""
    asked: list[str] = []

    def fetch(url: str) -> bytes:
        asked.append(url)
        path = url.split("?", 1)[0].rsplit("/", 1)[1]
        answer = pages.get(path, 404)
        if isinstance(answer, int):
            raise urllib.error.HTTPError(url, answer, "status", None, None)
        return answer

    return fetch, asked


def test_nothing_deployed_yet_is_a_pass():
    fetch, asked = _serving({})
    assert build_site.check_live("https://example.github.io/repo", fetch) is None
    assert asked[0].startswith("https://example.github.io/repo/site.json?t=")


def test_the_live_ledger_is_the_baseline():
    names = {"app": "app.a.zip", "content": "content.live.db", "lexicon": "lexicon.l.json"}
    fetch, asked = _serving(
        {"site.json": json.dumps(names).encode(), "content.live.db": build_site.LEDGER.read_bytes()}
    )
    assert build_site.check_live("https://example.github.io/repo/", fetch) == []
    assert asked[1] == "https://example.github.io/repo/content.live.db"


def test_a_live_ledger_that_cannot_be_read_fails_the_deploy():
    names = {"app": "app.a.zip", "content": "content.gone.db", "lexicon": "lexicon.l.json"}
    fetch, _ = _serving({"site.json": json.dumps(names).encode()})
    with pytest.raises(urllib.error.HTTPError):
        build_site.check_live("https://example.github.io/repo", fetch)
    fetch, _ = _serving({"site.json": 503})
    with pytest.raises(urllib.error.HTTPError):
        build_site.check_live("https://example.github.io/repo", fetch)


# ------------------------------------------------------------ the committed files


def test_content_refuses_to_start_a_ledger_without_init(tmp_path, monkeypatch):
    missing = tmp_path / "content.db"
    monkeypatch.setattr(build_site, "LEDGER", missing)
    with pytest.raises(SystemExit, match="--init"):
        build_site.build_content()
    assert not missing.exists()


def test_the_committed_ledger_holds_no_learner_activity():
    with sqlite3.connect(build_site.LEDGER) as connection:
        for table in ACTIVITY:
            count = connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            assert count == 0, f"the published ledger carries {count} {table} row(s)"


def test_the_committed_ledger_has_every_table_and_column_the_models_declare():
    """Phones build their learner tables from the committed ledger's definitions,
    and have no models to add a column from. A model column the ledger lacks
    would reach no phone, and every query naming it would fail there."""
    from pl import models

    with sqlite3.connect(f"file:{build_site.LEDGER}?mode=ro", uri=True) as connection:
        for table in models.Base.metadata.sorted_tables:
            present = {row[1] for row in connection.execute(f'PRAGMA table_info("{table.name}")')}
            assert present, f"the committed ledger has no {table.name!r} table"
            missing = [column.name for column in table.columns if column.name not in present]
            assert not missing, (
                f"the committed ledger's {table.name!r} lacks {missing}: "
                "rebuild it with `build_site.py content`"
            )


def test_the_committed_word_list_gives_every_content_token_morfeuszs_own_answer():
    """Criterion 4: on a phone, a word of the course reads exactly as on the server."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from pl import morph

    table = json.loads(build_site.LEXICON.read_text(encoding="utf-8"))
    engine = create_engine(f"sqlite:///{build_site.LEDGER}", future=True)
    try:
        with Session(engine) as db:
            tokens = build_site.content_surfaces(db)
    finally:
        engine.dispose()
    for token in sorted(tokens):
        readings = [[f.lemma, f.tag.raw] for f in morph.analyses(token)]
        assert table.get(token, []) == readings, token


def test_the_notice_carries_the_dictionarys_licence():
    notice = build_site.NOTICE.read_text(encoding="utf-8")
    assert "Redistributions in binary form must reproduce" in notice
    assert "sgjp" in notice


# ------------------------------------------------------------ assemble


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    out = tmp_path_factory.mktemp("site") / "_site"
    names = build_site.assemble(out, "/polish-learning-platform")
    return out, names


def _pages(out: Path) -> list[Path]:
    return sorted(out.rglob("*.html"))


def test_every_page_has_the_base_the_manifest_and_the_phones_runtime(site):
    out, names = site
    for page in _pages(out):
        html = page.read_text(encoding="utf-8")
        assert '<base href="/polish-learning-platform/">' in html, page
        assert '<link rel="manifest" href="static/manifest.webmanifest">' in html, page
        tag = re.search(r'<script src="static/device\.js\?v=\w+" ([^>]*)></script>', html)
        assert tag, page
        stamped = dict(re.findall(r'data-(\w+)="([^"]+)"', tag.group(1)))
        assert stamped == names, page
        assert html.index("static/common.js") < html.index("static/device.js"), page


def _versions(out: Path) -> set[str]:
    """The version every page asks for its scripts and stylesheet by."""
    versions = set()
    for page in _pages(out):
        html = page.read_text(encoding="utf-8")
        assets = re.findall(r'(?:src|href)="static/([^"]+\.(?:js|css)(?:\?[^"]*)?)"', html)
        assert assets, page
        for asset in assets:
            assert "?v=" in asset, f"{page.relative_to(out)}: static/{asset} is not versioned"
            versions.add(asset.split("?v=", 1)[1])
        assert 'href="static/manifest.webmanifest"' in html, page
    return versions


def test_a_page_asks_for_its_own_deploys_scripts(site, tmp_path, monkeypatch):
    """Pages lets a browser keep a file ten minutes, and the page scripts have
    fixed names. Each page asks for them by one version, which a change to any of
    them changes, so a fresh page never runs a cached older `device.js` against
    a newer app. The manifest keeps one address."""
    out, names = site
    (version,) = _versions(out)

    package = tmp_path / "pl"
    shutil.copytree(build_site.PACKAGE / "static", package / "static")
    shutil.copytree(build_site.PACKAGE / "templates", package / "templates")
    with (package / "static" / "common.js").open("a", encoding="utf-8") as handle:
        handle.write("\n/* another deploy */\n")
    monkeypatch.setattr(build_site, "PACKAGE", package)
    changed = build_site.assemble(tmp_path / "_site", "/polish-learning-platform")

    assert _versions(tmp_path / "_site") != {version}
    assert changed["app"] == names["app"]


def test_every_file_a_page_names_exists_and_site_json_names_the_same(site):
    out, names = site
    assert json.loads((out / "site.json").read_text(encoding="utf-8")) == names
    assert set(names) == {"app", "content", "lexicon", "concepts"}
    assert (out / names["app"]).is_dir()
    for name in [names["content"], names["lexicon"], names["concepts"], "NOTICE.txt"]:
        assert (out / name).is_file(), name
    assert (out / names["content"]).read_bytes() == build_site.LEDGER.read_bytes()
    assert (out / names["lexicon"]).read_bytes() == build_site.LEXICON.read_bytes()
    assert (out / "NOTICE.txt").read_bytes() == build_site.NOTICE.read_bytes()
    concepts = yaml.safe_load((build_site.DATA / "concepts.yaml").read_text(encoding="utf-8"))
    assert json.loads((out / names["concepts"]).read_text(encoding="utf-8")) == concepts


def test_there_is_a_page_for_every_concept(site):
    out, _ = site
    concepts = yaml.safe_load((build_site.DATA / "concepts.yaml").read_text(encoding="utf-8"))
    expected = {"index.html", "progress/index.html", "grammar/index.html"} | {
        f"grammar/{c['key']}/index.html" for c in concepts["concepts"]
    }
    assert {p.relative_to(out).as_posix() for p in _pages(out)} == expected


def test_no_root_absolute_url_is_left_outside_the_api(site):
    """Pages serves the site under its repository's path, so a link to "/" leaves
    it. Only the page's API calls stay absolute, and `device.js` answers those."""
    out, _ = site
    absolute = re.compile(r"""(?:href|src)=["'`]/|assign\(['"`]/""")
    for path in [*_pages(out), *(out / "static").glob("*.js")]:
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"<base [^>]*>", "", text)
        assert not absolute.search(text), f"{path.relative_to(out)}: {absolute.search(text).group(0)}"


def test_the_app_folder_carries_the_runtime_and_sql_js_with_their_notices(site):
    out, names = site
    app = out / names["app"]
    modules = {p.name for p in (build_site.PACKAGE / "static" / "course").glob("*.js")}
    assert {p.name for p in (app / "course").glob("*.js")} == modules
    assert "phone.js" in modules
    for name in ("sql-wasm.js", "sql-wasm.wasm", "sql.js-LICENSE"):
        assert (app / "vendor" / name).read_bytes() == (build_site.PACKAGE / "static" / "vendor" / name).read_bytes()
    assert "Copyright (c) 2017 sql.js authors" in (app / "vendor" / "sql.js-LICENSE").read_text(encoding="utf-8")
    assert "Copyright (c) 2022 Open Spaced Repetition" in (app / "course" / "fsrs.js").read_text(encoding="utf-8")
    # The runtime is served from the app folder only, never beside the page scripts.
    assert not (out / "static" / "course").exists()
    assert not (out / "static" / "vendor").exists()


def test_the_app_folder_is_named_by_its_contents(tmp_path):
    first = build_site.assemble(tmp_path / "one")
    assert build_site.assemble(tmp_path / "two") == first
    assert first["app"] == f"app.{first['app'].split('.', 1)[1]}" and len(first["app"]) == len("app.") + 12


def test_the_site_holds_no_python_and_loads_nothing_from_elsewhere(site):
    """Criterion 9: no Python and no Pyodide are published, and the phone asks no
    other server for anything."""
    out, _ = site
    for path in out.rglob("*"):
        if not path.is_file():
            continue
        assert path.suffix not in {".py", ".whl", ".zip"}, path.relative_to(out)
        if path.suffix in {".html", ".js", ".json", ".webmanifest", ".txt"}:
            text = path.read_text(encoding="utf-8").lower()
            assert "pyodide" not in text, path.relative_to(out)
    device = (out / "static" / "device.js").read_text(encoding="utf-8")
    assert not re.search(r"https?://", device)


def test_the_base_is_always_a_directory(tmp_path):
    for given, base in [("/", "/"), ("", "/"), ("/repo", "/repo/"), ("repo/", "/repo/")]:
        out = tmp_path / "_site"
        build_site.assemble(out, given)
        assert f'<base href="{base}">' in (out / "index.html").read_text(encoding="utf-8")
