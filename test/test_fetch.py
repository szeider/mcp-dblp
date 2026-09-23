"""Tests for `mcp-dblp-index fetch/update` and the server's first-start download.

A local http.server thread (with Range support) serves a fake manifest and a .zst of
the tiny index built from test/data/mini_dblp.xml.  No network access.
"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import zstandard

from mcp_dblp import backend as backend_mod
from mcp_dblp import local_index
from mcp_dblp.local_index import FetchError


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        srv = self.server
        rng = self.headers.get("Range")
        srv.requests.append((self.path, rng))
        body = srv.files.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        start = 0
        if rng:
            m = re.fullmatch(r"bytes=(\d+)-", rng)
            start = int(m.group(1))
            if start >= len(body):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(body)}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(body) - start))
        self.end_headers()
        self.wfile.write(body[start:])

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def http():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.files, srv.requests = {}, []
    srv.base = f"http://127.0.0.1:{srv.server_address[1]}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


@pytest.fixture
def server(http):
    http.files.clear()
    http.requests.clear()
    return http


def _index_copy(mini_index, dest, release: str) -> str:
    """Copy of the mini index with meta release set to release."""
    shutil.copy(mini_index, dest)
    conn = sqlite3.connect(dest)
    conn.execute("INSERT OR REPLACE INTO meta VALUES('release', ?)", (release,))
    conn.commit()
    conn.close()
    return str(dest)


def publish(server, sqlite_path, release: str, **override) -> dict:
    """Serve <file>.zst and latest.json for sqlite_path; returns the manifest."""
    with open(sqlite_path, "rb") as f:
        raw = f.read()
    zst = zstandard.ZstdCompressor(level=3).compress(raw)
    file = f"dblp-{release}.sqlite.zst"
    m = {
        "schema": 2,
        "release": release,
        "file": file,
        "size": len(zst),
        "sha256": hashlib.sha256(zst).hexdigest(),
        "sqlite_size": len(raw),
        "sqlite_sha256": hashlib.sha256(raw).hexdigest(),
        "urls": [f"{server.base}/dl/{file}"],
        "built": "2026-09-23T10:00:00Z",
        "builder": "mcp-dblp test",
    }
    m.update(override)
    server.files[f"/dl/{file}"] = zst
    server.files["/latest.json"] = json.dumps(m).encode()
    return m


def _zst_requests(server):
    return [r for r in server.requests if r[0].endswith(".zst")]


@pytest.fixture
def rel09(mini_index, tmp_path):
    return _index_copy(mini_index, tmp_path / "src-09.sqlite", "2026-09-01")


@pytest.fixture
def idx(tmp_path):
    d = tmp_path / "index"
    d.mkdir()
    return str(d)


def test_fetch_full(server, rel09, idx):
    m = publish(server, rel09, "2026-09-01")
    path = local_index.fetch(manifest=f"{server.base}/latest.json", index_dir=idx)
    assert path == os.path.join(idx, "dblp-2026-09-01.sqlite")
    assert local_index.current_index(idx) == path
    assert local_index.read_meta(path)["release"] == "2026-09-01"
    left = set(os.listdir(idx)) - {"current", "fetch.lock", "fetch_status.json"}
    assert left == {"dblp-2026-09-01.sqlite"}  # .zst, .part, .tmp removed
    st = local_index.read_status(idx)
    assert st["state"] == "done" and st["release"] == "2026-09-01" and st["error"] is None
    assert set(st) >= {"state", "bytes_done", "bytes_total", "started", "eta_seconds"}
    assert _zst_requests(server) == [(f"/dl/{m['file']}", None)]
    # installed already: no second download
    local_index.fetch(manifest=f"{server.base}/latest.json", index_dir=idx)
    assert len(_zst_requests(server)) == 1


def test_fetch_keep_and_env_manifest(server, rel09, idx, monkeypatch):
    m = publish(server, rel09, "2026-09-01")
    monkeypatch.setenv("MCP_DBLP_MANIFEST_URL", f"{server.base}/latest.json")
    local_index.fetch(keep=True, index_dir=idx)
    assert os.path.getsize(os.path.join(idx, m["file"])) == m["size"]


def test_fetch_resume_truncated_part(server, rel09, idx):
    m = publish(server, rel09, "2026-09-01")
    zst = server.files[f"/dl/{m['file']}"]
    half = len(zst) // 2
    part = os.path.join(idx, m["file"] + ".part")
    with open(part, "wb") as f:
        f.write(zst[:half])
    with open(part + ".sha256", "w") as f:
        f.write(m["sha256"] + "\n")
    local_index.fetch(manifest=f"{server.base}/latest.json", index_dir=idx)
    assert _zst_requests(server) == [(f"/dl/{m['file']}", f"bytes={half}-")]
    assert local_index.read_meta(local_index.current_index(idx))["release"] == "2026-09-01"


def test_fetch_part_of_other_build_is_discarded(server, rel09, idx):
    m = publish(server, rel09, "2026-09-01")
    part = os.path.join(idx, m["file"] + ".part")
    with open(part, "wb") as f:
        f.write(b"x" * 10)
    with open(part + ".sha256", "w") as f:
        f.write("0" * 64 + "\n")
    local_index.fetch(manifest=f"{server.base}/latest.json", index_dir=idx)
    assert _zst_requests(server) == [(f"/dl/{m['file']}", None)]


def test_fetch_sha256_mismatch_deletes_part(server, rel09, idx):
    m = publish(server, rel09, "2026-09-01", sha256="0" * 64)
    with pytest.raises(FetchError, match="corrupt download was deleted"):
        local_index.fetch(manifest=f"{server.base}/latest.json", index_dir=idx)
    part = os.path.join(idx, m["file"] + ".part")
    assert not os.path.exists(part) and not os.path.exists(part + ".sha256")
    assert local_index.current_index(idx) is None
    st = local_index.read_status(idx)
    assert st["state"] == "failed" and "sha256 mismatch" in st["error"]
    # a plain retry (no --force) downloads the file from the start and installs it
    m = publish(server, rel09, "2026-09-01")
    server.requests.clear()
    local_index.fetch(manifest=f"{server.base}/latest.json", index_dir=idx)
    assert _zst_requests(server) == [(f"/dl/{m['file']}", None)]
    assert local_index.current_index(idx) is not None


def test_fetch_bad_manifest(server, rel09, idx):
    publish(server, rel09, "2026-09-01", schema=3)
    with pytest.raises(FetchError, match="schema 3"):
        local_index.fetch(manifest=f"{server.base}/latest.json", index_dir=idx)
    with pytest.raises(FetchError, match="HTTP 404"):
        local_index.fetch(manifest=f"{server.base}/nope.json", index_dir=idx)


def test_fetch_release_mismatch(server, rel09, idx):
    publish(server, rel09, "2026-09-01")
    with pytest.raises(FetchError, match="not 2026-10-01"):
        local_index.fetch("2026-10-01", manifest=f"{server.base}/latest.json", index_dir=idx)


def test_fetch_busy(server, rel09, idx):
    publish(server, rel09, "2026-09-01")
    with local_index._fetch_lock(idx), pytest.raises(local_index.FetchBusyError):
        local_index.fetch(manifest=f"{server.base}/latest.json", index_dir=idx)


def test_update_newer_fetches(server, mini_index, rel09, idx, capsys):
    old = _index_copy(mini_index, os.path.join(idx, "dblp-2026-08-01.sqlite"), "2026-08-01")
    local_index.set_current(old, idx)
    publish(server, rel09, "2026-09-01")
    assert local_index.update(idx, manifest=f"{server.base}/latest.json", echo=False) == 0
    cur = local_index.current_index(idx)
    assert local_index.read_meta(cur)["release"] == "2026-09-01"
    assert not os.path.exists(old)  # previous current index deleted after the switch


def test_update_same_is_noop(server, rel09, idx, capsys):
    publish(server, rel09, "2026-09-01")
    local_index.fetch(manifest=f"{server.base}/latest.json", index_dir=idx)
    server.requests.clear()
    assert local_index.update(idx, manifest=f"{server.base}/latest.json", echo=False) == 0
    assert "up to date" in capsys.readouterr().out
    assert server.requests == [("/latest.json", None)]


# ----------------------------------------------------------- server-side behaviour


def test_current_switch_is_picked_up(mini_index, idx):
    a = _index_copy(mini_index, os.path.join(idx, "dblp-2026-08-01.sqlite"), "2026-08-01")
    b = _index_copy(mini_index, os.path.join(idx, "dblp-2026-09-01.sqlite"), "2026-09-01")
    local_index.set_current(a, idx)
    be = backend_mod.select_backend(env={}, index_dir=idx)
    assert be.release == "2026-08-01"
    assert be.search(query="treewidth")
    local_index.set_current(b, idx)
    assert be.release == "2026-09-01"
    # same path replaced by a new file (fetch --force of the same release)
    tmp = _index_copy(mini_index, os.path.join(idx, "new.tmp"), "2026-10-01")
    os.replace(tmp, b)
    assert be.release == "2026-10-01"
    assert be.search(query="treewidth")


class _FakeFetch:
    """Stands in for local_index.fetch in the server's background thread."""

    def __init__(self, action):
        self.action = action
        self.go = threading.Event()
        self.ready = threading.Event()

    def __call__(self, index_dir, status):
        return self.action(self, index_dir, status)


def test_first_start_download_message(idx, monkeypatch):
    def downloading(fake, index_dir, status):
        status.state("downloading", 1270 << 20, 412 << 20)
        status.data["eta_seconds"] = 240
        fake.ready.set()
        fake.go.wait(10)
        raise FetchError("stopped by test")

    fake = _FakeFetch(downloading)
    monkeypatch.setattr(local_index, "fetch", fake)
    b = backend_mod.select_backend(env={}, index_dir=idx, auto_fetch=True)
    assert b.fetcher is not None
    try:
        assert fake.ready.wait(5)
        msg = (
            "The dblp index is being downloaded for first use: 412 MB of 1270 MB, "
            "about 4 min left. Retry after that."
        )
        with pytest.raises(backend_mod.IndexUnavailable) as e:
            b.search(query="treewidth")
        assert str(e.value) == msg
        with pytest.raises(backend_mod.IndexUnavailable):
            b.bibtex_for_citation("conf/sat/X21", "X")
        # set_dblp_mirror keeps working without an index
        monkeypatch.setattr(backend_mod.dblp_client, "DBLP_BASE_URL", "https://dblp.org")
        assert "dblp.uni-trier.de" in b.set_mirror("dblp.uni-trier.de")
        assert backend_mod.dblp_client.DBLP_BASE_URL == "https://dblp.uni-trier.de"
    finally:
        fake.go.set()
        b.fetcher.thread.join(5)


def test_first_start_failure_message(idx, monkeypatch):
    def fail(fake, index_dir, status):
        raise FetchError("cannot read the manifest x: HTTP 503")

    monkeypatch.setattr(local_index, "fetch", _FakeFetch(fail))
    b = backend_mod.select_backend(env={}, index_dir=idx, auto_fetch=True)
    b.fetcher.thread.join(5)
    with pytest.raises(backend_mod.IndexUnavailable) as e:
        b.search(query="treewidth")
    assert str(e.value) == (
        "Download failed: cannot read the manifest x: HTTP 503. "
        "Run `mcp-dblp-index fetch` manually."
    )


def test_first_start_then_installed(mini_index, idx, monkeypatch):
    def install(fake, index_dir, status):
        p = _index_copy(mini_index, os.path.join(index_dir, "dblp-2026-09-01.sqlite"), "2026-09-01")
        local_index.set_current(p, index_dir)
        return p

    monkeypatch.setattr(local_index, "fetch", _FakeFetch(install))
    b = backend_mod.select_backend(env={}, index_dir=idx, auto_fetch=True)
    b.fetcher.thread.join(5)
    assert b.search(query="treewidth")[0]["dblp_key"] == "journals/tcs/GodelS20"


def test_no_auto_fetch_with_env_path_or_http(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("fetch must not start")

    monkeypatch.setattr(local_index, "fetch", boom)
    b = backend_mod.select_backend(
        env={"MCP_DBLP_INDEX": str(tmp_path / "x.sqlite")}, index_dir=str(tmp_path), auto_fetch=True
    )
    assert b.fetcher is None
    b = backend_mod.select_backend(
        env={"MCP_DBLP_INDEX": "http"}, index_dir=str(tmp_path), auto_fetch=True
    )
    assert isinstance(b, backend_mod.HttpBackend)


# ------------------------------------------------------------------- MCP_DBLP_HOME


def test_home_env_sets_all_dirs(server, rel09, tmp_path, monkeypatch, capsys):
    home = tmp_path / "big volume"
    monkeypatch.setenv("MCP_DBLP_HOME", str(home))
    assert local_index.default_index_dir() == str(home / "index")
    assert local_index.default_dump_dir() == str(home / "dump")
    publish(server, rel09, "2026-09-01")
    assert local_index.main(["fetch", "--manifest", f"{server.base}/latest.json"]) == 0
    idx = home / "index"
    assert (idx / "dblp-2026-09-01.sqlite").is_file()
    assert local_index.read_status(str(idx))["state"] == "done"
    assert (idx / "fetch_status.json").is_file()
    b = backend_mod.select_backend(env={})  # no index_dir: follows MCP_DBLP_HOME
    assert b.release == "2026-09-01"
    capsys.readouterr()
    local_index.main(["status"])
    out = capsys.readouterr().out
    assert f"MCP_DBLP_HOME={home}" in out and "release:      2026-09-01" in out


# ---------------------------------------------------------------------- disk space


def _free(monkeypatch, free: int):
    usage = shutil.disk_usage(".")
    monkeypatch.setattr(local_index.shutil, "disk_usage", lambda p: usage._replace(free=free))


def test_space_check_fails_before_download(server, rel09, idx, monkeypatch):
    publish(server, rel09, "2026-09-01", size=2_000_000_000, sqlite_size=4_000_000_000)
    _free(monkeypatch, 1_200_000_000)
    with pytest.raises(FetchError) as e:
        local_index.fetch(manifest=f"{server.base}/latest.json", index_dir=idx)
    msg = str(e.value)
    assert "not enough disk space in" in msg and idx in msg
    assert "about 6.3 GB, 1.2 GB free" in msg and "MCP_DBLP_HOME" in msg
    assert _zst_requests(server) == []
    assert local_index.read_status(idx)["state"] == "failed"


def test_space_check_subtracts_resumable_part(server, rel09, idx, monkeypatch):
    m = publish(server, rel09, "2026-09-01")
    zst = server.files[f"/dl/{m['file']}"]
    half = len(zst) // 2
    part = os.path.join(idx, m["file"] + ".part")
    with open(part, "wb") as f:
        f.write(zst[:half])
    full = round((m["size"] + m["sqlite_size"]) * 1.05)
    url = f"{server.base}/latest.json"
    # not resumable (no matching .sha256 tag): the whole download counts
    _free(monkeypatch, full - half)
    with pytest.raises(FetchError, match="not enough disk space"):
        local_index.fetch(manifest=url, index_dir=idx)
    with open(part + ".sha256", "w") as f:
        f.write(m["sha256"] + "\n")
    _free(monkeypatch, full - half - 1)
    with pytest.raises(FetchError, match="not enough disk space"):
        local_index.fetch(manifest=url, index_dir=idx)
    _free(monkeypatch, full - half)  # exactly enough once the .part counts
    local_index.fetch(manifest=url, index_dir=idx)
    assert _zst_requests(server) == [(f"/dl/{m['file']}", f"bytes={half}-")]


def test_first_start_shows_space_error(server, rel09, tmp_path, monkeypatch):
    publish(server, rel09, "2026-09-01")
    monkeypatch.setenv("MCP_DBLP_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MCP_DBLP_MANIFEST_URL", f"{server.base}/latest.json")
    _free(monkeypatch, 1000)
    b = backend_mod.select_backend(env={}, auto_fetch=True)  # the real fetch, in a thread
    b.fetcher.thread.join(10)
    with pytest.raises(backend_mod.IndexUnavailable) as e:
        b.search(query="treewidth")
    msg = str(e.value)
    assert msg.startswith("Download failed: not enough disk space in ")
    assert "set MCP_DBLP_HOME to a directory on a larger volume" in msg
    assert msg.endswith(". Run `mcp-dblp-index fetch` manually.")
    assert _zst_requests(server) == []


# --------------------------------------------- `current` without symlinks, cleanup


def _no_symlinks(monkeypatch):
    def symlink(*a, **k):
        raise OSError(1314, "A required privilege is not held by the client")

    monkeypatch.setattr(local_index.os, "symlink", symlink)


def test_current_text_file_when_symlinks_fail(server, mini_index, idx, monkeypatch):
    _no_symlinks(monkeypatch)
    rel08 = _index_copy(mini_index, os.path.join(idx, "..", "src-08.sqlite"), "2026-08-01")
    publish(server, rel08, "2026-08-01")
    local_index.fetch(manifest=f"{server.base}/latest.json", index_dir=idx)
    cur = os.path.join(idx, "current")
    assert not os.path.islink(cur)
    with open(cur) as f:
        assert f.read() == "dblp-2026-08-01.sqlite\n"
    b = backend_mod.select_backend(env={}, index_dir=idx)
    assert b.release == "2026-08-01"
    rel09 = _index_copy(mini_index, os.path.join(idx, "..", "src-09.sqlite"), "2026-09-01")
    publish(server, rel09, "2026-09-01")
    assert local_index.update(idx, manifest=f"{server.base}/latest.json", echo=False) == 0
    with open(cur) as f:
        assert f.read() == "dblp-2026-09-01.sqlite\n"
    assert b.release == "2026-09-01"  # the switch is picked up
    assert b.search(query="treewidth")
    assert not os.path.exists(os.path.join(idx, "dblp-2026-08-01.sqlite"))


def test_build_release_text_file_when_symlinks_fail(tmp_path, monkeypatch):
    _no_symlinks(monkeypatch)
    dump_dir, idx = tmp_path / "dump", tmp_path / "index"
    dump_dir.mkdir()
    import gzip

    data = os.path.join(os.path.dirname(__file__), "data", "mini_dblp.xml")
    with open(data, "rb") as src, gzip.open(dump_dir / "dblp-2026-09-01.xml.gz", "wb") as dst:
        shutil.copyfileobj(src, dst)
    out = local_index.build_release("2026-09-01", str(dump_dir), str(idx))
    assert not os.path.islink(idx / "current")
    assert (idx / "current").read_text() == "dblp-2026-09-01.sqlite\n"
    assert local_index.current_index(str(idx)) == out


def _touch(idx, *names):
    for n in names:
        with open(os.path.join(idx, n), "wb") as f:
            f.write(b"x")


def test_fetch_deletes_superseded_releases(server, mini_index, rel09, idx):
    old = _index_copy(mini_index, os.path.join(idx, "dblp-2026-08-01.sqlite"), "2026-08-01")
    local_index.set_current(old, idx)
    _touch(
        idx,
        "dblp-2026-07-01.sqlite",
        "dblp-2026-07-01.sqlite.zst",
        "dblp-2026-07-01.sqlite.zst.part",
        "dblp-2026-07-01.sqlite.zst.part.sha256",
        "dblp-2026-07-01.sqlite.tmp",
        "dblp-2026-10-01.sqlite",  # newer than the fetched release: kept
        "notes.txt",
    )
    publish(server, rel09, "2026-09-01")
    local_index.fetch(manifest=f"{server.base}/latest.json", index_dir=idx)
    assert sorted(os.listdir(idx)) == [
        "current",
        "dblp-2026-09-01.sqlite",
        "dblp-2026-10-01.sqlite",
        "fetch.lock",
        "fetch_status.json",
        "notes.txt",
    ]


def test_cleanup_never_deletes_current(mini_index, idx, monkeypatch):
    cur = _index_copy(mini_index, os.path.join(idx, "dblp-2026-08-01.sqlite"), "2026-08-01")
    local_index.set_current(cur, idx)
    _touch(idx, "dblp-2026-06-01.sqlite", "dblp-2026-07-01.sqlite", "dblp-2026-09-01.sqlite")
    monkeypatch.setenv("MCP_DBLP_INDEX", os.path.join(idx, "dblp-2026-06-01.sqlite"))
    deleted = local_index.cleanup_superseded(idx)
    assert deleted == [os.path.join(idx, "dblp-2026-07-01.sqlite")]
    # current, the MCP_DBLP_INDEX file and the newer release stay
    assert {"dblp-2026-06-01.sqlite", "dblp-2026-08-01.sqlite", "dblp-2026-09-01.sqlite"} <= set(
        os.listdir(idx)
    )


def test_cleanup_errors_ignored_and_retried(server, mini_index, rel09, idx, monkeypatch):
    old = _index_copy(mini_index, os.path.join(idx, "dblp-2026-08-01.sqlite"), "2026-08-01")
    local_index.set_current(old, idx)
    real_remove = os.remove

    def remove(path, *a, **k):  # Windows: a file open in a running server
        if os.path.basename(path) == "dblp-2026-08-01.sqlite":
            raise PermissionError(32, "The process cannot access the file")
        return real_remove(path, *a, **k)

    monkeypatch.setattr(local_index.os, "remove", remove)
    publish(server, rel09, "2026-09-01")
    url = f"{server.base}/latest.json"
    local_index.fetch(manifest=url, index_dir=idx)
    assert local_index.read_meta(local_index.current_index(idx))["release"] == "2026-09-01"
    assert os.path.exists(old)
    monkeypatch.setattr(local_index.os, "remove", real_remove)
    local_index.fetch(manifest=url, index_dir=idx)  # already installed: retries the cleanup
    assert not os.path.exists(old)
    assert len(_zst_requests(server)) == 1


# -------------------------------------------- instructions wait for a real answer


@pytest.mark.asyncio
async def test_instructions_wait_for_first_real_answer(mini_index, idx):
    from mcp.client import Client

    from mcp_dblp.server import create_server

    def fake_fetch(index_dir, status):
        status.state("downloading", 1270 << 20, 412 << 20)
        assert gate.wait(10)
        p = _index_copy(mini_index, os.path.join(index_dir, "dblp-2026-09-01.sqlite"), "2026-09-01")
        local_index.set_current(p, index_dir)
        return p

    gate = threading.Event()
    b = backend_mod.IndexBackend(index_dir=idx)
    b.start_fetch(fake_fetch)
    try:
        async with Client(create_server(b)) as client:
            for _ in range(2):
                r = await client.call_tool("search", {"query": "treewidth"})
                assert len(r.content) == 1
                assert r.content[0].text.startswith("The dblp index is being downloaded")
            gate.set()
            b.fetcher.thread.join(10)
            r = await client.call_tool("search", {"query": "treewidth"})
            assert len(r.content) == 2
            assert "journals/tcs/GodelS20" in r.content[0].text
            assert "DBLP Usage Instructions" in r.content[1].text
            r = await client.call_tool("search", {"query": "treewidth"})
            assert len(r.content) == 1
    finally:
        gate.set()
        b.fetcher.thread.join(10)


def test_first_start_follows_other_process(idx):
    # another process holds the lock: the server reports that process's status file
    other = local_index.FetchStatus(os.path.join(idx, local_index.STATUS_FILE))
    other.state("unpacking", 4150 << 20, 1000 << 20)
    with local_index._fetch_lock(idx):
        b = backend_mod.IndexBackend(index_dir=idx)
        b.start_fetch()
        b.fetcher.thread.join(5)
        assert b.fetcher.busy
        with pytest.raises(backend_mod.IndexUnavailable) as e:
            b.search(query="treewidth")
    assert str(e.value) == (
        "The dblp index has been downloaded and is being unpacked (1000 MB of 4150 MB). "
        "Retry in a few minutes."
    )
