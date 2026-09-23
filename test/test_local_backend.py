"""Tests for the local dump backend: renderer, query layer, backend selection, CLI helpers.

All tests use a tiny index built from test/data/mini_dblp.xml (no network).
"""

import gzip
import sqlite3

import pytest

from mcp_dblp import backend as backend_mod
from mcp_dblp import local_index
from mcp_dblp.bibtex_render import bibtex_for_key
from mcp_dblp.local_client import LocalDblp

RESULT_KEYS = {"title", "authors", "venue", "year", "type", "doi", "ee", "url", "dblp_key"}


@pytest.fixture(scope="module")
def conn(mini_index):
    c = sqlite3.connect(mini_index)
    yield c
    c.close()


@pytest.fixture(scope="module")
def db(mini_index):
    d = LocalDblp(str(mini_index))
    yield d
    d.close()


# --------------------------------------------------------------------------- builder


def test_meta(db):
    assert db.meta["schema"] == "2"
    assert db.meta["release"] == "2026-09-01"
    assert db.meta["records"] == "10"  # 9 publications + 1 homepage
    assert db.meta["publications"] == "9"


def test_title_markup_kept(conn):
    (title,) = conn.execute("SELECT title FROM publ WHERE key = 'journals/tcs/GodelS20'").fetchone()
    assert title == "Beyond O(n<sup>2</sup>) Time for Treewidth &amp; SAT."
    (fts,) = conn.execute("SELECT title FROM publ_fts WHERE publ_fts MATCH 'treewidth'").fetchone()
    assert fts is not None


# -------------------------------------------------------------------------- renderer


def test_render_article_sup_and_accent(conn):
    assert bibtex_for_key(conn, "journals/tcs/GodelS20") == (
        "@article{DBLP:journals/tcs/GodelS20,\n"
        '  author       = {Kurt G{\\"{o}}del and\n'
        "                  Stefan Szeider},\n"
        "  title        = {Beyond O(n\\({}^{\\mbox{2}}\\)) Time for Treewidth {\\&} {SAT}},\n"
        "  journal      = {Theor. Comput. Sci.},\n"
        "  volume       = {812},\n"
        "  pages        = {1--20},\n"
        "  year         = {2020},\n"
        "  url          = {https://doi.org/10.1016/j.tcs.2020.01.001},\n"
        "  doi          = {10.1016/J.TCS.2020.01.001},\n"
        "  timestamp    = {Mon, 15 Mar 2021 00:00:00 +0100},\n"
        "  biburl       = {https://dblp.org/rec/journals/tcs/GodelS20.bib},\n"
        "  bibsource    = {dblp computer science bibliography, https://dblp.org}\n"
        "}\n"
    )


def test_render_inproceedings_with_crossref(conn):
    bib = bibtex_for_key(conn, "conf/sat/KirchwegerS21")
    assert bib.startswith("@inproceedings{DBLP:conf/sat/KirchwegerS21,\n")
    # editors, booktitle, series, volume, publisher come from the proceedings record
    assert "  editor       = {Chu{-}Min Li and\n                  Felip Many{\\`{a}}},\n" in bib
    assert (
        "  booktitle    = {Theory and Applications of Satisfiability Testing - {SAT} 2021 - 24th\n"
        "                  International Conference, Barcelona, Spain, July 5-9, 2021, Proceedings},\n"
    ) in bib
    assert "  series       = {Lecture Notes in Computer Science},\n" in bib
    assert "  volume       = {12831},\n" in bib
    assert "  publisher    = {Springer},\n" in bib
    assert "  pages        = {34:1--34:16},\n" in bib
    assert "  title        = {{SAT} Modulo Symmetries for Graph Generation},\n" in bib
    # timestamp: the later of the record's and the crossref's mdate
    assert "  timestamp    = {Tue, 20 Jul 2021 00:00:00 +0200},\n" in bib


def test_render_proceedings_isbn(conn):
    bib = bibtex_for_key(conn, "conf/sat/2021")
    assert bib.startswith("@proceedings{DBLP:conf/sat/2021,\n")
    assert "  isbn         = {978-3-030-80222-6},\n" in bib


def test_render_corr_eprint(conn):
    bib = bibtex_for_key(conn, "journals/corr/abs-2301-01234")
    assert "  journal      = {CoRR},\n" in bib
    assert "  volume       = {abs/2301.01234},\n" in bib
    # dblp pads eprinttype with one extra space: "=" in column 17, not 16
    assert "  eprinttype    = {arXiv},\n  eprint       = {2301.01234},\n" in bib
    assert "  doi          = {10.48550/ARXIV.2301.01234},\n" in bib


def test_render_unknown_key(conn):
    assert bibtex_for_key(conn, "conf/xx/Nope99") is None


# ---------------------------------------------------------------------- local client


def test_search(db):
    r = db.search("SAT modulo symmetries")
    keys = [x["dblp_key"] for x in r]
    assert set(keys) == {"conf/sat/KirchwegerS21", "journals/corr/abs-2301-01234"}
    assert set(r[0]) >= RESULT_KEYS
    x = next(x for x in r if x["dblp_key"] == "conf/sat/KirchwegerS21")
    assert x["venue"] == "SAT"
    assert x["year"] == 2021
    assert x["type"] == "Conference and Workshop Papers"
    assert x["url"] == "https://dblp.org/rec/conf/sat/KirchwegerS21"
    assert x["authors"] == ["Markus Kirchweger", "Stefan Szeider"]


def test_search_filters_and_or(db):
    r = db.search("SAT modulo symmetries", year_from=2022)
    assert [x["dblp_key"] for x in r] == ["journals/corr/abs-2301-01234"]
    r = db.search("symmetries", venue_filter="corr")
    assert [x["dblp_key"] for x in r] == ["journals/corr/abs-2301-01234"]
    r = db.search("treewidth or coloring")
    assert {x["dblp_key"] for x in r} == {"journals/tcs/GodelS20", "conf/sat/ManyaG21"}
    r = db.search("author:Szeider 2020")
    assert [x["dblp_key"] for x in r] == ["journals/tcs/GodelS20"]
    assert db.search('C++ "NP-hard" (AND) : * ^ -') == []


def test_search_include_bibtex(db):
    r = db.search("Graph Coloring MaxSAT", include_bibtex=True)
    assert r[0]["dblp_key"] == "conf/sat/ManyaG21"
    # same citation-key rewrite as dblp_client.fetch_bibtex_entry
    assert r[0]["bibtex"].startswith("@inproceedings{Manya2021,")


def test_fuzzy_title_search(db):
    r = db.fuzzy_title_search("SAT Modulo Symetries for Graph Generaton", 0.8)
    assert r[0]["dblp_key"] == "conf/sat/KirchwegerS21"
    assert 0.8 <= r[0]["similarity"] < 1.0
    r = db.fuzzy_title_search("Graph Coloring", 0.5)
    assert r[0]["dblp_key"] == "conf/sat/ManyaG21"
    assert r[0]["similarity"] >= 0.8  # substring match
    assert db.fuzzy_title_search("Graph Coloring", 0.5, year_to=2020) == []


def test_author_publications(db):
    r = db.get_author_publications("kurt godel", 0.8)  # case and diacritics ignored
    assert r["name"] == "Kurt Gödel"
    assert r["publication_count"] == 2
    assert {p["dblp_key"] for p in r["publications"]} == {
        "journals/tcs/GodelS20",
        "conf/sat/ManyaG21",
    }
    assert set(r["stats"]) == {"venues", "years", "types"}
    r = db.get_author_publications("Stefan Szieder", 0.8)  # typo
    assert r["name"] == "Stefan Szeider"
    assert r["publication_count"] == 3


def test_author_typo_transposition_ranking(db):
    # szieder -> szeider (0.857) ties with szreder/szender and is beaten by sziede/szeder
    # (0.923), all present via journals/tcs/SzuderSSSS19; the transposition must still
    # be among the spelling variants (this failed on the full index before the fix).
    assert "szeider" in db._spelling_variants("szieder")
    r = db.get_author_publications("Stefan Szieder", 0.8)
    assert r["name"] == "Stefan Szeider"
    r = db.get_author_publications("Nobody Atall", 0.8)
    assert r["publication_count"] == 0 and r["publications"] == []


def test_venue_info(db):
    r = db.get_venue_info("SAT")
    assert r["url"] == "https://dblp.org/db/conf/sat/index.html"
    assert r["type"] == "Conference or Workshop"
    assert r["acronym"] == "SAT"
    assert r["venue"].startswith("Theory and Applications of Satisfiability Testing")
    assert db.get_venue_info("Theor. Comput. Sci.")["url"] == (
        "https://dblp.org/db/journals/tcs/index.html"
    )
    assert db.get_venue_info("zzzz qqqq") == {"venue": "", "acronym": "", "type": "", "url": ""}


# ------------------------------------------------------------------ backend selection


def test_select_env_path(mini_index, tmp_path):
    b = backend_mod.select_backend(env={"MCP_DBLP_INDEX": str(mini_index)}, index_dir=tmp_path)
    assert isinstance(b, backend_mod.IndexBackend)
    assert b.release == "2026-09-01"


def test_select_env_http_overrides_current(mini_index, tmp_path):
    local_index.set_current(str(mini_index), str(tmp_path))
    b = backend_mod.select_backend(env={"MCP_DBLP_INDEX": "http"}, index_dir=str(tmp_path))
    assert isinstance(b, backend_mod.HttpBackend)


def test_select_unset_no_current(tmp_path):
    # no silent web fallback: the tool call gets a message pointing to `fetch`
    b = backend_mod.select_backend(env={}, index_dir=str(tmp_path))
    assert isinstance(b, backend_mod.IndexBackend)
    assert b.fetcher is None
    with pytest.raises(backend_mod.IndexUnavailable, match="mcp-dblp-index fetch"):
        b.search(query="x")


def test_select_unset_current_symlink(mini_index, tmp_path):
    local_index.set_current(str(mini_index), str(tmp_path))
    assert local_index.current_index(str(tmp_path)) is not None
    b = backend_mod.select_backend(env={}, index_dir=str(tmp_path))
    assert isinstance(b, backend_mod.IndexBackend)
    assert b.release == "2026-09-01"


def test_select_current_text_file(mini_index, tmp_path):
    (tmp_path / "current").write_text(str(mini_index) + "\n")
    assert local_index.current_index(str(tmp_path)) == str(mini_index)
    b = backend_mod.select_backend(env={}, index_dir=str(tmp_path))
    assert b.release == "2026-09-01"


def test_select_bad_index_reports_error(tmp_path):
    bad = tmp_path / "bad.sqlite"
    bad.write_bytes(b"not a database")
    b = backend_mod.select_backend(env={"MCP_DBLP_INDEX": str(bad)}, index_dir=str(tmp_path))
    with pytest.raises(backend_mod.IndexUnavailable, match="Cannot open the local dblp index"):
        b.search(query="x")
    missing = tmp_path / "missing.sqlite"
    b = backend_mod.select_backend(env={"MCP_DBLP_INDEX": str(missing)}, index_dir=str(tmp_path))
    with pytest.raises(backend_mod.IndexUnavailable, match="does not exist"):
        b.search(query="x")


# ---------------------------------------------------------------- local backend tools


@pytest.fixture(scope="module")
def local(mini_index):
    return backend_mod.LocalBackend(str(mini_index))


def test_backend_bibtex_for_citation(local, conn):
    ref = bibtex_for_key(conn, "conf/sat/KirchwegerS21")
    for key in (
        "conf/sat/KirchwegerS21",
        "DBLP:conf/sat/KirchwegerS21",
        "dblp.org/rec/conf/sat/KirchwegerS21.bib",
        "https://dblp.uni-trier.de/rec/conf/sat/KirchwegerS21",
    ):
        bib = local.bibtex_for_citation(key, "Kirchweger2021")
        assert bib == ref.replace("{DBLP:conf/sat/KirchwegerS21,", "{Kirchweger2021,", 1)


def test_backend_missing_key_uses_web_fallback(local, monkeypatch):
    calls = []

    def fake_fetch(url, new_key):
        calls.append(url)
        return "% Error: Connection failed"

    monkeypatch.setattr(backend_mod.dblp_client, "fetch_and_process_bibtex", fake_fetch)
    bib = local.bibtex_for_citation("conf/xx/Nope99", "X")
    assert bib.startswith("% Error: DBLP key 'conf/xx/Nope99' not found in the local index")
    assert calls == [f"{backend_mod.dblp_client.DBLP_BASE_URL}/rec/conf/xx/Nope99.bib"]


def test_http_backend_rejects_non_bibtex(monkeypatch):
    monkeypatch.setattr(
        backend_mod.dblp_client,
        "fetch_and_process_bibtex",
        lambda url, key: "<!doctype html><title>Making sure you're not a bot!</title>",
    )
    bib = backend_mod.HttpBackend().bibtex_for_citation("conf/sat/X21", "X")
    assert bib.startswith("% Error")


def test_backend_set_mirror_message(local, monkeypatch):
    monkeypatch.setattr(backend_mod.dblp_client, "DBLP_BASE_URL", "https://dblp.org")
    msg = local.set_mirror("dblp.uni-trier.de")
    assert "local DBLP index" in msg and "fallback" in msg
    assert backend_mod.dblp_client.DBLP_BASE_URL == "https://dblp.uni-trier.de"


# ------------------------------------------------------------------------ CLI helpers


def test_candidate_releases():
    import datetime

    assert local_index._candidate_releases(datetime.date(2026, 2, 15), 3) == [
        "2026-02-01",
        "2026-01-01",
        "2025-12-01",
    ]


def test_status_and_set_current(mini_index, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("MCP_DBLP_INDEX", raising=False)
    assert local_index.status(str(tmp_path), str(tmp_path)) == 0
    assert "no current index" in capsys.readouterr().out
    local_index.set_current(str(mini_index), str(tmp_path))
    local_index.set_current(str(mini_index), str(tmp_path))  # replacing works
    local_index.status(str(tmp_path), str(tmp_path))
    out = capsys.readouterr().out
    assert "release:      2026-09-01" in out and "publications: 9" in out


def test_search_year_syntax(db):
    # the syntax recommended in instructions_prompt.md
    for q in ("author:Szeider year:2020", "author:Szeider 2020", "Szeider 2020"):
        assert [x["dblp_key"] for x in db.search(q)] == ["journals/tcs/GodelS20"], q


# ------------------------------------------------- search: name terms and match lines


def _bm25_order(db, match, year=None):
    """dblp keys in the plain bm25 order (the ranking search used before name terms)."""
    ids = [
        i for i, _ in sorted(db._fts_ids(match, 1000, year_eq=year), key=lambda x: (x[1], -x[0]))
    ]
    return [r["dblp_key"] for r in db._fetch(ids)]


def test_search_surname_beats_title_word(db):
    # bm25 alone ranks the title word 'Kolmogorov-Smirnov' above the author Ivan Smirnov
    assert _bm25_order(db, '"smirnov"', 2021)[0] == "journals/ipl/PetrovaW21"
    r = db.search("Smirnov 2021")
    assert [x["dblp_key"] for x in r] == ["journals/ipl/SmirnovB21", "journals/ipl/PetrovaW21"]
    assert r[0]["match"] == "smirnov = author 1 of 2"
    assert r[1]["match"] == "smirnov = title word only"


def test_search_title_phrase_with_surname(db):
    r = db.search("Kolmogorov-Smirnov test")
    assert r[0]["dblp_key"] == "journals/ipl/PetrovaW21"
    assert r[0]["match"] == "kolmogorov, smirnov, test = title"


def test_search_prefix_fallback_is_labelled(db):
    # no word 'vardi' or 'commons': only the prefix fallback (VarDial, Commonsense) matches
    r = db.search("Vardi commons")
    assert [x["dblp_key"] for x in r] == ["conf/vardial/NguyenT19"]
    assert r[0]["match"] == (
        'prefix match only: vardi ~ prefix of "VarDial" (venue); '
        'commons ~ prefix of "Commonsense" (title)'
    )


def test_search_topic_query_keeps_bm25_order(db):
    # no word is a person's family name: plain bm25 order, as before
    expected = ["conf/sat/ManyaG21", "conf/sat/KirchwegerS21", "journals/ipl/PetrovaW21"]
    assert _bm25_order(db, '"graph"') == expected
    r = db.search("graph")
    assert [x["dblp_key"] for x in r] == expected
    assert all(x["match"] == "graph = title" for x in r)
    r = db.search("graph or treewidth")
    assert [x["dblp_key"] for x in r] == ["journals/tcs/GodelS20", *expected]
    assert [x["dblp_key"] for x in db.search("graph", max_results=2)] == expected[:2]


def test_search_given_name_and_author_field(db):
    r = db.search("author:Szeider 2021")
    assert [(x["dblp_key"], x["match"]) for x in r] == [
        ("conf/sat/KirchwegerS21", "szeider = author 2 of 2")
    ]
    r = db.search("Ivan")  # a given name is reported as such
    assert [(x["dblp_key"], x["match"]) for x in r] == [
        ("journals/ipl/SmirnovB21", "ivan = given name of author 1 of 2")
    ]


def test_parse_subquery_author_term():
    parse = LocalDblp._parse_subquery
    assert parse("Smirnov test 2021", False, author_term="smirnov") == (
        'authors : "smirnov" AND "test"',
        2021,
    )
    # only plain words are restricted: a field-prefixed word keeps its field
    assert parse("test title:smirnov", False, author_term="smirnov") == (
        '"test" AND title : "smirnov"',
        None,
    )
    assert parse("Smirnov test 2021", False) == ('"smirnov" AND "test"', 2021)


def _article(key, authors, title, year=2021):
    people = "".join(f"<author>{a}</author>" for a in authors)
    return (
        f'<article mdate="2021-01-01" key="journals/jt/{key}">{people}'
        f"<title>{title}</title><year>{year}</year><journal>J. Test</journal></article>"
    )


@pytest.fixture(scope="module")
def pool_db(tmp_path_factory):
    """More matches than the candidate pool (30) holds: 40 papers with 'Smirnov' in the
    title and 40 by Ivan Smirnov; 40 'Graph' titles and 3 papers by Anna Graph."""
    recs = [
        _article(f"S{i}", ["Olga Petrova"], f"Smirnov bounds{' x' * (i % 5)} {i}.")
        for i in range(40)
    ]
    recs += [
        _article(f"A{i}", ["Ivan Smirnov", "Lena Berg"], f"Parsing streams {i}.") for i in range(40)
    ]
    recs += [
        _article(f"G{i}", ["Hans Weber"], f"Graph bounds{' y' * (i % 7)} {i}.") for i in range(40)
    ]
    recs += [_article(f"N{i}", ["Anna Graph"], f"Parsing lists {i}.") for i in range(3)]
    d = tmp_path_factory.mktemp("pool")
    dump = d / "dblp-2026-09-01.xml.gz"
    body = (
        '<?xml version="1.0" encoding="ISO-8859-1"?>\n<!DOCTYPE dblp SYSTEM "dblp.dtd">\n<dblp>\n'
    )
    with gzip.open(dump, "wb") as f:
        f.write((body + "\n".join(recs) + "\n</dblp>\n").encode("iso-8859-1"))
    out = d / "dblp-2026-09-01.sqlite"
    assert local_index.build(str(dump), str(out))
    x = LocalDblp(str(out))
    yield x
    x.close()


def test_search_pool_author_variant(pool_db):
    # bm25 fills the plain query's pool with title matches; the authors-only variant
    # brings in the papers by Smirnov, and 'smirnov' becomes a name term
    assert all("/S" in k for k in _bm25_order(pool_db, '"smirnov"', 2021)[:30])
    r = pool_db.search("Smirnov 2021")
    assert len(r) == 10
    assert all(x["match"] == "smirnov = author 1 of 2" for x in r)


def test_search_pool_topic_order_unchanged(pool_db):
    # 'graph' is a family name of 3 authors only: not a name term, and their papers
    # (found by the authors-only variant, with a higher authors-only bm25) must not
    # jump ahead of the plain bm25 order
    r = pool_db.search("graph")
    assert [x["dblp_key"] for x in r] == _bm25_order(pool_db, '"graph"')[:10]
    assert all(x["match"] == "graph = title" for x in r)


def test_author_publications_year_filter(db):
    r = db.get_author_publications("Stefan Szeider", 0.8, year_from=2021)
    assert [p["year"] for p in r["publications"]] == [2023, 2021]
    assert r["publication_count"] == 2 and r["total_publications"] == 3
    r = db.get_author_publications("Stefan Szeider", 0.8, year_to=2021)
    assert [p["dblp_key"] for p in r["publications"]] == [
        "conf/sat/KirchwegerS21",
        "journals/tcs/GodelS20",
    ]
    r = db.get_author_publications("Stefan Szeider", 0.8, year_from=2021, year_to=2021)
    assert [p["dblp_key"] for p in r["publications"]] == ["conf/sat/KirchwegerS21"]
    r = db.get_author_publications("Stefan Szeider", 0.8, max_results=1, year_to=2022)
    assert [p["year"] for p in r["publications"]] == [2021]  # filter before the cut
    r = db.get_author_publications("Stefan Szeider", 0.8, year_from=2024)
    assert r["publications"] == [] and r["publication_count"] == 0
    assert r["total_publications"] == 3
