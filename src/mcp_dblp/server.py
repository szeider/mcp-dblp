"""
MCP-DBLP Server Module

IMPORTANT: This file must define a 'main()' function that is imported by __init__.py!
Removing or renaming this function will break package imports and cause an error:
  ImportError: cannot import name 'main' from 'mcp_dblp.server'
"""

import asyncio
import logging
import os
import re
import sys
import time
from importlib import resources

import mcp.server.stdio
import mcp.types as types

# Import MCP SDK
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

# Backend: local dump index or dblp.org web API (same functions and result dicts)
from mcp_dblp import local_index
from mcp_dblp.backend import IndexUnavailable, select_backend

# Set up logging: the log file lives in MCP_DBLP_HOME (default ~/.mcp-dblp); if it cannot
# be created there, log to stderr only instead of failing to start.
_log_handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
log_file = os.path.join(local_index.home_dir(), "mcp_dblp_server.log")
try:
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    _log_handlers.insert(0, logging.FileHandler(log_file))
except OSError:
    log_file = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=_log_handlers,
)
logger = logging.getLogger("mcp_dblp")


try:
    from importlib.metadata import version

    version_str = version("mcp-dblp")
    logger.info(f"Loaded version: {version_str}")
except Exception:
    version_str = "x.x.x"  # Anonymous fallback version
    logger.warning(f"Using default version: {version_str}")


def _first_start_wait() -> float:
    """Seconds a tool call waits for the first-start download (MCP_DBLP_FIRST_START_WAIT)."""
    try:
        return max(0.0, float(os.environ.get("MCP_DBLP_FIRST_START_WAIT", "40")))
    except ValueError:
        return 40.0


# Answers of _dispatch_tool that report a failure (returned with is_error=True)
_ERROR_PREFIXES = ("Error", "Unknown tool:", "Failed to add entry")


def _tool_result(content: list[types.TextContent], error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=content, is_error=error)


_JSON_TYPES = {"string": (str,), "number": (int, float), "integer": (int,), "boolean": (bool,)}


def _argument_error(schema: dict | None, arguments: dict) -> str | None:
    """Check arguments against a tool's (flat) input schema.

    The v1 SDK validated tool arguments against the input schema; the v2 low-level
    Server only advertises the schema, so the check happens here."""
    if not schema:
        return None
    for key in schema.get("required", []):
        if arguments.get(key) is None:  # missing or null
            return f"Input validation error: '{key}' is a required property"
    properties = schema.get("properties", {})
    for key, value in arguments.items():
        expected = properties.get(key, {}).get("type")
        allowed = _JSON_TYPES.get(expected)
        if allowed is None or value is None:
            continue
        if not isinstance(value, allowed) or (expected != "boolean" and isinstance(value, bool)):
            return (
                f"Input validation error: {value!r} is not of type '{expected}' (argument '{key}')"
            )
    return None


# Longest tool answer in characters; longer lists are cut at a result boundary, since
# clients such as Claude Code reject oversized tool output outright.  Result lists
# tokenize densely (keys, names), so 60,000 characters already exceeded Claude Code's
# 25,000-token limit in a test (2026-09-23).
MAX_OUTPUT_CHARS = 30_000

# Empty answers say what to try next
NO_SEARCH_RESULTS = (
    "Found 0 publications matching your query. All words must match: try fewer or other "
    "words, check the spelling of names, note that a field prefix covers only the next word, "
    "or use fuzzy_title_search if you know the title."
)
NO_TITLE_RESULTS = (
    "Found 0 publications with similar titles. Try a lower similarity_threshold (e.g. 0.5), "
    "fewer words, or search with the first author's surname and title words."
)
PARENS_ERROR = (
    "Error: parentheses are not supported, so they cannot group 'or' alternatives. Write each "
    "alternative in full, e.g. 'Szeider backdoor or Cook backdoor'."
)
NO_AUTHOR_HINT = (
    "Try the full name as DBLP writes it (accents do not matter), a lower "
    "similarity_threshold, or search with the surname and title words."
)


def _cap_output(result: list[types.TextContent]) -> list[types.TextContent]:
    """Truncate an oversized result list at a result boundary and say so."""
    if not result or len(result[0].text) <= MAX_OUTPUT_CHARS:
        return result
    text = result[0].text
    cut = text.rfind("\n\n", 0, MAX_OUTPUT_CHARS)
    cut = cut if cut > 0 else MAX_OUTPUT_CHARS
    shown = text[:cut].count("DBLP key: ")
    total = text.count("DBLP key: ")
    note = (
        f"\n\n[Output truncated: showing {shown} of {total} results to stay within the "
        "client's size limit. Use a smaller max_results or narrow the search "
        "(year_from/year_to, venue_filter).]"
    )
    return [types.TextContent(type="text", text=text[:cut] + note)] + result[1:]


def export_bibtex_entries(entries, path):
    """Export BibTeX entries to a file at the specified path."""
    # Ensure .bib extension
    if not path.endswith(".bib"):
        path = f"{path}.bib"

    # Create parent directories if needed
    parent_dir = os.path.dirname(path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(entry + "\n\n")

    return path


def create_server(backend) -> Server:
    """The MCP server with all tools; every tool handler goes through backend (local
    index or web API, see backend.py).  A backend that is not ready (index still
    downloading) raises IndexUnavailable; the caller then gets that message, and the
    usage instructions wait for the first call that actually answers."""

    # Session-scoped buffer for BibTeX entries
    # Key: citation_key, Value: full bibtex string
    bibtex_buffer: dict[str, str] = {}

    # First-call instruction delivery
    instructions_delivered = False
    try:
        _instructions_text = (
            resources.files("mcp_dblp")
            .joinpath("instructions_prompt.md")
            .read_text(encoding="utf-8")
        )
    except Exception:
        _instructions_text = ""

    def _tools() -> list[types.Tool]:
        """All DBLP tools. The descriptions are the only guidance a client is sure to show
        the model, so they explain usage, output and pitfalls, not just the arguments."""
        year_from = {"type": "integer", "description": "Only publications from this year on."}
        year_to = {"type": "integer", "description": "Only publications up to this year."}
        venue_filter = {
            "type": "string",
            "description": "Case-insensitive substring of the venue as DBLP writes it: a "
            "conference acronym ('AAAI') or a journal abbreviation ('J. ACM').",
        }
        include_bibtex = {
            "type": "boolean",
            "description": "Also show each result's BibTeX (default false). Not needed for "
            "add_bibtex_entry.",
        }
        return [
            types.Tool(
                name="search",
                description=(
                    "Search the publications in DBLP (a local copy of the monthly DBLP dump; papers "
                    "added to DBLP after it are missing). All query words must occur in the title, "
                    "the author names or the venue (venues as DBLP writes them: 'Nat.' for Nature, "
                    "'J. ACM'; leave venue words out if unsure); case and accents do not matter.\n"
                    "Syntax: 'or' between alternatives (no parentheses); \"quoted phrase\" (the words "
                    "adjacent and in this order, anywhere in the field); the field "
                    "prefixes author:, title:, venue: and year: apply to the next word or quoted "
                    "phrase only; a 4-digit year restricts the results to that year; a trailing * "
                    "matches word beginnings. Example: 'author:Vaswani title:attention 2017'.\n"
                    "For a citation, search for the first author's surname plus one or two "
                    "distinctive title words and the year. A surname with a year alone can return "
                    "only papers of a namesake ('Vaswani 2017' lists papers of Namrata Vaswani).\n"
                    "Each result shows title, authors, venue (year), the DBLP key to pass to "
                    "add_bibtex_entry, and a 'Matched:' line that says where each query word was "
                    "found: 'szeider = author 2 of 3' (family name of the second of three authors), "
                    "'given name of author 1 of 2' (only a first name: usually another person), "
                    "'title', 'venue', 'title word only' (a name-like word found only in the title), "
                    "'~ prefix of \"X\"' (only the beginning of a longer word). Results marked "
                    "'prefix match only' come last and are usually other papers. Accept a result "
                    "only if the cited authors match as authors and title and year fit. The same "
                    "paper can appear as arXiv preprint (venue CoRR), conference and journal version."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search words, syntax above."},
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of results (default 10). Very long "
                            "answers are cut off; narrow the query instead.",
                        },
                        "year_from": year_from,
                        "year_to": year_to,
                        "venue_filter": venue_filter,
                        "include_bibtex": include_bibtex,
                    },
                    "required": ["query"],
                },
            ),
            types.Tool(
                name="fuzzy_title_search",
                description=(
                    "Find publications by title: also when the title is misspelled, abbreviated or "
                    "only its beginning is known. Results are ranked by title similarity (1.0 = "
                    "identical) and show [Similarity], authors, venue (year) and the DBLP key. The "
                    "same title often appears several times (arXiv preprint in CoRR, conference and "
                    "journal version, reprints): choose by venue and year."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "The title or its beginning; case does not matter.",
                        },
                        "similarity_threshold": {
                            "type": "number",
                            "description": "Minimum similarity from 0 to 1: 0.7 is a good "
                            "default, 0.9 for a nearly exact title, 0.5 for a garbled one.",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of results (default 10).",
                        },
                        "year_from": year_from,
                        "year_to": year_to,
                        "venue_filter": venue_filter,
                        "include_bibtex": include_bibtex,
                    },
                    "required": ["title", "similarity_threshold"],
                },
            ),
            types.Tool(
                name="get_author_publications",
                description=(
                    "List one person's publications in DBLP, newest first. DBLP tells people with "
                    "the same name apart by a 4-digit number ('Wei Wang 0010'). The answer starts "
                    "with the DBLP person listed and the other DBLP persons with a matching name; "
                    "pass such a name with its number as author_name to list that person. The name "
                    "without a number collects papers DBLP has not assigned to a numbered person, "
                    "so for a common name it mixes several people. To find one paper by an author "
                    "with a common name, search with title words is usually faster."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "author_name": {
                            "type": "string",
                            "description": "Full name ('Stefan Szeider'), optionally with DBLP's "
                            "number ('Wei Wang 0010'). Case, accents and small typos do not matter.",
                        },
                        "similarity_threshold": {
                            "type": "number",
                            "description": "Minimum name similarity from 0 to 1; 0.8 is a good "
                            "default.",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of publications (default 20).",
                        },
                        "include_bibtex": include_bibtex,
                        "year_from": year_from,
                        "year_to": year_to,
                    },
                    "required": ["author_name", "similarity_threshold"],
                },
            ),
            types.Tool(
                name="get_venue_info",
                description=(
                    "Look up a journal or conference series in DBLP: its full name, acronym, type, "
                    "DBLP page, number of publications and years. Accepts a conference acronym "
                    "('IJCAI', 'NeurIPS'), DBLP's journal abbreviation ('J. ACM', 'Theor. Comput. "
                    "Sci.') or a journal's full name ('Journal of the ACM'). Useful to choose a "
                    "venue_filter or to write a venue name out in full."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "venue_name": {
                            "type": "string",
                            "description": "Acronym, DBLP abbreviation or full name.",
                        }
                    },
                    "required": ["venue_name"],
                },
            ),
            types.Tool(
                name="set_dblp_mirror",
                description=(
                    "Rarely needed. Chooses the dblp.org host for the web fallback, which is tried "
                    "only for BibTeX keys missing from the local copy of DBLP (and for all requests "
                    "if the server runs with MCP_DBLP_INDEX=http); dblp.org currently blocks such "
                    "automated requests. Searches never contact dblp.org.\n"
                    "Available mirrors: dblp.org (default), dblp.uni-trier.de, dblp.dagstuhl.de. "
                    "Other hosts are rejected."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "host": {
                            "type": "string",
                            "description": "Mirror hostname, e.g. 'dblp.uni-trier.de'.",
                        }
                    },
                    "required": ["host"],
                },
            ),
            types.Tool(
                name="add_bibtex_entry",
                description=(
                    "Add one publication to the session's collection of BibTeX entries, under your "
                    "citation key. The entry is DBLP's own BibTeX for the record (the format of the "
                    "dblp.org .bib export); only its citation key is replaced. Returns the number "
                    "of entries in the collection, or an error: 'not found in the local index' "
                    "means that the key is mistyped or that the record is newer than the local "
                    "copy of DBLP. Reusing a citation key replaces the earlier entry ('replaced "
                    "existing entry'). Calls can run in parallel; call export_bibtex at the end."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "dblp_key": {
                            "type": "string",
                            "description": "The DBLP key from a result, e.g. "
                            "'conf/nips/VaswaniSPUJGKP17'. Also accepted: "
                            "'DBLP:conf/nips/VaswaniSPUJGKP17' and "
                            "'https://dblp.org/rec/conf/nips/VaswaniSPUJGKP17.bib' (the biburl "
                            "field of a DBLP BibTeX entry).",
                        },
                        "citation_key": {
                            "type": "string",
                            "description": "Key for the .bib file, e.g. 'Vaswani2017'; unique "
                            "within the collection.",
                        },
                    },
                    "required": ["dblp_key", "citation_key"],
                },
            ),
            types.Tool(
                name="export_bibtex",
                description=(
                    "Write all collected BibTeX entries to a .bib file and empty the collection. "
                    "Returns the number of entries and the file path. Entries for papers that are "
                    "not in DBLP have to be added to the file afterwards."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Absolute path for the .bib file (e.g., '/path/to/refs.bib'); a leading ~ is expanded, relative paths are rejected. The .bib extension is added automatically if missing. Parent directories are created if needed.",
                        },
                    },
                    "required": ["path"],
                },
            ),
        ]

    tools = _tools()
    schemas = {tool.name: tool.input_schema for tool in tools}

    async def handle_list_tools(ctx, params) -> types.ListToolsResult:
        """List all available DBLP tools with detailed descriptions."""
        return types.ListToolsResult(tools=tools)

    def _maybe_append_instructions(result: list[types.TextContent]) -> list[types.TextContent]:
        """Append usage instructions to the first tool call response in a session."""
        nonlocal instructions_delivered
        if not instructions_delivered and _instructions_text:
            instructions_delivered = True
            result.append(
                types.TextContent(
                    type="text",
                    text=f"\n---\n## DBLP Usage Instructions\n\n{_coverage_note()}{_instructions_text}",
                )
            )
        return result

    def _release_note() -> str:
        """A sentence for empty answers: how current the local copy is."""
        release = getattr(backend, "release", "")
        if not release:
            return ""
        return f" The local copy of DBLP is the dump of {release}; newer papers are missing."

    def _coverage_note() -> str:
        """Which DBLP release the answers come from ('' for the web backend)."""
        release = getattr(backend, "release", "")
        if not release:
            return ""
        return (
            f"**This server answers from the DBLP dump of {release}.** Papers that DBLP added "
            "after that date are not found.\n\n"
        )

    async def handle_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        """Handle tool calls from clients.

        While the first-start download is running, a call waits up to
        MCP_DBLP_FIRST_START_WAIT seconds (default 40, below common client timeouts)
        for the index, so that a model without a sleep tool does not poll in a tight
        loop."""
        name = params.name
        # a null optional argument means "not given": drop it so the default applies
        arguments = {k: v for k, v in (params.arguments or {}).items() if v is not None}
        problem = _argument_error(schemas.get(name), arguments)
        if problem:
            logger.warning(f"Tool call {name}: {problem}")
            return _tool_result([types.TextContent(type="text", text=problem)], error=True)
        logger.info(f"Tool call: {name} with arguments {arguments}")  # once, not per retry
        wait = _first_start_wait()
        deadline = time.monotonic() + wait
        while True:
            try:
                result = _cap_output(_dispatch_tool(name, arguments))
                break
            except IndexUnavailable as e:
                in_progress = getattr(backend, "fetch_in_progress", lambda: False)()
                if in_progress and time.monotonic() < deadline:
                    await asyncio.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
                    continue
                # no answer yet (index downloading): keep the usage instructions for the
                # first call that actually answers
                message = str(e)
                if in_progress and wait > 0:
                    message += (
                        f" This call waited {wait:.0f} s for it; the download continues in "
                        "the background, so calling a tool again keeps waiting."
                    )
                logger.warning(f"Tool call {name}: {message}")
                return _tool_result([types.TextContent(type="text", text=message)])
        failed = bool(result) and result[0].text.startswith(_ERROR_PREFIXES)
        return _tool_result(_maybe_append_instructions(result), error=failed)

    def _dispatch_tool(name: str, arguments: dict) -> list[types.TextContent]:
        """Dispatch a tool call and return the result."""
        try:
            match name:
                case "search":
                    query = arguments.get("query") or ""
                    if not query.strip():
                        return [
                            types.TextContent(
                                type="text",
                                text="Error: the query is empty; give words to search for.",
                            )
                        ]
                    # without grouping, '(A or B) C' would silently mean 'A' or 'B C'
                    unquoted = re.sub(r'"[^"]*"', "", query)
                    if ("(" in unquoted or ")" in unquoted) and re.search(
                        r"\sor\s", unquoted, re.I
                    ):
                        return [types.TextContent(type="text", text=PARENS_ERROR)]
                    include_bibtex = arguments.get("include_bibtex", False)
                    result = backend.search(
                        query=arguments.get("query"),
                        max_results=arguments.get("max_results", 10),
                        year_from=arguments.get("year_from"),
                        year_to=arguments.get("year_to"),
                        venue_filter=arguments.get("venue_filter"),
                        include_bibtex=include_bibtex,
                    )
                    if not result:
                        return [
                            types.TextContent(type="text", text=NO_SEARCH_RESULTS + _release_note())
                        ]
                    if include_bibtex:
                        return [
                            types.TextContent(
                                type="text",
                                text=f"Found {len(result)} publications matching your query:\n\n{format_results_with_bibtex(result)}",
                            )
                        ]
                    else:
                        return [
                            types.TextContent(
                                type="text",
                                text=f"Found {len(result)} publications matching your query:\n\n{format_results(result)}",
                            )
                        ]
                case "fuzzy_title_search":
                    include_bibtex = arguments.get("include_bibtex", False)
                    result = backend.fuzzy_title_search(
                        title=arguments.get("title"),
                        similarity_threshold=arguments.get("similarity_threshold"),
                        max_results=arguments.get("max_results", 10),
                        year_from=arguments.get("year_from"),
                        year_to=arguments.get("year_to"),
                        venue_filter=arguments.get("venue_filter"),
                        include_bibtex=include_bibtex,
                    )
                    if not result:
                        return [
                            types.TextContent(type="text", text=NO_TITLE_RESULTS + _release_note())
                        ]
                    if include_bibtex:
                        return [
                            types.TextContent(
                                type="text",
                                text=f"Found {len(result)} publications with similar titles:\n\n{format_results_with_similarity_and_bibtex(result)}",
                            )
                        ]
                    else:
                        return [
                            types.TextContent(
                                type="text",
                                text=f"Found {len(result)} publications with similar titles:\n\n{format_results_with_similarity(result)}",
                            )
                        ]
                case "get_author_publications":
                    include_bibtex = arguments.get("include_bibtex", False)
                    result = backend.get_author_publications(
                        author_name=arguments.get("author_name"),
                        similarity_threshold=arguments.get("similarity_threshold"),
                        max_results=arguments.get("max_results", 20),
                        include_bibtex=include_bibtex,
                        year_from=arguments.get("year_from"),
                        year_to=arguments.get("year_to"),
                    )
                    pub_count = result.get("publication_count", 0)
                    publications = result.get("publications", [])
                    header = format_author_header(result)
                    if not publications and not result.get("total_publications"):
                        return [
                            types.TextContent(
                                type="text",
                                text=f"No DBLP person matches '{arguments['author_name']}'. "
                                + NO_AUTHOR_HINT,
                            )
                        ]

                    if include_bibtex:
                        return [
                            types.TextContent(
                                type="text",
                                text=f"Found {pub_count} publications for author {arguments['author_name']}:\n{header}\n{format_results_with_bibtex(publications)}",
                            )
                        ]
                    else:
                        return [
                            types.TextContent(
                                type="text",
                                text=f"Found {pub_count} publications for author {arguments['author_name']}:\n{header}\n{format_results(publications)}",
                            )
                        ]
                case "get_venue_info":
                    result = backend.get_venue_info(venue_name=arguments.get("venue_name"))
                    if not result.get("url"):
                        return [
                            types.TextContent(
                                type="text",
                                text=f"No DBLP venue found for '{arguments['venue_name']}'. "
                                "DBLP names journals by their abbreviation (e.g. 'J. ACM', "
                                "'Theor. Comput. Sci.') and conferences by their acronym "
                                "(e.g. 'IJCAI'); a journal's full name works when it expands "
                                "such an abbreviation.",
                            )
                        ]
                    return [
                        types.TextContent(
                            type="text",
                            text=f"Venue information for {arguments['venue_name']}:\n\n{format_dict(result)}",
                        )
                    ]
                case "set_dblp_mirror":
                    host = arguments.get("host")
                    if not host:
                        return [
                            types.TextContent(
                                type="text",
                                text="Error: Missing required parameter 'host'",
                            )
                        ]
                    try:
                        message = backend.set_mirror(host)
                    except ValueError as e:
                        message = f"Error: {e}"
                    return [types.TextContent(type="text", text=message)]

                case "add_bibtex_entry":
                    dblp_key = arguments.get("dblp_key")
                    citation_key = arguments.get("citation_key")

                    # Validate inputs
                    if not dblp_key or not citation_key:
                        return [
                            types.TextContent(
                                type="text",
                                text="Error: Missing required parameter 'dblp_key' or 'citation_key'",
                            )
                        ]

                    # Fetch BibTeX (key sanitized by the backend: .bib extension, URL prefix)
                    bibtex = backend.bibtex_for_citation(dblp_key, citation_key)

                    # Check for fetch errors (function returns strings starting with % Error)
                    if bibtex.strip().startswith("% Error"):
                        return [
                            types.TextContent(
                                type="text",
                                text=f"Failed to add entry: {bibtex.strip()}\nCollection still contains {len(bibtex_buffer)} entries.",
                            )
                        ]

                    # Check if we're overwriting an existing key
                    was_overwritten = citation_key in bibtex_buffer

                    # Add to buffer (overwrite if key exists)
                    bibtex_buffer[citation_key] = bibtex

                    if was_overwritten:
                        return [
                            types.TextContent(
                                type="text",
                                text=f"Successfully added '{citation_key}' (replaced existing entry). Collection contains {len(bibtex_buffer)} entries.",
                            )
                        ]
                    else:
                        return [
                            types.TextContent(
                                type="text",
                                text=f"Successfully added '{citation_key}'. Collection contains {len(bibtex_buffer)} entries.",
                            )
                        ]

                case "export_bibtex":
                    if not bibtex_buffer:
                        return [
                            types.TextContent(
                                type="text",
                                text="Error: Collection is empty. Add entries using add_bibtex_entry first.",
                            )
                        ]

                    path = arguments.get("path")
                    if not path:
                        return [
                            types.TextContent(
                                type="text",
                                text="Error: Missing required parameter 'path'",
                            )
                        ]

                    path = os.path.expanduser(path)
                    if not os.path.isabs(path):
                        return [
                            types.TextContent(
                                type="text",
                                text=f"Error: 'path' must be an absolute path (got '{path}'). "
                                "The collection is unchanged.",
                            )
                        ]

                    # Convert dict values to list for writing
                    entries = list(bibtex_buffer.values())
                    filepath = export_bibtex_entries(entries, path)

                    count = len(bibtex_buffer)
                    bibtex_buffer.clear()  # Clear after export

                    return [
                        types.TextContent(
                            type="text", text=f"Exported {count} references to {filepath}"
                        )
                    ]
                case _:
                    return [types.TextContent(type="text", text=f"Unknown tool: {name}")]
        except IndexUnavailable:
            raise
        except Exception as e:
            logger.error(f"Tool execution failed: {str(e)}", exc_info=True)
            return [types.TextContent(type="text", text=f"Error executing {name}: {str(e)}")]

    return Server(
        "mcp-dblp",
        version=version_str,
        on_list_tools=handle_list_tools,
        on_call_tool=handle_call_tool,
    )


async def serve() -> None:
    """Main server function to handle MCP requests"""
    # Without an index this starts the download in a background thread; tool calls then
    # return its progress (IndexUnavailable) instead of blocking.
    server = create_server(select_backend(auto_fetch=True))
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="mcp-dblp",
                server_version=version_str,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


def format_author_header(result: dict) -> str:
    """Which dblp person get_author_publications chose, and the other persons with a
    matching name ('' for the web backend, which does not report them)."""
    name = result.get("dblp_name")
    if not name:
        return ""
    lines = [f"DBLP person: {name} ({result.get('total_publications', 0)} publications in total)"]
    shown = len(result.get("publications") or [])
    matching = result.get("matching_publications") or 0
    if shown < matching:
        lines.append(
            f"Showing the newest {shown} of {matching}; raise max_results or narrow "
            "year_from/year_to to see more."
        )
    others = result.get("other_candidates") or []
    if others:
        listed = ", ".join(f"{n} ({count})" for n, _, count in others)
        lines.append(
            f"Other DBLP persons with a matching name: {listed}. Pass one of these names "
            "as author_name to list that person."
        )
        if not re.search(r" \d{4}$", name) and any(
            re.sub(r" \d{4}$", "", n) == name for n, _, _ in others
        ):
            lines.append(
                "DBLP keeps papers it has not assigned to one of the numbered persons under "
                "the name without a number, so this list can mix several people. For a "
                "common name, a search with title words is usually faster."
            )
    return "\n".join(lines) + "\n"


def format_results(results):
    if not results:
        return "No results found."
    formatted = []
    for i, result in enumerate(results):
        title = result.get("title", "Untitled")
        authors = ", ".join(result.get("authors", []))
        venue = result.get("venue", "Unknown venue")
        year = result.get("year", "")
        dblp_key = result.get("dblp_key", "")
        formatted.append(f"{i + 1}. {title}")
        formatted.append(f"   Authors: {authors}")
        formatted.append(f"   Venue: {venue} ({year})")
        if dblp_key:
            formatted.append(f"   DBLP key: {dblp_key}")
        if result.get("match"):
            formatted.append(f"   Matched: {result['match']}")
        formatted.append("")
    return "\n".join(formatted)


def format_results_with_similarity(results):
    if not results:
        return "No results found."
    formatted = []
    for i, result in enumerate(results):
        title = result.get("title", "Untitled")
        authors = ", ".join(result.get("authors", []))
        venue = result.get("venue", "Unknown venue")
        year = result.get("year", "")
        similarity = result.get("similarity", 0.0)
        dblp_key = result.get("dblp_key", "")
        formatted.append(f"{i + 1}. {title} [Similarity: {similarity:.2f}]")
        formatted.append(f"   Authors: {authors}")
        formatted.append(f"   Venue: {venue} ({year})")
        if dblp_key:
            formatted.append(f"   DBLP key: {dblp_key}")
        formatted.append("")
    return "\n".join(formatted)


def format_results_with_bibtex(results):
    if not results:
        return "No results found."
    formatted = []
    for i, result in enumerate(results):
        title = result.get("title", "Untitled")
        authors = ", ".join(result.get("authors", []))
        venue = result.get("venue", "Unknown venue")
        year = result.get("year", "")
        dblp_key = result.get("dblp_key", "")
        formatted.append(f"{i + 1}. {title}")
        formatted.append(f"   Authors: {authors}")
        formatted.append(f"   Venue: {venue} ({year})")
        if dblp_key:
            formatted.append(f"   DBLP key: {dblp_key}")
        if result.get("match"):
            formatted.append(f"   Matched: {result['match']}")
        if "bibtex" in result and result["bibtex"]:
            formatted.append("\n   BibTeX:")
            bibtex_lines = result["bibtex"].strip().split("\n")
            formatted.append("      " + "\n      ".join(bibtex_lines))
        formatted.append("")
    return "\n".join(formatted)


def format_results_with_similarity_and_bibtex(results):
    if not results:
        return "No results found."
    formatted = []
    for i, result in enumerate(results):
        title = result.get("title", "Untitled")
        authors = ", ".join(result.get("authors", []))
        venue = result.get("venue", "Unknown venue")
        year = result.get("year", "")
        similarity = result.get("similarity", 0.0)
        dblp_key = result.get("dblp_key", "")
        formatted.append(f"{i + 1}. {title} [Similarity: {similarity:.2f}]")
        formatted.append(f"   Authors: {authors}")
        formatted.append(f"   Venue: {venue} ({year})")
        if dblp_key:
            formatted.append(f"   DBLP key: {dblp_key}")
        if "bibtex" in result and result["bibtex"]:
            formatted.append("\n   BibTeX:")
            bibtex_lines = result["bibtex"].strip().split("\n")
            formatted.append("      " + "\n      ".join(bibtex_lines))
        formatted.append("")
    return "\n".join(formatted)


def format_dict(data):
    formatted = []
    for key, value in data.items():
        formatted.append(f"{key}: {value}")
    return "\n".join(formatted)


def main() -> int:
    logger.info(f"Starting MCP-DBLP server with version: {version_str}")
    try:
        asyncio.run(serve())
        return 0
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
        return 0
    except Exception as e:
        logger.error(f"Server error: {str(e)}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
