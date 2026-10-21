"""Source-restriction filter: allow/block lists of hostnames.

Wraps any search backend and discards results whose URL doesn't match the
caller's policy. Domain matching is hostname-based with bare-suffix and
wildcard support:

    "pubmed.ncbi.nlm.nih.gov"  matches that exact host
    "ncbi.nlm.nih.gov"         matches host AND any subdomain
    "*.gov"                    matches any host whose suffix ends in .gov
    "gov"                      same as "*.gov" (bare TLD)

To keep N results after aggressive filtering, the wrapper over-fetches by a
configurable factor and stops once the cap is reached.

DeepDive ships the *mechanism* — no opinionated bundled lists. Bring your own
allow/block sets via the CLI flags ``--allow-domains`` / ``--block-domains``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from deepdive.models import SearchResult


@dataclass(frozen=True)
class SourceFilter:
    """Allow/block policy for search results.

    ``allow`` is a whitelist: when set, results are kept only if their host
    matches at least one entry. ``block`` is a blacklist applied after the
    whitelist. Both empty = pass-through.
    """

    allow: tuple[str, ...] = ()
    block: tuple[str, ...] = ()

    def keep(self, url: str) -> bool:
        host = _hostname(url)
        if not host:
            return False
        if self.allow and not any(_match(host, entry) for entry in self.allow):
            return False
        if self.block:
            low = url.lower()
            for entry in self.block:
                # Entries containing "/" are treated as URL substring patterns,
                # so `arxiv.org/search` or `wappoc_appmsg` can be blocked without
                # banning the whole host.
                if "/" in entry or entry.lower() in ("wappoc_appmsg", "captcha"):
                    if entry.lower().lstrip("/") in low:
                        return False
                elif _match(host, entry):
                    return False
        return True


@dataclass
class _SearchProtocol:
    """Anything DeepDive uses as a search backend."""

    async def search(self, query: str, max_results: int) -> list[SearchResult]: ...


@dataclass
class FilteringSearch:
    """Search backend wrapper that applies a SourceFilter to results.

    Over-fetches by ``oversample`` to compensate for filtering, then truncates
    to the requested ``max_results``.
    """

    inner: _SearchProtocol
    filter: SourceFilter
    oversample: int = 3

    rejected: list[str] = field(default_factory=list)
    """URLs the filter dropped on the most recent search; useful for audit/debug."""

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        # Pull more than we need so filtering doesn't starve the result set.
        fetch_n = max(max_results * self.oversample, max_results + 10)
        raw = await self.inner.search(query, max_results=fetch_n)
        kept: list[SearchResult] = []
        rejected: list[str] = []
        for r in raw:
            if self.filter.keep(str(r.url)):
                kept.append(r)
            else:
                rejected.append(str(r.url))
            if len(kept) >= max_results:
                break
        self.rejected = rejected
        return kept


def _hostname(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _match(host: str, entry: str) -> bool:
    """Match a hostname against a filter entry.

    Rules:
      - "*.gov" or "gov" — match any host whose dotted suffix ends in ``.gov``
      - "ncbi.nlm.nih.gov" — match the host or any of its subdomains
    """
    e = entry.lower().lstrip(".")
    if e.startswith("*."):
        e = e[2:]
    if not e:
        return False
    return host == e or host.endswith("." + e)
