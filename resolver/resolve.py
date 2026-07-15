#!/usr/bin/env python3
"""
DNS Resolver Script for adg-dnslookup

Resolves domains from list files using three DoH (DNS-over-HTTPS) resolvers:
  - Cloudflare (https://cloudflare-dns.com/dns-query)
  - Google     (https://dns.google/resolve)
  - Quad9      (https://dns.quad9.net:5053/dns-query)

Results are validated via majority-vote consensus and saved as JSON files
in the output directory.

Usage:
    python resolver/resolve.py
    python resolver/resolve.py --lists google cloudflare
    python resolver/resolve.py --output-dir custom_output/
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RESOLVERS: dict[str, dict[str, Any]] = {
    "cloudflare": {
        "url": "https://cloudflare-dns.com/dns-query",
        "headers": {"Accept": "application/dns-json"},
        "params_fn": lambda domain, qtype: {"name": domain, "type": qtype},
    },
    "google": {
        "url": "https://dns.google/resolve",
        "headers": {},  # Google's JSON API does not require the dns-json header
        "params_fn": lambda domain, qtype: {"name": domain, "type": qtype},
    },
    "quad9": {
        "url": "https://dns.quad9.net:5053/dns-query",
        "headers": {"Accept": "application/dns-json"},
        "params_fn": lambda domain, qtype: {"name": domain, "type": qtype},
    },
}

# Record type codes used in DNS JSON responses
RECORD_TYPE_A = 1
RECORD_TYPE_AAAA = 28

MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1.0  # seconds — exponential backoff multiplier
REQUEST_TIMEOUT = 10  # seconds
INTER_REQUEST_DELAY = 0.1  # seconds between requests to the same resolver
MAX_WORKERS = 5  # concurrent resolution threads

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("resolve")

# ---------------------------------------------------------------------------
# Helper: per-resolver rate-limit timestamps
# ---------------------------------------------------------------------------

# Tracks the last request timestamp per resolver so we can throttle.
_last_request_time: dict[str, float] = {}


def _throttle(resolver_name: str) -> None:
    """Enforce a minimum delay between consecutive requests to the same resolver."""
    now = time.monotonic()
    last = _last_request_time.get(resolver_name, 0.0)
    wait = INTER_REQUEST_DELAY - (now - last)
    if wait > 0:
        time.sleep(wait)
    _last_request_time[resolver_name] = time.monotonic()


# ---------------------------------------------------------------------------
# Core: query a single DoH resolver
# ---------------------------------------------------------------------------


def _query_doh(
    resolver_name: str,
    domain: str,
    qtype: str,
) -> list[str]:
    """Query a DoH resolver for *qtype* records of *domain*.

    Args:
        resolver_name: Key into ``RESOLVERS`` (e.g. ``"cloudflare"``).
        domain: The domain name to resolve.
        qtype: ``"A"`` or ``"AAAA"``.

    Returns:
        Sorted list of IP address strings. Empty list on failure.
    """
    cfg = RESOLVERS[resolver_name]
    params = cfg["params_fn"](domain, qtype)
    headers = cfg["headers"]
    url = cfg["url"]

    expected_rtype = RECORD_TYPE_A if qtype == "A" else RECORD_TYPE_AAAA

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _throttle(resolver_name)
            resp = requests.get(
                url,
                params=params,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()

            # Extract answer records matching the requested type.
            answers: list[str] = []
            for answer in data.get("Answer", []):
                if answer.get("type") == expected_rtype:
                    answers.append(answer["data"])
            return sorted(answers)

        except requests.RequestException as exc:
            backoff = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
            logger.warning(
                "%s query %s/%s attempt %d/%d failed: %s — retrying in %.1fs",
                resolver_name,
                domain,
                qtype,
                attempt,
                MAX_RETRIES,
                exc,
                backoff,
            )
            time.sleep(backoff)
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning(
                "%s returned unexpected payload for %s/%s: %s",
                resolver_name,
                domain,
                qtype,
                exc,
            )
            return []

    logger.error(
        "%s exhausted retries for %s/%s",
        resolver_name,
        domain,
        qtype,
    )
    return []


# ---------------------------------------------------------------------------
# Core: resolve a single domain across all resolvers
# ---------------------------------------------------------------------------


def _resolve_domain(domain: str) -> dict[str, Any] | None:
    """Resolve *domain* via all configured DoH resolvers.

    Returns a result dict with consensus IPs and per-resolver details,
    or ``None`` if **every** resolver failed for **both** record types.
    """
    resolver_results: dict[str, dict[str, list[str]]] = {}

    for rname in RESOLVERS:
        a_records = _query_doh(rname, domain, "A")
        aaaa_records = _query_doh(rname, domain, "AAAA")
        resolver_results[rname] = {"A": a_records, "AAAA": aaaa_records}

    # Check if all resolvers failed completely (no records at all).
    all_empty = all(
        not rr["A"] and not rr["AAAA"] for rr in resolver_results.values()
    )
    if all_empty:
        return None

    # --- Majority-vote consensus ---
    consensus_a, consensus_a_flag = _majority_vote(
        {rname: rr["A"] for rname, rr in resolver_results.items()}
    )
    consensus_aaaa, consensus_aaaa_flag = _majority_vote(
        {rname: rr["AAAA"] for rname, rr in resolver_results.items()}
    )
    consensus = consensus_a_flag and consensus_aaaa_flag

    return {
        "A": consensus_a,
        "AAAA": consensus_aaaa,
        "consensus": consensus,
        "resolver_results": resolver_results,
    }


def _majority_vote(
    results: dict[str, list[str]],
) -> tuple[list[str], bool]:
    """Determine the consensus IP set via majority vote.

    Args:
        results: Mapping of resolver name → sorted list of IPs.

    Returns:
        A tuple of (chosen IP list, whether consensus was reached).
        Consensus is ``True`` when at least 2 of 3 resolvers agree on the
        exact same IP set (order-insensitive comparison via frozensets).
    """
    # Convert each IP list to a frozenset for comparison.
    votes: list[tuple[str, frozenset[str]]] = [
        (rname, frozenset(ips)) for rname, ips in results.items()
    ]

    # Count how often each unique IP set appears.
    set_counter: Counter[frozenset[str]] = Counter(fs for _, fs in votes)

    most_common_set, count = set_counter.most_common(1)[0]
    consensus_reached = count >= 2

    return sorted(most_common_set), consensus_reached


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def read_domains(list_file: Path) -> list[str]:
    """Read domains from a list file, ignoring comments and blank lines.

    Args:
        list_file: Path to a ``.txt`` file with one domain per line.

    Returns:
        List of stripped, lowercased domain strings.
    """
    domains: list[str] = []
    with open(list_file, encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            domains.append(line.lower())
    return domains


def save_results(
    list_name: str,
    domains_result: dict[str, Any],
    output_dir: Path,
    resolved_at: str,
) -> Path:
    """Persist resolution results as a JSON file.

    Args:
        list_name: Logical name of the list (filename stem).
        domains_result: Mapping of domain → result dict.
        output_dir: Directory for output JSON files.
        resolved_at: ISO-8601 timestamp string.

    Returns:
        Path to the written JSON file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{list_name}.json"

    payload = {
        "list_name": list_name,
        "resolved_at": resolved_at,
        "resolver_sources": list(RESOLVERS.keys()),
        "domains": domains_result,
    }

    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    return output_path


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------


def resolve_list(list_file: Path, output_dir: Path) -> None:
    """Resolve every domain in *list_file* and save results.

    Uses a thread pool (``MAX_WORKERS`` threads) for parallel resolution
    while still respecting per-resolver rate limits.
    """
    list_name = list_file.stem
    domains = read_domains(list_file)
    if not domains:
        logger.warning("No domains found in %s — skipping", list_file)
        return

    logger.info(
        "Resolving %d domains from list '%s' …",
        len(domains),
        list_name,
    )

    resolved_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    domains_result: dict[str, Any] = {}
    failed_count = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        future_to_domain = {
            pool.submit(_resolve_domain, domain): domain for domain in domains
        }

        for future in as_completed(future_to_domain):
            domain = future_to_domain[future]
            try:
                result = future.result()
            except Exception:
                logger.exception("Unexpected error resolving %s", domain)
                result = None

            if result is None:
                logger.warning(
                    "All resolvers failed for %s — skipping", domain
                )
                failed_count += 1
                continue

            domains_result[domain] = result

    out = save_results(list_name, domains_result, output_dir, resolved_at)
    logger.info(
        "List '%s' done — %d resolved, %d failed → %s",
        list_name,
        len(domains_result),
        failed_count,
        out,
    )


def discover_lists(
    lists_dir: Path,
    selected: list[str] | None = None,
) -> list[Path]:
    """Find list files in *lists_dir*, optionally filtered by name.

    Args:
        lists_dir: Directory containing ``*.txt`` list files.
        selected: If provided, only return lists whose stems match.

    Returns:
        Sorted list of ``Path`` objects.
    """
    if not lists_dir.is_dir():
        logger.error("Lists directory does not exist: %s", lists_dir)
        sys.exit(1)

    all_lists = sorted(lists_dir.glob("*.txt"))
    if not all_lists:
        logger.error("No .txt files found in %s", lists_dir)
        sys.exit(1)

    if selected:
        selected_set = {s.lower() for s in selected}
        filtered = [p for p in all_lists if p.stem.lower() in selected_set]
        if not filtered:
            logger.error(
                "None of the requested lists %s found in %s",
                selected,
                lists_dir,
            )
            sys.exit(1)
        return filtered

    return all_lists


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Resolve domains via DoH and save consensus results.",
    )
    parser.add_argument(
        "--lists",
        nargs="+",
        metavar="NAME",
        help="Resolve only the specified list(s) by filename stem (e.g. 'google').",
    )
    parser.add_argument(
        "--output-dir",
        default="resolved",
        help="Directory for output JSON files (default: resolved/).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Entry point: discover lists, resolve domains, write results."""
    args = parse_args(argv)

    # Resolve paths relative to the project root (parent of resolver/).
    project_root = Path(__file__).resolve().parent.parent
    lists_dir = project_root / "lists"
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = project_root / output_dir

    logger.info("Project root : %s", project_root)
    logger.info("Lists dir    : %s", lists_dir)
    logger.info("Output dir   : %s", output_dir)

    list_files = discover_lists(lists_dir, args.lists)
    logger.info("Lists to resolve: %s", [p.stem for p in list_files])

    for lf in list_files:
        resolve_list(lf, output_dir)

    logger.info("All done ✓")


if __name__ == "__main__":
    main()
