"""Regression tests for the findings of the 2026-09-23 usability test with an inner Claude
(evaluation/metaclaude_2026-09-23/REPORT.md), on a small index built from generated XML
that reproduces each failure of the real index."""

import gzip

import pytest
from mcp.client import Client

from mcp_dblp import backend as backend_mod
from mcp_dblp import bibtex_render, local_index
from mcp_dblp.local_client import LocalDblp
from mcp_dblp.server import create_server

HEADER = '<?xml version="1.0" encoding="ISO-8859-1"?>\n<!DOCTYPE dblp SYSTEM "dblp.dtd">\n<dblp>\n'


def _rec(kind, key, year, title, people=(), who="author", **fields):
    tags = "".join(f"<{who}>{p}</{who}>" for p in people)
    tags += f"<title>{title}</title><year>{year}</year>"
    tags += "".join(f"<{k}>{v}</{k}>" for k, v in fields.items())
    return f'<{kind} mdate="2025-01-01" key="{key}">{tags}</{kind}>\n'


def _xml() -> str:
    recs = [
        _rec(
            "inproceedings",
            "conf/nips/VaswaniSPUJGKP17",
            2017,
            "Attention is All you Need.",
            ["Ashish Vaswani", "Noam Shazeer"],
            booktitle="NIPS",
        )
    ]
    # short titles with 'all' and 'you' fill the candidate pool of a misspelled title
    recs += [
        _rec(
            "article",
            f"journals/dc/D{i}",
            2020,
            f"All You Know {i}.",
            ["Ann Decoy"],
            journal="Decoys",
        )
        for i in range(120)
    ]
    # newer, longer titles that contain the partial title 'Attention is all'
    recs += [
        _rec(
            "article",
            f"journals/dc/A{i}",
            2018 + i,
            f"Attention is all we want for topic {i}.",
            ["Bo Decoy"],
            journal="Decoys",
        )
        for i in range(12)
    ]
    # venues: journals whose full names are also the exact names of small venues
    recs += [
        _rec(
            "article",
            f"journals/jacm/J{i}",
            1990 + i,
            f"Paper {i}.",
            ["Ada Lovel"],
            journal="J. ACM",
        )
        for i in range(2)
    ]
    recs += [
        _rec(
            "article",
            f"journals/ai/A{i}",
            1990 + i,
            f"Reasoning {i}.",
            ["Al Reason"],
            journal="Artif. Intell.",
        )
        for i in range(3)
    ]
    recs.append(
        _rec(
            "incollection",
            "books/ap/96/Fox96",
            1996,
            "Planning.",
            ["Maria Fox"],
            booktitle="Artificial Intelligence",
        )
    )
    recs += [
        _rec(
            "article",
            f"journals/tcs/T{i}",
            2000 + i,
            f"Automata {i}.",
            ["Tim Theor"],
            journal="Theor. Comput. Sci.",
        )
        for i in range(3)
    ]
    recs.append(
        _rec(
            "inproceedings",
            "conf/tcs/Old77",
            1977,
            "Old Results.",
            ["Otto Alt"],
            booktitle="Theoretical Computer Science",
        )
    )
    # IJCAI: the newest volume is a small co-located workshop volume
    recs.append(
        _rec(
            "proceedings",
            "conf/ijcai/2025",
            2025,
            "Proceedings of the Thirty-Fourth International Joint Conference on Artificial "
            "Intelligence, IJCAI 2025, Montreal, Canada, August 16-22, 2025",
            ["James Kwok"],
            who="editor",
            booktitle="IJCAI",
        )
    )
    recs += [
        _rec(
            "inproceedings",
            f"conf/ijcai/M25{i}",
            2025,
            f"Main Track Paper {i}.",
            ["Wei Wang 0010"] if i == 0 else ["Mia Main"],
            booktitle="IJCAI",
            crossref="conf/ijcai/2025",
        )
        for i in range(3)
    ]
    recs.append(
        _rec(
            "proceedings",
            "conf/ijcai/2025democrai",
            2026,
            "Democracy and AI - First International Workshop, DemocrAI 2025, Held in "
            "Conjunction with IJCAI 2025, Montreal, Canada, August 16, 2025, Proceedings",
            ["Wendy Work"],
            who="editor",
            booktitle="DemocrAI@IJCAI",
        )
    )
    recs.append(
        _rec(
            "inproceedings",
            "conf/ijcai/W26",
            2026,
            "Workshop Paper.",
            ["Will Work"],
            booktitle="DemocrAI@IJCAI",
            crossref="conf/ijcai/2025democrai",
        )
    )
    # namesakes: DBLP's unnumbered name plus two numbered persons
    recs += [
        _rec(
            "article",
            f"journals/dc/WW{i}",
            2021,
            f"Unassigned Wang Paper {i}.",
            ["Wei Wang"],
            journal="Decoys",
        )
        for i in range(2)
    ]
    recs.append(
        _rec(
            "article",
            "journals/dc/WW9",
            2022,
            "Numbered Wang Paper.",
            ["Wei Wang 0009"],
            journal="Decoys",
        )
    )
    # beta test: inner capitals in titles, and titles for the fuzzy match kinds
    recs.append(
        _rec(
            "inproceedings",
            "conf/cav/Biere24",
            2024,
            "CaDiCaL 2.0.",
            ["Armin Biere"],
            booktitle="CAV",
        )
    )
    recs += [
        _rec("article", f"journals/mt/M{i}", 2010 + i, t, ["Mat Theo"], journal="Decoys")
        for i, t in enumerate(["A random matching theory.", "Infinite matching theory."])
    ]
    recs.append(
        _rec(
            "article",
            "journals/sg/S1",
            2015,
            "Subgraph Coloring Games.",
            ["Sub Graph"],
            journal="Decoys",
        )
    )
    recs.append(
        _rec(
            "inproceedings",
            "conf/stoc/Cook71",
            1971,
            "The Complexity of Theorem-Proving Procedures",
            ["Stephen A. Cook"],
            booktitle="STOC",
        )
    )
    return HEADER + "".join(recs) + "</dblp>\n"


@pytest.fixture(scope="module")
def uindex(tmp_path_factory):
    d = tmp_path_factory.mktemp("usability")
    dump = d / "dblp-2026-09-01.xml.gz"
    with gzip.open(dump, "wt", encoding="iso-8859-1") as f:
        f.write(_xml())
    out = d / "dblp-2026-09-01.sqlite"
    assert local_index.build(str(dump), str(out))
    return out


@pytest.fixture(scope="module")
def udb(uindex):
    db = LocalDblp(str(uindex))
    yield db
    db.close()


@pytest.fixture(scope="module")
def mini(mini_index):
    db = LocalDblp(str(mini_index))
    yield db
    db.close()


def _keys(results):
    return [r["dblp_key"] for r in results]


# ---------------------------------------------------------------- field prefixes


def test_field_prefix_covers_one_word(mini):
    # 'venue:SAT Szeider' used to look for Szeider in the venue and found nothing
    assert "conf/sat/KirchwegerS21" in _keys(mini.search("venue:SAT Szeider"))
    assert _keys(mini.search("author:Kirchweger title:modulo symmetries 2021")) == [
        "conf/sat/KirchwegerS21"
    ]
    # a quoted phrase is one unit for its prefix
    items, year = LocalDblp._subquery_items('title:"graph generation" Szeider 2021')
    assert [(i.field, i.toks) for i in items] == [
        ("title", ["graph", "generation"]),
        (None, ["szeider"]),
    ]
    assert year == 2021


# ---------------------------------------------------------------- fuzzy titles


def test_fuzzy_title_misspelled(udb):
    r = udb.fuzzy_title_search("Atention is all you ned", 0.6)
    assert _keys(r)[:1] == ["conf/nips/VaswaniSPUJGKP17"]


def test_fuzzy_title_partial_prefers_shortest_containing_title(udb):
    r = udb.fuzzy_title_search("Attention is all", 0.7)
    assert _keys(r)[:1] == ["conf/nips/VaswaniSPUJGKP17"]
    sims = [x["similarity"] for x in r]
    assert sims == sorted(sims, reverse=True) and len(set(sims)) > 1  # graded, not all 0.8


# ---------------------------------------------------------------- venues


@pytest.mark.parametrize(
    "name, stream",
    [
        ("Journal of the ACM", "journals/jacm"),  # only generic words: 'journal', 'acm'
        ("Artificial Intelligence", "journals/ai"),  # also a book's exact booktitle
        ("Theoretical Computer Science", "journals/tcs"),  # also a 1977 conference
        ("J. ACM", "journals/jacm"),
    ],
)
def test_venue_full_journal_names(udb, name, stream):
    assert udb.get_venue_info(name)["stream"] == stream


def test_venue_named_after_main_proceedings(udb):
    r = udb.get_venue_info("IJCAI")
    assert r["venue"] == "International Joint Conference on Artificial Intelligence (IJCAI)"
    assert r["latest_proceedings"].startswith("Proceedings of the Thirty-Fourth")


# ---------------------------------------------------------------- namesakes


def test_author_numbered_person_selectable(udb):
    r = udb.get_author_publications("Wei Wang", 0.8)
    assert r["dblp_name"] == "Wei Wang" and r["total_publications"] == 2
    assert {n for n, _, _ in r["other_candidates"]} == {"Wei Wang 0010", "Wei Wang 0009"}
    r = udb.get_author_publications("Wei Wang 0010", 0.8)
    assert r["dblp_name"] == "Wei Wang 0010"
    assert _keys(r["publications"]) == ["conf/ijcai/M250"]


# ---------------------------------------------------------------- server output


def _text(result) -> str:
    return "\n".join(c.text for c in result.content)


@pytest.mark.asyncio
async def test_server_author_header_and_venue_not_found(uindex):
    b = backend_mod.IndexBackend(path=str(uindex))
    async with Client(create_server(b)) as c:
        t = _text(
            await c.call_tool(
                "get_author_publications", {"author_name": "Wei Wang", "similarity_threshold": 0.8}
            )
        )
        assert "DBLP person: Wei Wang (2 publications in total)" in t
        assert "Wei Wang 0010 (1)" in t and "Wei Wang 0009 (1)" in t
        assert "can mix several people" in t
        t = _text(
            await c.call_tool(
                "get_author_publications",
                {"author_name": "Wei Wang 0010", "similarity_threshold": 0.8},
            )
        )
        assert "DBLP person: Wei Wang 0010 (1 publications in total)" in t
        assert "can mix" not in t
        t = _text(await c.call_tool("get_venue_info", {"venue_name": "Journal of Nowhere"}))
        assert t.startswith("No DBLP venue found for 'Journal of Nowhere'")


# ------------------------------------------- second test run: guidance without the skill


@pytest.mark.asyncio
async def test_server_states_release_and_rejects_ambiguous_input(uindex):
    b = backend_mod.IndexBackend(path=str(uindex))
    async with Client(create_server(b)) as c:
        # the first answer carries the instructions, which name the dump's release
        t = _text(await c.call_tool("search", {"query": "zzqx"}))
        assert t.startswith("Found 0 publications")
        assert "dump of 2026-09-01" in t and "DBLP Usage Instructions" in t
        # parentheses cannot group alternatives: an explicit error instead of 'A' or 'B C'
        r = await c.call_tool("search", {"query": "(Vaswani or Shazeer) attention"})
        assert r.is_error and "parentheses are not supported" in _text(r)
        r = await c.call_tool("search", {"query": "(Vaswani) attention"})  # no 'or': harmless
        assert not r.is_error and "conf/nips/VaswaniSPUJGKP17" in _text(r)
        r = await c.call_tool("search", {"query": "  "})
        assert r.is_error and "query is empty" in _text(r)
        # a publisher name alone is not a venue
        t = _text(await c.call_tool("get_venue_info", {"venue_name": "ACM"}))
        assert t.startswith("No DBLP venue found for 'ACM'")
        # a cut list says so
        t = _text(
            await c.call_tool(
                "get_author_publications",
                {"author_name": "Ann Decoy", "similarity_threshold": 0.8, "max_results": 5},
            )
        )
        assert "Showing the newest 5 of 120" in t


# ---------------------------------- beta test: capitals in titles, fuzzy title match kinds


@pytest.mark.parametrize(
    "title, expected",
    [
        (
            "Revisiting DRUP-based Interpolants with CaDiCaL 2.0",
            "Revisiting {DRUP}-based Interpolants with {CaDiCaL} 2.0",
        ),
        ("McCarthy's LaTeX on the iPhone in 3D", "{McCarthy's} {LaTeX} on the {iPhone} in {3D}"),
        ("Q-Learning for Multi-Agent k-SAT", "{Q}-Learning for Multi-Agent k-{SAT}"),
        ("Graph Minors. II. Tree-Width", "Graph Minors. {II}. Tree-Width"),
        # proper nouns cannot be told from ordinary title-case words: unchanged
        ("Keller's Conjecture and the Lean Library", "Keller's Conjecture and the Lean Library"),
    ],
)
def test_title_protects_inner_capitals(title, expected):
    assert bibtex_render.text_field(title, inner_caps=True) == expected


def test_inner_capitals_only_in_titles(udb):
    math = "Ramsey Numbers $R(C_4, K_9)$"
    assert bibtex_render.text_field(math, inner_caps=True) == bibtex_render.text_field(math)
    assert bibtex_render.text_field("LIPIcs") == "LIPIcs"  # venues keep dblp's rule
    assert "  title        = {{CaDiCaL} 2.0},\n" in udb.bibtex_for_key("conf/cav/Biere24")


def test_fuzzy_title_match_kinds(udb):
    # case, hyphens and punctuation do not matter: the same words score 1.0
    r = udb.fuzzy_title_search("the complexity of theorem proving procedures", 0.9)
    assert (r[0]["dblp_key"], r[0]["similarity"], r[0]["title_match"]) == (
        "conf/stoc/Cook71",
        1.0,
        "same title",
    )
    # a longer title containing a short query is no near-exact hit ...
    assert udb.fuzzy_title_search("Matching Theory", 0.9) == []
    r = udb.fuzzy_title_search("Matching Theory", 0.7)
    assert {x["title_match"] for x in r} == {"title contains the query"}
    assert all(x["similarity"] < 0.85 for x in r)
    # ... and containment respects word boundaries: 'subgraph coloring' is only similar
    r = udb.fuzzy_title_search("Graph Coloring", 0.5)
    assert [x["title_match"] for x in r if x["dblp_key"] == "journals/sg/S1"] == ["similar title"]


@pytest.mark.asyncio
async def test_server_shows_title_match_kind(uindex):
    b = backend_mod.IndexBackend(path=str(uindex))
    async with Client(create_server(b)) as c:
        r = await c.call_tool(
            "fuzzy_title_search",
            {"title": "The Complexity of Theorem-Proving Procedures.", "similarity_threshold": 0.9},
        )
        assert "[Similarity: 1.00, same title]" in _text(r)
