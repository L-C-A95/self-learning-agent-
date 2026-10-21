"""Search backend that reads from a local corpus index.

Implements the same ``async search(query, max_results)`` interface as
``DuckDuckGoClient`` and ``SearxNGClient`` so it drops into the pipeline
unchanged. Each chunk becomes a SearchResult with a ``deepdive-corpus://``
URL and the chunk text as the snippet — the scraper layer recognizes this
scheme and returns the chunk verbatim instead of fetching the network.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from urllib.parse import quote

import structlog

from deepdive.corpus.indexer import CorpusIndex
from deepdive.models import ScrapedPage, SearchResult

log = structlog.get_logger(__name__)

# Below this cosine similarity the chunk is treated as "not about this question".
# Calibration on bge-m3 + a 2-paper corpus: genuinely relevant hits scored
# 0.585-0.595, unrelated ones 0.429-0.493. 0.52 sits in the gap. Override with
# DEEPDIVE_MIN_SIM (set 0 to disable and get the old "always return top-k").
MIN_SIM = float(os.environ.get("DEEPDIVE_MIN_SIM", "0.52"))


def _corpus_url(source_path: str, offset: int) -> str:
    """Stable, parseable URL for an in-corpus chunk."""
    return f"http://localhost/corpus/{quote(source_path, safe='')}#offset={offset}"


class LocalCorpusClient:
    """Cosine-similarity search over a sqlite-vec corpus."""

    def __init__(self, index: CorpusIndex) -> None:
        self.index = index

    async def precheck(self, question: str) -> tuple[bool, float | None]:
        """Is the ORIGINAL question anywhere near this corpus?

        The pipeline rewrites the question into several search queries before
        searching, and a rewritten query can drift onto an unrelated topic and
        score above the threshold. Gating on the user's own wording closes that
        hole: if the question itself is off-corpus, no amount of query
        paraphrasing should turn it into an answer.
        """
        hits = await self.index.search(question, k=1)
        if not hits:
            return False, None
        best = hits[0].similarity
        if best is None:
            return True, None
        return best >= MIN_SIM, best

    async def search(self, query: str, *, max_results: int = 5) -> list[SearchResult]:
        hits = await self.index.search(query, k=max_results, min_similarity=MIN_SIM)
        if not hits:
            # Ask for the unfiltered top-1 purely so the log can say how far off
            # we were — "0.47 < 0.52" is actionable, "no results" is not.
            probe = await self.index.search(query, k=1)
            best = f"{probe[0].similarity:.3f}" if probe and probe[0].similarity else "n/a"
            log.warning(
                "corpus_no_relevant_chunks",
                query=query,
                best_similarity=best,
                threshold=MIN_SIM,
            )
            return []
        out: list[SearchResult] = []
        for h in hits:
            url = _corpus_url(h.source_path, h.offset_start)
            title = Path(h.source_path).name
            snippet = h.text[:300]
            try:
                out.append(SearchResult(url=url, title=title, snippet=snippet, source="corpus"))
            except Exception:
                continue
        return out


class LocalCorpusScraper:
    """Scraper that resolves ``deepdive-corpus://`` URLs to ScrapedPage objects.

    For non-corpus URLs, delegates to the inner scraper (or returns None when
    operating in strict offline mode and no inner scraper was provided).
    """

    def __init__(
        self,
        index: CorpusIndex,
        inner=None,
        *,
        offline: bool = False,
    ) -> None:
        self.index = index
        self.inner = inner
        self.offline = offline

    async def fetch(self, url: str) -> ScrapedPage | None:
        if "/corpus/" in url and "localhost" in url:
            # Re-parse to find the chunk by its source_path + offset.
            return await self._fetch_corpus(url)
        if self.offline:
            return None
        if self.inner is not None:
            return await self.inner.fetch(url)
        return None

    async def _fetch_corpus(self, url: str) -> ScrapedPage | None:
        from urllib.parse import unquote, urlparse

        parsed = urlparse(url)
        # Path format: /corpus/<encoded source_path>
        if not parsed.path.startswith("/corpus/"):
            return None
        source_path = unquote(parsed.path[len("/corpus/") :])
        # Offset is in the fragment as `offset=N`
        offset = 0
        if parsed.fragment.startswith("offset="):
            with contextlib.suppress(ValueError):
                offset = int(parsed.fragment.split("=", 1)[1])
        # Look the chunk up directly so we get exactly the indexed text.
        cur = self.index.conn.execute(
            "SELECT c.text, p.page_no FROM chunks c "
            "JOIN documents d ON d.doc_id = c.doc_id "
            "LEFT JOIN pages p ON p.doc_id = c.doc_id "
            " AND c.offset_start >= p.start_offset AND c.offset_start < p.end_offset "
            "WHERE d.source_path = ? AND c.offset_start = ?",
            (source_path, offset),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return ScrapedPage(
            url=url,
            title=Path(source_path).name,
            text=row[0],
            page=row[1],
        )
