"""Local query layer over the slim SQLite index of the dblp XML dump (schema version 2,
see local_index.py).

Mirrors the public functions of src/mcp_dblp/dblp_client.py (same signatures, same
result dict shapes) so the MCP server can switch between the HTTP API and a local
backend.  All access is read-only (``file:...?mode=ro``).

Usage:
    db = LocalDblp("~/.mcp-dblp/index/dblp-2026-09-01.sqlite")
    db.search("SAT modulo symmetries")
"""

from __future__ import annotations

import difflib
import html
import json
import logging
import os
import re
import sqlite3
import unicodedata
import urllib.request
from collections import Counter
from typing import Any, NamedTuple

from mcp_dblp import bibtex_render

logger = logging.getLogger("dblp_local")

DBLP_REC_URL = "https://dblp.org/rec/"
DBLP_DB_URL = "https://dblp.org/db/"

# Publication type strings as returned by the dblp search API (info.type).
_API_TYPE = {
    "article": "Journal Articles",
    "inproceedings": "Conference and Workshop Papers",
    "proceedings": "Editorship",
    "book": "Books and Theses",
    "phdthesis": "Books and Theses",
    "mastersthesis": "Books and Theses",
    "incollection": "Parts in Books or Collections",
    "data": "Data and Artifacts",
}
_API_PUBLTYPE = {"informal": "Informal and Other Publications", "encyclopedia": "Reference Works"}

_VENUE_TYPE = {
    "conf": "Conference or Workshop",
    "journals": "Journal",
    "series": "Series",
    "reference": "Reference Work",
    "books": "Book",
    "phd": "Thesis",
}

STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "for",
        "from",
        "how",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "over",
        "that",
        "the",
        "their",
        "this",
        "to",
        "under",
        "using",
        "via",
        "what",
        "when",
        "which",
        "with",
        "without",
    ]
)

SCHEMA_VERSION = "2"
TYPES = [
    "article",
    "inproceedings",
    "proceedings",
    "book",
    "incollection",
    "phdthesis",
    "mastersthesis",
    "data",
]  # publ.type codes (local_index.TYPES)
T_ARTICLE, T_INPROC, T_PROC = 0, 1, 2
ID_SHIFT = 21  # publ.id = (year - 1900) << 21 | number within the year (local_index.py)
# fuzzy_title_search: a title word in fewer titles than TYPO_DF may be misspelled; then
# every word in fewer than VARIANT_DF titles also accepts close spellings
TYPO_DF, VARIANT_DF = 50, 1000


def _year_lo(year: int) -> int:
    return max(0, int(year) - 1900) << ID_SHIFT


_PUBL_COLS = (
    "id, key, type, publtype, title, year, venue, series, extra, publisher, doi, ee, "
    "authors, editors"
)
_MARKUP = re.compile(r"</?(?:i|b|u|tt|em|sub|sup)>")
_GENERIC_VENUE_WORDS = frozenset(
    [
        "journal",
        "international",
        "transactions",
        "proceedings",
        "conference",
        "symposium",
        "workshop",
        "annual",
        "letters",
        "review",
        "reviews",
        "research",
        "ieee",
        "acm",
    ]
)
_FIELD_PREFIX = {"title": "title", "author": "authors", "authors": "authors", "venue": "venue"}
_TOKEN_RE = re.compile(r"[^\W_]+")  # approximates the unicode61 tokenizer
_YEAR_RE = re.compile(r"^(19|20)\d\d$")
_SUFFIX_RE = re.compile(r" \d{4}$")  # dblp homonym suffix, e.g. "Manish Singh 0001"


def _fold(s: str) -> str:
    """Lowercase and strip diacritics (like unicode61 remove_diacritics 2)."""
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).casefold()


def _tokens(s: str) -> list[str]:
    return _TOKEN_RE.findall(_fold(s))


def _q(s: str) -> str:
    """Quote a string as an FTS5 string literal (phrase)."""
    return '"' + s.replace('"', '""') + '"'


def _strip_suffix(name: str) -> str:
    return _SUFFIX_RE.sub("", name)


def plain_title(t: str | None) -> str:
    """Stored titles keep dblp's inline markup as an escaped XML fragment; flatten it."""
    if not t or not _MARKUP.search(t):
        return t or ""
    return html.unescape(_MARKUP.sub("", t))


def _split(s: str | None) -> list[str]:
    return [a for a in (s or "").split(" | ") if a]


def _next_str(prefix: str) -> str:
    """Smallest string greater than every string starting with prefix."""
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


def sanitize_key(dblp_key: str) -> str:
    """Same sanitization as server.add_bibtex_entry, plus a 'DBLP:' prefix strip."""
    key = (dblp_key or "").strip()
    if key.endswith(".bib"):
        key = key[:-4]
    key = re.sub(r"^https?://", "", key)
    for host in ("dblp.org", "dblp.uni-trier.de", "dblp.dagstuhl.de"):
        prefix = host + "/rec/"
        if key.startswith(prefix):
            key = key[len(prefix) :]
            break
    if key.startswith("DBLP:"):
        key = key[5:]
    return key


def _http_style_rekey(bibtex: str, dblp_key: str) -> str:
    """Reproduce dblp_client.fetch_bibtex_entry's citation-key rewrite."""
    m = re.match(r"@(\w+){([^,]+),", bibtex)
    if not m:
        return bibtex
    old_key = m.group(2)
    ay = re.search(r"([A-Z][a-z]+).*?(\d{2,4})", dblp_key)
    if ay:
        year = ay.group(2)
        if len(year) == 2:
            year = "20" + year if int(year) < 50 else "19" + year
        new_key = f"{ay.group(1)}{year}"
    else:
        parts = dblp_key.split("/")
        new_key = parts[-1] if parts else dblp_key
    return bibtex.replace(f"{{{old_key},", f"{{{new_key},", 1)


def sqlite_ro_uri(path: str) -> str:
    """Read-only SQLite URI for a file path (escapes '?', '#', '%', spaces, drive letters)."""
    return "file:" + urllib.request.pathname2url(os.path.abspath(path)) + "?mode=ro"


SAME_TITLE, CONTAINS_QUERY, SIMILAR_TITLE = (
    "same title",
    "title contains the query",
    "similar title",
)


def _norm_title(s: str) -> str:
    """A title as its words: case, accents and punctuation dropped, so 'Theorem-Proving
    Procedures.' equals 'theorem proving procedures'."""
    return " ".join(_tokens(s))


def _title_match(q: str, t: str, threshold: float = 0.0) -> tuple[float, str]:
    """Similarity of two normalized titles (_norm_title) and how they match.

    Same words: 1.0.  The query's words inside a longer title: 0.5 + 0.5 * (the share of
    the title they cover).  That is at least the SequenceMatcher ratio 2c/(1+c), so a
    short query in a long title still shows up at a low threshold, while a longer title
    containing a short query ('A random matching theory' for 'Matching Theory': 0.81)
    stays below a near-exact threshold such as 0.9.  Otherwise the SequenceMatcher ratio
    (typos, abbreviations), 0.0 when its upper bounds are below threshold already.
    History: a flat 0.8 for containment (1.4-2.0.0) made containing titles tie, 0.8 + 0.2c
    (2.0.1) let them pass 0.9, and raw-string containment matched inside words
    ('graph coloring' in 'subgraph coloring')."""
    if q == t:
        return 1.0, SAME_TITLE
    if t and f" {q} " in f" {t} ":
        return 0.5 + 0.5 * len(q) / len(t), CONTAINS_QUERY
    sm = difflib.SequenceMatcher(None, q, t)
    if sm.real_quick_ratio() < threshold or sm.quick_ratio() < threshold:
        return 0.0, SIMILAR_TITLE  # upper bounds of ratio(): cannot reach threshold
    return sm.ratio(), SIMILAR_TITLE


def _fuzzy_ratio(query: str, title: str) -> float:
    """Title similarity of fuzzy_title_search for raw strings (_title_match)."""
    return _title_match(_norm_title(query), _norm_title(title))[0]


# ------------------------------------------------- search: how a result matched the query


class _Item(NamedTuple):
    """One word or quoted phrase of a subquery (LocalDblp._subquery_items)."""

    field: str | None  # FTS column from a title:/author:/venue: prefix
    toks: list[str]
    star: bool  # trailing '*' (explicit prefix search)
    phrase: bool  # "quoted phrase"


def _usable_term(t: str) -> bool:
    """A token worth explaining: not a stopword, a number or a single letter (an initial
    says little and would favour every author with that middle initial)."""
    return len(t) >= 2 and t not in STOPWORDS and not t.isdigit()


def _is_plain(it: _Item) -> bool:
    """A plain single word (unfielded, unquoted, no '*', one token): the query words that
    may be a person's name."""
    return (
        it.field is None
        and not it.phrase
        and not it.star
        and len(it.toks) == 1
        and _usable_term(it.toks[0])
    )


def _plain_terms(items: list[_Item]) -> list[str]:
    """The distinct plain words of a subquery, in query order."""
    return list(dict.fromkeys(it.toks[0] for it in items if _is_plain(it)))


def _name_parts(name: str) -> tuple[set[str], set[str]]:
    """(given, family) tokens of a dblp person name: the first word is the given name,
    every later word (middle names included) counts as family name, and a one-word name
    is a family name.  The homonym suffix (' 0001') is dropped."""
    words = _strip_suffix(name).split()
    if len(words) < 2:
        return set(), set(_tokens(" ".join(words)))
    return set(_tokens(words[0])), set(_tokens(" ".join(words[1:])))


class _Hit(NamedTuple):
    """Where one query term occurs in one result."""

    family: int | None  # first person (0-based) with the term in the family name
    given: int | None  # first person with the term as given name
    title: bool
    venue: bool
    prefix: tuple[str, str] | None  # (word, field) when the term only starts a word


def _term_hits(res: dict[str, Any], terms: list[str], who: str) -> dict[str, _Hit]:
    """Classify each query term against a result (who: 'author' or 'editor', the
    people listed in res['authors'])."""
    names = [_name_parts(a) for a in res["authors"]]
    title = set(_tokens(res["title"]))
    venue = set(_tokens(res["venue"]))
    hits = {}
    for t in terms:
        family = next((i for i, (_, f) in enumerate(names) if t in f), None)
        given = next((i for i, (g, _) in enumerate(names) if t in g), None)
        in_title, in_venue = t in title, t in venue
        prefix = None
        if family is None and given is None and not in_title and not in_venue:
            prefix = _prefix_word(t, res, who)
        hits[t] = _Hit(family, given, in_title, in_venue, prefix)
    return hits


def _prefix_word(t: str, res: dict[str, Any], who: str) -> tuple[str, str] | None:
    """The first word (as printed) of the title, a person's name or the venue that
    starts with t, and its field."""
    fields = [("title", res["title"])]
    fields += [(who, _strip_suffix(a)) for a in res["authors"]]
    fields.append(("venue", res["venue"]))
    for field, text in fields:
        for w in _TOKEN_RE.findall(text):
            if _fold(w).startswith(t):
                return w, field
    return None


def _explain(
    hits: dict[str, _Hit], name_terms: set[str], n_people: int, who: str, prefix_stage: bool
) -> str:
    """Compact text for result['match'], e.g. 'szeider = author 2 of 2; backdoor = title'.
    Terms with the same description are grouped; terms that match nowhere are left out."""
    groups: dict[str, list[str]] = {}
    for t, h in hits.items():
        if h.family is not None:
            d = f"= {who} {h.family + 1} of {n_people}"
        elif h.given is not None:
            d = f"= given name of {who} {h.given + 1} of {n_people}"
        elif h.title or h.venue:
            d = "= title" if h.title else "= venue"
            if t in name_terms:
                d += " word only"
        elif h.prefix:
            d = f'~ prefix of "{h.prefix[0]}" ({h.prefix[1]})'
        else:
            continue
        groups.setdefault(d, []).append(t)
    text = "; ".join(f"{', '.join(ts)} {d}" for d, ts in groups.items())
    if prefix_stage:
        return f"prefix match only: {text}" if text else "prefix match only"
    return text


class LocalDblp:
    def __init__(self, db_path: str):
        path = os.path.abspath(os.path.expanduser(db_path))
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        self.db_path = path
        self.conn = sqlite3.connect(sqlite_ro_uri(path), uri=True, check_same_thread=False)
        self.conn.execute("PRAGMA cache_size=-262144")  # 256 MB page cache
        self.conn.execute("PRAGMA temp_store=MEMORY")
        try:
            self.meta = dict(self.conn.execute("SELECT k, v FROM meta"))
        except sqlite3.OperationalError:
            self.meta = {}
        if self.meta.get("schema") != SCHEMA_VERSION:
            raise ValueError(
                f"{path}: index schema {self.meta.get('schema')!r}, "
                f"expected {SCHEMA_VERSION!r}; rebuild with mcp-dblp-index build"
            )
        # Per-column term statistics (rare words, spelling candidates).  Lives in the
        # temp schema, so it works on a read-only main database.
        self.conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS temp.fts_vocab "
            "USING fts5vocab(main, publ_fts, 'col')"
        )

    def close(self):
        self.conn.close()

    # ------------------------------------------------------------------ rows

    def _result(self, row: tuple) -> dict[str, Any]:
        (
            _id,
            key,
            tcode,
            publtype,
            title,
            year,
            venue,
            series,
            extra,
            publisher,
            doi,
            ee,
            authors,
            editors,
        ) = row
        typ = TYPES[tcode] if tcode is not None and tcode < len(TYPES) else ""
        api_type = _API_PUBLTYPE.get(publtype or "", _API_TYPE.get(typ, typ))
        if typ == "article":
            v = venue or ""
        elif typ in ("inproceedings", "proceedings", "incollection"):
            v = venue or series or ""
        else:
            school = json.loads(extra).get("school") if extra else None
            v = venue or series or school or publisher or ""
        first_ee = _split(ee)[0] if ee else ("https://doi.org/" + doi if doi else "")
        return {
            "title": plain_title(title),
            "authors": _split(authors or editors),
            "venue": v,
            "year": year,
            "type": api_type,
            "doi": doi or "",
            "ee": first_ee,
            "url": DBLP_REC_URL + key,
            "dblp_key": key,
        }

    def _fetch_rows(self, ids: list[int]) -> dict[int, tuple]:
        """publ rows (_PUBL_COLS) by id."""
        out: dict[int, tuple] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            sql = f"SELECT {_PUBL_COLS} FROM publ WHERE id IN ({','.join('?' * len(chunk))})"
            for row in self.conn.execute(sql, chunk):
                out[row[0]] = row
        return out

    def _fetch(self, ids: list[int]) -> list[dict[str, Any]]:
        """Result dicts for publ ids, in the given order."""
        rows = self._fetch_rows(ids)
        return [self._result(rows[i]) for i in ids if i in rows]

    def get_record(self, key: str) -> dict[str, Any] | None:
        """All fields of one record (old column names, via the publ_v view), plus
        author/editor lists and the search-result dict, or None."""
        key = sanitize_key(key)
        cur = self.conn.execute("SELECT * FROM publ_v WHERE key = ?", (key,))
        row = cur.fetchone()
        if row is None:
            return None
        rec = {d[0]: v for d, v in zip(cur.description, row, strict=True)}
        rec["author_list"] = _split(rec.get("authors"))
        rec["editor_list"] = _split(rec.get("editors"))
        row = self.conn.execute(
            f"SELECT {_PUBL_COLS} FROM publ WHERE id = ?", (rec["id"],)
        ).fetchone()
        rec["result"] = self._result(row)
        return rec

    # ------------------------------------------------------------- FTS core

    # Ranking all matches of a very broad query (e.g. 'learning': 630k rows on the full
    # index) with bm25 takes seconds.  Above RANK_WINDOW matches only the RANK_WINDOW
    # matches with the highest rowids are ranked; since publ.id starts with the year,
    # that is the newest RANK_WINDOW matches.  Counting matches is cheap.
    RANK_WINDOW = 30000
    RANK_EXPR = "bm25(3.0, 2.0, 1.0)"  # title, authors, venue

    @staticmethod
    def _id_range(year_from, year_to, year_eq) -> tuple[int | None, int | None]:
        """publ.id bounds for a year range (ids start with the year)."""
        lo = hi = None
        if year_from is not None:
            lo = _year_lo(year_from)
        if year_to is not None:
            hi = _year_lo(int(year_to) + 1) - 1
        if year_eq is not None:
            lo = max(lo or 0, _year_lo(year_eq))
            h = _year_lo(int(year_eq) + 1) - 1
            hi = h if hi is None else min(hi, h)
        return lo, hi

    def _fts_ids(
        self,
        match: str,
        limit: int,
        year_from: int | None = None,
        year_to: int | None = None,
        venue_filter: str | None = None,
        year_eq: int | None = None,
    ) -> list[tuple[int, float]]:
        """Run one FTS query; (publ id, bm25 rank) pairs, best first."""
        if venue_filter:
            # Push the venue filter into the index; the exact substring test still applies.
            vt = _tokens(venue_filter)
            if vt:
                match = f"({match}) AND venue : ({' AND '.join(_q(t) + '*' for t in vt)})"
        lo, hi = self._id_range(year_from, year_to, year_eq)
        cond, cargs = "", []
        if lo is not None:
            cond += " AND publ_fts.rowid >= ?"
            cargs.append(lo)
        if hi is not None:
            cond += " AND publ_fts.rowid <= ?"
            cargs.append(hi)
        try:
            n = self.conn.execute(
                f"SELECT count(*) FROM publ_fts WHERE publ_fts MATCH ?{cond}", [match, *cargs]
            ).fetchone()[0]
            if n == 0:
                return []
            windows: list[int | None] = [None]
            if n > self.RANK_WINDOW:
                windows = (
                    [self.RANK_WINDOW, 4 * self.RANK_WINDOW]
                    if n > 4 * self.RANK_WINDOW
                    else [self.RANK_WINDOW, None]
                )
                logger.info(f"broad query {match!r}: {n} matches, ranking the newest")
            rows = []
            for w in windows:
                sql = [
                    "SELECT publ_fts.rowid, publ_fts.rank FROM publ_fts",
                    "JOIN publ ON publ.id = publ_fts.rowid" if venue_filter else "",
                    f"WHERE publ_fts MATCH ? AND publ_fts.rank MATCH '{self.RANK_EXPR}'",
                    cond,
                ]
                args: list[Any] = [match, *cargs]
                if w is not None:
                    r0 = self.conn.execute(
                        f"SELECT rowid FROM publ_fts WHERE publ_fts MATCH ?{cond} "
                        "ORDER BY rowid DESC LIMIT 1 OFFSET ?",
                        [match, *cargs, w - 1],
                    ).fetchone()[0]
                    sql.append("AND publ_fts.rowid >= ?")
                    args.append(r0)
                if venue_filter:
                    sql.append("AND instr(lower(publ.venue), ?) > 0")
                    args.append(venue_filter.lower())
                sql.append("ORDER BY publ_fts.rank LIMIT ?")
                args.append(int(limit))
                rows = self.conn.execute(" ".join(sql), args).fetchall()
                if len(rows) >= limit:
                    break
            return rows
        except sqlite3.OperationalError as e:
            logger.error(f"FTS query failed ({match!r}): {e}")
            return []

    @staticmethod
    def _subquery_items(q: str) -> tuple[list[_Item], int | None]:
        """Split one AND-subquery into words and phrases plus an optional year.

        Supports "quoted phrases", field prefixes title:/author:/venue: (a prefix
        applies to the next word or quoted phrase only, so 'venue:AAAI Szeider'
        finds Szeider's AAAI papers), a trailing '$' (dblp's exact-word marker,
        ignored) and a trailing '*' (prefix search).  A 4-digit year (1900-2099),
        bare or as year:YYYY, becomes a year constraint.
        """
        items: list[_Item] = []
        year = None
        field = None
        for m in re.finditer(r'(\w+):|"([^"]*)"|(\S+)', q):
            if m.group(1) is not None:
                name = m.group(1).lower()
                if name in _FIELD_PREFIX:
                    field = _FIELD_PREFIX[name]
                    continue
                if name == "year":  # dblp's year:2017; the year itself follows
                    continue
                word = m.group(0)  # unknown prefix: treat as text
            elif m.group(2) is not None:
                toks = _tokens(m.group(2))
                if toks:
                    items.append(_Item(field, toks, False, True))
                field = None
                continue
            else:
                word = m.group(3)
            star = word.endswith("*")
            toks = _tokens(word)
            if not toks or toks == ["and"]:
                continue
            if len(toks) == 1 and _YEAR_RE.match(toks[0]) and year is None:
                year = int(toks[0])
            else:
                items.append(_Item(field, toks, star, False))
            field = None  # a prefix covers one word or phrase
        return items, year

    @staticmethod
    def _parse_subquery(
        q: str, prefix_match: bool, author_term: str | None = None
    ) -> tuple[str | None, int | None]:
        """Turn one AND-subquery into an FTS5 expression plus an optional year
        (syntax: _subquery_items).  With author_term (one of the subquery's plain
        terms, _plain_terms), that word must occur in the authors column; all other
        words keep matching anywhere."""
        items, year = LocalDblp._subquery_items(q)
        parts = []
        for it in items:
            expr = _q(" ".join(it.toks))
            if (
                not it.phrase
                and (
                    it.star
                    or (
                        prefix_match
                        and len(it.toks) == 1
                        and len(it.toks[0]) >= 4
                        and it.toks[0] not in STOPWORDS
                    )
                )
                and not it.toks[-1].isdigit()
            ):
                expr += "*"
            field = it.field
            if author_term is not None and _is_plain(it) and it.toks[0] == author_term:
                field, author_term = "authors", None  # restrict the first occurrence only
            parts.append(f"{field} : {expr}" if field else expr)
        if not parts:
            return None, year
        return " AND ".join(parts), year

    # ---------------------------------------------------------------- search

    def search(
        self,
        query: str,
        max_results: int = 10,
        year_from: int | None = None,
        year_to: int | None = None,
        venue_filter: str | None = None,
        include_bibtex: bool = False,
    ) -> list[dict[str, Any]]:
        """Boolean search ('or' between subqueries, words AND-ed), bm25-ranked.

        bm25 weights a title word above an author name, so a surname that also occurs
        in titles ('Smirnov' in 'Kolmogorov-Smirnov') would rank title matches first.
        Therefore each result is classified against the query words (result['match']),
        and a word that the candidates mostly match as an author's family name is a
        name term: results that match more name terms as family names rank first.
        Queries without name terms keep the plain bm25 order.  Results of the prefix
        fallback always come last and say so ('prefix match only: ...').
        """
        max_results = int(max_results)
        if "(" in query or ")" in query:
            logger.warning("Parentheses are not supported in boolean queries; ignored.")
        subqueries = [s.strip() for s in re.split(r"\s+or\s+", query, flags=re.I) if s.strip()]
        pool = max(3 * max_results, 30)  # candidates per FTS query
        scored: dict[int, float] = {}
        prefix_ids: set[int] = set()  # found by the prefix fallback
        extra_ids: set[int] = set()  # found only by an author-restricted variant
        terms: dict[str, None] = {}  # words to explain, in query order
        for sub in subqueries:
            items, _ = self._subquery_items(sub)
            terms.update((t, None) for it in items for t in it.toks if _usable_term(t))
            # Exact words first; fall back to prefix matching when too few hits
            # (the dblp API prefix-matches words by default).
            for prefix_match in (False, True):
                match, year = self._parse_subquery(sub, prefix_match)
                if match is None:
                    break
                if year is not None and (
                    (year_from is not None and year < year_from)
                    or (year_to is not None and year > year_to)
                ):
                    break
                filt = (year_from, year_to, venue_filter)
                rows = self._fts_ids(match, pool, *filt, year_eq=year)
                n_base = len(rows)
                if not prefix_match and n_base >= pool:
                    # More matches than the pool holds: also take the best matches with
                    # a plain word in an author name, however bm25 ranks them overall.
                    for t in _plain_terms(items)[:4]:
                        m, _ = self._parse_subquery(sub, False, author_term=t)
                        rows += self._fts_ids(m, pool, *filt, year_eq=year)
                for n, (pid, score) in enumerate(rows):
                    extra = n >= n_base
                    if pid not in scored or (pid in extra_ids and not extra and not prefix_match):
                        scored[pid] = score
                        extra_ids.discard(pid)
                        if prefix_match:
                            prefix_ids.add(pid)
                        elif extra:
                            extra_ids.add(pid)
                if len(scored) >= max_results:
                    break
        results = self._ranked(scored, prefix_ids, extra_ids, list(terms), max_results)
        if include_bibtex:
            for r in results:
                r["bibtex"] = self.fetch_bibtex_entry(r["dblp_key"])
        return results

    def _ranked(
        self,
        scored: dict[int, float],
        prefix_ids: set[int],
        extra_ids: set[int],
        terms: list[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        """Fetch and classify all candidates; the best `limit` as result dicts with
        'match', ordered by (prefix stage, tier, bm25, newest).  Within a tier, hits of
        an author-restricted variant that were not in the plain query's pool come after
        the pool: their bm25 (authors-only IDF) is not comparable, and their plain bm25
        is below the pool's."""
        rows = self._fetch_rows(list(scored))
        cands = [pid for pid in scored if pid in rows]
        res, hits, who = {}, {}, {}
        for pid in cands:
            res[pid] = self._result(rows[pid])
            who[pid] = "author" if rows[pid][12] else "editor"  # authors column empty
            hits[pid] = _term_hits(res[pid], terms, who[pid])
        n_family = Counter(t for h in hits.values() for t, x in h.items() if x.family is not None)
        n_title = Counter(t for h in hits.values() for t, x in h.items() if x.title)
        name_terms = {t for t in terms if n_family[t] >= max(1, n_title[t])}
        tier = {pid: sum(1 for t in name_terms if hits[pid][t].family is None) for pid in cands}
        order = sorted(
            cands, key=lambda i: (i in prefix_ids, tier[i], i in extra_ids, scored[i], -i)
        )
        out = []
        for pid in order[: max(limit, 0)]:
            r = res[pid]
            text = _explain(hits[pid], name_terms, len(r["authors"]), who[pid], pid in prefix_ids)
            if text:
                r["match"] = text
            out.append(r)
        return out

    # ----------------------------------------------------------- fuzzy title

    def _doc_freq(self, words: list[str], col: str = "title") -> dict[str, int]:
        if not words:
            return {}
        sql = (
            "SELECT term, doc FROM temp.fts_vocab WHERE col = ? AND term IN "
            f"({','.join('?' * len(words))})"
        )
        return dict(self.conn.execute(sql, [col, *words]).fetchall())

    def fuzzy_title_search(
        self,
        title: str,
        similarity_threshold: float,
        max_results: int = 10,
        year_from: int | None = None,
        year_to: int | None = None,
        venue_filter: str | None = None,
        include_bibtex: bool = False,
    ) -> list[dict[str, Any]]:
        max_results = int(max_results)
        toks = _tokens(title)
        if not toks:
            return []
        sig = [t for t in dict.fromkeys(toks) if t not in STOPWORDS] or list(dict.fromkeys(toks))
        df = self._doc_freq(sig)
        known = sorted((w for w in sig if df.get(w, 0) > 0), key=lambda w: df[w])
        filt = (year_from, year_to, venue_filter)
        pool = max(100, 10 * max_results)

        queries = [f"title : {_q(' '.join(toks))}"]  # whole title as a phrase
        if any(df.get(w, 0) < TYPO_DF for w in sig):
            # A misspelled word ('Atention is all you ned') breaks every AND query below,
            # and the OR fallback drowns in common words: also accept close vocabulary
            # terms for the rarer words.  Words of one or two letters are left out: a
            # truncated 'al' for 'all' is itself a common token, so it would be required.
            words = [w for w in sig if len(w) > 2] or sig
            parts = [
                "(" + " OR ".join(_q(v) for v in self._spelling_variants(w, col="title")) + ")"
                if df.get(w, 0) < VARIANT_DF
                else _q(w)
                for w in words
            ]
            queries.append("title : (" + " AND ".join(parts) + ")")
        if known:
            queries.append("title : (" + " AND ".join(_q(w) for w in known) + ")")
        if len(known) > 3:
            queries.append("title : (" + " AND ".join(_q(w) for w in known[:3]) + ")")
        if len(known) > 2:
            queries.append("title : (" + " AND ".join(_q(w) for w in known[:2]) + ")")

        cand: dict[int, None] = {}
        for i, match in enumerate(queries):
            if i >= 2 and len(cand) >= 3 * pool:
                break
            for pid, _ in self._fts_ids(match, pool, *filt):
                cand[pid] = None
        if len(cand) < max_results and len(known) > 1:
            # Misspelled words can be rare but real tokens, breaking every AND query above:
            # fall back to an OR over the rarest words, ranked by bm25 (more matches first).
            rare = [w for w in known if df[w] <= 50000][:5] or known[:2]
            match = "title : (" + " OR ".join(_q(w) for w in rare) + ")"
            for pid, _ in self._fts_ids(match, pool, *filt):
                cand[pid] = None

        qn = " ".join(toks)  # _norm_title(title)
        out = []
        for pub in self._fetch(list(cand)):
            ratio, how = _title_match(qn, _norm_title(pub["title"]), similarity_threshold)
            if ratio >= similarity_threshold:
                pub["similarity"] = ratio
                pub["title_match"] = how
                out.append(pub)
        out.sort(key=lambda p: (-p["similarity"], -(p["year"] or 0)))
        out = out[:max_results]
        if include_bibtex:
            for pub in out:
                bib = self.fetch_bibtex_entry(pub["dblp_key"])
                if bib:
                    pub["bibtex"] = bib
        return out

    # ---------------------------------------------------------------- author

    AUTHOR_ROW_CAP = 50000  # rows read per author query (prolific names: a few thousand)

    def _people_rows(self, match: str) -> list[tuple[int, list[str]]]:
        """(publ id, author+editor names) for records whose FTS authors column matches."""
        rows = self.conn.execute(
            "SELECT publ.id, publ.authors, publ.editors FROM publ_fts "
            "JOIN publ ON publ.id = publ_fts.rowid WHERE publ_fts MATCH ? LIMIT ?",
            (match, self.AUTHOR_ROW_CAP),
        ).fetchall()
        return [(i, _split(a) + _split(e)) for i, a, e in rows]

    def _spelling_variants(self, tok: str, n: int = 4, col: str = "authors") -> list[str]:
        """tok plus the closest vocabulary terms of FTS column col (same first 2 letters)."""
        lo = tok[:2]
        rows = self.conn.execute(
            "SELECT term, doc FROM temp.fts_vocab WHERE col = ? AND term >= ? AND term < ?",
            (col, lo, _next_str(lo)),
        )
        letters = sorted(tok)
        scored = []
        for t, doc in rows:
            sm = difflib.SequenceMatcher(None, tok, t)
            if sm.real_quick_ratio() >= 0.75 and sm.quick_ratio() >= 0.75:
                r = sm.ratio()
                if r >= 0.75:
                    # ties: prefer a transposition of tok (same letters), then frequent terms
                    scored.append((r, sorted(t) == letters, doc, t))
        best = [x[3] for x in sorted(scored, reverse=True)[:n]]
        return list(dict.fromkeys([tok, *best]))

    def _author_index(self, author_name: str) -> tuple[dict[str, float], dict[str, list[int]]]:
        """Candidate dblp names (with homonym suffix) -> similarity, and name -> publ ids."""
        toks = _tokens(author_name)
        if not toks:
            return {}, {}
        qf = " ".join(toks)
        # 'Wei Wang 0010' names one numbered dblp person; without a number, all
        # persons of that name match equally
        with_suffix = bool(_SUFFIX_RE.search(author_name.strip()))

        def collect(match, exact_only):
            ids: dict[str, list[int]] = {}
            for pid, names in self._people_rows(match):
                for nm in names:
                    ids.setdefault(nm, []).append(pid)
            sims = {}
            for nm in ids:
                nf = " ".join(_tokens(nm if with_suffix else _strip_suffix(nm)))
                if nf == qf:
                    sims[nm] = 1.0
                elif not exact_only:
                    sm = difflib.SequenceMatcher(None, qf, nf)
                    if sm.quick_ratio() >= 0.5:
                        sims[nm] = sm.ratio()
            return sims, ids

        # 1. the name as a phrase: case/diacritic-insensitive exact match
        sims, ids = collect(f"authors : {_q(qf)}", True)
        if sims:
            return sims, ids
        # 2. typo tolerance: every token or a close vocabulary term, in any order
        parts = [" OR ".join(_q(v) for v in self._spelling_variants(t)) for t in toks]
        return collect("authors : (" + " AND ".join(f"({p})" for p in parts) + ")", False)

    def get_author_publications(
        self,
        author_name: str,
        similarity_threshold: float,
        max_results: int = 20,
        include_bibtex: bool = False,
        year_from: int | None = None,
        year_to: int | None = None,
    ) -> dict[str, Any]:
        """Publications of the best-matching dblp person, newest first.  year_from /
        year_to filter them before the max_results cut; total_publications stays the
        person's total, publication_count is the number returned."""
        max_results = int(max_results)
        sims, ids = self._author_index(author_name)
        cands = {n: s for n, s in sims.items() if s >= similarity_threshold}
        if not cands:
            return {
                "name": author_name,
                "publication_count": 0,
                "publications": [],
                "stats": {"venues": [], "years": [], "types": {}},
                "query": author_name,
                "dblp_name": "",
                "similarity": 0.0,
                "total_publications": 0,
                "other_candidates": [],
            }
        counts = {n: len(set(ids[n])) for n in cands}
        ranked = sorted(cands, key=lambda n: (-cands[n], -counts[n], n))
        best = ranked[0]
        lo, hi = self._id_range(year_from, year_to, None)  # ids start with the year
        in_range = sorted(
            (i for i in set(ids[best]) if (lo is None or i >= lo) and (hi is None or i <= hi)),
            reverse=True,
        )  # id order = year order
        pubs = self._fetch(in_range[:max_results])
        if include_bibtex:
            for p in pubs:
                p["bibtex"] = self.fetch_bibtex_entry(p["dblp_key"])
        return {
            "name": _strip_suffix(best),
            "publication_count": len(pubs),
            "publications": pubs,
            "stats": {
                "venues": Counter(p["venue"] for p in pubs).most_common(5),
                "years": Counter(p["year"] for p in pubs).most_common(5),
                "types": dict(Counter(p["type"] for p in pubs)),
            },
            # additions over the HTTP client
            "query": author_name,
            "dblp_name": best,
            "similarity": cands[best],
            "total_publications": counts[best],
            "matching_publications": len(in_range),  # within year_from/year_to
            "other_candidates": [(n, round(cands[n], 3), counts[n]) for n in ranked[1:10]],
        }

    # ----------------------------------------------------------------- venue

    def _stream_stats(self, stream: str) -> tuple[int, int | None, int | None]:
        lo = stream + "/"
        return self.conn.execute(
            "SELECT count(*), min(year), max(year) FROM publ WHERE key > ? AND key < ?",
            (lo, _next_str(lo)),
        ).fetchone()

    def _venue_rows(self, match: str, limit: int = 200000):
        return self.conn.execute(
            "SELECT publ.key, COALESCE(publ.venue, '') FROM publ_fts "
            "JOIN publ ON publ.id = publ_fts.rowid WHERE publ_fts MATCH ? LIMIT ?",
            (match, limit),
        )

    def _venue_candidates(self, venue_name: str) -> Counter:
        """Count records per stream (key prefix like 'conf/cp') whose venue matches."""
        name = venue_name.strip()
        counts: Counter = Counter()
        if re.match(r"^(conf|journals|series|reference|books)/[\w-]+/?$", name):
            counts[name.rstrip("/")] = 10**9
            return counts

        def norm(v):  # "CP (1)" -> "cp"
            return re.sub(r"\s*\(\d+\)$", "", v).strip().casefold()

        toks = _tokens(name)
        if not toks:
            return counts
        # a) exact short name (booktitle / journal abbreviation), via FTS venue column
        for key, venue in self._venue_rows(f"venue : ^{_q(' '.join(toks))}"):
            if norm(venue) == name.casefold():
                counts["/".join(key.split("/")[:2])] += 1
        # c) full journal name vs abbreviation ("Journal of Graph Theory" ~ "J. Graph Theory").
        # Checked even after an exact match, because a journal's full name can also be
        # the exact name of a small venue ('Artificial Intelligence' is a book's title,
        # 'Theoretical Computer Science' a 1977 conference); get_venue_info takes the
        # stream with the most records.
        words = [t for t in toks if t not in STOPWORDS]
        if len(words) >= 2:
            counts.update(self._abbrev_candidates(words, norm))
        if counts:
            return counts
        # b) name equals a stream segment (e.g. 'jgt', 'nips')
        seg = re.sub(r"[^a-z0-9]", "", name.lower())
        for kind in ("conf", "journals", "series"):
            n = self._stream_stats(f"{kind}/{seg}")[0] if seg else 0
            if n:
                counts[f"{kind}/{seg}"] = n
        if counts:
            return counts
        # d) conference full name: proceedings titles
        for (key,) in self.conn.execute(
            "SELECT publ.key FROM publ_fts JOIN publ ON publ.id = publ_fts.rowid "
            f"WHERE publ_fts MATCH ? AND publ.type = {T_PROC} LIMIT 2000",
            ("title : " + _q(" ".join(toks)),),
        ):
            counts["/".join(key.split("/")[:2])] += 1
        return counts

    def _abbrev_candidates(self, words: list[str], norm) -> Counter:
        """Streams whose venue abbreviation fully matches the full name `words`
        (non-stopword tokens), with their record counts; empty if none matches fully."""
        # FTS prefixes of the distinctive words; if all words are generic ('Journal of
        # the ACM'), the short ones: abbreviations keep acronyms ('ACM') but shorten
        # long words ('J.'), so 'jou*' would miss
        qwords = (
            [w for w in words if w not in _GENERIC_VENUE_WORDS]
            or [w for w in words if len(w) <= 4]
            or words
        )
        qwords = sorted(qwords, key=len, reverse=True)[:3]
        match = "venue : (" + " AND ".join(_q(w[:3]) + "*" for w in qwords) + ")"
        vc: Counter = Counter()
        for key, venue in self._venue_rows(match):
            vc[(norm(venue), "/".join(key.split("/")[:2]))] += 1
        best_score, counts = 0.0, Counter()
        for (venue, stream), n in vc.items():
            s = _abbrev_score(venue, words)
            if s > best_score:
                best_score, counts = s, Counter({stream: n})
            elif s == best_score and s > 0:
                counts[stream] += n
        return counts if best_score >= 0.99 else Counter()

    def _main_proceedings_title(self, stream: str) -> str:
        """Title of the stream's main recent proceedings volume: the one with the most
        papers among the volumes of its last three years.  The newest volume can be a
        small co-located workshop volume (IJCAI's newest is 'Democracy and AI ... Held in
        Conjunction with IJCAI 2025', 8 papers, next to the main volume's 1280)."""
        lo, hi = stream + "/", _next_str(stream + "/")
        latest = self.conn.execute(
            f"SELECT max(year) FROM publ WHERE key > ? AND key < ? AND type = {T_PROC}",
            (lo, hi),
        ).fetchone()[0]
        if latest is None:
            return ""
        row = self.conn.execute(
            "SELECT p.title FROM publ p WHERE p.type = ? AND p.key = ("
            "SELECT crossref FROM publ WHERE key > ? AND key < ? AND crossref IS NOT NULL "
            "AND year >= ? GROUP BY crossref ORDER BY count(*) DESC, crossref DESC LIMIT 1)",
            (T_PROC, lo, hi, latest - 2),
        ).fetchone()
        if row is None:  # no crossrefs: the newest volume
            row = self.conn.execute(
                f"SELECT title FROM publ WHERE key > ? AND key < ? AND type = {T_PROC} "
                "ORDER BY year DESC LIMIT 1",
                (lo, hi),
            ).fetchone()
        return plain_title(row[0]) if row else ""

    def get_venue_info(self, venue_name: str) -> dict[str, Any]:
        empty = {"venue": "", "acronym": "", "type": "", "url": ""}
        toks = [t for t in _tokens(venue_name) if t not in STOPWORDS]
        if len(toks) == 1 and toks[0] in _GENERIC_VENUE_WORDS:
            return empty  # 'ACM', 'IEEE', 'Journal': a publisher or a word, not a venue
        counts = self._venue_candidates(venue_name)
        if not counts:
            return empty
        stream = counts.most_common(1)[0][0]
        kind = stream.split("/")[0]
        n, y0, y1 = self._stream_stats(stream)
        if n == 0:
            return empty
        lo = stream + "/"
        # Most common short name among the stream's publications
        short = Counter(
            re.sub(r"\s*\(\d+\)$", "", r[0] or "")
            for r in self.conn.execute(
                "SELECT venue FROM publ WHERE key > ? AND key < ? "
                f"AND type IN ({T_ARTICLE}, {T_INPROC}) ORDER BY key DESC LIMIT 2000",
                (lo, _next_str(lo)),
            )
        ).most_common(1)
        short_name = short[0][0] if short else ""
        proc_title = self._main_proceedings_title(stream)
        full = short_name
        if kind == "conf" and proc_title:
            full = _proceedings_series_name(proc_title) or short_name
            if short_name and short_name not in full:
                full = f"{full} ({short_name})"
        return {
            "venue": full,
            "acronym": short_name if kind == "conf" else "",
            "type": _VENUE_TYPE.get(kind, "Other"),
            "url": f"{DBLP_DB_URL}{stream}/index.html",
            # additions over the HTTP client
            "stream": stream,
            "publication_count": n,
            "year_from": y0,
            "year_to": y1,
            "latest_proceedings": proc_title,
            "other_candidates": [s for s, _ in counts.most_common(6)[1:]],
        }

    # ---------------------------------------------------------------- bibtex

    def bibtex_for_key(self, dblp_key: str, key_prefix: str = "DBLP:") -> str | None:
        """dblp's BibTeX for a key (citation key '<key_prefix><key>'), or None if unknown."""
        key = sanitize_key(dblp_key)
        if not key:
            return None
        return bibtex_render.bibtex_for_key(self.conn, key, key_prefix)

    def fetch_bibtex_entry(self, dblp_key: str) -> str:
        """BibTeX for a key, rendered locally; '' if the key is unknown.

        Applies the same citation-key rewrite as dblp_client.fetch_bibtex_entry
        (first capitalized word + year from the dblp key) so output is comparable.
        """
        key = sanitize_key(dblp_key)
        if not key:
            return ""
        try:
            bib = bibtex_render.bibtex_for_key(self.conn, key)
        except Exception as e:
            logger.error(f"Error rendering BibTeX for {key}: {e}", exc_info=True)
            return f"% Error: could not render BibTeX for {key}: {e}"
        if not bib:
            return ""
        return _http_style_rekey(bib, key)


def _abbrev_score(venue: str, words: list[str]) -> float:
    """Fraction of venue tokens that are, in order, prefixes of the query words."""
    vt = [t for t in _tokens(venue) if t not in STOPWORDS]
    if not vt:
        return 0.0
    i = hit = 0
    for t in vt:
        while i < len(words) and not words[i].startswith(t):
            i += 1
        if i == len(words):
            break
        hit += 1
        i += 1
    # penalize unmatched query words so "J. Graph Theory" beats "Graph Theory Notes"
    return hit / len(vt) * (hit / max(len(words), 1))


_ORDINAL_WORD = (
    r"(?:(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)[- ])?"
    r"(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh|twelfth|"
    r"thirteenth|fourteenth|fifteenth|sixteenth|seventeenth|eighteenth|nineteenth|twentieth|"
    r"thirtieth|fortieth|fiftieth|sixtieth|seventieth|eightieth|ninetieth)"
)


def _proceedings_series_name(title: str) -> str:
    """'29th International Conference on X, CP 2023, ...' -> 'International Conference on X'
    (also 'Proceedings of the Thirty-Fourth International Joint Conference on ...')."""
    first = max(title.split(",")[0].split(" - "), key=len)
    first = re.sub(
        rf"^(Proceedings of the\s+)?(\d{{4}}\s+)?((\d+(st|nd|rd|th)|{_ORDINAL_WORD})\s+)?",
        "",
        first,
        flags=re.I,
    )
    first = re.sub(r"\s+\d{4}$", "", first)
    return first.strip().rstrip(".")
