from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from actants import LLM

from deepdive.analysis.claims import ClaimExtractor, cross_reference
from deepdive.config import DeepDiveConfig
from deepdive.models import Claim, ResearchReport, ScrapedPage, SearchResult
from deepdive.offline import assert_loopback
from deepdive.report.builder import ReportBuilder
from deepdive.scraper.fetch import Scraper
from deepdive.search.filters import FilteringSearch, SourceFilter
from deepdive.search.query_gen import generate_queries
from deepdive.trace import (
    RecordingScraper,
    RecordingSearch,
    TraceRecorder,
    record_provider,
)

EventType = Literal[
    "start",
    "queries_generated",
    "search_result",
    "page_scraped",
    "claims_extracted",
    "report_section",
    "done",
    "error",
]


@dataclass
class ResearchEvent:
    type: EventType
    data: dict


def _make_search_client(config: DeepDiveConfig):
    if config.search_backend == "corpus":
        if not config.corpus_path:
            raise ValueError(
                "search_backend='corpus' requires DEEPDIVE_CORPUS_PATH or "
                "--corpus to point at a sqlite-vec index built with "
                "`deepdive index`."
            )
        from deepdive.corpus.indexer import CorpusIndex
        from deepdive.search.local_corpus import LocalCorpusClient

        # Note: caller is responsible for the index lifecycle in tests; here we
        # create a long-lived index that stays open for the pipeline's life.
        index = CorpusIndex(config.corpus_path)
        index._conn = index._open()  # eager open
        return LocalCorpusClient(index)
    if config.search_backend == "searxng":
        from deepdive.search.searxng import SearxNGClient

        return SearxNGClient(base_url=config.searxng_base_url)
    from deepdive.search.duckduckgo import DuckDuckGoClient

    return DuckDuckGoClient()


class ResearchPipeline:
    """Runs the full question → queries → search → scrape → analyze → report loop."""

    def __init__(
        self,
        config: DeepDiveConfig | None = None,
        llm: LLM | None = None,
        search_client: object | None = None,
        scraper: Scraper | None = None,
        *,
        ground: bool | None = None,
        trace_path: str | Path | None = None,
        source_filter: SourceFilter | None = None,
    ) -> None:
        self.config = config or DeepDiveConfig()
        self.llm = llm or LLM(model=self.config.llm_model)
        self.search_client = search_client or _make_search_client(self.config)
        self.scraper = scraper or Scraper(timeout=self.config.scrape_timeout_seconds)
        # Per-run override beats config; default falls back to config.
        self.ground = ground if ground is not None else self.config.ground_citations

        # Corpus scraper: when search_backend is "corpus", URLs returned have
        # the form http://localhost/corpus/... — route those to the corpus
        # store, and delegate everything else to the web scraper.
        if self.config.search_backend == "corpus" and self.config.corpus_path:
            from deepdive.corpus.indexer import CorpusIndex
            from deepdive.search.local_corpus import LocalCorpusScraper

            inner_index = getattr(self.search_client, "index", None)
            if inner_index is None:
                inner_index = CorpusIndex(self.config.corpus_path)
                inner_index._conn = inner_index._open()
            self.scraper = LocalCorpusScraper(
                inner_index, inner=self.scraper, offline=self.config.offline
            )

        # Offline mode: enforce that the LLM endpoint is loopback at construction
        # time so we fail fast rather than mid-run. Scraper-side enforcement is
        # wired below so non-loopback fetches are dropped (unless we already
        # wrapped with a corpus scraper above, which enforces offline itself).
        if self.config.offline:
            assert_loopback(self.config.llm_base_url, what="LLM endpoint")
            from deepdive.search.local_corpus import LocalCorpusScraper

            if not isinstance(self.scraper, LocalCorpusScraper):
                self.scraper = _OfflineScraper(self.scraper)

        # Optional source-restriction filter wraps the search backend.
        # Applied before recording so the trace captures only the filtered set.
        if source_filter is not None:
            self.search_client = FilteringSearch(self.search_client, source_filter)

        # Optional trace recording. Wraps llm/search/scraper so every external
        # call is appended to the trace, enabling byte-identical offline replay.
        self._recorder: TraceRecorder | None = None
        if trace_path is not None:
            self._recorder = TraceRecorder(trace_path)
            # Capture config fields that affect prompt-building so a replay can
            # reconstruct the same prompts. Without this, a replay run with a
            # different default queries_per_question would generate a different
            # prompt and miss the recorded llm_call key.
            self._recorder.record_event(
                "config",
                {
                    "queries_per_question": self.config.queries_per_question,
                    "results_per_query": self.config.results_per_query,
                    "max_pages_per_research": self.config.max_pages_per_research,
                    "ground_citations": self.config.ground_citations,
                    "include_ungrounded": self.config.include_ungrounded,
                    "llm_model": self.config.llm_model,
                },
            )
            self.llm.provider = record_provider(self.llm.provider, self._recorder)
            self.search_client = RecordingSearch(self.search_client, self._recorder)
            self.scraper = RecordingScraper(self.scraper, self._recorder)

    async def run(self, question: str) -> AsyncIterator[ResearchEvent]:
        """Run the full pipeline. If trace recording is on, every event is
        mirrored to the recorder and the trace file is finalized on exit."""
        try:
            async for event in self._run_inner(question):
                if self._recorder is not None:
                    self._recorder.record_event(event.type, event.data)
                yield event
        finally:
            if self._recorder is not None:
                self._recorder.close()

    async def _run_inner(self, question: str) -> AsyncIterator[ResearchEvent]:
        yield ResearchEvent("start", {"question": question})

        try:
            queries = await generate_queries(
                question, n=self.config.queries_per_question, llm=self.llm
            )
        except Exception as exc:
            yield ResearchEvent(
                "error",
                {"stage": "query_generation", "message": f"{type(exc).__name__}: {exc}"},
            )
            return
        yield ResearchEvent(
            "queries_generated",
            {"queries": [q.text for q in queries]},
        )

        # Corpus backends: check the user's own question against the index before
        # trusting any rewritten query. Otherwise an off-corpus question gets
        # paraphrased into something that scores above the relevance threshold,
        # and the report is built on chunks that merely look close enough.
        gate_ok = True
        if hasattr(self.search_client, "precheck"):
            try:
                gate_ok, best = await self.search_client.precheck(question)
            except Exception as exc:  # never let the gate itself break a run
                gate_ok = True
                best = None
                yield ResearchEvent(
                    "error",
                    {"stage": "relevance_gate", "message": f"{type(exc).__name__}: {exc}"},
                )
            else:
                if not gate_ok:
                    yield ResearchEvent(
                        "error",
                        {
                            "stage": "relevance_gate",
                            "message": "no chunk in the corpus is close enough to the "
                            f"question (best cosine similarity {best:.3f})"
                            if best is not None
                            else "the corpus is empty",
                        },
                    )

        seen_urls: set[str] = set()
        all_results: list[SearchResult] = []
        for q in queries:
            if not gate_ok:
                break
            try:
                results = await self.search_client.search(
                    q.text, max_results=self.config.results_per_query
                )
            except Exception as exc:
                yield ResearchEvent(
                    "error",
                    {"stage": "search", "query": q.text, "message": str(exc)},
                )
                continue
            for r in results:
                url = str(r.url)
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                all_results.append(r)
                yield ResearchEvent(
                    "search_result",
                    {"url": url, "title": r.title, "query": q.text},
                )
            if len(all_results) >= self.config.max_pages_per_research:
                break

        urls_to_scrape = [str(r.url) for r in all_results[: self.config.max_pages_per_research]]
        pages: list[ScrapedPage] = []
        for url in urls_to_scrape:
            page = await self.scraper.fetch(url)
            if page is not None:
                pages.append(page)
                yield ResearchEvent("page_scraped", {"url": url, "chars": len(page.text)})

        extractor = ClaimExtractor(llm=self.llm, ground=self.ground)
        all_claims: list[Claim] = []
        for page in pages:
            # Per-page isolation: a timeout or LLM error on one page must not
            # abort the entire research run. Surface the error as an event and
            # continue. Without this, a single slow source kills 30 minutes of work.
            try:
                claims = await extractor.extract(page)
            except Exception as exc:
                yield ResearchEvent(
                    "error",
                    {
                        "stage": "claim_extraction",
                        "url": str(page.url),
                        "message": f"{type(exc).__name__}: {exc}",
                    },
                )
                continue
            if self.ground and not self.config.include_ungrounded:
                claims = [c for c in claims if c.grounded]
            all_claims.extend(claims)
            yield ResearchEvent(
                "claims_extracted",
                {"url": str(page.url), "count": len(claims)},
            )

        merged = cross_reference(all_claims)

        builder = ReportBuilder(llm=self.llm)
        try:
            report: ResearchReport = await builder.build(question, merged)
        except Exception as exc:
            yield ResearchEvent(
                "error",
                {"stage": "report", "message": f"{type(exc).__name__}: {exc}"},
            )
            return
        for section in report.sections:
            yield ResearchEvent(
                "report_section",
                {"heading": section.heading, "body": section.body},
            )

        yield ResearchEvent(
            "done",
            {"report": report.model_dump(mode="json")},
        )


class _OfflineScraper:
    """Wrapper that drops fetches for non-loopback URLs in offline mode."""

    def __init__(self, inner) -> None:
        self.inner = inner

    async def fetch(self, url: str) -> ScrapedPage | None:
        from deepdive.offline import is_loopback

        if not is_loopback(url):
            return None
        return await self.inner.fetch(url)
