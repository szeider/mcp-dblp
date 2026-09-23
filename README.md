# MCP-DBLP

[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-green.svg)](https://modelcontextprotocol.io/) [![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT) [![Python Version](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)

A Model Context Protocol (MCP) server that gives Large Language Models access to the DBLP computer science bibliography (accompanying paper published at [AI4SC @ AAAI-26](https://www.tib-op.org/ojs/index.php/ocp/article/view/3161/3236)).

<a href="https://glama.ai/mcp/servers/cm42scf3iv">
  <img width="380" height="200" src="https://glama.ai/mcp/servers/cm42scf3iv/badge" alt="MCP-DBLP MCP server" />
</a>

## Overview

MCP-DBLP lets an LLM search DBLP through the Model Context Protocol and build a BibTeX file from the results. The model chooses the papers and the citation keys; the server writes the entries from DBLP's data, so the model never rewrites bibliographic fields.

Since version 2.0, MCP-DBLP works from a local copy of DBLP instead of the dblp.org web API, which now blocks automated clients. On first start the server downloads the local index, a prebuilt SQLite index of the monthly DBLP dump (about 1.3 GB, see [The local DBLP index](#the-local-dblp-index)). After that, all searches run locally, without rate limits.

## Features

- Boolean search with `and` and `or` (no parentheses), filtered by year and venue
- Fuzzy matching of titles and author names
- Author-aware ranking: each search result names the query words that matched an author, the title, or the venue, and marks prefix-only matches
- BibTeX in DBLP's own format, rendered from DBLP's data; it matches the `.bib` export on dblp.org except for the time of day in the `timestamp` field, and titles also brace words with inner capitals (`{CaDiCaL}`, `{DRUP}-based`) so that bibliography styles do not lowercase them
- Export of the collected entries to a `.bib` file, written by the server
- A new local index every month, installed with `mcp-dblp-index update`

## Available tools

| Tool                      | Description                                                    |
| ------------------------- | -------------------------------------------------------------- |
| `search`                  | Search DBLP with boolean queries                               |
| `fuzzy_title_search`      | Find publications by approximate title                         |
| `get_author_publications` | List an author's publications, optionally by year              |
| `get_venue_info`          | Look up a venue's name, acronym, type and DBLP page            |
| `add_bibtex_entry`        | Add the entry for a DBLP key to the collection                 |
| `export_bibtex`           | Write the collected entries to a `.bib` file                   |
| `set_dblp_mirror`         | Choose the dblp.org host for the web fallback (rarely needed)  |

## Feedback

Provide feedback to the author via this [form](https://form.jotform.com/szeider/mcp-dblp-feedback-form).

## System requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- About 5.5 GB of free disk space on first start (1.3 GB download plus 4.2 GB index), 4.2 GB afterwards

## Installation

### Claude Code

Run:

```bash
claude mcp add mcp-dblp -- uvx mcp-dblp
```

### Claude Desktop

Add to your Claude Desktop configuration file:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "mcp-dblp": {
      "command": "uvx",
      "args": ["mcp-dblp"]
    }
  }
}
```

### From source (development)

```bash
git clone https://github.com/szeider/mcp-dblp.git
cd mcp-dblp
uv venv && source .venv/bin/activate
uv pip install -e .
```

Then configure Claude Desktop with:

```json
{
  "mcpServers": {
    "mcp-dblp": {
      "command": "uv",
      "args": ["--directory", "/path/to/mcp-dblp/", "run", "mcp-dblp"]
    }
  }
}
```

## The local DBLP index

DBLP publishes all of its data as a monthly XML dump under the CC0 license. The companion repository [mcp-dblp-index](https://github.com/szeider/mcp-dblp-index) builds the local index from each dump and publishes it as a GitHub release.

**First start.** If no local index is installed, the server downloads the current release in the background (about 1.3 GB, unpacked to 4.2 GB). This takes about 2 minutes at 100 Mbit/s and about 9 minutes at 20 Mbit/s. Meanwhile, each tool call waits up to 40 seconds for the index and then answers with the download progress, so the first search takes longer than usual. An interrupted download resumes where it stopped.

**Updates.** A new local index is released every month. The installed one changes only when you update it:

```bash
uvx --from mcp-dblp mcp-dblp-index update
```

The old index keeps serving until the new one is installed. Papers added to DBLP after the release date are not in the index until the next release.

**Command line.** `mcp-dblp-index` manages the index:

| Command    | What it does                                                        |
| ---------- | ------------------------------------------------------------------- |
| `status`   | Show the installed index (release date, number of publications)     |
| `fetch`    | Download and install the current local index                        |
| `update`   | Install the newest release if it is newer than the installed one   |
| `download` | Download the raw DBLP XML dump (for building the index yourself)    |
| `build`    | Build the index from a downloaded dump (about 10 minutes)           |

**Environment variables.** Set them in the server's environment; in Claude Desktop, use the `env` field of the server entry:

| Variable                     | Meaning                                                                                           |
| ---------------------------- | ------------------------------------------------------------------------------------------------- |
| `MCP_DBLP_HOME`              | Directory for the index and downloads (default `~/.mcp-dblp`), e.g. on a disk with more space      |
| `MCP_DBLP_INDEX`             | Path of a specific index file; `http` switches to the dblp.org web API (blocked by dblp.org)      |
| `MCP_DBLP_FIRST_START_WAIT`  | Seconds a tool call waits for the first-start download (default 40)                                |

```json
{
  "mcpServers": {
    "mcp-dblp": {
      "command": "uvx",
      "args": ["mcp-dblp"],
      "env": { "MCP_DBLP_HOME": "/Volumes/Data/mcp-dblp" }
    }
  }
}
```

## Instructions

Usage instructions are delivered to the model with the first answer of each session. See [instructions_prompt.md](./src/mcp_dblp/instructions_prompt.md).

## Tool details

### search

Search DBLP for publications. All query words must occur in the title, the author names or the venue (case and accents do not matter). Each result shows its DBLP key and a `Matched:` line that says where each query word was found, for example `szeider = author 2 of 3`.

**Parameters:**

- `query` (string, required): The search words. `or` separates alternatives (no parentheses), `"quoted phrases"` match adjacent words, the field prefixes `author:`, `title:`, `venue:` and `year:` apply to the next word or phrase, and a 4-digit year restricts the results to that year. Example: `author:Vaswani title:attention 2017`
- `max_results` (integer, optional): Maximum number of publications to return. Default is 10
- `year_from` (integer, optional): Lower bound for publication year
- `year_to` (integer, optional): Upper bound for publication year
- `venue_filter` (string, optional): Case-insensitive substring filter for publication venues (e.g., 'iclr')
- `include_bibtex` (boolean, optional): Whether to include BibTeX entries in the results. Default is false

### fuzzy_title_search

Find publications by title, also when the title is misspelled or only its beginning is known. Results are ranked by title similarity and labelled "same title", "title contains the query" or "similar title".

**Parameters:**

- `title` (string, required): Full or partial title of the publication (case-insensitive)
- `similarity_threshold` (number, required): A float between 0 and 1 where 1.0 means an exact match
- `max_results` (integer, optional): Maximum number of publications to return. Default is 10
- `year_from` (integer, optional): Lower bound for publication year
- `year_to` (integer, optional): Upper bound for publication year
- `venue_filter` (string, optional): Case-insensitive substring filter for publication venues
- `include_bibtex` (boolean, optional): Whether to include BibTeX entries in the results. Default is false

### get_author_publications

List an author's publications, newest first. The name is matched fuzzily. DBLP tells namesakes apart by a number ("Wei Wang 0010"); the answer names the DBLP person listed and the other persons with a matching name.

**Parameters:**

- `author_name` (string, required): Full author name, optionally with DBLP's number (case and accents do not matter)
- `similarity_threshold` (number, required): A float between 0 and 1 where 1.0 means an exact match
- `max_results` (integer, optional): Maximum number of publications to return. Default is 20
- `include_bibtex` (boolean, optional): Whether to include BibTeX entries in the results. Default is false
- `year_from` (integer, optional): Only publications from this year on
- `year_to` (integer, optional): Only publications up to this year

### get_venue_info

Look up a venue: its name, acronym, type (conference or journal), DBLP page, number of publications, and year range.

**Parameters:**

- `venue_name` (string, required): A conference acronym ('IJCAI'), DBLP's journal abbreviation ('J. ACM') or a journal's full name ('Journal of the ACM')

### add_bibtex_entry

Add a BibTeX entry to the collection for later export.

**Parameters:**

- `dblp_key` (string, required): The DBLP key from search results (e.g., "conf/nips/VaswaniSPUJGKP17"); `DBLP:KEY` and the `biburl` of a DBLP entry (`https://dblp.org/rec/KEY.bib`) work too
- `citation_key` (string, required): The citation key to use in the .bib file (e.g., "Vaswani2017")

**Behavior:**

- Renders the BibTeX entry for the key from the local index, in DBLP's format
- Replaces the citation key with your custom key
- Adds the entry to the session's collection (an entry with the same citation key is replaced)
- Returns success or an error, with the size of the collection

The server renders every entry from DBLP's data; the model chooses only the citation key and never edits the entry. A key missing from the local index is looked up on dblp.org, which currently blocks automated clients, so such a call returns an error and leaves the collection unchanged.

### export_bibtex

Export all collected BibTeX entries to a .bib file.

**Parameters:**

- `path` (string, required): Absolute path for the .bib file (e.g., "/path/to/refs.bib"); a leading `~` is expanded. Relative paths are rejected.

**Behavior:**

- Saves all entries added via `add_bibtex_entry` to the specified path
- The .bib extension is added automatically if missing
- Parent directories are created if needed
- Clears the collection after successful export
- Returns the full path to the saved file
- Returns an error if the collection is empty

### set_dblp_mirror

Choose which dblp.org host the web fallback uses: keys missing from the local index, or all requests with `MCP_DBLP_INDEX=http`. Searches with the local index never contact dblp.org, and dblp.org currently blocks automated clients, so this tool is rarely useful.

**Parameters:**

- `host` (string, required): `dblp.org`, `dblp.uni-trier.de`, or `dblp.dagstuhl.de`; other hosts are rejected

## Example

### Input text

> Our exploration focuses on two types of explanation problems, abductive and contrastive, in local and global contexts (Marques-Silva 2023). Abductive explanations (Ignatiev, Narodytska, and Marques-Silva 2019), corresponding to prime-implicant explanations (Shih, Choi, and Darwiche 2018) and sufficient reason explanations (Darwiche and Ji 2022), clarify specific decision-making instances, while contrastive explanations (Miller 2019; Ignatiev et al. 2020), corresponding to necessary reason explanations (Darwiche and Ji 2022), make explicit the reasons behind the non-selection of alternatives. Conversely, global explanations (Ribeiro, Singh, and Guestrin 2016; Ignatiev, Narodytska, and Marques-Silva 2019) aim to unravel models' decision patterns across various inputs.

### Output text

> Our exploration focuses on two types of explanation problems, abductive and contrastive, in local and global contexts \cite{MarquesSilvaI23}. Abductive explanations \cite{IgnatievNM19}, corresponding to prime-implicant explanations \cite{ShihCD18} and sufficient reason explanations \cite{DarwicheJ22}, clarify specific decision-making instances, while contrastive explanations \cite{Miller19,IgnatievNA020}, corresponding to necessary reason explanations \cite{DarwicheJ22}, make explicit the reasons behind the non-selection of alternatives. Conversely, global explanations \cite{Ribeiro0G16,IgnatievNM19} aim to unravel models' decision patterns across various inputs.

### Output BibTeX

`export_bibtex` answers `Exported 7 references to /path/to/refs.bib` and writes:

```bibtex
@article{MarquesSilvaI23,
  author       = {Jo{\~{a}}o Marques{-}Silva and
                  Alexey Ignatiev},
  title        = {No silver bullet: interpretable {ML} models must be explained},
  journal      = {Frontiers Artif. Intell.},
  volume       = {6},
  year         = {2023},
  url          = {https://doi.org/10.3389/frai.2023.1128212},
  doi          = {10.3389/FRAI.2023.1128212},
  timestamp    = {Tue, 07 May 2024 00:00:00 +0200},
  biburl       = {https://dblp.org/rec/journals/frai/MarquesSilvaI23.bib},
  bibsource    = {dblp computer science bibliography, https://dblp.org}
}

@inproceedings{IgnatievNM19,
  author       = {Alexey Ignatiev and
                  Nina Narodytska and
                  Jo{\~{a}}o Marques{-}Silva},
  title        = {Abduction-Based Explanations for Machine Learning Models},
  booktitle    = {The Thirty-Third {AAAI} Conference on Artificial Intelligence, {AAAI}
                  2019, The Thirty-First Innovative Applications of Artificial Intelligence
                  Conference, {IAAI} 2019, The Ninth {AAAI} Symposium on Educational
                  Advances in Artificial Intelligence, {EAAI} 2019, Honolulu, Hawaii,
                  USA, January 27 - February 1, 2019},
  pages        = {1511--1519},
  publisher    = {{AAAI} Press},
  year         = {2019},
  url          = {https://doi.org/10.1609/aaai.v33i01.33011511},
  doi          = {10.1609/AAAI.V33I01.33011511},
  timestamp    = {Mon, 04 Sep 2023 00:00:00 +0200},
  biburl       = {https://dblp.org/rec/conf/aaai/IgnatievNM19.bib},
  bibsource    = {dblp computer science bibliography, https://dblp.org}
}

@inproceedings{ShihCD18,
  author       = {Andy Shih and
                  Arthur Choi and
                  Adnan Darwiche},
  editor       = {J{\'{e}}r{\^{o}}me Lang},
  title        = {A Symbolic Approach to Explaining Bayesian Network Classifiers},
  booktitle    = {Proceedings of the Twenty-Seventh International Joint Conference on
                  Artificial Intelligence, {IJCAI} 2018, July 13-19, 2018, Stockholm,
                  Sweden},
  pages        = {5103--5111},
  publisher    = {ijcai.org},
  year         = {2018},
  url          = {https://doi.org/10.24963/ijcai.2018/708},
  doi          = {10.24963/IJCAI.2018/708},
  timestamp    = {Tue, 20 Aug 2019 00:00:00 +0200},
  biburl       = {https://dblp.org/rec/conf/ijcai/ShihCD18.bib},
  bibsource    = {dblp computer science bibliography, https://dblp.org}
}

@inproceedings{DarwicheJ22,
  author       = {Adnan Darwiche and
                  Chunxi Ji},
  title        = {On the Computation of Necessary and Sufficient Explanations},
  booktitle    = {Thirty-Sixth {AAAI} Conference on Artificial Intelligence, {AAAI}
                  2022, Thirty-Fourth Conference on Innovative Applications of Artificial
                  Intelligence, {IAAI} 2022, The Twelveth Symposium on Educational Advances
                  in Artificial Intelligence, {EAAI} 2022 Virtual Event, February 22
                  - March 1, 2022},
  pages        = {5582--5591},
  publisher    = {{AAAI} Press},
  year         = {2022},
  url          = {https://doi.org/10.1609/aaai.v36i5.20498},
  doi          = {10.1609/AAAI.V36I5.20498},
  timestamp    = {Wed, 18 Mar 2026 00:00:00 +0100},
  biburl       = {https://dblp.org/rec/conf/aaai/DarwicheJ22.bib},
  bibsource    = {dblp computer science bibliography, https://dblp.org}
}

@article{Miller19,
  author       = {Tim Miller},
  title        = {Explanation in artificial intelligence: Insights from the social sciences},
  journal      = {Artif. Intell.},
  volume       = {267},
  pages        = {1--38},
  year         = {2019},
  url          = {https://doi.org/10.1016/j.artint.2018.07.007},
  doi          = {10.1016/J.ARTINT.2018.07.007},
  timestamp    = {Sun, 07 Dec 2025 00:00:00 +0100},
  biburl       = {https://dblp.org/rec/journals/ai/Miller19.bib},
  bibsource    = {dblp computer science bibliography, https://dblp.org}
}

@inproceedings{IgnatievNA020,
  author       = {Alexey Ignatiev and
                  Nina Narodytska and
                  Nicholas Asher and
                  Jo{\~{a}}o Marques{-}Silva},
  editor       = {Matteo Baldoni and
                  Stefania Bandini},
  title        = {From Contrastive to Abductive Explanations and Back Again},
  booktitle    = {AIxIA 2020 - Advances in Artificial Intelligence - XIXth International
                  Conference of the Italian Association for Artificial Intelligence,
                  Virtual Event, November 25-27, 2020, Revised Selected Papers},
  series       = {Lecture Notes in Computer Science},
  volume       = {12414},
  pages        = {335--355},
  publisher    = {Springer},
  year         = {2020},
  url          = {https://doi.org/10.1007/978-3-030-77091-4\_21},
  doi          = {10.1007/978-3-030-77091-4\_21},
  timestamp    = {Tue, 15 Jun 2021 00:00:00 +0200},
  biburl       = {https://dblp.org/rec/conf/aiia/IgnatievNA020.bib},
  bibsource    = {dblp computer science bibliography, https://dblp.org}
}

@inproceedings{Ribeiro0G16,
  author       = {Marco T{\'{u}}lio Ribeiro and
                  Sameer Singh and
                  Carlos Guestrin},
  editor       = {Balaji Krishnapuram and
                  Mohak Shah and
                  Alexander J. Smola and
                  Charu C. Aggarwal and
                  Dou Shen and
                  Rajeev Rastogi},
  title        = {"Why Should {I} Trust You?": Explaining the Predictions of Any Classifier},
  booktitle    = {Proceedings of the 22nd {ACM} {SIGKDD} International Conference on
                  Knowledge Discovery and Data Mining, San Francisco, CA, USA, August
                  13-17, 2016},
  pages        = {1135--1144},
  publisher    = {{ACM}},
  year         = {2016},
  url          = {https://doi.org/10.1145/2939672.2939778},
  doi          = {10.1145/2939672.2939778},
  timestamp    = {Sun, 01 Feb 2026 00:00:00 +0100},
  biburl       = {https://dblp.org/rec/conf/kdd/Ribeiro0G16.bib},
  bibsource    = {dblp computer science bibliography, https://dblp.org}
}
```

## Disclaimer

MCP-DBLP is a research prototype. Use it at your own risk.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
