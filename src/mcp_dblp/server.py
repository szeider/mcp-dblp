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
from pathlib import Path

import mcp.server.stdio
import mcp.types as types

# Import MCP SDK
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

# Import DBLP client functions
from mcp_dblp.dblp_client import (
    calculate_statistics,
    fetch_and_process_bibtex,
    fuzzy_title_search,
    get_author_publications,
    get_venue_info,
    search,
)

# Set up logging
log_dir = os.path.expanduser("~/.mcp-dblp")
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, "mcp_dblp_server.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("mcp_dblp")


try:
    from importlib.metadata import version

    version_str = version("mcp-dblp")
    logger.info(f"Loaded version: {version_str}")
except Exception:
    version_str = "x.x.x"  # Anonymous fallback version
    logger.warning(f"Using default version: {version_str}")


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


async def serve() -> None:
    """Main server function to handle MCP requests"""

    server = Server("mcp-dblp")

    # Session-scoped buffer for BibTeX entries
    # Key: citation_key, Value: full bibtex string
    bibtex_buffer: dict[str, str] = {}


    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        """List all available DBLP tools with detailed descriptions."""
        return [
            types.Tool(
                name="get_instructions",
                description=(
                    "Get detailed DBLP usage instructions. Key points:\n"
                    "- Batch searches in parallel (5-10 at a time) for efficiency\n"
                    "- Add entries immediately after each search result (don't batch add_bibtex_entry calls)\n"
                    "- Use author+year for best results: search('Vaswani 2017') not just title\n"
                    "- Copy dblp_key EXACTLY from search results to add_bibtex_entry\n"
                    "- Export once at the end with export_bibtex\n"
                    "Call this tool for complete workflow details, search strategies, and examples."
                ),
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="search",
                description=(
                    "Search DBLP for publications using a boolean query string.\n"
                    "Arguments:\n"
                    "  - query (string, required): A query string that may include boolean operators 'and' and 'or' (case-insensitive).\n"
                    "    For example, 'Swin and Transformer'. Parentheses are not supported.\n"
                    "  - max_results (number, optional): Maximum number of publications to return. Default is 10.\n"
                    "  - year_from (number, optional): Lower bound for publication year.\n"
                    "  - year_to (number, optional): Upper bound for publication year.\n"
                    "  - venue_filter (string, optional): Case-insensitive substring filter for publication venues (e.g., 'iclr').\n"
                    "  - include_bibtex (boolean, optional): Whether to include BibTeX entries in the results. Default is false.\n"
                    "Returns a list of publication objects including title, authors, venue, year, type, doi, ee, and url."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": "number"},
                        "year_from": {"type": "number"},
                        "year_to": {"type": "number"},
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
                    "  - max_results (number, optional): Maximum number of publications to return. Default is 10.\n"
                    "  - year_from (number, optional): Lower bound for publication year.\n"
                    "  - year_to (number, optional): Upper bound for publication year.\n"
                    "  - venue_filter (string, optional): Case-insensitive substring filter for publication venues.\n"
                    "  - include_bibtex (boolean, optional): Whether to include BibTeX entries in the results. Default is false.\n"
                    "Returns a list of publication objects sorted by title similarity score."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "similarity_threshold": {"type": "number"},
                        "max_results": {"type": "number"},
                        "year_from": {"type": "number"},
                        "year_to": {"type": "number"},
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
                    "  - max_results (number, optional): Maximum number of publications to return. Default is 20.\n"
                    "  - include_bibtex (boolean, optional): Whether to include BibTeX entries in the results. Default is false.\n"
                    "Returns a dictionary with keys: name, publication_count, publications, and stats (which includes top venues, years, and types)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "author_name": {"type": "string"},
                        "similarity_threshold": {"type": "number"},
                        "max_results": {"type": "number"},
                        "include_bibtex": {"type": "boolean"},
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
                name="calculate_statistics",
                description=(
                    "Calculate statistics from a list of publication results.\n"
                    "Arguments:\n"
                    "  - results (array, required): An array of publication objects, each with at least 'title', 'authors', 'venue', and 'year'.\n"
                    "Returns a dictionary with:\n"
                    "  - total_publications: Total count.\n"
                    "  - time_range: Dictionary with 'min' and 'max' publication years.\n"
                    "  - top_authors: List of tuples (author, count) sorted by count.\n"
                    "  - top_venues: List of tuples (venue, count) sorted by count (empty venue is treated as '(empty)')."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"results": {"type": "array"}},
                    "required": ["results"],
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

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        """Handle tool calls from clients"""
        try:
            logger.info(f"Tool call: {name} with arguments {arguments}")
            match name:
                case "get_instructions":
                    try:
                        instructions_path = (
                            Path(__file__).resolve().parents[2] / "instructions_prompt.md"
                        )
                        with open(instructions_path, encoding="utf-8") as f:
                            instructions = f.read()
                        return [types.TextContent(type="text", text=instructions)]
                    except Exception as e:
                        return [
                            types.TextContent(
                                type="text", text=f"Error loading instructions: {e}"
                            )
                        ]
                case "search":
                    if "query" not in arguments:
                        return [
                            types.TextContent(
                                type="text", text="Error: Missing required parameter 'query'"
                            )
                        ]
                    include_bibtex = arguments.get("include_bibtex", False)
                    result = search(
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
                    if "title" not in arguments or "similarity_threshold" not in arguments:
                        return [
                            types.TextContent(
                                type="text",
                                text="Error: Missing required parameter 'title' or 'similarity_threshold'",
                            )
                        ]
                    include_bibtex = arguments.get("include_bibtex", False)
                    result = fuzzy_title_search(
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
                    if "author_name" not in arguments or "similarity_threshold" not in arguments:
                        return [
                            types.TextContent(
                                type="text",
                                text="Error: Missing required parameter 'author_name' or 'similarity_threshold'",
                            )
                        ]
                    include_bibtex = arguments.get("include_bibtex", False)
                    result = get_author_publications(
                        author_name=arguments.get("author_name"),
                        similarity_threshold=arguments.get("similarity_threshold"),
                        max_results=arguments.get("max_results", 20),
                        include_bibtex=include_bibtex,
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
                    if "venue_name" not in arguments:
                        return [
                            types.TextContent(
                                type="text", text="Error: Missing required parameter 'venue_name'"
                            )
                        ]
                    result = get_venue_info(venue_name=arguments.get("venue_name"))
                    return [
                        types.TextContent(
                            type="text",
                            text=f"Venue information for {arguments['venue_name']}:\n\n{format_dict(result)}",
                        )
                    ]
                case "calculate_statistics":
                    if "results" not in arguments:
                        return [
                            types.TextContent(
                                type="text", text="Error: Missing required parameter 'results'"
                            )
                        ]
                    result = calculate_statistics(results=arguments.get("results"))
                    return [
                        types.TextContent(
                            type="text", text=f"Statistics calculated:\n\n{format_dict(result)}"
                        )
                    ]
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

                    # Sanitize dblp_key: remove .bib extension and URL prefix if present
                    dblp_key = dblp_key.strip()
                    if dblp_key.endswith(".bib"):
                        dblp_key = dblp_key[:-4]
                    if "dblp.org/rec/" in dblp_key:
                        dblp_key = dblp_key.split("dblp.org/rec/")[-1]

                    # Construct DBLP BibTeX URL
                    url = f"https://dblp.org/rec/{dblp_key}.bib"

                    # Fetch BibTeX
                    bibtex = fetch_and_process_bibtex(url, citation_key)

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
        except Exception as e:
            logger.error(f"Tool execution failed: {str(e)}", exc_info=True)
            return [types.TextContent(type="text", text=f"Error executing {name}: {str(e)}")]

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
        formatted.append(f"{i + 1}. {title}")
        formatted.append(f"   Authors: {authors}")
        formatted.append(f"   Venue: {venue} ({year})")
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
        formatted.append(f"{i + 1}. {title} [Similarity: {similarity:.2f}]")
        formatted.append(f"   Authors: {authors}")
        formatted.append(f"   Venue: {venue} ({year})")
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
        formatted.append(f"{i + 1}. {title}")
        formatted.append(f"   Authors: {authors}")
        formatted.append(f"   Venue: {venue} ({year})")
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
        formatted.append(f"{i + 1}. {title} [Similarity: {similarity:.2f}]")
        formatted.append(f"   Authors: {authors}")
        formatted.append(f"   Venue: {venue} ({year})")
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
