# DBLP MCP Server — Usage Instructions

These instructions are delivered automatically with the first tool call in each session.

## Workflow

1. **Search** each citation with `search`: the first author's surname, one or two distinctive title words, and the year.
2. **Check** the result's `Matched:` line and pick the right version of the paper (see below).
3. **Add** it with `add_bibtex_entry` (the `dblp_key` from the result and your `citation_key`).
4. **Export** all entries at the end with `export_bibtex`.

The entries are DBLP's own BibTeX. Never write BibTeX for a paper that is in DBLP yourself.

## Search Strategy

- `search("Vaswani attention 2017")` or `search("author:Vaswani title:attention year:2017")`
- A surname plus a year alone can return only a namesake's papers: `search("Vaswani 2017")` lists ten papers by Namrata Vaswani, not "Attention Is All You Need".
- The field prefixes `author:`, `title:`, `venue:` and `year:` apply to the next word or quoted phrase only. `or` separates alternatives.
- Venue words match only as DBLP writes the venue: journal abbreviations such as "Nat." for Nature or "J. ACM", conference acronyms such as "NeurIPS". `search("LeCun deep learning Nature 2015")` finds nothing. Leave venue words out, or look the venue up with `get_venue_info`.

If the first search fails:
1. Other title words, or field prefixes: `search("author:Smith title:transformer")`
2. The title: `fuzzy_title_search("Attention is All You Need", similarity_threshold=0.7)`. It tolerates typos and partial titles.
3. The author: `get_author_publications("Yoshua Bengio", similarity_threshold=0.8, year_from=2013, year_to=2013)`. The first lines of the answer name the DBLP person shown and the other persons with that name; DBLP numbers namesakes ("Wei Wang 0010").
4. Name variations: full name, surname only, other spellings (accents do not matter).

## Checking a Result

- The `Matched:` line shows where each query word was found. The authors named in the citation should match as authors (`= author 1 of 3`; for "X et al." the first author), not as a given name or a title word. Results marked "prefix match only" are usually other papers.
- The same paper often has several records: the arXiv preprint (venue CoRR), the conference version, a journal version, reprints in collections. Take the version the citation names, and CoRR only for a citation of the arXiv preprint.
- Compare the whole title with the citation, not only the matched words: with a wrong year in the query, a search can return a sibling paper ("Graph Minors. III." instead of "Graph Minors. II.").
- If the citation's year, venue or title differ from DBLP's record, tell the user instead of changing it silently.
- If no result fits, do not pick the closest one: report the citation as not found.

## Existing .bib Files

The `biburl` field of a DBLP entry (`https://dblp.org/rec/KEY.bib`) or a citation key of the form `DBLP:KEY` identifies the record: pass it to `add_bibtex_entry` directly, without a search.

## Parallel Calls

There are no rate limits. Batch 10 or more searches in one parallel request, then the adds:
```
search("Vaswani attention 2017")       # parallel
search("Srivastava dropout 2014")      # parallel
search("He residual learning 2016")    # parallel
```
```
add_bibtex_entry(dblp_key="conf/nips/VaswaniSPUJGKP17", citation_key="Vaswani2017")         # parallel
add_bibtex_entry(dblp_key="journals/jmlr/SrivastavaHKSS14", citation_key="Srivastava2014")  # parallel
```
Read every reply: `add_bibtex_entry` gives the number of entries in the collection or an error, and "(replaced existing entry)" means that the citation key was already used.

## Coverage

The server searches a local copy made from the monthly DBLP dump. Papers that DBLP added after that dump are not found, and their keys fail in `add_bibtex_entry` with "not found in the local index". `set_dblp_mirror` does not help, because dblp.org blocks automated clients. DBLP covers computer science: papers from other fields, and many books, are not in it.

## When Papers Are Not Found

After 3 or more different searches, mark the citation as [CITATION NOT FOUND] and ask the user how to proceed. Do NOT fabricate BibTeX entries: only DBLP's entries are trustworthy.

## Key Rules

- Copy `dblp_key` exactly from the results; a failing add usually means a mistyped key.
- Citation keys must be unique (e.g., `Szeider09`, `Vaswani2017`).
- Always produce the .bib file with `export_bibtex`, not by writing it yourself.
- `export_bibtex` empties the collection; add entries for papers outside DBLP to the file afterwards.
