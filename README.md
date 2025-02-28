# MCP-DBLP

[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-green.svg)](https://modelcontextprotocol.io/) [![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT) [![Python Version](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)

A Model Context Protocol (MCP) server that provides access to the DBLP computer science bibliography database for Large Language Models.

------

## Overview

The MCP-DBLP integrates the DBLP (Digital Bibliography & Library Project) API with LLMs through the Model Context Protocol, enabling AI models to:

- Search and retrieve academic publications from the DBLP database
- Process citations and generate BibTeX entries
- Perform fuzzy matching on publication titles and author names
- Extract and format bibliographic information
- Process embedded references in documents

## Features

- Comprehensive search capabilities with boolean queries
- Fuzzy title and author name matching
- BibTeX entry retrieval directly from DBLP
- Publication filtering by year and venue
- Statistical analysis of publication data

## Available Tools

| Tool Name                 | Description                                        |
| ------------------------- | -------------------------------------------------- |
| `search`                  | Search DBLP for publications using boolean queries |
| `fuzzy_title_search`      | Search publications with fuzzy title matching      |
| `get_author_publications` | Retrieve publications for a specific author        |
| `get_venue_info`          | Get detailed information about a publication venue |
| `calculate_statistics`    | Generate statistics from publication results       |

------

## System Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

------

## Installation

1. Install an MCP-compatible client (e.g., [Claude Desktop app](https://claude.ai/download))

2. Install the MCP-DBLP:

   ```bash
   git clone https://github.com/username/mcp-dblp.git
   cd mcp-dblp
   pip install -e .
   ```

3. Create the configuration file:

   For macOS/Linux:

   ```bash
   ~/Library/Application/Support/Claude/claude_desktop_config.json
   ```

   For Windows:

   ```bash
   %APPDATA%\Claude\claude_desktop_config.json
   ```

   Add the following content:

   ```json
   {
     "mcpServers": {
       "mcp-dblp": {
         "command": "uv",
         "args": [
           "--directory",
           "/absolute/path/to/mcp-dblp/",
           "run",
           "mcp-dblp"
         ]
       }
     }
   }
   ```

   Windows: ```C:\\absolute\\path\\to\\mcp-dblp```

------

## Prompt

Incuded is an [instructions prompt](./instructions_prompt.md) which shoudl be issued togther with the text conatining citations. On Claude Desktop, the instructions prompt is available via the electrical plug icon. 

## Tool Details

### search

Search DBLP for publications using a boolean query string.

**Parameters:**

- `query` (string, required): A query string that may include boolean operators 'and' and 'or' (case-insensitive)
- `max_results` (number, optional): Maximum number of publications to return. Default is 10
- `year_from` (number, optional): Lower bound for publication year
- `year_to` (number, optional): Upper bound for publication year
- `venue_filter` (string, optional): Case-insensitive substring filter for publication venues (e.g., 'iclr')
- `include_bibtex` (boolean, optional): Whether to include BibTeX entries in the results. Default is false

### fuzzy_title_search

Search DBLP for publications with fuzzy title matching.

**Parameters:**

- `title` (string, required): Full or partial title of the publication (case-insensitive)
- `similarity_threshold` (number, required): A float between 0 and 1 where 1.0 means an exact match
- `max_results` (number, optional): Maximum number of publications to return. Default is 10
- `year_from` (number, optional): Lower bound for publication year
- `year_to` (number, optional): Upper bound for publication year
- `venue_filter` (string, optional): Case-insensitive substring filter for publication venues
- `include_bibtex` (boolean, optional): Whether to include BibTeX entries in the results. Default is false

### get_author_publications

Retrieve publication details for a specific author with fuzzy matching.

**Parameters:**

- `author_name` (string, required): Full or partial author name (case-insensitive)
- `similarity_threshold` (number, required): A float between 0 and 1 where 1.0 means an exact match
- `max_results` (number, optional): Maximum number of publications to return. Default is 20
- `include_bibtex` (boolean, optional): Whether to include BibTeX entries in the results. Default is false

### get_venue_info

Retrieve detailed information about a publication venue.

**Parameters:**

- `venue_name` (string, required): Venue name or abbreviation (e.g., 'ICLR' or full name)

### calculate_statistics

Calculate statistics from a list of publication results.

**Parameters:**

- `results` (array, required): An array of publication objects, each with at least 'title', 'authors', 'venue', and 'year'

------

## Example

### Input text:

> Our exploration focuses on two types of explanation problems, abductive and contrastive, in local and global contexts (Marques-Silva 2023). Abductive explanations (Ignatiev, Narodytska, and Marques-Silva 2019), corresponding to prime-implicant explanations (Shih, Choi, and Darwiche 2018) and sufficient reason explanations (Darwiche and Ji 2022), clarify specific decision-making instances, while contrastive explanations (Miller 2019; Ignatiev et al. 2020), corresponding to necessary reason explanations (Darwiche and Ji 2022), make explicit the reasons behind the non-selection of alternatives. Conversely, global explanations (Ribeiro, Singh, and Guestrin 2016; Ignatiev, Narodytska, and Marques-Silva 2019) aim to unravel models’ decision patterns across various inputs.

### Output text:

> Our exploration focuses on two types of explanation problems, abductive and contrastive, in local and global contexts \cite{MarquesSilvaI23}. Abductive explanations \cite{IgnatievNM19}, corresponding to prime-implicant explanations \cite{ShihCD18} and sufficient reason explanations \cite{DarwicheJ22}, clarify specific decision-making instances, while contrastive explanations \cite{Miller19}; \cite{IgnatievNA020}, corresponding to necessary reason explanations \cite{DarwicheJ22}, make explicit the reasons behind the non-selection of alternatives. Conversely, global explanations \cite{Ribeiro0G16}; \cite{IgnatievNM19} aim to unravel models' decision patterns across various inputs.
>
> `@article{MarquesSilvaI23,`
>   `author       = {Jo{\~{a}}o Marques{-}Silva and`
>                   `Alexey Ignatiev},`
>   `title        = {No silver bullet: interpretable {ML} models must be explained},`
>   `journal      = {Frontiers Artif. Intell.},`
>   `volume       = {6},`
>   `year         = {2023},`
>   `url          = {https://doi.org/10.3389/frai.2023.1128212},`
>   `doi          = {10.3389/FRAI.2023.1128212},`
>   `timestamp    = {Tue, 07 May 2024 20:23:47 +0200},`
>   `biburl       = {https://dblp.org/rec/journals/frai/MarquesSilvaI23.bib},`
>   `bibsource    = {dblp computer science bibliography, https://dblp.org}`
> `}`
>
> `@inproceedings{IgnatievNM19,`
>   `author       = {Alexey Ignatiev and`
>                   `Nina Narodytska and`
>                   `Jo{\~{a}}o Marques{-}Silva},`
>   `title        = {Abduction-Based Explanations for Machine Learning Models},`
>   `booktitle    = {The Thirty-Third {AAAI} Conference on Artificial Intelligence, {AAAI}`
>                   `2019, The Thirty-First Innovative Applications of Artificial Intelligence`
>                   `Conference, {IAAI} 2019, The Ninth {AAAI} Symposium on Educational`
>                   `Advances in Artificial Intelligence, {EAAI} 2019, Honolulu, Hawaii,`
>                   `USA, January 27 - February 1, 2019},`
>   `pages        = {1511--1519},`
>   `publisher    = {{AAAI} Press},`
>   `year         = {2019},`
>   `url          = {https://doi.org/10.1609/aaai.v33i01.33011511},`
>   `doi          = {10.1609/AAAI.V33I01.33011511},`
>   `timestamp    = {Mon, 04 Sep 2023 12:29:24 +0200},`
>   `biburl       = {https://dblp.org/rec/conf/aaai/IgnatievNM19.bib},`
>   `bibsource    = {dblp computer science bibliography, https://dblp.org}`
> `}`
>
> `@inproceedings{ShihCD18,`
>   `author       = {Andy Shih and`
>                   `Arthur Choi and`
>                   `Adnan Darwiche},`
>   `editor       = {J{\'{e}}r{\^{o}}me Lang},`
>   `title        = {A Symbolic Approach to Explaining Bayesian Network Classifiers},`
>   `booktitle    = {Proceedings of the Twenty-Seventh International Joint Conference on`
>                   `Artificial Intelligence, {IJCAI} 2018, July 13-19, 2018, Stockholm,`
>                   `Sweden},`
>   `pages        = {5103--5111},`
>   `publisher    = {ijcai.org},`
>   `year         = {2018},`
>   `url          = {https://doi.org/10.24963/ijcai.2018/708},`
>   `doi          = {10.24963/IJCAI.2018/708},`
>   `timestamp    = {Tue, 20 Aug 2019 16:19:08 +0200},`
>   `biburl       = {https://dblp.org/rec/conf/ijcai/ShihCD18.bib},`
>   `bibsource    = {dblp computer science bibliography, https://dblp.org}`
> `}`
>
> `@inproceedings{DarwicheJ22,`
>   `author       = {Adnan Darwiche and`
>                   `Chunxi Ji},`
>   `title        = {On the Computation of Necessary and Sufficient Explanations},`
>   `booktitle    = {Thirty-Sixth {AAAI} Conference on Artificial Intelligence, {AAAI}`
>                   `2022, Thirty-Fourth Conference on Innovative Applications of Artificial`
>                   `Intelligence, {IAAI} 2022, The Twelveth Symposium on Educational Advances`
>                   `in Artificial Intelligence, {EAAI} 2022 Virtual Event, February 22`
>                   `- March 1, 2022},`
>   `pages        = {5582--5591},`
>   `publisher    = {{AAAI} Press},`
>   `year         = {2022},`
>   `url          = {https://doi.org/10.1609/aaai.v36i5.20498},`
>   `doi          = {10.1609/AAAI.V36I5.20498},`
>   `timestamp    = {Mon, 04 Sep 2023 16:50:24 +0200},`
>   `biburl       = {https://dblp.org/rec/conf/aaai/DarwicheJ22.bib},`
>   `bibsource    = {dblp computer science bibliography, https://dblp.org}`
> `}`
>
> `@article{Miller19,`
>   `author       = {Tim Miller},`
>   `title        = {Explanation in artificial intelligence: Insights from the social sciences},`
>   `journal      = {Artif. Intell.},`
>   `volume       = {267},`
>   `pages        = {1--38},`
>   `year         = {2019},`
>   `url          = {https://doi.org/10.1016/j.artint.2018.07.007},`
>   `doi          = {10.1016/J.ARTINT.2018.07.007},`
>   `timestamp    = {Thu, 25 May 2023 12:52:41 +0200},`
>   `biburl       = {https://dblp.org/rec/journals/ai/Miller19.bib},`
>   `bibsource    = {dblp computer science bibliography, https://dblp.org}`
> `}`
>
> `@inproceedings{IgnatievNA020,`
>   `author       = {Alexey Ignatiev and`
>                   `Nina Narodytska and`
>                   `Nicholas Asher and`
>                   `Jo{\~{a}}o Marques{-}Silva},`
>   `editor       = {Matteo Baldoni and`
>                   `Stefania Bandini},`
>   `title        = {From Contrastive to Abductive Explanations and Back Again},`
>   `booktitle    = {AIxIA 2020 - Advances in Artificial Intelligence - XIXth International`
>                   `Conference of the Italian Association for Artificial Intelligence,`
>                   `Virtual Event, November 25-27, 2020, Revised Selected Papers},`
>   `series       = {Lecture Notes in Computer Science},`
>   `volume       = {12414},`
>   `pages        = {335--355},`
>   `publisher    = {Springer},`
>   `year         = {2020},`
>   `url          = {https://doi.org/10.1007/978-3-030-77091-4\_21},`
>   `doi          = {10.1007/978-3-030-77091-4\_21},`
>   `timestamp    = {Tue, 15 Jun 2021 17:23:54 +0200},`
>   `biburl       = {https://dblp.org/rec/conf/aiia/IgnatievNA020.bib},`
>   `bibsource    = {dblp computer science bibliography, https://dblp.org}`
> `}`
>
> `@inproceedings{Ribeiro0G16,`
>   `author       = {Marco T{\'{u}}lio Ribeiro and`
>                   `Sameer Singh and`
>                   `Carlos Guestrin},`
>   `editor       = {Balaji Krishnapuram and`
>                   `Mohak Shah and`
>                   `Alexander J. Smola and`
>                   `Charu C. Aggarwal and`
>                   `Dou Shen and`
>                   `Rajeev Rastogi},`
>   `title        = {"Why Should {I} Trust You?": Explaining the Predictions of Any Classifier},`
>   `booktitle    = {Proceedings of the 22nd {ACM} {SIGKDD} International Conference on`
>                   `Knowledge Discovery and Data Mining, San Francisco, CA, USA, August`
>                   `13-17, 2016},`
>   `pages        = {1135--1144},`
>   `publisher    = {{ACM}},`
>   `year         = {2016},`
>   `url          = {https://doi.org/10.1145/2939672.2939778},`
>   `doi          = {10.1145/2939672.2939778},`
>   `timestamp    = {Fri, 25 Dec 2020 01:14:16 +0100},`
>   `biburl       = {https://dblp.org/rec/conf/kdd/Ribeiro0G16.bib},`
>   `bibsource    = {dblp computer science bibliography, https://dblp.org}`
> `}`

------

## Disclaimer

This MCP-DBLP is in its prototype stage and should be used with caution. Users are encouraged to experiment, but any use in critical environments is at their own risk.

------

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

------