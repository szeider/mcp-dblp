"""
MCP-DBLP - A MCP server providing access to DBLP publication data
"""


def main():
    """Server entry point (kept for `from mcp_dblp import main`).

    Imported lazily so that `mcp-dblp-index` and other submodule imports do not
    set up the server's logging.
    """
    from mcp_dblp.server import main as _main

    return _main()


__all__ = ["main"]  # Initialize MCP server package
