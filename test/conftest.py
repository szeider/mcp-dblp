"""Shared fixtures: a tiny local index built from test/data/mini_dblp.xml, an
isolated MCP_DBLP_HOME for every test, and a skip guard for tests that need the
dblp.org web API."""

import gzip
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

DATA = Path(__file__).resolve().parent / "data"
RELEASE = "2026-09-01"


@pytest.fixture(scope="session")
def mini_index(tmp_path_factory) -> Path:
    """Path to a slim index (schema 2) built from the hand-written XML fixture."""
    from mcp_dblp import local_index

    d = tmp_path_factory.mktemp("mini")
    dump = d / f"dblp-{RELEASE}.xml.gz"
    with open(DATA / "mini_dblp.xml", "rb") as src, gzip.open(dump, "wb") as dst:
        shutil.copyfileobj(src, dst)
    out = d / f"dblp-{RELEASE}.sqlite"
    assert local_index.build(str(dump), str(out))
    return out


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory, monkeypatch):
    """Keep every test away from the real ~/.mcp-dblp and from real index downloads."""
    monkeypatch.setenv("MCP_DBLP_HOME", str(tmp_path_factory.mktemp("mcp_dblp_home")))
    monkeypatch.setenv("MCP_DBLP_MANIFEST_URL", "http://127.0.0.1:9/no-manifest.json")
    monkeypatch.setenv("MCP_DBLP_FIRST_START_WAIT", "0")  # tests opt in to the wait
    monkeypatch.delenv("MCP_DBLP_INDEX", raising=False)


_online: bool | str | None = None


def _dblp_web_status() -> bool | str:
    """True if the dblp.org search API answers with JSON, else a skip reason."""
    import requests

    from mcp_dblp import dblp_client

    try:
        r = requests.get(
            f"{dblp_client.DBLP_BASE_URL}/search/publ/api",
            params={"q": "test", "format": "json", "h": 1},
            headers=dblp_client.HEADERS,
            timeout=dblp_client.REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        return f"dblp.org unreachable: {e}"
    if "not a bot" in r.text or "anubis" in r.text.lower():
        return "dblp.org serves its Anubis bot-check page to this client"
    return True


@pytest.fixture
def dblp_online():
    """Skip the test when the dblp.org web API is blocked (Anubis) or unreachable."""
    global _online
    if _online is None:
        _online = _dblp_web_status()
    if _online is not True:
        pytest.skip(_online)
