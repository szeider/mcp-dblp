"""
MCP-DBLP Server Module

IMPORTANT: This file must define a 'main()' function that is imported by __init__.py!
Removing or renaming this function will break package imports and cause an error:
  ImportError: cannot import name 'main' from 'mcp_dblp.server'
"""



import sys
import asyncio
import logging
from typing import List, Dict, Any, Optional
import os
from pathlib import Path
import re
import datetime
import requests
import argparse

# Import MCP SDK
from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
import mcp.server.stdio
import mcp.types as types

# Import DBLP client functions
from mcp_dblp.dblp_client import (
    search, 
    add_ccf_class, 
    get_author_publications, 
    get_venue_info, 
    calculate_statistics,
    fuzzy_title_search,
    fetch_and_process_bibtex  
)

# Set up logging
log_dir = os.path.expanduser("~/.mcp-dblp")
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, "mcp_dblp_server.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger("mcp_dblp")


try:
    from importlib.metadata import version
    version_str = version("mcp-dblp")
    logger.info(f"Loaded version: {version_str}")
except Exception:
    version_str = "x.x.x"  # Anonymous fallback version
    logger.warning(f"Using default version: {version_str}")



def parse_html_links(html_string):
    """Parse HTML links of the form <a href=biburl>key</a> and extract URLs and keys."""
    pattern = r'<a\s+href=([^>]+)>([^<]+)</a>'
    matches = re.findall(pattern, html_string)
    result = []
    for url, key in matches:
        url = url.strip('"\'')
        key = key.strip()
        result.append((url, key))
    return result



def export_bibtex_entries(entries, export_dir):
    """Export BibTeX entries to a file with timestamp filename."""
    os.makedirs(export_dir, exist_ok=True)
    
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"{timestamp}.bib"
    filepath = os.path.join(export_dir, filename)
    
    with open(filepath, 'w', encoding='utf-8') as f:
        for entry in entries:
            f.write(entry + "\n\n")
    
    return filepath

async def serve(export_dir=None) -> None:
    """Main server function to handle MCP requests"""
    if export_dir is None:
        export_dir = os.path.expanduser("~/.mcp-dblp/exports")
    
    server = Server("mcp-dblp")
    server.capabilities = {}

    # Provide a list of available prompts including our instructions prompt.
    @server.list_prompts()
    async def handle_list_prompts() -> List[types.Prompt]:
        return [
            types.Prompt(
                name="MCP-DBLP Instructions",
                description="Basic instructions for using the DBLP tools; get this prompt before any interaction with MCP-DBLP.",
                arguments=[]
            )
        ]

    # Get prompt endpoint that loads our instructions from a file.
    @server.get_prompt()
    async def handle_get_prompt(name: str, arguments: Optional[dict] = None) -> types.GetPromptResult:
        try:
            # Assume instructions_prompt.md is located at the project root
            instructions_path = Path(__file__).resolve().parents[2] / "instructions_prompt.md"
            with open(instructions_path, "r", encoding="utf-8") as f:
                instructions_prompt = f.read()
        except Exception as e:
            instructions_prompt = f"Error loading instructions prompt: {e}"
        return types.GetPromptResult(
            description="MCP-DBLP Instructions",
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(
                        type="text",
                        text=instructions_prompt
                    )
                )
            ]
        )

    @server.list_tools()
    async def list_tools() -> List[types.Tool]:
        """List all available DBLP tools with detailed descriptions."""
        return [
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
                        "include_bibtex": {"type": "boolean"}
                    },
                    "required": ["query"]
                }
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
                        "include_bibtex": {"type": "boolean"}
                    },
                    "required": ["title", "similarity_threshold"]
                }
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
                        "include_bibtex": {"type": "boolean"}
                    },
                    "required": ["author_name", "similarity_threshold"]
                }
            ),
            types.Tool(
                name="get_venue_info",
                description=(
                    "Retrieve detailed information about a publication venue.\n"
                    "Arguments:\n"
                    "  - venue_name (string, required): Venue name or abbreviation (e.g., 'ICLR' or full name).\n"
                    "Returns a dictionary with fields: abbreviation, name, publisher, type, and category.\n"
                    "Note: Some fields may be empty if DBLP does not provide the information."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "venue_name": {"type": "string"}
                    },
                    "required": ["venue_name"]
                }
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
                    "properties": {
                        "results": {"type": "array"}
                    },
                    "required": ["results"]
                }
            ),
            types.Tool(
                name="export_bibtex",
                description=(
                    "Export BibTeX entries from a collection of HTML hyperlinks.\n"
                    "Arguments:\n"
                    "  - links (string, required): HTML string containing one or more <a href=biburl>key</a> links.\n"
                    "    The href attribute should contain a URL to a BibTeX file, and the link text is used as the citation key.\n"
                    "    Example input with three links:\n"
                    "    \"<a href=https://dblp.org/rec/journals/example1.bib>Smith2023</a>\n"
                    "     <a href=https://dblp.org/rec/conf/example2.bib>Jones2022</a>\n"
                    "     <a href=https://dblp.org/rec/journals/example3.bib>Brown2021</a>\"\n"
                    "Process:\n"
                    "  - For each link, the tool fetches the BibTeX content from the URL\n"
                    "  - The citation key in each BibTeX entry is replaced with the key from the link text\n"
                    "  - All entries are combined and saved to a .bib file with a timestamp filename\n"
                    "Returns:\n"
                    "  - A message with the full path to the saved .bib file"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "links": {"type": "string"}
                    },
                    "required": ["links"]
                }
            )
        ]

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict) -> List[types.TextContent]:
        """Handle tool calls from clients"""
        try:
            logger.info(f"Tool call: {name} with arguments {arguments}")
            match name:
                case "search":
                    if "query" not in arguments:
                        return [types.TextContent(
                            type="text",
                            text="Error: Missing required parameter 'query'"
                        )]
                    include_bibtex = arguments.get("include_bibtex", False)
                    result = search(
                        query=arguments.get("query"),
                        max_results=arguments.get("max_results", 10),
                        year_from=arguments.get("year_from"),
                        year_to=arguments.get("year_to"),
                        venue_filter=arguments.get("venue_filter"),
                        include_bibtex=include_bibtex
                    )
                    if include_bibtex:
                        return [types.TextContent(
                            type="text",
                            text=f"Found {len(result)} publications matching your query:\n\n{format_results_with_bibtex(result)}"
                        )]
                    else:
                        return [types.TextContent(
                            type="text",
                            text=f"Found {len(result)} publications matching your query:\n\n{format_results(result)}"
                        )]
                case "fuzzy_title_search":
                    if "title" not in arguments or "similarity_threshold" not in arguments:
                        return [types.TextContent(
                            type="text",
                            text="Error: Missing required parameter 'title' or 'similarity_threshold'"
                        )]
                    include_bibtex = arguments.get("include_bibtex", False)
                    result = fuzzy_title_search(
                        title=arguments.get("title"),
                        similarity_threshold=arguments.get("similarity_threshold"),
                        max_results=arguments.get("max_results", 10),
                        year_from=arguments.get("year_from"),
                        year_to=arguments.get("year_to"),
                        venue_filter=arguments.get("venue_filter"),
                        include_bibtex=include_bibtex
                    )
                    if include_bibtex:
                        return [types.TextContent(
                            type="text",
                            text=f"Found {len(result)} publications with similar titles:\n\n{format_results_with_similarity_and_bibtex(result)}"
                        )]
                    else:
                        return [types.TextContent(
                            type="text",
                            text=f"Found {len(result)} publications with similar titles:\n\n{format_results_with_similarity(result)}"
                        )]
                case "get_author_publications":
                    if "author_name" not in arguments or "similarity_threshold" not in arguments:
                        return [types.TextContent(
                            type="text",
                            text="Error: Missing required parameter 'author_name' or 'similarity_threshold'"
                        )]
                    include_bibtex = arguments.get("include_bibtex", False)
                    result = get_author_publications(
                        author_name=arguments.get("author_name"),
                        similarity_threshold=arguments.get("similarity_threshold"),
                        max_results=arguments.get("max_results", 20),
                        include_bibtex=include_bibtex
                    )
                    pub_count = result.get("publication_count", 0)
                    publications = result.get("publications", [])
                    
                    if include_bibtex:
                        return [types.TextContent(
                            type="text",
                            text=f"Found {pub_count} publications for author {arguments['author_name']}:\n\n{format_results_with_bibtex(publications)}"
                        )]
                    else:
                        return [types.TextContent(
                            type="text",
                            text=f"Found {pub_count} publications for author {arguments['author_name']}:\n\n{format_results(publications)}"
                        )]
                case "get_venue_info":
                    if "venue_name" not in arguments:
                        return [types.TextContent(
                            type="text",
                            text="Error: Missing required parameter 'venue_name'"
                        )]
                    result = get_venue_info(
                        venue_name=arguments.get("venue_name")
                    )
                    return [types.TextContent(
                        type="text",
                        text=f"Venue information for {arguments['venue_name']}:\n\n{format_dict(result)}"
                    )]
                case "calculate_statistics":
                    if "results" not in arguments:
                        return [types.TextContent(
                            type="text",
                            text="Error: Missing required parameter 'results'"
                        )]
                    result = calculate_statistics(
                        results=arguments.get("results")
                    )
                    return [types.TextContent(
                        type="text",
                        text=f"Statistics calculated:\n\n{format_dict(result)}"
                    )]
                case "export_bibtex":
                    if "links" not in arguments:
                        return [types.TextContent(
                            type="text",
                            text="Error: Missing required parameter 'links'"
                        )]
                    
                    html_links = arguments.get("links")
                    links = parse_html_links(html_links)
                    
                    if not links:
                        return [types.TextContent(
                            type="text",
                            text="Error: No valid links found in the input"
                        )]
                    
                    # Fetch and process BibTeX entries
                    bibtex_entries = []
                    for url, key in links:
                        bibtex = fetch_and_process_bibtex(url, key)
                        bibtex_entries.append(bibtex)
                    
                    # Export to file
                    filepath = export_bibtex_entries(bibtex_entries, export_dir)
                    
                    return [types.TextContent(
                        type="text",
                        text=f"Exported {len(bibtex_entries)} BibTeX entries to {filepath}"
                    )]
                case _:
                    return [types.TextContent(
                        type="text",
                        text=f"Unknown tool: {name}"
                    )]
        except Exception as e:
            logger.error(f"Tool execution failed: {str(e)}", exc_info=True)
            return [types.TextContent(
                type="text",
                text=f"Error executing {name}: {str(e)}"
            )]

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
        formatted.append(f"{i+1}. {title}")
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
        formatted.append(f"{i+1}. {title} [Similarity: {similarity:.2f}]")
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
        formatted.append(f"{i+1}. {title}")
        formatted.append(f"   Authors: {authors}")
        formatted.append(f"   Venue: {venue} ({year})")
        if "bibtex" in result and result["bibtex"]:
            formatted.append("\n   BibTeX:")
            bibtex_lines = result["bibtex"].strip().split('\n')
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
        formatted.append(f"{i+1}. {title} [Similarity: {similarity:.2f}]")
        formatted.append(f"   Authors: {authors}")
        formatted.append(f"   Venue: {venue} ({year})")
        if "bibtex" in result and result["bibtex"]:
            formatted.append("\n   BibTeX:")
            bibtex_lines = result["bibtex"].strip().split('\n')
            formatted.append("      " + "\n      ".join(bibtex_lines))
        formatted.append("")
    return "\n".join(formatted)

def format_dict(data):
    formatted = []
    for key, value in data.items():
        formatted.append(f"{key}: {value}")
    return "\n".join(formatted)

def main() -> int:
    parser = argparse.ArgumentParser(description="MCP-DBLP Server")
    parser.add_argument("--exportdir", type=str, default=os.path.expanduser("~/.mcp-dblp/exports"),
                        help="Directory to export BibTeX files to")
    args = parser.parse_args()
    
    logger.info(f"Starting MCP-DBLP server with version: {version_str}")
    try:
        asyncio.run(serve(export_dir=args.exportdir))
        return 0
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
        return 0
    except Exception as e:
        logger.error(f"Server error: {str(e)}", exc_info=True)
        return 1

if __name__ == "__main__":
    sys.exit(main())