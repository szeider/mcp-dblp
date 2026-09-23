"""Regression tests for the code-review fixes (key sanitizing, fetch status, reopening,
index building, SQLite URIs, BibTeX markup, web BibTeX check)."""

import datetime
import gzip
import os
import sqlite3
import subprocess
import sys

import pytest

from mcp_dblp import backend as backend_mod
from mcp_dblp import bibtex_render, dblp_client, local_index


def _iso(seconds_ago: float) -> str:
    t = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=seconds_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------ one key sanitizer for both backends


@pytest.mark.parametrize(
    "raw",
    [
        "conf/cp/KirchwegerS21",
        "conf/cp/KirchwegerS21.bib",
        "https://dblp.org/rec/conf/cp/KirchwegerS21.bib",
        "http://dblp.uni-trier.de/rec/conf/cp/KirchwegerS21",
        "dblp.dagstuhl.de/rec/conf/cp/KirchwegerS21.bib",
        "DBLP:conf/cp/KirchwegerS21",
        "  conf/cp/KirchwegerS21  ",
    ],
)
def test_http_backend_sanitizes_like_local(raw):
    assert backend_mod.HttpBackend.sanitize_key(raw) == "conf/cp/KirchwegerS21"


# ------------------------------------------------ a dead peer fetch is not "in progress"


@pytest.mark.parametrize(
    "snap, active",
    [
        (None, False),
        ({}, False),
        ({"state": "downloading", "updated": _iso(5)}, True),
        ({"state": "unpacking", "updated": _iso(5)}, True),
        ({"state": "downloading", "updated": _iso(600)}, False),  # stale: the peer died
        ({"state": "done", "updated": _iso(5)}, False),
        ({"state": "failed", "updated": _iso(5)}, False),
        ({"state": "weird", "updated": _iso(5)}, False),
        ({"state": "downloading", "updated": "not a time"}, False),
    ],
)
def test_status_active(snap, active):
    assert backend_mod._status_active(snap) is active


def test_busy_fetch_follows_live_status_only(tmp_path, monkeypatch):
    idx = str(tmp_path / "index")
    f = backend_mod.BackgroundFetch(idx, fetch_fn=lambda **kw: None)
    f.busy = True
    status = {"state": "downloading", "updated": _iso(3)}
    monkeypatch.setattr(local_index, "read_status", lambda index_dir=None: status)
    assert f.in_progress()
    status["updated"] = _iso(backend_mod.STALE_STATUS + 10)
    assert not f.in_progress()


# ------------------------------------------------ a failed open is retried later


def test_failed_open_is_retried(mini_index, tmp_path, monkeypatch):
    path = str(tmp_path / "dblp-2026-09-01.sqlite")
    with open(path, "wb") as f:
        f.write(b"not a database")
    b = backend_mod.IndexBackend(path=path)
    with pytest.raises(backend_mod.IndexUnavailable, match="Cannot open"):
        b.local()
    # the same file (same inode and mtime) becomes readable, e.g. a lock went away
    st = os.stat(path)
    with open(mini_index, "rb") as src, open(path, "r+b") as dst:
        dst.write(src.read())
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    with pytest.raises(backend_mod.IndexUnavailable):
        b.local()  # still inside REOPEN_AFTER: the cached failure is reported
    monkeypatch.setattr(backend_mod, "REOPEN_AFTER", 0)
    assert b.local().release == "2026-09-01"


# ------------------------------------------------ build() starts from an empty file


def _dump(tmp_path, xml_records: str):
    dump = tmp_path / "dblp-2026-09-01.xml.gz"
    body = (
        '<?xml version="1.0" encoding="ISO-8859-1"?>\n'
        '<!DOCTYPE dblp SYSTEM "dblp.dtd">\n<dblp>\n' + xml_records + "\n</dblp>\n"
    )
    with gzip.open(dump, "wb") as f:
        f.write(body.encode("iso-8859-1"))
    return str(dump)


_BOOK = """<book mdate="2020-01-01" key="books/x/Two">
<author>Ada Lovelace</author><title>Two Series.</title><year>2020</year>
<series href="db/series/first/index.html">First Series</series>
<series href="db/series/second/index.html">Second Series</series>
<publisher>P</publisher></book>"""
_ARTICLE = """<article mdate="2020-01-01" key="journals/x/New">
<author>Alan Turing</author><title>New Record.</title><year>2020</year>
<journal>J. X</journal></article>"""


def test_build_replaces_existing_output(tmp_path):
    out = str(tmp_path / "out.sqlite")
    assert local_index.build(_dump(tmp_path, _BOOK), out)
    assert local_index.build(_dump(tmp_path, _ARTICLE), out)
    conn = sqlite3.connect(out)
    keys = [k for (k,) in conn.execute("SELECT key FROM publ_v")]
    conn.close()
    assert keys == ["journals/x/New"]


def test_series_href_belongs_to_first_series(tmp_path):
    out = str(tmp_path / "out.sqlite")
    assert local_index.build(_dump(tmp_path, _BOOK), out)
    conn = sqlite3.connect(out)
    series, href = conn.execute(
        "SELECT series, series_href FROM publ_v WHERE key = 'books/x/Two'"
    ).fetchone()
    conn.close()
    assert (series, href) == ("First Series", "db/series/first/index.html")


# ------------------------------------------------ read-only URIs for awkward paths


@pytest.mark.parametrize("name", ["with space", "hash#dir", "q?mark", "pct%20dir"])
def test_sqlite_ro_uri_opens_awkward_paths(mini_index, tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    path = d / "dblp-2026-09-01.sqlite"
    path.write_bytes(mini_index.read_bytes())
    assert local_index.read_meta(str(path)).get("release") == "2026-09-01"
    conn = sqlite3.connect(local_index.sqlite_ro_uri(str(path)), uri=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("CREATE TABLE t(x)")  # really read-only
    conn.close()


# ------------------------------------------------ BibTeX: <u> markup is dropped


def test_underline_markup_dropped():
    out = bibtex_render.text_field("Ordered types and a generalized <u>for</u> statement")
    assert out == "Ordered types and a generalized for statement"
    out = bibtex_render.text_field("R&amp;D <u>now</u>")
    assert out == "R{\\&}D now"


# ------------------------------------------------ web BibTeX: a bot page is not BibTeX


class _Resp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status

    def raise_for_status(self):
        pass


def test_fetch_bibtex_entry_rejects_html(monkeypatch):
    page = "<!doctype html><html><head><title>Making sure you're not a bot!</title>"
    monkeypatch.setattr(dblp_client.requests, "get", lambda *a, **k: _Resp(page))
    out = dblp_client.fetch_bibtex_entry("conf/cp/KirchwegerS21")
    assert out.startswith("% Error") and "not BibTeX" in out


# ------------------------------------------------ server: null arguments, error flags, log


@pytest.fixture
def local_backend(mini_index):
    return backend_mod.IndexBackend(path=str(mini_index))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_null_arguments(mode, local_backend):
    from mcp.client import Client

    from mcp_dblp.server import create_server

    async with Client(create_server(local_backend), mode=mode) as c:
        r = await c.call_tool("search", {"query": None})
        assert r.is_error
        assert r.content[0].text == "Input validation error: 'query' is a required property"
        r = await c.call_tool("search", {"query": "treewidth", "max_results": None})
        assert not r.is_error and "journals/tcs/GodelS20" in r.content[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_error_answers_are_flagged(mode, local_backend, tmp_path):
    from mcp.client import Client

    from mcp_dblp.server import create_server

    async with Client(create_server(local_backend), mode=mode) as c:
        ok = await c.call_tool("search", {"query": "treewidth"})
        assert not ok.is_error
        r = await c.call_tool("export_bibtex", {"path": str(tmp_path / "x.bib")})
        assert r.is_error and r.content[0].text.startswith("Error: Collection is empty")
        r = await c.call_tool("add_bibtex_entry", {"dblp_key": "conf/x/Nope", "citation_key": "n"})
        assert r.is_error and r.content[0].text.startswith("Failed to add entry")
        r = await c.call_tool("set_dblp_mirror", {"host": "evil.example.com"})
        assert r.is_error


def _import_server(env_home: str) -> subprocess.CompletedProcess:
    code = (
        "import mcp_dblp.server as s; "
        "print('LOG', s.log_file); "
        "import logging; logging.getLogger('mcp_dblp').info('hello from test')"
    )
    env = {**os.environ, "MCP_DBLP_HOME": env_home}
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)


def test_log_file_in_mcp_dblp_home(tmp_path):
    out = _import_server(str(tmp_path / "home"))
    assert out.returncode == 0, out.stderr
    log = tmp_path / "home" / "mcp_dblp_server.log"
    assert f"LOG {log}" in out.stdout
    assert "hello from test" in log.read_text()


def test_unwritable_home_logs_to_stderr_only(tmp_path):
    blocker = tmp_path / "a_file"
    blocker.write_text("x")
    out = _import_server(str(blocker / "home"))  # a directory below a regular file
    assert out.returncode == 0, out.stderr
    assert "LOG None" in out.stdout
    assert "hello from test" in out.stderr
