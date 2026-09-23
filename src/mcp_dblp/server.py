"""
MCP-DBLP Server Module

IMPORTANT: This file must define a 'main()' function that is imported by __init__.py!
Removing or renaming this function will break package imports and cause an error:
  ImportError: cannot import name 'main' from 'mcp_dblp.server'
"""

import asyncio
import logging
import os
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


# Longest tool answer in characters (about 15k tokens); longer lists are cut at a result
# boundary, since clients such as Claude Code reject oversized tool output outright.
MAX_OUTPUT_CHARS = 60_000


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
        """All DBLP tools with detailed descriptions."""
        return [
            types.Tool(
                name="search",
                description=(
                    "Search DBLP for publications using a boolean query string.\n"
                    "Arguments:\n"
                    "  - query (string, required): A query string that may include boolean operators 'and' and 'or' (case-insensitive).\n"
                    "    For example, 'Swin and Transformer'. Parentheses are not supported.\n"
                    "  - max_results (integer, optional): Maximum number of publications to return. Default is 10.\n"
                    "  - year_from (integer, optional): Lower bound for publication year.\n"
                    "  - year_to (integer, optional): Upper bound for publication year.\n"
                    "  - venue_filter (string, optional): Case-insensitive substring filter for publication venues (e.g., 'iclr').\n"
                    "  - include_bibtex (boolean, optional): Whether to include BibTeX entries in the results. Default is false.\n"
                    "Returns a list of publication objects including title, authors, venue, year, type, doi, ee, and url."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer"},
                        "year_from": {"type": "integer"},
                        "year_to": {"type": "integer"},
                        "venue_filter": {"type": "string"},
                        "include_bibtex": {"type": "boolean"},
                    },
                    "required": ["query"],
                },
            ),
            types.Tool(
                name="fuzzy_title_search",
                description=(
                    "Search DBLP for publications with fuzzy title matching.\n"
                    "Arguments:\n"
                    "  - title (string, required): Full or partial title of the publication (case-insensitive).\n"
                    "  - similarity_threshold (number, required): A float between 0 and 1 where 1.0 means an exact match.\n"
                    "  - max_results (integer, optional): Maximum number of publications to return. Default is 10.\n"
                    "  - year_from (integer, optional): Lower bound for publication year.\n"
                    "  - year_to (integer, optional): Upper bound for publication year.\n"
                    "  - venue_filter (string, optional): Case-insensitive substring filter for publication venues.\n"
                    "  - include_bibtex (boolean, optional): Whether to include BibTeX entries in the results. Default is false.\n"
                    "Returns a list of publication objects sorted by title similarity score."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "similarity_threshold": {"type": "number"},
                        "max_results": {"type": "integer"},
                        "year_from": {"type": "integer"},
                        "year_to": {"type": "integer"},
                        "venue_filter": {"type": "string"},
                        "include_bibtex": {"type": "boolean"},
                    },
                    "required": ["title", "similarity_threshold"],
                },
            ),
            types.Tool(
                name="get_author_publications",
                description=(
                    "Retrieve publication details for a specific author with fuzzy matching.\n"
                    "Arguments:\n"
                    "  - author_name (string, required): Full or partial author name (case-insensitive).\n"
                    "  - similarity_threshold (number, required): A float between 0 and 1 where 1.0 means an exact match.\n"
                    "  - max_results (integer, optional): Maximum number of publications to return. Default is 20.\n"
                    "  - include_bibtex (boolean, optional): Whether to include BibTeX entries in the results. Default is false.\n"
                    "  - year_from (integer, optional): Only publications from this year on.\n"
                    "  - year_to (integer, optional): Only publications up to this year.\n"
                    "Returns a dictionary with keys: name, publication_count, publications, and stats (which includes top venues, years, and types)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "author_name": {"type": "string"},
                        "similarity_threshold": {"type": "number"},
                        "max_results": {"type": "integer"},
                        "include_bibtex": {"type": "boolean"},
                        "year_from": {"type": "integer"},
                        "year_to": {"type": "integer"},
                    },
                    "required": ["author_name", "similarity_threshold"],
                },
            ),
            types.Tool(
                name="get_venue_info",
                description=(
                    "Retrieve information about a publication venue from DBLP.\n"
                    "Arguments:\n"
                    "  - venue_name (string, required): Venue name or abbreviation (e.g., 'ICLR', 'NeurIPS', or full name).\n"
                    "Returns a dictionary with fields:\n"
                    "  - venue: Full venue title\n"
                    "  - acronym: Venue acronym/abbreviation (if available)\n"
                    "  - type: Venue type (e.g., 'Conference or Workshop', 'Journal', 'Repository')\n"
                    "  - url: Canonical DBLP URL for the venue\n"
                    "Note: Publisher, ISSN, and other metadata are not available through this endpoint."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"venue_name": {"type": "string"}},
                    "required": ["venue_name"],
                },
            ),
            types.Tool(
                name="set_dblp_mirror",
                description=(
                    "Choose the dblp.org mirror for the web fallback. With the local DBLP index "
                    "(the default), searches never contact dblp.org, so this is rarely needed: it only "
                    "affects BibTeX keys missing from the local index, and the web backend "
                    "(MCP_DBLP_INDEX=http).\n"
                    "Available mirrors:\n"
                    "  - dblp.org (default)\n"
                    "  - dblp.uni-trier.de\n"
                    "  - dblp.dagstuhl.de\n"
                    "Other hosts are rejected.\n"
                    "Arguments:\n"
                    "  - host (string, required): Mirror hostname (e.g., 'dblp.uni-trier.de')."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"host": {"type": "string"}},
                    "required": ["host"],
                },
            ),
            types.Tool(
                name="add_bibtex_entry",
                description=(
                    "Add a BibTeX entry to the collection for later export. Call this once for each paper you want to export.\n"
                    "Arguments:\n"
                    "  - dblp_key (string, required): The DBLP key from search results (e.g., 'conf/nips/VaswaniSPUJGKP17').\n"
                    "  - citation_key (string, required): The citation key to use in the .bib file (e.g., 'Vaswani2017').\n"
                    "Workflow:\n"
                    "  1. Fetches BibTeX directly from DBLP using the provided key\n"
                    "  2. Replaces the citation key with your custom key\n"
                    "  3. Adds to collection (duplicate citation_key will be overwritten)\n"
                    "  4. Returns count of entries currently in collection\n"
                    "After adding all entries, call export_bibtex to save them to a .bib file."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "dblp_key": {"type": "string"},
                        "citation_key": {"type": "string"},
                    },
                    "required": ["dblp_key", "citation_key"],
                },
            ),
            types.Tool(
                name="export_bibtex",
                description=(
                    "Export all collected BibTeX entries to a .bib file. Call this after adding all entries with add_bibtex_entry.\n"
                    "Workflow:\n"
                    "  1. Saves all collected entries to a .bib file at the specified path\n"
                    "  2. Clears the collection for next export\n"
                    "  3. Returns the full path to the exported file\n"
                    "Returns error if no entries have been added yet."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Absolute path for the .bib file (e.g., '/path/to/refs.bib'). The .bib extension is added automatically if missing. Parent directories are created if needed.",
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
                    text=f"\n---\n## DBLP Usage Instructions\n\n{_instructions_text}",
                )
            )
        return result

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
            logger.info(f"Tool call: {name} with arguments {arguments}")
            match name:
                case "search":
                    include_bibtex = arguments.get("include_bibtex", False)
                    result = backend.search(
                        query=arguments.get("query"),
                        max_results=arguments.get("max_results", 10),
                        year_from=arguments.get("year_from"),
                        year_to=arguments.get("year_to"),
                        venue_filter=arguments.get("venue_filter"),
                        include_bibtex=include_bibtex,
                    )
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

                    if include_bibtex:
                        return [
                            types.TextContent(
                                type="text",
                                text=f"Found {pub_count} publications for author {arguments['author_name']}:\n\n{format_results_with_bibtex(publications)}",
                            )
                        ]
                    else:
                        return [
                            types.TextContent(
                                type="text",
                                text=f"Found {pub_count} publications for author {arguments['author_name']}:\n\n{format_results(publications)}",
                            )
                        ]
                case "get_venue_info":
                    result = backend.get_venue_info(venue_name=arguments.get("venue_name"))
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
