# DBLP MCP Server — Usage Instructions

These instructions are delivered automatically with the first tool call in each session.

## Workflow

1. **Search** for each citation using `search` (author+year is most reliable)
2. **Add** each found entry immediately with `add_bibtex_entry` (pass `dblp_key` from search results and your `citation_key`)
3. **Export** all collected entries at the end with `export_bibtex`

BibTeX entries are fetched directly from DBLP — never create them manually.

## Search Strategy

**Use author+year for best results:**
- `search("Vaswani 2017")` or `search("author:Vaswani year:2017")`
- `search("Attention is All You Need Vaswani")`
- Title-only searches are less reliable (DBLP ranking may not prioritize the original paper)

**Progressive fallback:**
1. Author + year: `search("Smith 2023")`
2. Add title keywords: `search("Smith transformer 2023")`
3. Fuzzy title: `fuzzy_title_search("Attention is All You Need", similarity_threshold=0.7)`
4. Author publications: `get_author_publications("Yoshua Bengio", similarity_threshold=0.8)`
5. Try name variations (full name, last name only, different spellings)

Only mark as [CITATION NOT FOUND] after 3+ different search attempts.

## Parallel Searches, Sequential Adds

**Batch 5-10 searches in a single parallel request** for efficiency:
```
search("Vaswani 2017")       # parallel
search("Bengio 2015")        # parallel
search("LeCun deep learning") # parallel
```

Then **add each result one by one** (do NOT batch `add_bibtex_entry` calls):
```
add_bibtex_entry(dblp_key="conf/nips/VaswaniSPUJGKP17", citation_key="Vaswani2017")
add_bibtex_entry(dblp_key="journals/nature/LeCunBH15", citation_key="LeCun2015")
```

Adding one-by-one gives immediate feedback on failures so you can retry before moving on.

## DBLP Mirrors

If you encounter timeouts or connection errors, switch to a mirror:

```
set_dblp_mirror(host="dblp.uni-trier.de")
```

Available mirrors (all official):
- **dblp.org** (default) — primary, operated by Schloss Dagstuhl
- **dblp.uni-trier.de** — University of Trier mirror
- **dblp.dagstuhl.de** — Dagstuhl mirror

Once set, the mirror applies to all subsequent requests in the session.

## When Papers Are Not Found

After 3+ different search attempts, mark as [CITATION NOT FOUND] and ask the user how to proceed.
Do NOT fabricate BibTeX entries — only DBLP-sourced entries are trustworthy.

## Key Rules

- Copy `dblp_key` EXACTLY from search results — character by character
- If `add_bibtex_entry` fails, the key was likely copied incorrectly
- Ensure all citation keys are unique (e.g., `Szeider09`, `Vaswani2017`)
- Always use `export_bibtex` to produce the .bib file — do not write .bib files manually
- `export_bibtex` clears the collection; append any manually provided entries to the file afterward
