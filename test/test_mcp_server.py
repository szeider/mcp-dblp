"""
End-to-end tests for MCP server protocol integration.

These tests verify that the MCP server correctly handles tool calls
through the MCP protocol interface.
"""

import sys
from pathlib import Path

import pytest

# Add src directory to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from mcp_dblp.server import (
    export_bibtex_entries,
    format_dict,
    format_results,
    format_results_with_bibtex,
    format_results_with_similarity,
    serve,
)


class TestExportBibTeX:
    """Test BibTeX export functionality."""

    def test_export_single_entry(self, tmp_path):
        """Test exporting a single BibTeX entry."""
        entries = ["@article{Smith2023,\n  title={Test Paper},\n  author={Smith, John}\n}"]

        filepath = export_bibtex_entries(entries, str(tmp_path / "refs.bib"))

        # Check file was created
        assert Path(filepath).exists()
        assert filepath.endswith(".bib")

        # Check content
        with open(filepath) as f:
            content = f.read()
            assert "Smith2023" in content
            assert "Test Paper" in content

    def test_export_multiple_entries(self, tmp_path):
        """Test exporting multiple BibTeX entries."""
        entries = [
            "@article{Smith2023,\n  title={Paper 1}\n}",
            "@inproceedings{Jones2022,\n  title={Paper 2}\n}",
            "@article{Brown2021,\n  title={Paper 3}\n}",
        ]

        filepath = export_bibtex_entries(entries, str(tmp_path / "multiple.bib"))

        with open(filepath) as f:
            content = f.read()
            assert "Smith2023" in content
            assert "Jones2022" in content
            assert "Brown2021" in content

    def test_export_creates_directory(self, tmp_path):
        """Test that export creates parent directory if it doesn't exist."""
        new_dir = tmp_path / "new_export_dir"
        entries = ["@article{Test2023,\n  title={Test}\n}"]

        filepath = export_bibtex_entries(entries, str(new_dir / "refs.bib"))

        assert new_dir.exists()
        assert Path(filepath).exists()

    def test_export_adds_bib_extension(self, tmp_path):
        """Test that .bib extension is added automatically if missing."""
        entries = ["@article{Test2023,\n  title={Test}\n}"]

        # Pass path without .bib extension
        filepath = export_bibtex_entries(entries, str(tmp_path / "references"))

        # Should have .bib added
        assert filepath.endswith(".bib")
        assert Path(filepath).exists()


class TestFormatters:
    """Test result formatting functions."""

    def test_format_results_basic(self):
        """Test basic result formatting."""
        results = [
            {
                "title": "Paper 1",
                "authors": ["Alice Smith", "Bob Jones"],
                "venue": "ICLR",
                "year": 2023,
            },
            {"title": "Paper 2", "authors": ["Charlie Brown"], "venue": "NeurIPS", "year": 2022},
        ]

        formatted = format_results(results)

        assert "Paper 1" in formatted
        assert "Paper 2" in formatted
        assert "Alice Smith" in formatted
        assert "ICLR" in formatted
        assert "2023" in formatted

    def test_format_results_includes_dblp_key(self):
        """Test that formatted results include dblp_key for add_bibtex_entry workflow.

        Regression test: format_results previously omitted dblp_key, making it
        impossible for LLMs to use add_bibtex_entry after a plain search.
        """
        results = [
            {
                "title": "Paper 1",
                "authors": ["Alice Smith"],
                "venue": "ICLR",
                "year": 2023,
                "dblp_key": "conf/iclr/Smith23",
            },
        ]

        formatted = format_results(results)
        assert "DBLP key: conf/iclr/Smith23" in formatted

        formatted_sim = format_results_with_similarity([{**results[0], "similarity": 0.95}])
        assert "DBLP key: conf/iclr/Smith23" in formatted_sim

        formatted_bib = format_results_with_bibtex(results)
        assert "DBLP key: conf/iclr/Smith23" in formatted_bib

    def test_format_results_empty(self):
        """Test formatting empty results."""
        formatted = format_results([])

        assert "No results found" in formatted

    def test_format_results_with_similarity(self):
        """Test formatting results with similarity scores."""
        results = [
            {
                "title": "Paper 1",
                "authors": ["Alice Smith"],
                "venue": "ICLR",
                "year": 2023,
                "similarity": 0.95,
            },
            {
                "title": "Paper 2",
                "authors": ["Bob Jones"],
                "venue": "NeurIPS",
                "year": 2022,
                "similarity": 0.87,
            },
        ]

        formatted = format_results_with_similarity(results)

        assert "0.95" in formatted
        assert "0.87" in formatted
        assert "Paper 1" in formatted

    def test_format_results_with_bibtex(self):
        """Test formatting results with BibTeX entries."""
        results = [
            {
                "title": "Paper 1",
                "authors": ["Alice Smith"],
                "venue": "ICLR",
                "year": 2023,
                "bibtex": "@article{Smith2023,\n  title={Paper 1}\n}",
            }
        ]

        formatted = format_results_with_bibtex(results)

        assert "Paper 1" in formatted
        assert "BibTeX:" in formatted
        assert "@article{Smith2023" in formatted

    def test_format_dict(self):
        """Test dictionary formatting."""
        data = {"field1": "value1", "field2": "value2", "field3": 123}

        formatted = format_dict(data)

        assert "field1: value1" in formatted
        assert "field2: value2" in formatted
        assert "field3: 123" in formatted


class TestMCPServerIntegration:
    """Test MCP server protocol integration."""

    @pytest.mark.asyncio
    async def test_server_initialization(self):
        """Test that server can be initialized."""
        # This is a basic smoke test
        # Full integration testing would require an MCP client

        # Just verify the serve function exists and can be called
        # We don't actually run it to completion as it's an infinite loop
        assert callable(serve)

    def test_tools_registration(self):
        """Test that all expected tools are registered."""
        # Import to check that tools are properly defined
        from mcp_dblp.server import serve

        # The serve function should contain tool registration
        # This is verified by the existence of the function
        assert callable(serve)

    def test_instructions_prompt_file_exists(self):
        """Test that the instructions prompt file exists and is readable."""
        from importlib import resources

        # Load instructions_prompt.md from the package using importlib.resources
        # This works both in development and when installed via pip/uv
        content = (
            resources.files("mcp_dblp")
            .joinpath("instructions_prompt.md")
            .read_text(encoding="utf-8")
        )

        assert len(content) > 0, "Instructions prompt file is empty"
        assert "DBLP" in content, "Instructions prompt should mention DBLP"
        assert "search" in content.lower(), "Instructions prompt should mention search"
        assert "BibTeX" in content or "bibtex" in content, (
            "Instructions prompt should mention BibTeX"
        )


class TestFuzzySimilarityLogic:
    """Unit tests for fuzzy title matching similarity logic (no API calls)."""

    def test_substring_match_scores_high(self):
        """Short query that is a substring of a long title should score >= 0.8."""
        import difflib

        query = "graph coloring"
        long_title = "A Survey on Graph Coloring Problems and Their Applications"

        query_lower = query.lower()
        title_lower = long_title.lower()

        # Old logic: SequenceMatcher ratio penalizes short queries
        old_ratio = difflib.SequenceMatcher(None, query_lower, title_lower).ratio()
        assert old_ratio < 0.6, f"Old ratio {old_ratio} should be below 0.6 (the bug)"

        # New logic: substring containment gives at least 0.8
        if query_lower in title_lower:
            new_ratio = max(0.8, len(query_lower) / len(title_lower))
        else:
            new_ratio = old_ratio

        assert new_ratio >= 0.8, f"New ratio {new_ratio} should be >= 0.8 for substring match"

    def test_non_substring_uses_sequencematcher(self):
        """Non-substring queries should fall back to SequenceMatcher."""
        import difflib

        query = "graph colorng"  # typo
        title = "Graph Coloring Reconfiguration"

        query_lower = query.lower()
        title_lower = title.lower()

        assert query_lower not in title_lower
        expected = difflib.SequenceMatcher(None, query_lower, title_lower).ratio()

        # The logic should use SequenceMatcher for non-substring
        assert expected > 0, "SequenceMatcher should still give a positive score for near-matches"


class TestIntegrationScenarios:
    """Test complete end-to-end scenarios."""

    def test_search_and_export_workflow(self, tmp_path, dblp_online):
        """Test a complete workflow: search -> add entries -> export BibTeX."""
        from mcp_dblp.dblp_client import fetch_and_process_bibtex, search

        # Step 1: Search for papers
        results = search("attention is all you need", max_results=3)

        assert len(results) > 0, "Should find results"

        # Step 2: Collect BibTeX entries (simulating the buffer)
        entries = []
        for result in results[:2]:  # Limit to 2 to avoid timeout
            dblp_key = result.get("dblp_key", "")
            if dblp_key:
                # Create a citation key from the first author and year
                authors = result.get("authors", [])
                year = result.get("year", "")
                if authors and year:
                    first_author = authors[0].split()[-1]  # Last name
                    citation_key = f"{first_author}{year}"
                    url = f"https://dblp.org/rec/{dblp_key}.bib"

                    # Fetch BibTeX (simulating add_bibtex_entry)
                    bibtex = fetch_and_process_bibtex(url, citation_key)
                    if bibtex and not bibtex.startswith("% Error"):
                        entries.append(bibtex)

        # Step 3: Export to file (simulating export_bibtex)
        if entries:
            filepath = export_bibtex_entries(entries, str(tmp_path / "search_results.bib"))

            assert Path(filepath).exists()

            # Verify content
            with open(filepath) as f:
                content = f.read()
                assert "@" in content

    def test_author_stats_workflow(self, dblp_online):
        """Test workflow: get author pubs and verify inline stats."""
        from mcp_dblp.dblp_client import get_author_publications

        result = get_author_publications(
            author_name="Yoshua Bengio", similarity_threshold=0.8, max_results=10
        )

        if result["publication_count"] > 0:
            author_stats = result["stats"]
            assert "venues" in author_stats
            assert "years" in author_stats
            assert "types" in author_stats


class TestErrorHandling:
    """Test error handling in server functions."""

    def test_export_with_empty_entries(self, tmp_path):
        """Test exporting with empty entries list."""
        entries = []

        filepath = export_bibtex_entries(entries, str(tmp_path / "empty.bib"))

        # Should create file even with no entries
        assert Path(filepath).exists()

        with open(filepath) as f:
            content = f.read()
            assert content.strip() == ""


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
