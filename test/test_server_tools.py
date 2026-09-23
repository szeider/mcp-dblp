"""Tool-level checks over an in-memory MCP session with the local mini index."""

import pytest
from mcp.client import Client

from mcp_dblp import backend as backend_mod
from mcp_dblp import dblp_client
from mcp_dblp.server import create_server

# 2026-07-28 (stateless, the v2 default) and the 2025-era initialize handshake
MODES = ["auto", "legacy"]


@pytest.fixture
def session_backend(mini_index):
    return backend_mod.IndexBackend(path=str(mini_index))


@pytest.fixture(autouse=True)
def _reset_mirror():
    yield
    dblp_client.DBLP_BASE_URL = "https://dblp.org"


def _text(result) -> str:
    return "\n".join(c.text for c in result.content)


def test_set_base_url_accepts_known_mirrors():
    assert dblp_client.set_dblp_base_url("dblp.uni-trier.de") == "https://dblp.uni-trier.de"
    assert dblp_client.set_dblp_base_url("https://dblp.dagstuhl.de/") == "https://dblp.dagstuhl.de"
    assert dblp_client.set_dblp_base_url(" dblp.org ") == "https://dblp.org"


@pytest.mark.parametrize(
    "host", ["evil.example.com", "https://evil.example.com", "dblp.org.evil.com", "", "localhost"]
)
def test_set_base_url_rejects_other_hosts(host):
    with pytest.raises(ValueError, match="not a dblp mirror"):
        dblp_client.set_dblp_base_url(host)
    assert dblp_client.DBLP_BASE_URL == "https://dblp.org"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_mirror_tool_rejects_unknown_host(mode, session_backend):
    async with Client(create_server(session_backend), mode=mode) as c:
        r = await c.call_tool("set_dblp_mirror", {"host": "evil.example.com"})
        assert _text(r).startswith("Error: 'evil.example.com' is not a dblp mirror")
        assert dblp_client.DBLP_BASE_URL == "https://dblp.org"
        r = await c.call_tool("set_dblp_mirror", {"host": "dblp.uni-trier.de"})
        assert "https://dblp.uni-trier.de" in _text(r)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_export_requires_absolute_path(mode, session_backend, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    async with Client(create_server(session_backend), mode=mode) as c:
        r = await c.call_tool(
            "add_bibtex_entry", {"dblp_key": "journals/tcs/GodelS20", "citation_key": "g"}
        )
        assert "Successfully added" in _text(r)
        r = await c.call_tool("export_bibtex", {"path": "refs.bib"})
        assert "must be an absolute path" in _text(r)
        assert not (tmp_path / "refs.bib").exists()
        # the collection survives a rejected export
        out = tmp_path / "sub" / "refs.bib"
        r = await c.call_tool("export_bibtex", {"path": str(out)})
        assert "Exported 1 references" in _text(r)
        assert out.read_text(encoding="utf-8").startswith("@")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_export_expands_home(mode, session_backend, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    async with Client(create_server(session_backend), mode=mode) as c:
        await c.call_tool(
            "add_bibtex_entry", {"dblp_key": "journals/tcs/GodelS20", "citation_key": "g"}
        )
        r = await c.call_tool("export_bibtex", {"path": "~/refs"})
        assert "Exported 1 references" in _text(r)
        assert (tmp_path / "refs.bib").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_parallel_adds(mode, session_backend, tmp_path):
    # instructions_prompt.md allows batching add_bibtex_entry calls
    import asyncio

    keys = [
        "journals/tcs/GodelS20",
        "conf/sat/KirchwegerS21",
        "conf/sat/ManyaG21",
        "journals/ipl/PetrovaW21",
        "journals/ipl/SmirnovB21",
    ]
    async with Client(create_server(session_backend), mode=mode) as c:
        rs = await asyncio.gather(
            *(
                c.call_tool("add_bibtex_entry", {"dblp_key": k, "citation_key": f"k{i}"})
                for i, k in enumerate(keys)
            )
        )
        assert all("Successfully added" in _text(r) for r in rs)
        out = tmp_path / "refs.bib"
        r = await c.call_tool("export_bibtex", {"path": str(out)})
    assert "Exported 5 references" in _text(r)
    bib = out.read_text(encoding="utf-8")
    assert all(f"{{k{i}," in bib for i in range(5))


def test_package_import_does_not_configure_server_logging():
    import subprocess
    import sys

    code = (
        "import logging, mcp_dblp, mcp_dblp.local_index; print(len(logging.getLogger().handlers))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "0"
    assert "Loaded version" not in out.stderr


# ------------------------------------------------ first start: bounded wait per call


def _install_copy(mini_index, index_dir):
    import shutil
    import sqlite3

    from mcp_dblp import local_index

    dest = f"{index_dir}/dblp-2026-09-01.sqlite"
    shutil.copy(mini_index, dest)
    conn = sqlite3.connect(dest)
    conn.execute("INSERT OR REPLACE INTO meta VALUES('release', '2026-09-01')")
    conn.commit()
    conn.close()
    local_index.set_current(dest, index_dir)
    return dest


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_first_start_call_waits_for_index(mode, mini_index, tmp_path, monkeypatch, caplog):
    import logging
    import time

    caplog.set_level(logging.INFO, logger="mcp_dblp")
    monkeypatch.setenv("MCP_DBLP_FIRST_START_WAIT", "10")
    idx = str(tmp_path / "index")

    def slow_fetch(index_dir, status):
        status.state("downloading", 100 << 20, 10 << 20)
        time.sleep(1.0)
        return _install_copy(mini_index, index_dir)

    b = backend_mod.IndexBackend(index_dir=idx)
    b.start_fetch(slow_fetch)
    async with Client(create_server(b), mode=mode) as c:
        t0 = time.monotonic()
        r = await c.call_tool("search", {"query": "treewidth"})
        dt = time.monotonic() - t0
    assert "journals/tcs/GodelS20" in _text(r)  # answered, not the progress message
    assert "DBLP Usage Instructions" in _text(r)
    assert 0.8 < dt < 6
    # the call is retried while it waits, but logged once
    assert sum("Tool call: search" in m for m in caplog.messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_first_start_wait_is_bounded(mode, mini_index, tmp_path, monkeypatch):
    import threading
    import time

    monkeypatch.setenv("MCP_DBLP_FIRST_START_WAIT", "1")
    idx = str(tmp_path / "index")
    gate = threading.Event()

    def blocked_fetch(index_dir, status):
        status.state("downloading", 100 << 20, 10 << 20)
        assert gate.wait(20)
        return _install_copy(mini_index, index_dir)

    b = backend_mod.IndexBackend(index_dir=idx)
    b.start_fetch(blocked_fetch)
    try:
        async with Client(create_server(b), mode=mode) as c:
            t0 = time.monotonic()
            r = await c.call_tool("search", {"query": "treewidth"})
            dt = time.monotonic() - t0
            assert _text(r).startswith("The dblp index is being downloaded")
            assert "This call waited 1 s" in _text(r)
            assert "DBLP Usage Instructions" not in _text(r)
            assert 0.9 < dt < 4
            gate.set()
            b.fetcher.thread.join(10)
            r = await c.call_tool("search", {"query": "treewidth"})
            assert "journals/tcs/GodelS20" in _text(r)
    finally:
        gate.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_failed_fetch_answers_at_once(mode, tmp_path, monkeypatch):
    import time

    monkeypatch.setenv("MCP_DBLP_FIRST_START_WAIT", "30")

    def failing_fetch(index_dir, status):
        raise RuntimeError("cannot read the manifest")

    b = backend_mod.IndexBackend(index_dir=str(tmp_path / "index"))
    b.start_fetch(failing_fetch)
    b.fetcher.thread.join(5)
    async with Client(create_server(b), mode=mode) as c:
        t0 = time.monotonic()
        r = await c.call_tool("search", {"query": "treewidth"})
        dt = time.monotonic() - t0
    assert _text(r).startswith("Download failed: cannot read the manifest")
    assert "waited" not in _text(r)
    assert dt < 1.5


def test_output_cap_cuts_at_result_boundary():
    import mcp.types as types

    from mcp_dblp import server as srv

    block = "{i}. Title {i}\n   Authors: A\n   Venue: V (2020)\n   DBLP key: conf/x/K{i}\n"
    text = "Found 900 publications matching your query:\n\n" + "\n".join(
        block.format(i=i) for i in range(900)
    )
    assert len(text) > srv.MAX_OUTPUT_CHARS
    extra = types.TextContent(type="text", text="instructions")
    out = srv._cap_output([types.TextContent(type="text", text=text), extra])
    body = out[0].text
    assert len(body) < srv.MAX_OUTPUT_CHARS + 400
    assert "[Output truncated: showing" in body and "of 900 results" in body
    shown = int(body.split("showing ")[1].split(" of")[0])
    assert body.count("DBLP key: ") == shown and 0 < shown < 900
    assert body.rstrip().endswith("venue_filter).]")
    assert out[1] is extra  # later parts (instructions) untouched
    short = [types.TextContent(type="text", text="Found 1 publications")]
    assert srv._cap_output(short) is short


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_arguments_checked_against_schema(mode, session_backend):
    # v1 validated arguments against the input schema; v2's low-level Server does not
    async with Client(create_server(session_backend), mode=mode) as c:
        r = await c.call_tool("set_dblp_mirror", {"mirror": "dblp.org"})
        assert _text(r) == "Input validation error: 'host' is a required property"
        assert r.is_error
        r = await c.call_tool("search", {"query": "treewidth", "max_results": "ten"})
        assert _text(r).startswith("Input validation error: 'ten' is not of type 'integer'")
        r = await c.call_tool("search", {"query": "treewidth", "max_results": 1.5})
        assert r.is_error and "is not of type 'integer'" in _text(r)
        r = await c.call_tool("search", {"query": "treewidth", "include_bibtex": 1})
        assert "is not of type 'boolean'" in _text(r)
        r = await c.call_tool("search", {"query": "treewidth", "max_results": True})
        assert "is not of type 'integer'" in _text(r)
        r = await c.call_tool("search", {"query": "treewidth", "max_results": 2, "year_from": None})
        assert not r.is_error and "Found" in _text(r)


def test_argument_error_unit():
    from mcp_dblp.server import _argument_error

    schema = {
        "type": "object",
        "properties": {"q": {"type": "string"}, "n": {"type": "number"}, "b": {"type": "boolean"}},
        "required": ["q"],
    }
    assert _argument_error(schema, {"q": "x", "n": 3, "b": False}) is None
    assert _argument_error(schema, {"q": "x", "n": 2.5}) is None
    assert _argument_error(schema, {"q": "x", "extra": [1]}) is None  # extra keys allowed, as in v1
    assert "required" in _argument_error(schema, {"n": 1})
    assert "'number'" in _argument_error(schema, {"q": "x", "n": "1"})
    assert _argument_error(None, {"anything": 1}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_matched_line_and_author_year_filter(mode, session_backend):
    async with Client(create_server(session_backend), mode=mode) as c:
        t = _text(await c.call_tool("search", {"query": "Smirnov 2021"}))
        assert "   DBLP key: journals/ipl/SmirnovB21\n   Matched: smirnov = author 1 of 2\n" in t
        assert "   Matched: smirnov = title word only\n" in t
        args = {"author_name": "Stefan Szeider", "similarity_threshold": 0.8}
        r = await c.call_tool(
            "get_author_publications", {**args, "year_from": 2021, "year_to": 2021}
        )
        assert "Found 1 publications" in _text(r) and "conf/sat/KirchwegerS21" in _text(r)
        assert "Matched:" not in _text(r)
        r = await c.call_tool("get_author_publications", {**args, "year_from": "2021"})
        assert r.is_error and "'integer'" in _text(r)


def test_http_author_publications_year_filter(monkeypatch):
    pubs = [
        {
            "title": f"T{y}",
            "authors": ["Stefan Szeider"],
            "venue": "V",
            "year": y,
            "dblp_key": f"k{y}",
        }
        for y in (2023, 2021, 2020, None)
    ]
    monkeypatch.setattr(dblp_client, "search", lambda q, max_results: [dict(p) for p in pubs])
    r = dblp_client.get_author_publications("Stefan Szeider", 0.8, year_from=2021, year_to=2022)
    assert [p["year"] for p in r["publications"]] == [2021]
    assert r["publication_count"] == 1
    r = dblp_client.get_author_publications("Stefan Szeider", 0.8, max_results=1, year_to=2021)
    assert [p["year"] for p in r["publications"]] == [2021]  # filtered before the cut
    r = dblp_client.get_author_publications("Stefan Szeider", 0.8)
    assert len(r["publications"]) == 4
