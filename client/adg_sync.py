#!/usr/bin/env python3
"""
adg_sync.py — Sync resolved DNS data to AdGuard Home DNS Rewrites.

Two operating modes:
  online     — Downloads pre-resolved JSON from GitHub, diffs against AdGuard, applies changes.
  standalone — Reads local lists/*.txt, resolves via DoH with majority-vote consensus,
               detects DNS tampering, then syncs to AdGuard.

Usage:
  python adg_sync.py --mode online --lists google,social --dry-run
  python adg_sync.py --mode standalone --force
  python adg_sync.py --config /path/to/config.ini --non-interactive
"""

from __future__ import annotations

import argparse
import configparser
import ipaddress
import json
import logging
import os
import socket
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Optional dependency: dnspython (for DoT resolution)
# ---------------------------------------------------------------------------
try:
    import dns.resolver  # type: ignore[import-untyped]
    import dns.rdatatype  # type: ignore[import-untyped]

    HAS_DNSPYTHON = True
except ImportError:
    HAS_DNSPYTHON = False

try:
    import requests  # type: ignore[import-untyped]
except ImportError:
    sys.exit(
        "Error: 'requests' is required.  Install with:  pip install requests"
    )

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GITHUB_RAW_URL = "https://raw.githubusercontent.com/{repo}/{branch}/resolved/{list_name}.json"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.ini"
VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Terminal colours (ANSI)
# ---------------------------------------------------------------------------

def _supports_color() -> bool:
    """Return True when stdout is a colour-capable terminal."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


_COLOR = _supports_color()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def green(text: str) -> str:
    return _c("32", text)


def red(text: str) -> str:
    return _c("31", text)


def yellow(text: str) -> str:
    return _c("33", text)


def blue(text: str) -> str:
    return _c("34", text)


def bold(text: str) -> str:
    return _c("1", text)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Rewrite:
    """Represents a single AdGuard Home DNS rewrite entry."""

    domain: str
    answer: str  # IPv4, IPv6, or CNAME

    def to_dict(self) -> dict[str, str]:
        return {"domain": self.domain, "answer": self.answer}

    def __hash__(self) -> int:
        return hash((self.domain, self.answer))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Rewrite):
            return NotImplemented
        return self.domain == other.domain and self.answer == other.answer


@dataclass
class SyncPlan:
    """Changes to apply to AdGuard Home."""

    to_add: list[Rewrite] = field(default_factory=list)
    to_delete: list[Rewrite] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.to_add or self.to_delete)

    @property
    def summary(self) -> str:
        return f"+{len(self.to_add)} / -{len(self.to_delete)}"


@dataclass
class ResolvedRecord:
    """A resolved domain with its IPs."""

    domain: str
    ipv4: list[str] = field(default_factory=list)
    ipv6: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class Config:
    """Parsed configuration from config.ini + CLI overrides."""

    # AdGuard Home
    agh_url: str = "http://192.168.1.1:3000"
    agh_username: str = "admin"
    agh_password: str = ""

    # GitHub
    github_repo: str = ""
    github_branch: str = "main"

    # Resolvers (for standalone mode)
    doh_resolvers: list[str] = field(default_factory=list)
    dot_resolvers: list[str] = field(default_factory=list)

    # Lists
    enabled_lists: list[str] = field(default_factory=list)

    # Sync
    cleanup_old: bool = True
    log_file: str = "adg_sync.log"

    @classmethod
    def from_file(cls, path: Path) -> Config:
        """Parse a config.ini file into a Config instance."""
        cfg = configparser.ConfigParser()
        if not path.exists():
            logging.warning("Config file not found: %s — using defaults", path)
            return cls()

        cfg.read(path, encoding="utf-8")
        c = cls()

        # [adguard]
        if cfg.has_section("adguard"):
            c.agh_url = cfg.get("adguard", "url", fallback=c.agh_url).rstrip("/")
            c.agh_username = cfg.get("adguard", "username", fallback=c.agh_username)
            c.agh_password = cfg.get("adguard", "password", fallback="")

        # Environment variable overrides config-file password
        env_password = os.environ.get("AGH_PASSWORD", "")
        if env_password:
            c.agh_password = env_password
        if not c.agh_password:
            c.agh_password = cfg.get("adguard", "password", fallback="")

        # [github]
        if cfg.has_section("github"):
            c.github_repo = cfg.get("github", "repo", fallback="")
            c.github_branch = cfg.get("github", "branch", fallback="main")

        # [resolvers]
        if cfg.has_section("resolvers"):
            for key, value in cfg.items("resolvers"):
                if key.startswith("doh_"):
                    c.doh_resolvers.append(value)
                elif key.startswith("dot_"):
                    c.dot_resolvers.append(value)

        # [lists]
        if cfg.has_section("lists"):
            raw = cfg.get("lists", "enabled", fallback="")
            c.enabled_lists = [s.strip() for s in raw.split(",") if s.strip()]

        # [sync]
        if cfg.has_section("sync"):
            c.cleanup_old = cfg.getboolean("sync", "cleanup_old", fallback=True)
            c.log_file = cfg.get("sync", "log_file", fallback="adg_sync.log")

        return c


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(log_file: str, verbose: bool) -> logging.Logger:
    """Configure file + console logging."""
    logger = logging.getLogger("adg_sync")
    logger.setLevel(logging.DEBUG)

    # File handler — always DEBUG
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s"))
    logger.addHandler(fh)

    # Console handler — INFO or DEBUG depending on --verbose
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(ch)

    return logger


# ---------------------------------------------------------------------------
# AdGuard Home API client
# ---------------------------------------------------------------------------

class AdGuardClient:
    """Thin wrapper around the AdGuard Home DNS-rewrite API."""

    def __init__(self, url: str, username: str, password: str) -> None:
        self.base = url.rstrip("/")
        self.auth = (username, password) if username else None
        self.session = requests.Session()
        if self.auth:
            self.session.auth = self.auth
        self.session.headers.update({"Content-Type": "application/json"})

    # ── read ──────────────────────────────────────────────────────────
    def get_rewrites(self) -> list[Rewrite]:
        """GET /control/rewrite/list → list of current rewrites."""
        resp = self.session.get(f"{self.base}/control/rewrite/list", timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return [Rewrite(domain=r["domain"], answer=r["answer"]) for r in data]

    # ── write ─────────────────────────────────────────────────────────
    def add_rewrite(self, rw: Rewrite) -> None:
        """POST /control/rewrite/add."""
        resp = self.session.post(
            f"{self.base}/control/rewrite/add",
            data=json.dumps(rw.to_dict()),
            timeout=30,
        )
        resp.raise_for_status()

    def delete_rewrite(self, rw: Rewrite) -> None:
        """POST /control/rewrite/delete."""
        resp = self.session.post(
            f"{self.base}/control/rewrite/delete",
            data=json.dumps(rw.to_dict()),
            timeout=30,
        )
        resp.raise_for_status()


# ---------------------------------------------------------------------------
# DNS resolution helpers  (standalone mode)
# ---------------------------------------------------------------------------

def _is_valid_ip(addr: str) -> bool:
    """Return True for syntactically valid IPv4/IPv6 addresses."""
    try:
        ipaddress.ip_address(addr)
        return True
    except ValueError:
        return False


def resolve_system_dns(domain: str) -> list[str]:
    """Resolve *domain* via the OS default resolver."""
    ips: list[str] = []
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            results = socket.getaddrinfo(domain, None, family, socket.SOCK_STREAM)
            ips.extend(r[4][0] for r in results)
        except socket.gaierror:
            pass
    return sorted(set(ips))


def resolve_doh(domain: str, resolver_url: str, qtype: str = "A") -> list[str]:
    """Resolve *domain* via a DoH JSON API endpoint.

    Args:
        domain: The domain to resolve.
        resolver_url: The DoH resolver URL (e.g. https://dns.google/resolve).
        qtype: Query type — ``"A"`` or ``"AAAA"``.

    Returns:
        List of IP address strings.
    """
    params = {"name": domain, "type": qtype}
    headers = {"Accept": "application/dns-json"}
    try:
        resp = requests.get(resolver_url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        answers: list[dict[str, Any]] = data.get("Answer", [])
        target_type = 1 if qtype == "A" else 28
        return [
            a["data"]
            for a in answers
            if a.get("type") == target_type and _is_valid_ip(a["data"])
        ]
    except Exception:  # noqa: BLE001 — best-effort
        return []


def resolve_dot(domain: str, server: str, qtype: str = "A") -> list[str]:
    """Resolve *domain* via DNS-over-TLS using dnspython.

    Args:
        domain: The domain to resolve.
        server: DoT server in ``tls://IP:PORT`` format.
        qtype: ``"A"`` or ``"AAAA"``.

    Returns:
        List of IP address strings.
    """
    if not HAS_DNSPYTHON:
        return []

    # Parse tls://1.1.1.1:853 → host, port
    addr = server.removeprefix("tls://")
    host, _, port_str = addr.partition(":")
    port = int(port_str) if port_str else 853

    try:
        resolver = dns.resolver.Resolver(configure=False)
        resolver.nameservers = [host]
        resolver.port = port
        # dnspython >= 2.3 supports TLS via nameserver_ports
        rdtype = dns.rdatatype.RdataType.A if qtype == "A" else dns.rdatatype.RdataType.AAAA
        answer = resolver.resolve(domain, rdtype, tcp=True, lifetime=10)
        return [rdata.to_text() for rdata in answer]
    except Exception:  # noqa: BLE001
        return []


def majority_vote(results: list[list[str]]) -> list[str]:
    """Return IPs that appear in at least ⌈n/2⌉ of the result lists.

    This implements the same consensus logic as the server-side resolver:
    an IP is accepted only if a majority of resolvers agree on it.
    """
    non_empty = [r for r in results if r]
    if not non_empty:
        return []
    threshold = (len(non_empty) // 2) + 1
    counter: Counter[str] = Counter()
    for ips in non_empty:
        for ip in set(ips):  # deduplicate per-resolver
            counter[ip] += 1
    return sorted(ip for ip, count in counter.items() if count >= threshold)


# ---------------------------------------------------------------------------
# Standalone resolver orchestrator
# ---------------------------------------------------------------------------

def resolve_domain_standalone(
    domain: str,
    config: Config,
    logger: logging.Logger,
    *,
    interactive: bool = True,
    force: bool = False,
) -> ResolvedRecord:
    """Resolve a single domain using configured DoH/DoT resolvers with tamper detection.

    Args:
        domain: Domain to resolve.
        config: Parsed config.
        logger: Logger instance.
        interactive: Whether to prompt the user on tamper detection.
        force: Accept all IPs without prompting.

    Returns:
        A ``ResolvedRecord`` with consensus IPs.
    """
    record = ResolvedRecord(domain=domain)

    for qtype, attr in [("A", "ipv4"), ("AAAA", "ipv6")]:
        resolver_results: list[list[str]] = []

        # DoH resolvers
        for url in config.doh_resolvers:
            ips = resolve_doh(domain, url, qtype)
            resolver_results.append(ips)
            logger.debug("DoH %s %s %s → %s", url, domain, qtype, ips)

        # DoT resolvers (optional)
        for srv in config.dot_resolvers:
            ips = resolve_dot(domain, srv, qtype)
            resolver_results.append(ips)
            logger.debug("DoT %s %s %s → %s", srv, domain, qtype, ips)

        consensus = majority_vote(resolver_results)

        # Tamper detection: compare system DNS with DoH consensus
        if consensus and not force:
            system_ips = resolve_system_dns(domain)
            # Only compare IPs of the same address family
            if qtype == "A":
                system_family = [ip for ip in system_ips if ":" not in ip]
            else:
                system_family = [ip for ip in system_ips if ":" in ip]

            if system_family and set(system_family) != set(consensus):
                _handle_tamper(
                    domain=domain,
                    qtype=qtype,
                    system_ips=system_family,
                    doh_ips=consensus,
                    resolver_results=resolver_results,
                    resolver_names=config.doh_resolvers + config.dot_resolvers,
                    logger=logger,
                    interactive=interactive,
                )
                # In non-interactive mode, tamper → skip these IPs
                if not interactive:
                    logger.warning(
                        "Skipping %s %s %s due to tamper (non-interactive mode)",
                        domain, qtype, consensus,
                    )
                    continue

        setattr(record, attr, consensus)

    return record


def _handle_tamper(
    *,
    domain: str,
    qtype: str,
    system_ips: list[str],
    doh_ips: list[str],
    resolver_results: list[list[str]],
    resolver_names: list[str],
    logger: logging.Logger,
    interactive: bool,
) -> None:
    """Display a tamper warning and optionally prompt the user."""
    logger.warning("DNS tamper detected for %s (%s)", domain, qtype)

    # Build visual warning
    lines = [
        "",
        yellow(f"  ⚠️  TAMPER WARNING for {bold(domain)} ({qtype})"),
        red(f"      Your system DNS: {', '.join(system_ips)}"),
    ]
    for name, ips in zip(resolver_names, resolver_results, strict=False):
        label = name.split("//")[-1].split("/")[0] if "//" in name else name
        lines.append(green(f"      DoH {label}: {', '.join(ips) if ips else '(no answer)'}"))
    lines.append(yellow("      → DNS شما احتمالاً دستکاری شده!"))

    for line in lines:
        print(line)

    if interactive:
        try:
            answer = input(yellow("      → آیا IP از DoH استفاده شود؟ [Y/n] ")).strip().lower()
        except EOFError:
            answer = "y"
        if answer in ("", "y", "yes", "بله"):
            print(green("      ✓ Using DoH result"))
        else:
            print(red("      ✗ Skipping this domain"))
            # Clear the IPs so they won't be synced
            doh_ips.clear()
    print()


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def load_online(config: Config, lists: list[str], logger: logging.Logger) -> list[ResolvedRecord]:
    """Download resolved JSON files from GitHub.

    Args:
        config: Parsed config.
        lists: List names to download.
        logger: Logger instance.

    Returns:
        Flat list of ``ResolvedRecord`` from all requested lists.
    """
    records: list[ResolvedRecord] = []
    for list_name in lists:
        url = GITHUB_RAW_URL.format(
            repo=config.github_repo,
            branch=config.github_branch,
            list_name=list_name,
        )
        logger.info(blue(f"  ℹ️  Downloading {list_name}.json ..."))
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            logger.error(red(f"  ✗ Failed to download {list_name}: {exc}"))
            continue

        # The resolver produces: {"list_name": ..., "domains": {"domain": {"A": [...], "AAAA": [...]}}}
        domains_data = data.get("domains", {})
        for domain, info in domains_data.items():
            rec = ResolvedRecord(
                domain=domain,
                ipv4=info.get("A", []),
                ipv6=info.get("AAAA", []),
            )
            records.append(rec)
        logger.info(green(f"  ✅ {list_name}: {len(domains_data)} domains loaded"))

    return records


def load_standalone(
    config: Config,
    lists: list[str],
    logger: logging.Logger,
    *,
    interactive: bool = True,
    force: bool = False,
) -> list[ResolvedRecord]:
    """Read local lists/*.txt and resolve domains via DoH/DoT.

    Args:
        config: Parsed config.
        lists: List names to process.
        logger: Logger instance.
        interactive: Whether to prompt on tamper.
        force: Accept all IPs without prompting.

    Returns:
        Flat list of ``ResolvedRecord``.
    """
    lists_dir = Path(__file__).resolve().parent.parent / "lists"
    records: list[ResolvedRecord] = []

    for list_name in lists:
        txt_path = lists_dir / f"{list_name}.txt"
        if not txt_path.exists():
            logger.error(red(f"  ✗ List file not found: {txt_path}"))
            continue

        domains = _read_domain_list(txt_path)
        logger.info(blue(f"  ℹ️  Resolving {list_name} ({len(domains)} domains) ..."))

        for domain in domains:
            rec = resolve_domain_standalone(
                domain, config, logger,
                interactive=interactive, force=force,
            )
            if rec.ipv4 or rec.ipv6:
                records.append(rec)
                logger.debug("  Resolved %s → v4=%s v6=%s", domain, rec.ipv4, rec.ipv6)
            else:
                logger.warning(yellow(f"  ⚠️  No IPs for {domain}"))

        logger.info(green(f"  ✅ {list_name}: {len(domains)} domains processed"))

    return records


def _read_domain_list(path: Path) -> list[str]:
    """Read a domain-list file, stripping comments and blanks."""
    domains: list[str] = []
    with open(path, encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            # Handle hosts-file format: "0.0.0.0 domain" or just "domain"
            parts = line.split()
            domain = parts[-1] if parts else ""
            if domain and "." in domain:
                domains.append(domain.lower())
    return domains


# ---------------------------------------------------------------------------
# Sync engine
# ---------------------------------------------------------------------------

def compute_sync_plan(
    desired: list[ResolvedRecord],
    current_rewrites: list[Rewrite],
    cleanup_old: bool,
    logger: logging.Logger,
) -> SyncPlan:
    """Compute the diff between desired state and current AdGuard rewrites.

    The algorithm:
      1. Build a set of all domains managed by our lists (``managed_domains``).
      2. Filter current rewrites to only those whose domain is in ``managed_domains``.
      3. For each desired domain:
         - If a rewrite exists with the same IP → no change.
         - If a rewrite exists with a different IP → delete old, add new.
         - If no rewrite exists → add.
      4. If ``cleanup_old`` is True, delete current rewrites whose domain is
         managed but no longer appears in the desired list.

    Returns:
        A ``SyncPlan`` with additions and deletions.
    """
    plan = SyncPlan()

    # Build desired state: domain → set of IPs
    desired_map: dict[str, set[str]] = {}
    for rec in desired:
        ips: set[str] = set()
        ips.update(rec.ipv4)
        ips.update(rec.ipv6)
        if ips:
            desired_map[rec.domain] = ips

    managed_domains = set(desired_map.keys())

    # Index current rewrites by domain
    current_by_domain: dict[str, set[Rewrite]] = {}
    for rw in current_rewrites:
        if rw.domain in managed_domains or (cleanup_old and _is_valid_ip(rw.answer)):
            current_by_domain.setdefault(rw.domain, set()).add(rw)

    # — Additions & updates —
    for domain, desired_ips in desired_map.items():
        existing = current_by_domain.get(domain, set())
        existing_ips = {rw.answer for rw in existing}

        for ip in desired_ips:
            if ip not in existing_ips:
                plan.to_add.append(Rewrite(domain=domain, answer=ip))

        for rw in existing:
            if rw.answer not in desired_ips:
                plan.to_delete.append(rw)

    # — Cleanup: remove rewrites for domains no longer in lists —
    if cleanup_old:
        for rw in current_rewrites:
            if rw.domain not in managed_domains and rw.domain in current_by_domain:
                plan.to_delete.append(rw)

    # Deduplicate
    plan.to_add = list(set(plan.to_add))
    plan.to_delete = list(set(plan.to_delete))

    logger.debug(
        "Sync plan: %d additions, %d deletions",
        len(plan.to_add), len(plan.to_delete),
    )
    return plan


def apply_sync_plan(
    plan: SyncPlan,
    client: AdGuardClient,
    logger: logging.Logger,
    *,
    dry_run: bool = False,
) -> None:
    """Execute the sync plan against AdGuard Home.

    Args:
        plan: Computed sync plan.
        client: AdGuard API client.
        logger: Logger instance.
        dry_run: If True, only log what would happen.
    """
    if not plan.has_changes:
        logger.info(blue("  ℹ️  No changes needed — AdGuard is up to date"))
        return

    prefix = "[DRY-RUN] " if dry_run else ""

    # Deletions first
    for rw in plan.to_delete:
        logger.info(red(f"  🗑  {prefix}DELETE  {rw.domain} → {rw.answer}"))
        if not dry_run:
            try:
                client.delete_rewrite(rw)
            except requests.RequestException as exc:
                logger.error(red(f"  ✗ Failed to delete {rw.domain}: {exc}"))

    # Additions
    for rw in plan.to_add:
        logger.info(green(f"  ✅ {prefix}ADD     {rw.domain} → {rw.answer}"))
        if not dry_run:
            try:
                client.add_rewrite(rw)
            except requests.RequestException as exc:
                logger.error(red(f"  ✗ Failed to add {rw.domain}: {exc}"))

    logger.info(
        bold(f"\n  Summary: {prefix}{plan.summary}")
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="adg_sync",
        description="Sync DNS rewrite entries to AdGuard Home.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s --mode online --lists google,social --dry-run\n"
            "  %(prog)s --mode standalone --force\n"
            "  %(prog)s --config /etc/adg/config.ini --non-interactive\n"
        ),
    )
    parser.add_argument(
        "--mode",
        choices=["online", "standalone"],
        default="online",
        help="Working mode (default: online)",
    )
    parser.add_argument(
        "--lists",
        type=str,
        default="",
        help="Comma-separated list names (overrides config)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without applying",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose output",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=str(DEFAULT_CONFIG_PATH),
        help=f"Config file path (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Accept all IPs without confirmation",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="For cron: skip suspicious domains, don't prompt",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )
    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point."""
    parser = build_parser()
    args = parser.parse_args()

    # Load config
    config = Config.from_file(Path(args.config))

    # CLI list override
    lists = (
        [s.strip() for s in args.lists.split(",") if s.strip()]
        if args.lists
        else config.enabled_lists
    )
    if not lists:
        parser.error(
            "No lists specified.  Use --lists or set [lists] enabled in config.ini"
        )

    # Setup logging
    logger = setup_logging(config.log_file, args.verbose)
    logger.info(bold(f"\n{'═' * 60}"))
    logger.info(bold(f"  adg_sync v{VERSION}  —  mode={args.mode}"))
    logger.info(bold(f"{'═' * 60}\n"))

    # Validate config
    if args.mode == "online" and not config.github_repo:
        parser.error("Online mode requires [github] repo in config.ini")

    if not config.agh_password:
        logger.warning(
            yellow("  ⚠️  No AdGuard password set.  Use AGH_PASSWORD env var or config.ini")
        )

    # ── Load data ─────────────────────────────────────────────────────
    if args.mode == "online":
        records = load_online(config, lists, logger)
    else:
        if not config.doh_resolvers and not config.dot_resolvers:
            parser.error(
                "Standalone mode requires at least one resolver in [resolvers]"
            )
        records = load_standalone(
            config, lists, logger,
            interactive=not args.non_interactive,
            force=args.force,
        )

    if not records:
        logger.error(red("  ✗ No records loaded — nothing to sync"))
        sys.exit(1)

    logger.info(blue(f"\n  ℹ️  Total records: {len(records)} domains\n"))

    # ── Connect to AdGuard ────────────────────────────────────────────
    client = AdGuardClient(config.agh_url, config.agh_username, config.agh_password)

    try:
        current_rewrites = client.get_rewrites()
    except requests.RequestException as exc:
        logger.error(red(f"  ✗ Cannot connect to AdGuard Home: {exc}"))
        sys.exit(1)

    logger.info(blue(f"  ℹ️  Current AdGuard rewrites: {len(current_rewrites)}"))

    # ── Compute & apply diff ──────────────────────────────────────────
    plan = compute_sync_plan(records, current_rewrites, config.cleanup_old, logger)

    if not plan.has_changes:
        logger.info(green("\n  ✅ Everything is up to date — no changes needed\n"))
        return

    logger.info(
        bold(f"\n  Planned changes: {plan.summary}")
    )
    if args.dry_run:
        logger.info(yellow("  (dry-run mode — no changes will be applied)\n"))

    apply_sync_plan(plan, client, logger, dry_run=args.dry_run)

    logger.info(green("\n  ✅ Sync complete\n"))


if __name__ == "__main__":
    main()
