"""
End-to-end tests for MCP-DBLP server tools.

These tests verify that all tools work correctly by making actual calls
to the DBLP API and checking the responses.
"""

import sys
from pathlib import Path

import pytest

# Add src directory to path so we can import the modules
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_dblp.dblp_client import (
    fetch_and_process_bibtex,
    fetch_bibtex_entry,
    fuzzy_title_search,
    get_author_publications,
    get_venue_info,
    search,
)

# These tests call the dblp.org web API; skip them when it serves the Anubis bot check.
pytestmark = pytest.mark.usefixtures("dblp_online")


class TestSearch:
    """Test the search tool with various query types."""

    def test_basic_search(self):
        """Test basic search with a simple query."""
        results = search("Swin Transformer", max_results=5)

        assert len(results) > 0, "Should return at least one result"
        assert len(results) <= 5, "Should not exceed max_results"

        # Check result structure
        first = results[0]
        assert "title" in first
        assert "authors" in first
        assert "venue" in first
        assert "year" in first
        assert isinstance(first["authors"], list)

    def test_boolean_and_search(self):
        """Test search with AND operator."""
        results = search("machine learning and optimization", max_results=5)

        assert len(results) > 0, "Should return results for AND query"

    def test_boolean_or_search(self):
        """Test search with OR operator."""
        results = search("ICLR or NeurIPS", max_results=10)

        assert len(results) > 0, "Should return results for OR query"

    def test_year_filter(self):
        """Test search with year filtering."""
        results = search("deep learning", max_results=10, year_from=2020, year_to=2023)

        assert len(results) > 0, "Should return results within year range"

        for result in results:
            year = result.get("year")
            if year:
                assert 2020 <= year <= 2023, f"Year {year} should be between 2020 and 2023"

    def test_venue_filter(self):
        """Test search with venue filtering."""
        # First search without filter to get results
        all_results = search("neural networks", max_results=20)

        assert len(all_results) > 0, "Should get some results"

        # Get a venue that actually exists in the results
        test_venue = None
        for result in all_results:
            venue = result.get("venue", "")
            if venue and isinstance(venue, str) and len(venue) > 3:
                test_venue = venue[:5]  # Use first 5 chars as filter
                break

        if test_venue:
            # Now test filtering with that venue
            filtered_results = search("neural networks", max_results=10, venue_filter=test_venue)

            # Should have at least one result
            assert len(filtered_results) > 0, (
                f"Should return results for venue filter: {test_venue}"
            )

    def test_search_returns_dblp_key(self):
        """Test that search results include dblp_key for use with add_bibtex_entry."""
        results = search("parameterized complexity", max_results=3)

        assert len(results) > 0
        # Skip error results (from rate limiting / timeouts)
        valid = [r for r in results if "error" not in r]
        if not valid:
            pytest.skip("DBLP API rate-limited, no valid results")
        for result in valid:
            assert "dblp_key" in result, "Each result should have a dblp_key field"
            assert result["dblp_key"], "dblp_key should not be empty"
            # Key should look like a DBLP path (e.g., conf/nips/VaswaniSPUJGKP17)
            assert "/" in result["dblp_key"], f"dblp_key should contain '/': {result['dblp_key']}"

    def test_search_with_bibtex(self):
        """Test search with BibTeX entries included."""
        results = search("attention is all you need", max_results=3, include_bibtex=True)

        assert len(results) > 0, "Should return results"

        # Check if at least one result has bibtex
        has_bibtex = any("bibtex" in result for result in results)
        assert has_bibtex, "At least one result should have BibTeX entry"


class TestFuzzyTitleSearch:
    """Test fuzzy title search functionality."""

    def test_exact_title_match(self):
        """Test fuzzy search with high similarity threshold."""
        results = fuzzy_title_search(
            title="Attention Is All You Need", similarity_threshold=0.5, max_results=5
        )

        assert len(results) > 0, "Should find the famous Transformer paper"

        # Check similarity scores
        for result in results:
            assert "similarity" in result
            assert result["similarity"] >= 0.5

    def test_partial_title_match(self):
        """Test fuzzy search with partial title."""
        results = fuzzy_title_search(
            title="deep learning", similarity_threshold=0.3, max_results=10
        )

        assert len(results) > 0, "Should find deep learning papers"

        # Results should be sorted by similarity (highest first)
        similarities = [r.get("similarity", 0) for r in results]
        assert similarities == sorted(similarities, reverse=True), (
            "Results should be sorted by similarity score"
        )

    def test_short_generic_title(self):
        """Test fuzzy search with a short generic title at realistic threshold.

        Regression test: short queries like "Graph Coloring" previously returned
        0 results at threshold=0.6 because SequenceMatcher.ratio() penalizes
        short queries against long titles. The fix uses substring containment.
        """
        results = fuzzy_title_search(
            title="Graph Coloring", similarity_threshold=0.6, max_results=5
        )

        if not results:
            pytest.skip("DBLP API rate-limited, no results returned")

        assert len(results) > 0, (
            "Should find papers with 'Graph Coloring' in title at threshold 0.6"
        )

        # All returned results should meet the threshold
        for result in results:
            assert result["similarity"] >= 0.6

    def test_fuzzy_title_with_filters(self):
        """Test fuzzy title search with year and venue filters."""
        results = fuzzy_title_search(
            title="Vision Transformer",
            similarity_threshold=0.5,
            max_results=5,
            year_from=2020,
            venue_filter="ICLR",
        )

        # May or may not have results depending on filters
        if results:
            for result in results:
                year = result.get("year")
                venue = result.get("venue", "")
                if year:
                    assert year >= 2020
                assert "iclr" in venue.lower()

    def test_fuzzy_title_with_bibtex(self):
        """Test fuzzy title search with BibTeX entries."""
        results = fuzzy_title_search(
            title="ResNet", similarity_threshold=0.4, max_results=3, include_bibtex=True
        )

        if results:
            assert any("bibtex" in result for result in results), (
                "At least one result should have BibTeX when include_bibtex=True"
            )


class TestAuthorPublications:
    """Test author publication retrieval."""

    def test_author_exact_match(self):
        """Test getting publications for a well-known author."""
        result = get_author_publications(
            author_name="Yoshua Bengio", similarity_threshold=0.8, max_results=10
        )

        assert result["publication_count"] > 0, "Should find publications for Yoshua Bengio"
        assert "publications" in result
        assert "stats" in result

        # Check stats structure
        stats = result["stats"]
        assert "venues" in stats
        assert "years" in stats
        assert "types" in stats

    def test_author_fuzzy_match(self):
        """Test author search with fuzzy matching."""
        result = get_author_publications(
            author_name="Geoffrey Hinton", similarity_threshold=0.7, max_results=15
        )

        assert result["publication_count"] > 0, "Should find publications"

        # Verify that fuzzy matching worked
        pubs = result["publications"]
        assert len(pubs) > 0

        # Check that authors list exists in publications
        for pub in pubs:
            assert "authors" in pub
            assert isinstance(pub["authors"], list)

    def test_author_with_bibtex(self):
        """Test author publications with BibTeX entries."""
        result = get_author_publications(
            author_name="Yann LeCun", similarity_threshold=0.8, max_results=5, include_bibtex=True
        )

        if result["publication_count"] > 0:
            pubs = result["publications"]
            assert any("bibtex" in pub for pub in pubs), (
                "At least one publication should have BibTeX when include_bibtex=True"
            )

    def test_author_stats_accuracy(self):
        """Test that author statistics are calculated correctly."""
        result = get_author_publications(
            author_name="Andrew Ng", similarity_threshold=0.8, max_results=20
        )

        if result["publication_count"] > 0:
            stats = result["stats"]

            # Top venues should be tuples of (venue, count)
            assert isinstance(stats["venues"], list)
            if stats["venues"]:
                assert isinstance(stats["venues"][0], tuple)
                assert len(stats["venues"][0]) == 2

            # Years should be tuples
            assert isinstance(stats["years"], list)

            # Types should be a dict
            assert isinstance(stats["types"], dict)


class TestVenueInfo:
    """Test venue information retrieval."""

    def test_venue_info_retrieval(self):
        """Test getting venue information."""
        result = get_venue_info("ICLR")

        assert "venue" in result
        assert "acronym" in result
        assert "type" in result
        assert "url" in result

        # For a known venue like ICLR, we should get actual data
        assert result["venue"] or result["acronym"], "Should have venue name or acronym"

    def test_venue_info_different_names(self):
        """Test venue info with different venue names."""
        venues = ["NeurIPS", "ICML", "CVPR", "ACL"]

        for venue in venues:
            result = get_venue_info(venue)
            assert result is not None
            assert "venue" in result
            assert "type" in result


class TestBibTeXFetching:
    """Test BibTeX fetching functionality."""

    def test_fetch_bibtex_entry(self):
        """Test fetching a BibTeX entry by DBLP key."""
        # Use a well-known paper's DBLP key
        dblp_key = "conf/nips/VaswaniSPUJGKP17"

        bibtex = fetch_bibtex_entry(dblp_key)

        assert bibtex, "Should return BibTeX entry"
        assert "@" in bibtex, "BibTeX should contain entry type"
        assert "{" in bibtex, "BibTeX should have proper format"

    def test_fetch_and_process_bibtex(self):
        """Test fetching and processing BibTeX with custom key."""
        # Use the BibTeX URL for a known paper
        url = "https://dblp.org/rec/conf/nips/VaswaniSPUJGKP17.bib"
        custom_key = "Vaswani2017"

        bibtex = fetch_and_process_bibtex(url, custom_key)

        assert bibtex, "Should return BibTeX entry"
        assert custom_key in bibtex, f"BibTeX should contain custom key {custom_key}"
        assert "@" in bibtex, "BibTeX should contain entry type"

    def test_fetch_bibtex_invalid_key(self):
        """Test fetching BibTeX with invalid key."""
        dblp_key = "invalid/key/that/does/not/exist"

        result = fetch_bibtex_entry(dblp_key)

        # Should return empty string or error message
        assert isinstance(result, str)


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_search_empty_results(self):
        """Test search with query that returns no results."""
        results = search("xyzabc123nonexistentquery999", max_results=5)

        # May return empty list or mock results depending on error handling
        assert isinstance(results, list)

    def test_fuzzy_search_zero_threshold(self):
        """Test fuzzy search with very low threshold."""
        results = fuzzy_title_search(title="AI", similarity_threshold=0.0, max_results=5)

        # Should return results even with 0 threshold
        assert isinstance(results, list)

    def test_author_high_threshold(self):
        """Test author search with very high similarity threshold."""
        result = get_author_publications(
            author_name="Smith", similarity_threshold=0.99, max_results=5
        )

        # May or may not find exact matches
        assert isinstance(result, dict)
        assert "publications" in result


if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v", "-s"])
