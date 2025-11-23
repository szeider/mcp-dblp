import contextlib
import difflib
import logging
import re
from collections import Counter
from typing import Any

import requests

logger = logging.getLogger("dblp_client")

# Default timeout for all HTTP requests
REQUEST_TIMEOUT = 10  # seconds

# Headers for DBLP API requests
# DBLP recommends using an identifying User-Agent to avoid rate-limiting
# See: https://dblp.org/faq/1474706.html
HEADERS = {
    "User-Agent": "mcp-dblp/1.1.1 (https://github.com/szeider/mcp-dblp)",
    "Accept": "application/json",
}


def _fetch_publications(single_query: str, max_results: int) -> list[dict[str, Any]]:
    """Helper function to fetch publications for a single query string."""
    results = []
    try:
        url = "https://dblp.org/search/publ/api"
        params = {"q": single_query, "format": "json", "h": max_results}
        response = requests.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        hits = data.get("result", {}).get("hits", {})
        total = int(hits.get("@total", "0"))
        logger.info(f"Found {total} results for query: {single_query}")
        if total > 0:
            publications = hits.get("hit", [])
            if not isinstance(publications, list):
                publications = [publications]
            for pub in publications:
                info = pub.get("info", {})
                authors = []
                authors_data = info.get("authors", {}).get("author", [])
                if not isinstance(authors_data, list):
                    authors_data = [authors_data]
                for author in authors_data:
                    if isinstance(author, dict):
                        authors.append(author.get("text", ""))
                    else:
                        authors.append(str(author))

                # Extract the proper DBLP URL or ID for BibTeX retrieval
                dblp_url = info.get("url", "")
                dblp_key = ""

                if dblp_url:
                    # Extract the key from the URL (e.g., https://dblp.org/rec/journals/jmlr/ChowdheryNDBMGMBCDDRSSTWPLLNSZDYJGKPSN23)
                    dblp_key = dblp_url.replace("https://dblp.org/rec/", "")
                elif "key" in pub:
                    dblp_key = pub.get("key", "").replace("dblp:", "")
                else:
                    dblp_key = pub.get("@id", "").replace("dblp:", "")

                result = {
                    "title": info.get("title", ""),
                    "authors": authors,
                    "venue": info.get("venue", ""),
                    "year": int(info.get("year", 0)) if info.get("year") else None,
                    "type": info.get("type", ""),
                    "doi": info.get("doi", ""),
                    "ee": info.get("ee", ""),
                    "url": info.get("url", ""),
                    "dblp_key": dblp_key,  # Use more specific name for the DBLP key
                }
                results.append(result)
    except requests.exceptions.Timeout:
        logger.error(f"Timeout error searching DBLP after {REQUEST_TIMEOUT} seconds")
        # Provide timeout error information
        timeout_msg = f"ERROR: Query '{single_query}' timed out after {REQUEST_TIMEOUT} seconds"
        results.append(
            {
                "title": timeout_msg,
                "authors": [],
                "venue": "Error",
                "year": None,
                "error": f"Timeout after {REQUEST_TIMEOUT} seconds",
            }
        )
    except Exception as e:
        logger.error(f"Error searching DBLP: {e}")
        # Return error result instead of mock data
        error_msg = f"ERROR: DBLP API error for query '{single_query}': {str(e)}"
        results.append(
            {
                "title": error_msg,
                "authors": [],
                "venue": "Error",
                "year": None,
                "error": str(e),
            }
        )
    return results


def search(
    query: str,
    max_results: int = 10,
    year_from: int | None = None,
    year_to: int | None = None,
    venue_filter: str | None = None,
    include_bibtex: bool = False,
) -> list[dict[str, Any]]:
    """
    Search DBLP using their public API.

    Parameters:
        query (str): The search query string.
        max_results (int, optional): Maximum number of results to return. Default is 10.
        year_from (int, optional): Lower bound for publication year.
        year_to (int, optional): Upper bound for publication year.
        venue_filter (str, optional): Case-insensitive substring filter
            for publication venues.
        include_bibtex (bool, optional): Whether to include BibTeX entries
            in the results. Default is False.

    Returns:
        List[Dict[str, Any]]: A list of publication dictionaries.
    """
    query_lower = query.lower()
    if "(" in query or ")" in query:
        logger.warning(
            "Parentheses are not supported in boolean queries. "
            "They will be treated as literal characters."
        )
    results = []
    if " or " in query_lower:
        subqueries = [q.strip() for q in query_lower.split(" or ") if q.strip()]
        seen = set()
        for q in subqueries:
            for pub in _fetch_publications(q, max_results):
                identifier = (pub.get("title"), pub.get("year"))
                if identifier not in seen:
                    results.append(pub)
                    seen.add(identifier)
    else:
        results = _fetch_publications(query, max_results)

    filtered_results = []
    for result in results:
        if year_from or year_to:
            year = result.get("year")
            if year:
                try:
                    year = int(year)
                    if (year_from and year < year_from) or (year_to and year > year_to):
                        continue
                except (ValueError, TypeError):
                    pass
        if venue_filter:
            venue = result.get("venue", "")
            if venue_filter.lower() not in venue.lower():
                continue
        filtered_results.append(result)

    if not filtered_results:
        logger.info("No results found. Consider revising your query syntax.")

    filtered_results = filtered_results[:max_results]

    # Fetch BibTeX entries if requested
    if include_bibtex:
        for result in filtered_results:
            if "dblp_key" in result and result["dblp_key"]:
                result["bibtex"] = fetch_bibtex_entry(result["dblp_key"])

    return filtered_results


def get_author_publications(
    author_name: str,
    similarity_threshold: float,
    max_results: int = 20,
    include_bibtex: bool = False,
) -> dict[str, Any]:
    """
    Get publication information for a specific author with fuzzy matching.

    Parameters:
        author_name (str): Author name to search for.
        similarity_threshold (float): Threshold for fuzzy matching (0-1).
        max_results (int, optional): Maximum number of results to return. Default is 20.
        include_bibtex (bool, optional): Whether to include BibTeX entries. Default is False.

    Returns:
        Dict[str, Any]: Dictionary with author publication information.
    """
    logger.info(
        f"Getting publications for author: {author_name} with similarity threshold {similarity_threshold}"
    )
    author_query = f"author:{author_name}"
    publications = search(author_query, max_results=max_results * 2)

    filtered_publications = []
    for pub in publications:
        best_ratio = 0.0
        for candidate in pub.get("authors", []):
            ratio = difflib.SequenceMatcher(None, author_name.lower(), candidate.lower()).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
        if best_ratio >= similarity_threshold:
            filtered_publications.append(pub)

    filtered_publications = filtered_publications[:max_results]

    # Fetch BibTeX entries if requested
    if include_bibtex:
        for pub in filtered_publications:
            if "dblp_key" in pub and pub["dblp_key"]:
                pub["bibtex"] = fetch_bibtex_entry(pub["dblp_key"])

    venues = Counter([p.get("venue", "") for p in filtered_publications])
    years = Counter([p.get("year", "") for p in filtered_publications])
    types = Counter([p.get("type", "") for p in filtered_publications])

    return {
        "name": author_name,
        "publication_count": len(filtered_publications),
        "publications": filtered_publications,
        "stats": {
            "venues": venues.most_common(5),
            "years": years.most_common(5),
            "types": dict(types),
        },
    }


def fuzzy_title_search(
    title: str,
    similarity_threshold: float,
    max_results: int = 10,
    year_from: int | None = None,
    year_to: int | None = None,
    venue_filter: str | None = None,
    include_bibtex: bool = False,
) -> list[dict[str, Any]]:
    """
    Search DBLP for publications with fuzzy title matching.

    Uses multiple search strategies to improve recall:
    1. Search with "title:" prefix
    2. Search without prefix (broader matching)
    3. Calculate similarity scores and rank by best match

    Note: DBLP's search ranking may not prioritize the exact paper you're looking for.
    For best results, include author name or year in the title parameter
    (e.g., "Attention is All You Need Vaswani" or use the regular search() function).

    Parameters:
        title (str): Full or partial title of the publication (case-insensitive).
        similarity_threshold (float): A float between 0 and 1 where 1.0 means an exact match.
        max_results (int, optional): Maximum number of publications to return. Default is 10.
        year_from (int, optional): Lower bound for publication year.
        year_to (int, optional): Upper bound for publication year.
        venue_filter (str, optional): Case-insensitive substring filter for publication venues.
        include_bibtex (bool, optional): Whether to include BibTeX entries. Default is False.

    Returns:
        List[Dict[str, Any]]: A list of publication objects sorted by title similarity score.
    """
    logger.info(f"Searching for title: '{title}' with similarity threshold {similarity_threshold}")

    candidates = []
    seen_titles = set()

    # Strategy 1: Search with title prefix
    title_query = f"title:{title}"
    results = search(
        title_query,
        max_results=max_results * 3,
        year_from=year_from,
        year_to=year_to,
        venue_filter=venue_filter,
    )
    for pub in results:
        t = pub.get("title", "")
        if t not in seen_titles:
            candidates.append(pub)
            seen_titles.add(t)

    # Strategy 2: Search without prefix
    results = search(
        title,
        max_results=max_results * 2,
        year_from=year_from,
        year_to=year_to,
        venue_filter=venue_filter,
    )
    for pub in results:
        t = pub.get("title", "")
        if t not in seen_titles:
            candidates.append(pub)
            seen_titles.add(t)

    # Calculate similarity scores
    filtered = []
    for pub in candidates:
        pub_title = pub.get("title", "")
        ratio = difflib.SequenceMatcher(None, title.lower(), pub_title.lower()).ratio()
        if ratio >= similarity_threshold:
            pub["similarity"] = ratio
            filtered.append(pub)

    # Sort by similarity score (highest first)
    filtered = sorted(filtered, key=lambda x: x.get("similarity", 0), reverse=True)

    filtered = filtered[:max_results]

    # Fetch BibTeX entries if requested
    if include_bibtex:
        for pub in filtered:
            if "dblp_key" in pub and pub["dblp_key"]:
                bibtex = fetch_bibtex_entry(pub["dblp_key"])
                if bibtex:
                    pub["bibtex"] = bibtex

    return filtered


def fetch_and_process_bibtex(url, new_key):
    """
    Fetch BibTeX from URL and replace the key with new_key.

    Parameters:
        url (str): URL to the BibTeX file
        new_key (str): New citation key to replace the original one

    Returns:
        str: BibTeX content with replaced citation key, or error message
    """
    try:
        response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        bibtex = response.text

        # Replace the key in format @TYPE{KEY, ... -> @TYPE{new_key, ...
        bibtex = re.sub(r"@(\w+){([^,]+),", r"@\1{" + new_key + ",", bibtex, count=1)
        return bibtex
    except requests.exceptions.Timeout:
        logger.error(f"Timeout fetching {url} after {REQUEST_TIMEOUT} seconds")
        return f"% Error: Timeout fetching {url} after {REQUEST_TIMEOUT} seconds"
    except Exception as e:
        logger.error(f"Error fetching {url}: {str(e)}", exc_info=True)
        return f"% Error fetching {url}: {str(e)}"


def fetch_bibtex_entry(dblp_key: str) -> str:
    """
    Fetch BibTeX entry from DBLP by key.

    Parameters:
        dblp_key (str): DBLP publication key.

    Returns:
        str: BibTeX entry, or empty string if not found.
    """
    try:
        # Make sure we have a valid key
        if not dblp_key or dblp_key.isspace():
            logger.warning("Empty or invalid DBLP key provided")
            return ""

        # Try multiple URL formats to increase chances of success
        urls_to_try = []

        # Format 1: Direct key
        urls_to_try.append(f"https://dblp.org/rec/{dblp_key}.bib")

        # Format 2: If the key has slashes, it might be a full path
        if "/" in dblp_key:
            urls_to_try.append(f"https://dblp.org/rec/{dblp_key}.bib")

        # Format 3: If the key has a colon, it might be a DBLP-style key
        if ":" in dblp_key:
            clean_key = dblp_key.replace(":", "/")
            urls_to_try.append(f"https://dblp.org/rec/{clean_key}.bib")

        # Try each URL until one works
        for url in urls_to_try:
            logger.info(f"Fetching BibTeX from: {url}")
            response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            logger.info(f"Response status: {response.status_code}")

            if response.status_code == 200:
                bibtex = response.text
                if not bibtex or bibtex.isspace():
                    logger.warning(f"Received empty BibTeX content for URL: {url}")
                    continue

                logger.info(f"BibTeX content (first 100 chars): {bibtex[:100]}")

                # Extract the citation type and key (e.g., @article{DBLP:journals/jmlr/ChowdheryNDBMGMBCDDRSSTWPLLNSZDYJGKPSN23,)
                citation_key_match = re.match(r"@(\w+){([^,]+),", bibtex)
                if citation_key_match:
                    citation_type = citation_key_match.group(1)
                    old_key = citation_key_match.group(2)
                    logger.info(f"Found citation type: {citation_type}, key: {old_key}")

                    # Create a new key based on the first author's last name and year
                    # Try to extract author and year from the DBLP key or from the BibTeX content
                    author_year_match = re.search(r"([A-Z][a-z]+).*?(\d{2,4})", dblp_key)

                    if author_year_match:
                        author = author_year_match.group(1)
                        year = author_year_match.group(2)
                        if len(year) == 2:  # Convert 2-digit year to 4-digit
                            year = "20" + year if int(year) < 50 else "19" + year
                        new_key = f"{author}{year}"
                        logger.info(f"Generated new key: {new_key}")
                    else:
                        # If we can't extract from key, create a simpler key from the DBLP key
                        parts = dblp_key.split("/")
                        new_key = parts[-1] if parts else dblp_key
                        logger.info(f"Using fallback key: {new_key}")

                    # Replace the old key with the new key
                    bibtex = bibtex.replace(f"{{{old_key},", f"{{{new_key},", 1)
                    logger.info("Replaced old key with new key")

                    return bibtex
                else:
                    logger.warning(
                        f"Could not parse citation key pattern from BibTeX: {bibtex[:100]}..."
                    )
                    return bibtex  # Return the original if we couldn't parse it

        # If we've tried all URLs and none worked
        logger.warning(
            f"Failed to fetch BibTeX for key: {dblp_key} after trying multiple URL formats"
        )
        return ""

    except requests.exceptions.Timeout:
        logger.error(f"Timeout fetching BibTeX for {dblp_key} after {REQUEST_TIMEOUT} seconds")
        return f"% Error: Timeout fetching BibTeX for {dblp_key} after {REQUEST_TIMEOUT} seconds"
    except Exception as e:
        logger.error(f"Error fetching BibTeX for {dblp_key}: {str(e)}", exc_info=True)
        return (
            f"% Error: An unexpected error occurred while fetching BibTeX for {dblp_key}: {str(e)}"
        )


def get_venue_info(venue_name: str) -> dict[str, Any]:
    """
    Get information about a publication venue using DBLP venue search API.
    Returns venue name, acronym, type, and DBLP URL.
    """
    logger.info(f"Getting information for venue: {venue_name}")
    try:
        url = "https://dblp.org/search/venue/api"
        params = {"q": venue_name, "format": "json", "h": 1}
        response = requests.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()

        hits = data.get("result", {}).get("hits", {})
        total = int(hits.get("@total", "0"))

        if total > 0:
            hit = hits.get("hit", [])
            if isinstance(hit, list):
                hit = hit[0]

            info = hit.get("info", {})
            return {
                "venue": info.get("venue", ""),
                "acronym": info.get("acronym", ""),
                "type": info.get("type", ""),
                "url": info.get("url", ""),
            }
        else:
            logger.warning(f"No venue found for: {venue_name}")
            return {
                "venue": "",
                "acronym": "",
                "type": "",
                "url": "",
            }
    except Exception as e:
        logger.error(f"Error fetching venue info for {venue_name}: {str(e)}")
        return {
            "venue": "",
            "acronym": "",
            "type": "",
            "url": "",
        }


def calculate_statistics(results: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Calculate statistics from publication results.
    (Documentation omitted for brevity)
    """
    logger.info(f"Calculating statistics for {len(results)} results")
    authors = Counter()
    venues = Counter()
    years = []

    for result in results:
        for author in result.get("authors", []):
            authors[author] += 1

        venue = result.get("venue", "")
        # Handle venue as list or string
        if isinstance(venue, list):
            venue = ", ".join(venue) if venue else ""
        if venue:
            venues[venue] += 1
        else:
            venues["(empty)"] += 1

        year = result.get("year")
        if year:
            with contextlib.suppress(ValueError, TypeError):
                years.append(int(year))

    stats = {
        "total_publications": len(results),
        "time_range": {"min": min(years) if years else None, "max": max(years) if years else None},
        "top_authors": sorted(authors.items(), key=lambda x: x[1], reverse=True),
        "top_venues": sorted(venues.items(), key=lambda x: x[1], reverse=True),
    }

    return stats
