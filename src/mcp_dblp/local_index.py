# ruff: noqa: T201
"""Local dblp index: builder for the slim SQLite index, and the `mcp-dblp-index` CLI.

Normal use needs no builder: `fetch` downloads the prebuilt index that is published
monthly as a GitHub Release of https://github.com/szeider/mcp-dblp-index (manifest
latest.json, asset dblp-<release>.sqlite.zst).  The builder streams a gzipped dblp
XML dump (no unpacking) into SQLite, for those who build the index themselves.

CLI (see main()):
  mcp-dblp-index fetch [--release YYYY-MM-01] [--manifest URL] [--force] [--keep]
                  download the prebuilt index (resumable), verify size and sha256,
                  unpack, check meta, point `current` at it, delete the previous
                  current index and the .zst (--keep keeps the .zst)
  mcp-dblp-index update [--manifest URL] [--force] [--keep]
                  fetch if the manifest's release is newer than the current index
  mcp-dblp-index status                            show the current index
  mcp-dblp-index download [--release YYYY-MM-01]   fetch dump + .md5 from Dagstuhl
  mcp-dblp-index build [--release YYYY-MM-01]      build the index from the dump,
                                                   point `current` at it
  mcp-dblp-index build-file DUMP OUT [--limit N] [--every N]   raw builder
    --limit N   stop after N records (all types counted)
    --every N   keep only every N-th record (uniform sample, for size estimates)

Manifest (MCP_DBLP_MANIFEST_URL overrides the default URL; --release reads the
latest.json asset of release dblp-<release> instead):
  {"schema": 2, "release": "2026-09-01", "file": "dblp-2026-09-01.sqlite.zst",
   "size": <bytes of .zst>, "sha256": <hex of .zst>, "sqlite_size": <bytes>,
   "sqlite_sha256": <hex of the sqlite>, "urls": [<.zst url>, ...],
   "built": <ISO time>, "builder": "mcp-dblp <version>"}
Files live under $MCP_DBLP_HOME (default ~/.mcp-dblp): index/ holds the indexes,
`current` (a symlink, or a text file with the file name where symlinks are not
allowed, e.g. Windows without admin rights) and fetch_status.json (progress, see
FetchStatus; the server reads it while it downloads the index on first start);
dump/ holds dblp XML dumps for `build`.  After a successful fetch, index files of
releases older than the new one are deleted (a file still in use is retried at the
next fetch); the file `current` points to is never deleted.

The dump references an external DTD (dblp-*.dtd, hosted on dblp.org behind the
bot check) only for named character entities.  We replace the DOCTYPE with an
internal subset declaring every HTML5 named entity, so expat resolves them.

Schema version 2 ("H + person", see prototype/build_index_slim.py for the measurements):
  publ      one row per publication (no www records).  The rowid `id` is
            (year - 1900) << 21 | running number within the year (missing year:
            bucket 0), so ascending id = ascending year, a rowid range selects a
            year range, and ids fit in 4 bytes.  Compact encodings:
              type   INT code (TYPES), publtype TEXT
              mdate  INT yyyymmdd
              venue  journal or booktitle; vk = 1 when the field is the other one
                     than the type default (article -> journal, else booktitle)
              ee     first ee, plus URN (nbn-resolving) and ECCC ees; NULL when it
                     is exactly https://doi.org/<doi>
              extra  JSON with rare fields: month school address isbn note chapter
                     series_href
              title  keeps dblp's inline markup as an XML fragment when present
                     ('H<sub>2</sub>O', entities escaped); plain text otherwise.
                     The FTS title column gets the flattened text (plain_title).
            dropped: html url, cdrom, publnr, ees after the first except URN
            (nbn-resolving) and ECCC links
            NOTE: publ_fts must not be rebuilt with 'rebuild' (it would index the
            markup); re-run the INSERT ... plain_title(...) below instead.
  publ_v    view with the old column names/encodings (type text, journal,
            booktitle, mdate text, ee, school, ...) for renderers
  publ_fts  FTS5 over title/authors/venue, external content (content='publ'),
            no 'data' records
  person    homepages/ records: names (aliases), urls, note
  meta      dump, release, records, publications, schema
There is no author table: author lookups use the FTS authors column plus an exact
check on publ.authors / publ.editors.
"""

import argparse
import contextlib
import datetime
import gzip
import hashlib
import html.entities
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
from xml.sax.saxutils import escape as xml_escape

import requests
from lxml import etree

from mcp_dblp.local_client import sqlite_ro_uri

SCHEMA_VERSION = "2"
RECORD_TAGS = {
    "article",
    "inproceedings",
    "proceedings",
    "book",
    "incollection",
    "phdthesis",
    "mastersthesis",
    "www",
    "data",
}
SCALAR_FIELDS = [
    "title",
    "year",
    "journal",
    "booktitle",
    "volume",
    "number",
    "pages",
    "month",
    "publisher",
    "series",
    "school",
    "address",
    "isbn",
    "note",
    "crossref",
    "url",
    "chapter",
    "publnr",
    "cdrom",
]
TYPES = [
    "article",
    "inproceedings",
    "proceedings",
    "book",
    "incollection",
    "phdthesis",
    "mastersthesis",
    "data",
]
TYPE_CODE = {t: i for i, t in enumerate(TYPES)}
EXTRA_FIELDS = ["month", "school", "address", "isbn", "note", "chapter"]
ID_SHIFT = 21  # id = (year - 1900) << 21 | running number within that year (< 2M/year)


def year_bucket(year: int | None) -> int:
    """High part of publ.id; 0 for a missing year (or one before 1900)."""
    return max(0, (year or 0) - 1900)


TOKENIZE = "unicode61 remove_diacritics 2"

SCHEMA = """
PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-512000;
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS publ(
  id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, type INT, publtype TEXT, mdate INT,
  title TEXT, year INT, venue TEXT, vk INT, volume TEXT, number TEXT, pages TEXT,
  publisher TEXT, series TEXT, crossref TEXT, doi TEXT, ee TEXT,
  authors TEXT, editors TEXT, stream TEXT, extra TEXT
);
CREATE TABLE IF NOT EXISTS person(key TEXT PRIMARY KEY, names TEXT, urls TEXT, note TEXT);
"""
_TYPE_CASE = "CASE type " + " ".join(f"WHEN {i} THEN '{t}'" for i, t in enumerate(TYPES)) + " END"
POST = f"""
CREATE VIEW IF NOT EXISTS publ_v AS SELECT
  id, key, {_TYPE_CASE} AS type,
  printf('%04d-%02d-%02d', mdate / 10000, mdate / 100 % 100, mdate % 100) AS mdate,
  publtype, title, year,
  CASE WHEN (type = 0) = (vk IS NULL) THEN venue END AS journal,
  CASE WHEN (type = 0) = (vk IS NULL) THEN NULL ELSE venue END AS booktitle,
  venue, volume, number, pages, publisher, series, crossref, doi,
  COALESCE(ee, 'https://doi.org/' || doi) AS ee, authors, editors, stream,
  json_extract(extra, '$.month') AS month, json_extract(extra, '$.school') AS school,
  json_extract(extra, '$.address') AS address, json_extract(extra, '$.isbn') AS isbn,
  json_extract(extra, '$.note') AS note, json_extract(extra, '$.chapter') AS chapter,
  json_extract(extra, '$.series_href') AS series_href
FROM publ;
CREATE VIRTUAL TABLE IF NOT EXISTS publ_fts USING fts5(
  title, authors, venue, content='publ', content_rowid='id', tokenize="{TOKENIZE}"
);
INSERT INTO publ_fts(rowid, title, authors, venue)
  SELECT id, plain_title(title), authors, COALESCE(venue, '') FROM publ
  WHERE type <> {TYPE_CODE["data"]} ORDER BY id;
INSERT INTO publ_fts(publ_fts) VALUES('optimize');
"""


def entity_subset() -> bytes:
    decls = []
    for name, ch in html.entities.html5.items():
        if not name.endswith(";"):
            continue
        name = name[:-1]
        if name in ("amp", "lt", "gt", "quot", "apos"):
            continue
        val = "".join(f"&#{ord(c)};" for c in ch)
        decls.append(f'<!ENTITY {name} "{val}">')
    return ("<!DOCTYPE dblp [\n" + "\n".join(decls) + "\n]>\n").encode("ascii")


class DoctypeSwap(io.RawIOBase):
    """Wrap a binary stream; replace the <!DOCTYPE ...> line with our subset."""

    def __init__(self, raw):
        self.raw = raw
        head = b""
        while b"<dblp>" not in head:
            chunk = raw.read(65536)
            if not chunk:
                raise ValueError("no <dblp> root found")
            head += chunk
        head = re.sub(rb"<!DOCTYPE[^>]*>\s*", entity_subset(), head, count=1)
        self.buf = head

    def readable(self):
        return True

    def readinto(self, b):
        if self.buf:
            n = min(len(b), len(self.buf))
            b[:n] = self.buf[:n]
            self.buf = self.buf[n:]
            return n
        data = self.raw.read(len(b))
        b[: len(data)] = data
        return len(data)


def text(el) -> str:
    """Text content including inline markup (<i>, <sub>...) flattened."""
    return "".join(el.itertext()).strip()


def inner_xml(el) -> str:
    """Element content with inline markup kept as an XML fragment ('x<sup>2</sup>').

    Plain text (no child elements) is returned unescaped, exactly like text().
    """
    if len(el) == 0:
        return text(el)
    parts = [xml_escape(el.text or "")]
    parts.extend(etree.tostring(ch, encoding="unicode", with_tail=True) for ch in el)
    return "".join(parts).strip()


_KEEP_EE = re.compile(r"nbn-resolving\.(?:org|de)/urn:|eccc\.weizmann\.ac\.il/")


_TAG = re.compile(r"</?(?:i|b|u|tt|em|sub|sup)>")


def plain_title(t: str | None) -> str:
    """Flatten a stored title fragment to plain text (what the FTS index sees).

    Only values with inline markup are fragments (escaped); plain titles are
    returned unchanged.  bibtex_render.TAG plus <u>."""
    if not t or not _TAG.search(t):
        return t or ""
    return html.unescape(_TAG.sub("", t))


def publ_row(seq, key, tag, mdate, publtype, f, ees, people, streams, series_href):
    """One publ row (schema version 2).  seq: dict year bucket -> next number."""
    year = int(f["year"]) if f["year"] and f["year"].isdigit() else None
    b = year_bucket(year)
    k = seq.get(b, 0)
    seq[b] = k + 1
    if k >= 1 << ID_SHIFT:
        raise OverflowError(f"more than {1 << ID_SHIFT} records in year {year}")
    doi = next((e.split("doi.org/", 1)[1] for e in ees if "doi.org/" in e), None)
    # the renderer uses the first ee (url field) and scans all ees for a URN
    # (nbn-resolving) and an ECCC report id; other ees are dropped
    kept = ees[:1] + [e for e in ees[1:] if _KEEP_EE.search(e)]
    ee = " | ".join(kept) or None
    if ee and doi and ee == "https://doi.org/" + doi:
        ee = None
    venue = f["journal"] or f["booktitle"]
    is_journal = f["journal"] is not None
    vk = 1 if venue is not None and is_journal != (tag == "article") else None
    extra = {k: f[k] for k in EXTRA_FIELDS if f[k]}
    if series_href:
        extra["series_href"] = series_href
    return (
        (b << ID_SHIFT) | k,
        key,
        TYPE_CODE[tag],
        publtype,
        int(mdate.replace("-", "")) if mdate else None,
        f["title"],
        year,
        venue,
        vk,
        f["volume"],
        f["number"],
        f["pages"],
        f["publisher"],
        f["series"],
        f["crossref"],
        doi,
        ee,
        " | ".join(p[1] for p in people if p[0] == "author") or None,
        " | ".join(p[1] for p in people if p[0] == "editor") or None,
        " | ".join(streams) or None,
        json.dumps(extra, ensure_ascii=False, separators=(",", ":")) if extra else None,
    )


def build(dump: str, out: str, limit: int | None = None, keep=None, every: int = 1) -> bool:
    """Build the index at out from dump.  Returns False if the dump ended early
    (truncated or malformed); the records read so far are kept.

    keep: optional predicate(element) -> bool; records failing it are skipped."""
    _remove(out, out + "-journal", out + "-wal", out + "-shm")  # never append to an old index
    db = sqlite3.connect(out)
    db.executescript(SCHEMA)
    raw = gzip.open(dump, "rb")  # noqa: SIM115 (closed after parsing)
    src = io.BufferedReader(DoctypeSwap(raw), 1 << 20)
    ctx = etree.iterparse(src, events=("end",), tag=list(RECORD_TAGS), huge_tree=True)
    t0 = time.time()
    n = n_publ = 0
    complete = True
    seq: dict[int, int] = {}
    publ_rows, person_rows = [], []
    try:
        for _, el in ctx:
            n += 1
            key = el.get("key")
            if (every > 1 and n % every) or (keep is not None and not keep(el)):
                el.clear()
                while el.getprevious() is not None:
                    del el.getparent()[0]
                if limit and n >= limit:
                    break
                continue
            f = {k: None for k in SCALAR_FIELDS}
            ees, streams, urls = [], [], []
            people = []  # (role, name)
            series_href = None
            for ch in el:
                tag = ch.tag
                if tag in ("author", "editor"):
                    people.append((tag, text(ch)))
                elif tag == "ee":
                    ees.append(text(ch))
                elif tag == "stream":
                    streams.append(text(ch))
                elif tag == "url":
                    urls.append(text(ch))
                elif tag in f and f[tag] is None:  # the first occurrence of a field wins
                    f[tag] = inner_xml(ch) if tag == "title" else text(ch)
                    if tag == "series":
                        series_href = ch.get("href")
            if el.tag == "www":
                if key and key.startswith("homepages/"):
                    person_rows.append(
                        (key, " | ".join(p[1] for p in people), " | ".join(urls), f["note"])
                    )
            else:
                publ_rows.append(
                    publ_row(
                        seq,
                        key,
                        el.tag,
                        el.get("mdate"),
                        el.get("publtype"),
                        f,
                        ees,
                        people,
                        streams,
                        series_href,
                    )
                )
                n_publ += 1
            el.clear()
            while el.getprevious() is not None:
                del el.getparent()[0]
            if len(publ_rows) >= 20000:
                flush(db, publ_rows, person_rows)
                print(f"{n:>9} records  {time.time() - t0:7.0f}s", file=sys.stderr, flush=True)
            if limit and n >= limit:
                break
    except (EOFError, etree.XMLSyntaxError) as e:
        print(f"stopped early after {n} records: {e}", file=sys.stderr)
        complete = False
    finally:
        raw.close()
    flush(db, publ_rows, person_rows)
    print(f"parsed {n} records in {time.time() - t0:.0f}s; building indexes", file=sys.stderr)
    db.create_function("plain_title", 1, plain_title, deterministic=True)
    db.executescript(POST)
    m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(dump))
    meta = {
        "dump": os.path.basename(dump),
        "release": m.group(1) if m else "",
        "records": str(n),
        "publications": str(n_publ),
        "schema": SCHEMA_VERSION,
    }
    if every > 1:
        meta["sample_every"] = str(every)
    db.executemany("INSERT OR REPLACE INTO meta VALUES(?, ?)", meta.items())
    db.commit()
    db.close()
    print(f"done in {time.time() - t0:.0f}s", file=sys.stderr)
    return complete


def flush(db, publ_rows, person_rows):
    db.executemany("INSERT OR REPLACE INTO publ VALUES(" + ",".join("?" * 21) + ")", publ_rows)
    db.executemany("INSERT OR REPLACE INTO person VALUES(?,?,?,?)", person_rows)
    db.commit()
    publ_rows.clear()
    person_rows.clear()


# ---------------------------------------------------------------------------
# Index location and the `mcp-dblp-index` CLI
# ---------------------------------------------------------------------------

HOME_ENV = "MCP_DBLP_HOME"  # base directory for index/ and dump/ (default ~/.mcp-dblp)
DEFAULT_HOME = "~/.mcp-dblp"
CURRENT = "current"  # symlink (or text file with a file name) in the index directory
DUMP_URL = "https://drops.dagstuhl.de/storage/artifacts/dblp/xml/{y}/dblp-{r}.xml.gz"
RELEASE_RE = re.compile(r"^\d{4}-\d{2}-01$")
HTTP_TIMEOUT = 60


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("mcp-dblp")
    except Exception:
        return "x.x.x"


def user_agent() -> str:
    return f"mcp-dblp/{_version()} (https://github.com/szeider/mcp-dblp)"


def home_dir() -> str:
    """$MCP_DBLP_HOME, else ~/.mcp-dblp (read at every call, so tests can change it)."""
    return os.path.abspath(os.path.expanduser(os.environ.get(HOME_ENV) or DEFAULT_HOME))


def default_index_dir() -> str:
    return os.path.join(home_dir(), "index")


def default_dump_dir() -> str:
    return os.path.join(home_dir(), "dump")


def index_file(release: str, index_dir: str | None = None) -> str:
    return os.path.join(index_dir or default_index_dir(), f"dblp-{release}.sqlite")


def dump_file(release: str, dump_dir: str | None = None) -> str:
    return os.path.join(dump_dir or default_dump_dir(), f"dblp-{release}.xml.gz")


def current_index(index_dir: str | None = None) -> str | None:
    """Path of the index that `current` points to, or None.

    `current` is a symlink to the index file, or (where symlinks are unavailable) a
    text file holding its path (relative paths are relative to the index directory).
    """
    index_dir = index_dir or default_index_dir()
    cur = os.path.join(index_dir, CURRENT)
    if os.path.islink(cur):
        target = os.path.join(index_dir, os.readlink(cur))
        return target if os.path.isfile(target) else None
    if not os.path.isfile(cur):
        return None
    with open(cur, "rb") as f:
        head = f.read(16)
    if head.startswith(b"SQLite format 3"):
        return cur
    with open(cur, encoding="utf-8") as f:
        target = os.path.join(index_dir, os.path.expanduser(f.read().strip()))
    return target if os.path.isfile(target) else None


def set_current(path: str, index_dir: str | None = None) -> None:
    """Point `current` at path (atomically replaced)."""
    index_dir = index_dir or default_index_dir()
    cur = os.path.join(index_dir, CURRENT)
    tmp = cur + ".tmp"
    if os.path.lexists(tmp):
        os.remove(tmp)
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(index_dir))
    except ValueError:  # Windows: path on another drive
        rel = os.path.abspath(path)
    try:
        os.symlink(rel, tmp)
    except (OSError, NotImplementedError):
        # no symlink support (Windows without admin rights / developer mode):
        # plain text file with the file name, read back by current_index()
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(rel + "\n")
    os.replace(tmp, cur)


def read_meta(path: str) -> dict[str, str]:
    conn = sqlite3.connect(sqlite_ro_uri(path), uri=True)
    try:
        return dict(conn.execute("SELECT k, v FROM meta"))
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _check_release(release: str) -> str:
    if not RELEASE_RE.match(release):
        raise SystemExit(f"invalid release {release!r}; expected YYYY-MM-01")
    return release


def _get(url: str, **kw) -> requests.Response:
    return requests.get(url, headers={"User-Agent": user_agent()}, timeout=HTTP_TIMEOUT, **kw)


def _candidate_releases(today=None, months: int = 4) -> list[str]:
    """First-of-month dates, newest first, starting with the current month."""
    d = today or datetime.date.today()
    y, m = d.year, d.month
    out = []
    for _ in range(months):
        out.append(f"{y:04d}-{m:02d}-01")
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return out


def latest_release() -> str:
    """Newest release on the Dagstuhl server (probes the .md5 of recent months)."""
    for r in _candidate_releases():
        url = DUMP_URL.format(y=r[:4], r=r) + ".md5"
        try:
            resp = requests.head(url, headers={"User-Agent": user_agent()}, timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            raise SystemExit(f"cannot reach {url}: {e}") from e
        if resp.status_code == 200:
            return r
    raise SystemExit("no dblp dump found for the last months on drops.dagstuhl.de")


def _md5_of(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def download(release: str, dump_dir: str | None = None) -> str:
    """Download dump + .md5 into dump_dir, verify the md5; returns the dump path."""
    dump_dir = dump_dir or default_dump_dir()
    os.makedirs(dump_dir, exist_ok=True)
    url = DUMP_URL.format(y=release[:4], r=release)
    path = dump_file(release, dump_dir)
    resp = _get(url + ".md5")
    if resp.status_code != 200:
        raise SystemExit(f"{url}.md5: HTTP {resp.status_code} (no such release?)")
    expected = resp.text.split()[0].lower() if resp.text.split() else ""
    if not re.fullmatch(r"[0-9a-f]{32}", expected):
        raise SystemExit(f"{url}.md5: unexpected content {resp.text[:80]!r}")
    with open(path + ".md5", "w", encoding="utf-8") as f:
        f.write(resp.text)
    if os.path.exists(path):
        print(f"{path} exists; verifying md5", file=sys.stderr)
        if _md5_of(path) == expected:
            print("md5 ok", file=sys.stderr)
            return path
        print("md5 mismatch; downloading again", file=sys.stderr)
    part = path + ".part"
    h = hashlib.md5()
    t0 = time.time()
    with _get(url, stream=True) as r:
        if r.status_code != 200:
            raise SystemExit(f"{url}: HTTP {r.status_code}")
        total = int(r.headers.get("Content-Length") or 0)
        done = last = 0
        with open(part, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if done - last >= 50 << 20:
                    last = done
                    pct = f" ({100 * done / total:.0f}%)" if total else ""
                    print(f"  {done >> 20} MB{pct}  {time.time() - t0:.0f}s", file=sys.stderr)
    if h.hexdigest() != expected:
        os.remove(part)
        raise SystemExit(f"md5 mismatch for {url}: got {h.hexdigest()}, expected {expected}")
    os.replace(part, path)
    print(f"downloaded {path} ({os.path.getsize(path) >> 20} MB, md5 ok)", file=sys.stderr)
    return path


def _newest_dump(dump_dir: str | None = None) -> str | None:
    dump_dir = dump_dir or default_dump_dir()
    if not os.path.isdir(dump_dir):
        return None
    rels = sorted(
        m.group(1)
        for f in os.listdir(dump_dir)
        if (m := re.fullmatch(r"dblp-(\d{4}-\d{2}-01)\.xml\.gz", f))
    )
    return rels[-1] if rels else None


def build_release(
    release: str, dump_dir: str | None = None, index_dir: str | None = None, switch: bool = True
) -> str:
    """Build index_dir/dblp-<release>.sqlite from the dump and point `current` at it."""
    index_dir = index_dir or default_index_dir()
    dump = dump_file(release, dump_dir)
    if not os.path.isfile(dump):
        raise SystemExit(f"{dump} not found; run 'mcp-dblp-index download --release {release}'")
    os.makedirs(index_dir, exist_ok=True)
    out = index_file(release, index_dir)
    part = out + ".part"
    for p in (part, part + "-journal"):
        if os.path.exists(p):
            os.remove(p)
    if not build(dump, part):
        raise SystemExit(f"{dump} is truncated or malformed; partial index left at {part}")
    meta = read_meta(part)
    if meta.get("release") != release:  # the dump name carries the date; keep it consistent
        conn = sqlite3.connect(part)
        conn.execute("INSERT OR REPLACE INTO meta VALUES('release', ?)", (release,))
        conn.commit()
        conn.close()
    os.replace(part, out)
    if switch:
        set_current(out, index_dir)
        print(f"current -> {out}", file=sys.stderr)
    return out


def status(index_dir: str | None = None, dump_dir: str | None = None) -> int:
    index_dir = index_dir or default_index_dir()
    cur = current_index(index_dir)
    if os.environ.get(HOME_ENV):
        print(f"{HOME_ENV}={os.environ[HOME_ENV]}")
    env = os.environ.get("MCP_DBLP_INDEX")
    if env:
        print(f"MCP_DBLP_INDEX={env} (overrides {os.path.join(index_dir, CURRENT)})")
    if cur is None:
        print(f"no current index in {index_dir}; run 'mcp-dblp-index fetch'")
    else:
        meta = read_meta(cur)
        print(f"current index: {os.path.realpath(cur)}")
        print(f"  release:      {meta.get('release', '?')}")
        print(f"  schema:       {meta.get('schema', '?')}")
        print(f"  records:      {meta.get('records', '?')}")
        print(f"  publications: {meta.get('publications', '?')}")
        print(f"  size:         {os.path.getsize(cur) / 2**20:.0f} MB")
    for d, pat in (
        (index_dir, r"dblp-.*\.sqlite"),
        (dump_dir or default_dump_dir(), r"dblp-.*\.xml\.gz"),
    ):
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if re.fullmatch(pat, f):
                    p = os.path.join(d, f)
                    print(f"  {p}  {os.path.getsize(p) / 2**20:.0f} MB")
    return 0


# ---------------------------------------------------------------------------
# Prebuilt index: manifest, fetch, update
# ---------------------------------------------------------------------------

MANIFEST_URL = "https://github.com/szeider/mcp-dblp-index/releases/latest/download/latest.json"
RELEASE_MANIFEST_URL = (
    "https://github.com/szeider/mcp-dblp-index/releases/download/dblp-{release}/latest.json"
)
MANIFEST_ENV = "MCP_DBLP_MANIFEST_URL"
STATUS_FILE = "fetch_status.json"
LOCK_FILE = "fetch.lock"
CHUNK = 1 << 20
DOWNLOAD_PASSES = 3  # attempts over all manifest urls (each resumes the .part)
# zstd -19 uses windows <= 8 MB; allow long-distance mode (--long=31) as well.
MAX_WINDOW = 1 << 31
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_FILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.sqlite\.zst")


class FetchError(Exception):
    """A fetch or update failed; the message is meant for the user."""


class FetchBusyError(FetchError):
    """Another process is fetching into the same index directory."""


def manifest_url(release: str | None = None, url: str | None = None) -> str:
    """--manifest URL, else the manifest of release (if given), else the env/default URL."""
    if url:
        return url
    if release:
        return RELEASE_MANIFEST_URL.format(release=release)
    return os.environ.get(MANIFEST_ENV) or MANIFEST_URL


def read_manifest(url: str) -> dict:
    """Download and validate the manifest (format: module docstring)."""
    try:
        resp = _get(url)
    except requests.RequestException as e:
        raise FetchError(f"cannot read the manifest {url}: {e}") from e
    if resp.status_code != 200:
        raise FetchError(f"cannot read the manifest {url}: HTTP {resp.status_code}")
    try:
        m = resp.json()
    except ValueError as e:
        raise FetchError(f"manifest {url} is not JSON: {resp.text[:80]!r}") from e
    return validate_manifest(m, url)


def validate_manifest(m, url: str = "manifest") -> dict:
    if not isinstance(m, dict):
        raise FetchError(f"{url}: not a JSON object")
    missing = [
        k
        for k in ("schema", "release", "file", "size", "sha256", "sqlite_size", "sqlite_sha256")
        if k not in m
    ]
    if missing or not m.get("urls"):
        raise FetchError(f"{url}: missing fields {missing + ([] if m.get('urls') else ['urls'])}")
    if str(m["schema"]) != SCHEMA_VERSION:
        raise FetchError(
            f"{url}: index schema {m['schema']}, this mcp-dblp reads schema {SCHEMA_VERSION}; "
            "upgrade mcp-dblp (or use --release for an older index)"
        )
    if not isinstance(m["release"], str) or not RELEASE_RE.match(m["release"]):
        raise FetchError(f"{url}: invalid release {m['release']!r}")
    if not isinstance(m["file"], str) or not _FILE_RE.fullmatch(m["file"]):
        raise FetchError(f"{url}: invalid file name {m['file']!r}")
    for k in ("size", "sqlite_size"):
        if not isinstance(m[k], int) or m[k] <= 0:
            raise FetchError(f"{url}: invalid {k} {m[k]!r}")
    for k in ("sha256", "sqlite_sha256"):
        if not isinstance(m[k], str) or not _SHA256_RE.fullmatch(m[k].lower()):
            raise FetchError(f"{url}: invalid {k} {m[k]!r}")
        m[k] = m[k].lower()
    urls = m["urls"]
    if not isinstance(urls, list) or not all(
        isinstance(u, str) and u.startswith(("https://", "http://")) for u in urls
    ):
        raise FetchError(f"{url}: invalid urls {urls!r}")
    return m


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class FetchStatus:
    """Progress of a fetch, kept in memory and mirrored to fetch_status.json.

    File format (JSON object):
      state        starting | downloading | verifying | unpacking | installing |
                   done | failed
      bytes_done   progress of the current state (downloading/verifying: bytes of
      bytes_total  the .zst; unpacking: bytes of the sqlite written)
      eta_seconds  estimated seconds left in the current state, or null
      started      UTC ISO time the fetch started
      updated      UTC ISO time of this write
      release      the release being fetched (null before the manifest is read)
      error        the error message when state = failed, else null
      pid          the fetching process
    The file is rewritten atomically at most once per second (and on every state
    change).  With echo=True progress is also printed to stderr.
    """

    def __init__(self, path: str | None = None, echo: bool = False):
        self.path = path
        self.echo = echo
        self._lock = threading.Lock()
        self._last_write = 0.0
        self._last_echo = 0.0
        self._t0 = time.monotonic()
        self._b0 = 0
        self.data = {
            "state": "starting",
            "bytes_done": 0,
            "bytes_total": 0,
            "eta_seconds": None,
            "started": _now_iso(),
            "updated": _now_iso(),
            "release": None,
            "error": None,
            "pid": os.getpid(),
        }

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.data)

    def state(self, state: str, bytes_total: int = 0, bytes_done: int = 0, **kw) -> None:
        """Enter a new state (resets the rate estimate); always written."""
        with self._lock:
            self._t0, self._b0 = time.monotonic(), bytes_done
            self.data.update(
                state=state, bytes_total=bytes_total, bytes_done=bytes_done, eta_seconds=None, **kw
            )
        self._publish(force=True)

    def progress(self, bytes_done: int) -> None:
        with self._lock:
            self.data["bytes_done"] = bytes_done
            dt = time.monotonic() - self._t0
            moved = bytes_done - self._b0
            if dt >= 1 and moved > 0 and self.data["bytes_total"]:
                left = max(0, self.data["bytes_total"] - bytes_done)
                self.data["eta_seconds"] = round(left * dt / moved)
        self._publish()

    def _publish(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_write < 1:
            return
        self._last_write = now
        with self._lock:
            self.data["updated"] = _now_iso()
            data = dict(self.data)
        if self.path:
            tmp = f"{self.path}.{os.getpid()}.{threading.get_ident()}.tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                os.replace(tmp, self.path)
            except OSError:
                pass  # progress reporting must never break the fetch
        if self.echo:
            self._echo(data, force, now)

    def _echo(self, d: dict, force: bool, now: float) -> None:
        tty = sys.stderr.isatty()
        if not (force or tty or now - self._last_echo >= 30):
            return
        self._last_echo = now
        line = f"{d['state']}"
        if d["bytes_total"]:
            done, total = d["bytes_done"] >> 20, d["bytes_total"] >> 20
            line += f"  {done} of {total} MB ({100 * d['bytes_done'] / d['bytes_total']:.0f}%)"
            if d["eta_seconds"] is not None:
                line += f", {format_eta(d['eta_seconds'])} left"
        if d["state"] == "failed":
            line += f": {d['error']}"
        end = "" if tty and d["state"] in ("downloading", "verifying", "unpacking") else "\n"
        print(("\r" if tty else "") + line.ljust(60), end=end, file=sys.stderr, flush=True)


def format_eta(seconds: int | None) -> str:
    if seconds is None:
        return "an unknown time"
    if seconds < 60:
        return "less than a minute"
    return f"about {round(seconds / 60)} min"


def read_status(index_dir: str | None = None) -> dict | None:
    """The last fetch_status.json written in index_dir, or None."""
    try:
        with open(
            os.path.join(index_dir or default_index_dir(), STATUS_FILE), encoding="utf-8"
        ) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def describe_status(d: dict | None) -> str:
    """User-facing message for a fetch in progress (d: FetchStatus data)."""
    if not d:
        return "The dblp index is being downloaded for first use. Retry in a few minutes."
    state = d.get("state")
    done, total = (d.get("bytes_done") or 0) >> 20, (d.get("bytes_total") or 0) >> 20
    if d.get("eta_seconds") is None:
        eta, retry = "", "Retry in a few minutes."
    else:
        eta, retry = f", {format_eta(d['eta_seconds'])} left", "Retry after that."
    if state == "failed":
        error = str(d.get("error") or "unknown error").rstrip(".")
        return f"Download failed: {error}. Run `mcp-dblp-index fetch` manually."
    if state == "downloading":
        return (
            f"The dblp index is being downloaded for first use: {done} MB of {total} MB"
            f"{eta}. {retry}"
        )
    if state in ("verifying", "unpacking"):
        return (
            f"The dblp index has been downloaded and is being "
            f"{'verified' if state == 'verifying' else 'unpacked'} "
            f"({done} MB of {total} MB{eta}). {retry}"
        )
    if state == "installing":
        return "The dblp index is being installed. Retry in a few seconds."
    if state == "done":
        return "The dblp index has just been installed. Retry now."
    return "The dblp index is being downloaded for first use. Retry in a few minutes."


@contextlib.contextmanager
def _fetch_lock(index_dir: str):
    """Exclusive, non-blocking lock on index_dir/fetch.lock (released on exit or crash)."""
    f = open(os.path.join(index_dir, LOCK_FILE), "a+")  # noqa: SIM115 (closed below)
    try:
        try:
            import fcntl

            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                raise FetchBusyError(f"another fetch is running in {index_dir}") from e
        except ImportError:  # Windows
            import msvcrt

            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as e:
                raise FetchBusyError(f"another fetch is running in {index_dir}") from e
        yield
    finally:
        f.close()


def _remove(*paths: str) -> None:
    for p in paths:
        with contextlib.suppress(FileNotFoundError):
            os.remove(p)


def _sha256_file(path: str, st: FetchStatus) -> str:
    h = hashlib.sha256()
    done = 0
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
            done += len(chunk)
            st.progress(done)
    return h.hexdigest()


def _download_from(url: str, part: str, size: int, st: FetchStatus) -> None:
    """Download url into part, resuming with an HTTP Range request."""
    have = os.path.getsize(part) if os.path.exists(part) else 0
    if have == size:
        return
    headers = {"User-Agent": user_agent()}
    if have:
        headers["Range"] = f"bytes={have}-"
    with requests.get(url, headers=headers, timeout=HTTP_TIMEOUT, stream=True) as r:
        if r.status_code == 206:
            m = re.match(r"bytes (\d+)-", r.headers.get("Content-Range", ""))
            if not m or int(m.group(1)) != have:
                raise FetchError(
                    f"{url}: unexpected Content-Range {r.headers.get('Content-Range')}"
                )
            mode = "ab"
        elif r.status_code == 200:  # no range support: start over
            have, mode = 0, "wb"
        else:
            raise FetchError(f"{url}: HTTP {r.status_code}")
        st.state("downloading", size, have)
        with open(part, mode) as f:
            for chunk in r.iter_content(CHUNK):
                f.write(chunk)
                have += len(chunk)
                if have > size:
                    raise FetchError(f"{url}: more data than the manifest size {size}")
                st.progress(have)
    if have != size:
        raise FetchError(f"{url}: connection closed after {have} of {size} bytes")


def _download(m: dict, zst: str, st: FetchStatus) -> None:
    """Download the .zst of manifest m to zst (via zst.part) and verify size + sha256."""
    part = zst + ".part"
    tag = part + ".sha256"  # the .part is resumed only for the same expected sha256
    try:
        with open(tag, encoding="utf-8") as f:
            same = f.read().strip() == m["sha256"]
    except OSError:
        same = False
    if not same:
        _remove(part)
        with open(tag, "w", encoding="utf-8") as f:
            f.write(m["sha256"] + "\n")
    if os.path.exists(part) and os.path.getsize(part) > m["size"]:
        _remove(part)
    errors = []
    for attempt in range(DOWNLOAD_PASSES):
        for url in m["urls"]:
            try:
                _download_from(url, part, m["size"], st)
                break
            except (requests.RequestException, FetchError, OSError) as e:
                errors.append(str(e))
        else:
            if attempt + 1 < DOWNLOAD_PASSES:
                time.sleep(2 * (attempt + 1))
            continue
        break
    else:
        raise FetchError(f"download failed ({errors[-1]}); run again to resume")
    st.state("verifying", m["size"])
    got = _sha256_file(part, st)
    if got != m["sha256"]:
        # a complete but corrupt download cannot be resumed; the next fetch starts over
        _remove(part, tag)
        raise FetchError(
            f"sha256 mismatch for {part}: got {got}, expected {m['sha256']}; the corrupt "
            "download was deleted, run `mcp-dblp-index fetch` again"
        )
    os.replace(part, zst)
    _remove(tag)


def _unpack(m: dict, zst: str, tmp: str, st: FetchStatus) -> None:
    """Stream-decompress zst into tmp and verify sqlite_size + sqlite_sha256.

    Python 3.14's compression.zstd (ZstdDecompressor, or compression.zstd.open) could
    replace the zstandard package here once 3.14 is the minimum version."""
    import zstandard

    st.state("unpacking", m["sqlite_size"])
    h = hashlib.sha256()
    n = 0
    dctx = zstandard.ZstdDecompressor(max_window_size=MAX_WINDOW)
    try:
        with (
            open(zst, "rb") as fin,
            dctx.stream_reader(fin, read_across_frames=True) as reader,
            open(tmp, "wb") as fout,
        ):
            while chunk := reader.read(CHUNK):
                fout.write(chunk)
                h.update(chunk)
                n += len(chunk)
                if n > m["sqlite_size"]:
                    raise FetchError(f"{zst} unpacks to more than {m['sqlite_size']} bytes")
                st.progress(n)
            fout.flush()
            os.fsync(fout.fileno())
        if n != m["sqlite_size"]:
            raise FetchError(f"{zst} unpacked to {n} bytes, expected {m['sqlite_size']}")
        if h.hexdigest() != m["sqlite_sha256"]:
            raise FetchError(f"sha256 mismatch of the unpacked index {tmp}")
    except zstandard.ZstdError as e:
        _remove(tmp)
        raise FetchError(f"cannot unpack {zst}: {e}") from e
    except BaseException:
        _remove(tmp)
        raise


def fetch(
    release: str | None = None,
    manifest: str | None = None,
    force: bool = False,
    keep: bool = False,
    index_dir: str | None = None,
    status: FetchStatus | None = None,
    echo: bool = False,
    manifest_data: dict | None = None,
) -> str:
    """Download, verify and install the prebuilt index; returns its path.

    Raises FetchBusyError if another fetch holds the lock, FetchError on any failure (also
    recorded as state 'failed' in the status file)."""
    index_dir = index_dir or default_index_dir()
    os.makedirs(index_dir, exist_ok=True)
    st = status or FetchStatus(os.path.join(index_dir, STATUS_FILE), echo=echo)
    try:
        with _fetch_lock(index_dir):
            return _fetch(release, manifest, force, keep, index_dir, st, manifest_data)
    except FetchBusyError:
        raise
    except Exception as e:
        msg = str(e) if isinstance(e, FetchError) else f"{type(e).__name__}: {e}"
        st.state("failed", error=msg)
        raise FetchError(msg) from e


def _fetch(release, manifest, force, keep, index_dir, st, manifest_data) -> str:
    m = manifest_data or read_manifest(manifest_url(release, manifest))
    if release and m["release"] != release:
        raise FetchError(f"the manifest is for release {m['release']}, not {release}")
    st.state("starting", release=m["release"])
    zst = os.path.join(index_dir, m["file"])
    final = zst[: -len(".zst")]
    tmp = final + ".tmp"
    cur = current_index(index_dir)
    if (
        not force
        and cur
        and os.path.realpath(cur) == os.path.realpath(final)
        and read_meta(cur).get("release") == m["release"]
    ):
        cleanup_superseded(index_dir, st.echo)  # retry deletions that failed last time
        st.state("done")
        if st.echo:
            print(f"release {m['release']} is already installed: {final}", file=sys.stderr)
        return final
    if force:
        _remove(zst, zst + ".part", zst + ".part.sha256")
    _remove(tmp)
    check_space(m, zst, index_dir)

    zst_ok = False
    if os.path.isfile(zst) and os.path.getsize(zst) == m["size"]:
        st.state("verifying", m["size"])
        zst_ok = _sha256_file(zst, st) == m["sha256"]
    if not zst_ok:
        _remove(zst)
        _download(m, zst, st)
    _unpack(m, zst, tmp, st)

    st.state("installing")
    meta = read_meta(tmp)
    if meta.get("schema") != SCHEMA_VERSION or meta.get("release") != m["release"]:
        _remove(tmp)
        raise FetchError(
            f"the unpacked index has schema {meta.get('schema')!r} and release "
            f"{meta.get('release')!r}; expected {SCHEMA_VERSION!r} and {m['release']!r}"
        )
    os.replace(tmp, final)
    set_current(final, index_dir)
    if not keep:
        _remove(zst)
    cleanup_superseded(index_dir, st.echo)
    st.state("done")
    if st.echo:
        print(f"current -> {final} (release {m['release']})", file=sys.stderr)
    return final


SPACE_MARGIN = 0.05


def _resumable_bytes(m: dict, zst: str) -> int:
    """Bytes of the .zst already on disk: a complete .zst, or a .part of the same build."""
    try:
        if os.path.getsize(zst) == m["size"]:
            return m["size"]
    except OSError:
        pass
    part = zst + ".part"
    try:
        with open(part + ".sha256", encoding="utf-8") as f:
            if f.read().strip() != m["sha256"]:
                return 0
        return min(os.path.getsize(part), m["size"])
    except OSError:
        return 0


def check_space(m: dict, zst: str, index_dir: str) -> None:
    """Fail unless index_dir has room for the .zst plus the sqlite (+5%), minus the
    bytes of a resumable download."""
    need = round((m["size"] + m["sqlite_size"]) * (1 + SPACE_MARGIN)) - _resumable_bytes(m, zst)
    free = shutil.disk_usage(index_dir).free
    if free < need:
        raise FetchError(
            f"not enough disk space in {index_dir}: the index needs about {need / 1e9:.1f} GB, "
            f"{free / 1e9:.1f} GB free; free up space or set {HOME_ENV} to a directory on a "
            "larger volume"
        )


_RELEASE_FILE_RE = re.compile(
    r"dblp-(\d{4}-\d{2}-\d{2})\.sqlite"
    r"(?:\.zst|\.zst\.part|\.zst\.part\.sha256|\.tmp|\.part|\.part-journal)?"
)


def cleanup_superseded(index_dir: str | None = None, echo: bool = False) -> list[str]:
    """Delete index files (dblp-<release>.sqlite and its .zst/.part/.tmp) of releases
    older than the current index's release; returns the deleted paths.

    Never deletes the file `current` (or MCP_DBLP_INDEX) points to.  Errors are ignored:
    on Windows a file still open by a running server cannot be deleted; the next fetch
    retries.  A POSIX server keeps reading a deleted file until it notices the new
    `current` on its next tool call."""
    index_dir = index_dir or default_index_dir()
    cur = current_index(index_dir)
    if not cur:
        return []
    keep = {os.path.realpath(cur)}
    env = (os.environ.get("MCP_DBLP_INDEX") or "").strip()
    if env:
        keep.add(os.path.realpath(os.path.expanduser(env)))
    m = _RELEASE_FILE_RE.fullmatch(os.path.basename(os.path.realpath(cur)))
    release = m.group(1) if m else read_meta(cur).get("release", "")
    if not RELEASE_RE.match(release or ""):
        return []
    deleted = []
    for name in sorted(os.listdir(index_dir)):
        m = _RELEASE_FILE_RE.fullmatch(name)
        path = os.path.join(index_dir, name)
        if not m or m.group(1) >= release or os.path.realpath(path) in keep:
            continue
        try:
            os.remove(path)
            deleted.append(path)
        except OSError as e:
            if echo:
                print(f"cannot delete {path} yet ({e}); the next fetch retries", file=sys.stderr)
            continue
        if echo:
            print(f"deleted superseded {path}", file=sys.stderr)
    return deleted


def update(
    index_dir: str | None = None,
    manifest: str | None = None,
    force: bool = False,
    keep: bool = False,
    echo: bool = True,
) -> int:
    """Fetch the manifest's release if it is newer than the current index."""
    m = read_manifest(manifest_url(None, manifest))
    cur = current_index(index_dir)
    have = read_meta(cur).get("release", "") if cur else ""
    if have and have >= m["release"] and not force:
        print(f"current index is up to date (release {have})")
        with contextlib.suppress(FetchBusyError), _fetch_lock(index_dir or default_index_dir()):
            cleanup_superseded(index_dir, echo)  # retry deletions that failed last time
        return 0
    print(f"updating {have or '(none)'} -> {m['release']}", file=sys.stderr)
    fetch(force=force, keep=keep, index_dir=index_dir, echo=echo, manifest_data=m)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="mcp-dblp-index",
        description=(
            "Manage the local dblp index used by mcp-dblp.  'fetch' and 'update' install the "
            "prebuilt index published monthly at github.com/szeider/mcp-dblp-index; "
            "'download' and 'build' build it yourself from the dblp XML dump."
        ),
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, text in (
        ("fetch", "download and install the prebuilt index (default: latest release)"),
        ("update", "fetch the prebuilt index if the published release is newer"),
    ):
        p = sub.add_parser(name, help=text, description=text)
        if name == "fetch":
            p.add_argument("--release", help="YYYY-MM-01 (default: the latest release)")
        p.add_argument(
            "--manifest", help=f"manifest URL (default: ${MANIFEST_ENV} or {MANIFEST_URL})"
        )
        p.add_argument(
            "--force",
            help="download again even if installed / up to date, discarding partial files",
            action="store_true",
        )
        p.add_argument("--keep", help="keep the downloaded .zst", action="store_true")
    sub.add_parser("status", help="show the current index")
    p = sub.add_parser("download", help="download a dblp XML dump (default: newest)")
    p.add_argument("--release", help="YYYY-MM-01")
    p = sub.add_parser("build", help="build the index from a downloaded dump (default: newest)")
    p.add_argument("--release", help="YYYY-MM-01")
    p = sub.add_parser("build-file", help="build an index from DUMP into OUT (no switching)")
    p.add_argument("dump")
    p.add_argument("out")
    p.add_argument("--limit", type=int)
    p.add_argument("--every", type=int, default=1)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "fetch":
            release = _check_release(a.release) if a.release else None
            fetch(release, a.manifest, a.force, a.keep, echo=True)
        elif a.cmd == "update":
            return update(manifest=a.manifest, force=a.force, keep=a.keep)
        elif a.cmd == "download":
            download(_check_release(a.release) if a.release else latest_release())
        elif a.cmd == "build":
            release = _check_release(a.release) if a.release else _newest_dump()
            if not release:
                raise SystemExit(
                    f"no dump in {default_dump_dir()}; run 'mcp-dblp-index download' first"
                )
            build_release(release)
        elif a.cmd == "status":
            return status()
        elif a.cmd == "build-file":
            return 0 if build(a.dump, a.out, a.limit, every=a.every) else 1
    except FetchError as e:
        raise SystemExit(f"mcp-dblp-index {a.cmd}: {e}") from None
    return 0


if __name__ == "__main__":
    sys.exit(main())
