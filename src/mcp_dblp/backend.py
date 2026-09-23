"""Backend selection: a local index of the dblp dump (default) or the dblp.org web API.

Both backends expose the same methods with the signatures and result dicts of
dblp_client, so the tool handlers in server.py are identical for both.

Selection (select_backend):
  1. MCP_DBLP_INDEX=http (or none/off/web)  web API (HttpBackend)
  2. MCP_DBLP_INDEX=<path to .sqlite>       local index at that path (IndexBackend)
  3. otherwise                              $MCP_DBLP_HOME/index/current (IndexBackend;
                                            MCP_DBLP_HOME defaults to ~/.mcp-dblp)
The web API is never a silent fallback.  IndexBackend re-resolves its index on every
tool call and reopens it when the file behind `current` changed (after
`mcp-dblp-index update`).  When no index is available, tool methods raise
IndexUnavailable with a message for the caller.  With auto_fetch (the server), a
missing `current` starts `mcp-dblp-index fetch` in a background thread and the
message reports its progress; a tool call waits for it only up to a bound (server.py).
"""

from __future__ import annotations

import datetime
import logging
import os
import threading
import time
from typing import Any

from mcp_dblp import dblp_client, local_index

logger = logging.getLogger("mcp_dblp")

ENV_VAR = "MCP_DBLP_INDEX"
HTTP_VALUES = {"http", "none", "off", "web"}


def _is_error(bibtex: str) -> bool:
    return bibtex.strip().startswith("% Error")


class HttpBackend:
    """dblp.org web API (dblp_client)."""

    name = "http"
    release = ""

    def describe(self) -> str:
        return f"dblp web API ({dblp_client.DBLP_BASE_URL})"

    def search(self, **kw) -> list[dict[str, Any]]:
        return dblp_client.search(**kw)

    def fuzzy_title_search(self, **kw) -> list[dict[str, Any]]:
        return dblp_client.fuzzy_title_search(**kw)

    def get_author_publications(self, **kw) -> dict[str, Any]:
        return dblp_client.get_author_publications(**kw)

    def get_venue_info(self, **kw) -> dict[str, Any]:
        return dblp_client.get_venue_info(**kw)

    def set_mirror(self, host: str) -> str:
        new_url = dblp_client.set_dblp_base_url(host)
        return f"DBLP mirror switched to {new_url}. All subsequent requests will use this mirror."

    @staticmethod
    def sanitize_key(dblp_key: str) -> str:
        """Remove a .bib extension, a dblp record URL prefix (any mirror, with or
        without scheme) and a 'DBLP:' prefix; the same rules as the local backend."""
        from mcp_dblp.local_client import sanitize_key

        return sanitize_key(dblp_key)

    def bibtex_for_citation(self, dblp_key: str, citation_key: str) -> str:
        """dblp's BibTeX for dblp_key with citation key citation_key, or '% Error ...'."""
        dblp_key = self.sanitize_key(dblp_key)
        url = f"{dblp_client.DBLP_BASE_URL}/rec/{dblp_key}.bib"
        bibtex = dblp_client.fetch_and_process_bibtex(url, citation_key)
        if not _is_error(bibtex) and not bibtex.lstrip().startswith("@"):
            # e.g. an HTML bot-check page served with status 200
            return f"% Error fetching {url}: response is not BibTeX ({bibtex.strip()[:60]!r})"
        return bibtex


class LocalBackend:
    """Local SQLite index of the dblp XML dump (local_client.LocalDblp).

    add_bibtex_entry falls back to the web API for keys missing from the index
    (records newer than the dump)."""

    name = "local"

    def __init__(self, path: str):
        from mcp_dblp.local_client import LocalDblp  # needs only sqlite3

        self.db = LocalDblp(path)
        self.path = self.db.db_path
        self.release = self.db.meta.get("release", "")
        self.http = HttpBackend()

    def describe(self) -> str:
        return (
            f"local dblp index {self.path} (release {self.release or 'unknown'}, "
            f"{self.db.meta.get('publications', '?')} publications)"
        )

    def search(self, **kw) -> list[dict[str, Any]]:
        return self.db.search(**kw)

    def fuzzy_title_search(self, **kw) -> list[dict[str, Any]]:
        return self.db.fuzzy_title_search(**kw)

    def get_author_publications(self, **kw) -> dict[str, Any]:
        return self.db.get_author_publications(**kw)

    def get_venue_info(self, **kw) -> dict[str, Any]:
        return self.db.get_venue_info(**kw)

    def set_mirror(self, host: str) -> str:
        new_url = dblp_client.set_dblp_base_url(host)
        return (
            f"A local DBLP index (release {self.release or 'unknown'}) is in use; searches do "
            f"not contact dblp.org. The mirror setting ({new_url}) only affects the web "
            "fallback for BibTeX keys that are missing from the local index."
        )

    def bibtex_for_citation(self, dblp_key: str, citation_key: str) -> str:
        from mcp_dblp.local_client import sanitize_key

        key = sanitize_key(dblp_key)
        try:
            bibtex = self.db.bibtex_for_key(key)
        except Exception as e:
            logger.error(f"Error rendering BibTeX for {key}: {e}", exc_info=True)
            return f"% Error: could not render BibTeX for {key}: {e}"
        if bibtex is not None:
            return dblp_client.replace_citation_key(bibtex, citation_key)
        logger.info(f"{key} not in local index; trying the dblp web API")
        bibtex = self.http.bibtex_for_citation(key, citation_key)
        if _is_error(bibtex):
            reason = bibtex.strip().removeprefix("% Error").lstrip(": ")
            if "not BibTeX" in reason:  # dblp.org's bot check page
                reason = "dblp.org answered with a web page, it blocks automated requests"
            return (
                f"% Error: DBLP key '{key}' not found in the local index "
                f"(release {self.release or 'unknown'}). Check the key against the search "
                "results; if it is correct, the record is newer than the local index and "
                f"cannot be added. (The web fallback failed too: {reason[:200]})"
            )
        return bibtex


class IndexUnavailable(Exception):  # noqa: N818 (a state, not a program error)
    """No local index can be used; str(exc) is the message for the tool caller."""


# FetchStatus states of a running fetch; a status file not rewritten for STALE_STATUS
# seconds belongs to a fetch that died (it is rewritten about once per second).
ACTIVE_STATES = ("starting", "downloading", "verifying", "unpacking", "installing")
STALE_STATUS = 120


def _status_active(snap: dict | None) -> bool:
    if not snap or snap.get("state") not in ACTIVE_STATES:
        return False
    try:
        updated = datetime.datetime.fromisoformat(str(snap.get("updated")).replace("Z", "+00:00"))
    except ValueError:
        return False
    age = datetime.datetime.now(datetime.UTC) - updated
    return age.total_seconds() < STALE_STATUS


class BackgroundFetch:
    """`mcp-dblp-index fetch` in a daemon thread, started by the server on first use."""

    def __init__(self, index_dir: str | None = None, fetch_fn=None):
        self.index_dir = index_dir or local_index.default_index_dir()
        os.makedirs(self.index_dir, exist_ok=True)
        self.status = local_index.FetchStatus(os.path.join(self.index_dir, local_index.STATUS_FILE))
        self.fetch_fn = fetch_fn or local_index.fetch
        self.error: str | None = None
        self.busy = False  # another process is fetching; follow its status file
        self.thread = threading.Thread(target=self._run, name="dblp-index-fetch", daemon=True)

    def start(self) -> None:
        logger.warning(f"No local dblp index in {self.index_dir}; downloading it in the background")
        self.thread.start()

    def _run(self) -> None:
        try:
            path = self.fetch_fn(index_dir=self.index_dir, status=self.status)
            logger.info(f"dblp index installed: {path}")
        except local_index.FetchBusyError:
            self.busy = True
            logger.info("Another process is fetching the dblp index; following its progress")
        except Exception as e:
            self.error = str(e) or type(e).__name__
            logger.error(f"dblp index download failed: {self.error}")

    def message(self) -> str:
        if self.error:
            return (
                f"Download failed: {self.error.rstrip('.')}. Run `mcp-dblp-index fetch` manually."
            )
        snap = local_index.read_status(self.index_dir) if self.busy else self.status.snapshot()
        return local_index.describe_status(snap)

    def in_progress(self) -> bool:
        """True while a download (ours, or another process's) may still install an index."""
        if self.error:
            return False
        if self.busy:
            return _status_active(local_index.read_status(self.index_dir))
        return self.thread.is_alive()


REOPEN_AFTER = 30  # seconds before an index that failed to open is tried again


class IndexBackend:
    """The local index, resolved on every call (MCP_DBLP_INDEX path or `current`).

    Holds a LocalBackend for the resolved file and replaces it when the file changes
    (path, inode or mtime), so an index switched by `mcp-dblp-index update` is picked up
    on the next tool call.  An index that fails to open is reported, and the previously
    opened one (if any) keeps serving."""

    name = "local"

    def __init__(self, path: str | None = None, index_dir: str | None = None):
        self.fixed_path = path
        self.index_dir = index_dir
        self._local: LocalBackend | None = None
        self._ident: tuple | None = None
        self._bad: tuple | None = None  # (ident, message, time) of a file that failed to open
        self.fetcher: BackgroundFetch | None = None

    def start_fetch(self, fetch_fn=None) -> BackgroundFetch:
        self.fetcher = BackgroundFetch(self.index_dir, fetch_fn)
        self.fetcher.start()
        return self.fetcher

    def fetch_in_progress(self) -> bool:
        """True while the first-start download may still make the index available."""
        return self.fetcher is not None and self.fetcher.in_progress()

    def _resolve(self) -> str | None:
        return self.fixed_path or local_index.current_index(self.index_dir)

    def _missing_message(self) -> str:
        if self.fixed_path:
            return (
                f"The local dblp index {self.fixed_path} (set by {ENV_VAR}) does not exist. "
                f"Fix {ENV_VAR}, or unset it and run `mcp-dblp-index fetch`."
            )
        if self.fetcher:
            return self.fetcher.message()
        index_dir = self.index_dir or local_index.default_index_dir()
        return (
            f"No local dblp index in {index_dir}. Run `mcp-dblp-index fetch` to download it "
            f"(or set {ENV_VAR}=http to use the dblp.org web API)."
        )

    def local(self) -> LocalBackend:
        """The LocalBackend for the current index; raises IndexUnavailable."""
        path = self._resolve()
        st = None
        if path:
            try:
                st = os.stat(path)
            except OSError:
                st = None
        if st is None:
            if self._local is not None and self.fixed_path is None:
                # `current` vanished (e.g. being replaced by hand): keep serving the old one
                return self._local
            raise IndexUnavailable(self._missing_message())
        ident = (os.path.realpath(path), st.st_dev, st.st_ino, st.st_mtime_ns)
        if ident == self._ident:
            return self._local
        if self._bad and self._bad[0] == ident and time.monotonic() - self._bad[2] < REOPEN_AFTER:
            if self._local is not None:
                return self._local
            raise IndexUnavailable(self._bad[1])
        try:
            new = LocalBackend(path)
        except Exception as e:
            msg = (
                f"Cannot open the local dblp index {path}: {e}. "
                "Run `mcp-dblp-index fetch --force` to download it again."
            )
            logger.error(msg)
            self._bad = (ident, msg, time.monotonic())
            if self._local is not None:
                return self._local
            raise IndexUnavailable(msg) from e
        old, self._local, self._ident, self._bad = self._local, new, ident, None
        if old is not None:
            old.db.close()
        logger.info(f"Backend: {new.describe()}")
        return new

    @property
    def release(self) -> str:
        try:
            return self.local().release
        except IndexUnavailable:
            return ""

    def describe(self) -> str:
        try:
            return self.local().describe()
        except IndexUnavailable as e:
            return str(e)

    def search(self, **kw) -> list[dict[str, Any]]:
        return self.local().search(**kw)

    def fuzzy_title_search(self, **kw) -> list[dict[str, Any]]:
        return self.local().fuzzy_title_search(**kw)

    def get_author_publications(self, **kw) -> dict[str, Any]:
        return self.local().get_author_publications(**kw)

    def get_venue_info(self, **kw) -> dict[str, Any]:
        return self.local().get_venue_info(**kw)

    def bibtex_for_citation(self, dblp_key: str, citation_key: str) -> str:
        return self.local().bibtex_for_citation(dblp_key, citation_key)

    def set_mirror(self, host: str) -> str:
        try:
            return self.local().set_mirror(host)
        except IndexUnavailable as e:
            new_url = dblp_client.set_dblp_base_url(host)
            return (
                f"DBLP mirror set to {new_url}. It is used only as a fallback for BibTeX keys "
                f"missing from the local index, which is not available yet: {e}"
            )


def select_backend(env: dict | None = None, index_dir: str | None = None, auto_fetch=False):
    """HttpBackend for MCP_DBLP_INDEX=http, else an IndexBackend (see module docstring).

    auto_fetch: start a background fetch when neither MCP_DBLP_INDEX nor `current`
    names an index (the server does this at start)."""
    env = os.environ if env is None else env
    value = (env.get(ENV_VAR) or "").strip()
    if value.lower() in HTTP_VALUES:
        backend = HttpBackend()
        logger.info(f"Backend: {backend.describe()}")
        return backend
    path = os.path.abspath(os.path.expanduser(value)) if value else None
    backend = IndexBackend(path, index_dir)
    try:
        backend.local()  # logs the backend
    except IndexUnavailable as e:
        if auto_fetch and path is None and local_index.current_index(index_dir) is None:
            backend.start_fetch()
        else:
            logger.error(str(e))
    return backend
