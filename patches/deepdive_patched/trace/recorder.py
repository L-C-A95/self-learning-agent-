"""Append-only event recorder + provider/search/scraper wrappers."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from actants.llm.base import (
    BaseLLMProvider,
    ChatMessage,
    CompletionResult,
    StreamEvent,
    ToolSpec,
)
from pydantic import BaseModel

from deepdive.models import ScrapedPage, SearchResult


def _key_messages(messages: list[ChatMessage]) -> str:
    """Stable key for a list of messages — used to look up recorded responses on replay."""
    items = []
    for m in messages:
        items.append(
            {
                "role": m.role,
                "content": m.content,
                "tool_calls": [tc.model_dump() for tc in (m.tool_calls or [])],
                "tool_call_id": m.tool_call_id,
            }
        )
    return json.dumps(items, sort_keys=True, default=str)


class TraceRecorder:
    """Append-only JSONL recorder. Flushes per write so a crash mid-research
    still leaves a usable partial trace."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", encoding="utf-8")
        self._write({"type": "trace_start", "ts": time.time()})

    def _write(self, obj: dict[str, Any]) -> None:
        self._fh.write(json.dumps(obj, default=str) + "\n")
        self._fh.flush()

    def record_llm_call(
        self, messages: list[ChatMessage], model: str, result: CompletionResult
    ) -> None:
        self._write(
            {
                "type": "llm_call",
                "ts": time.time(),
                "key": _key_messages(messages),
                "model": model,
                "result": result.model_dump(),
            }
        )

    def record_search(self, query: str, max_results: int, results: list[SearchResult]) -> None:
        self._write(
            {
                "type": "search",
                "ts": time.time(),
                "key": json.dumps({"query": query, "max_results": max_results}),
                "results": [r.model_dump() for r in results],
            }
        )

    def record_scrape(self, url: str, page: ScrapedPage | None) -> None:
        self._write(
            {
                "type": "scrape",
                "ts": time.time(),
                "key": url,
                "page": page.model_dump() if page else None,
            }
        )

    def record_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Record a high-level pipeline event (start/queries_generated/etc.)."""
        self._write({"type": "event", "ts": time.time(), "event_type": event_type, "data": data})

    def close(self) -> None:
        if not self._fh.closed:
            self._write({"type": "trace_end", "ts": time.time()})
            self._fh.close()

    def __enter__(self) -> TraceRecorder:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class _RecordingProvider(BaseLLMProvider):
    """Wraps a real provider and records every complete()/stream_events() call."""

    def __init__(self, inner: BaseLLMProvider, recorder: TraceRecorder) -> None:
        self._inner = inner
        self._recorder = recorder
        self.name = f"recording({inner.name})"
        self.supports_tool_calls = inner.supports_tool_calls
        self.supports_streaming_tools = getattr(inner, "supports_streaming_tools", False)

    async def complete(
        self,
        messages: list[ChatMessage],
        model: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        *,
        tools: list[ToolSpec] | None = None,
        **kwargs: Any,
    ) -> CompletionResult:
        result = await self._inner.complete(
            messages, model, temperature, max_tokens, tools=tools, **kwargs
        )
        self._recorder.record_llm_call(messages, model, result)
        return result

    async def stream(
        self,
        messages: list[ChatMessage],
        model: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        # Stream-only path is not used by the pipeline today, but pass through.
        async for chunk in self._inner.stream(messages, model, temperature, max_tokens, **kwargs):
            yield chunk

    async def stream_events(
        self,
        messages: list[ChatMessage],
        model: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        *,
        tools: list[ToolSpec] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        # Buffer the full stream, then record it as a single complete() result so
        # replay is symmetric with non-streaming calls.
        from actants.llm.base import (
            FinishDelta,
            TextDelta,
            TokenUsage,
            ToolCallDelta,
            UsageDelta,
        )

        text_parts: list[str] = []
        tool_calls = []
        usage = TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)
        async for event in self._inner.stream_events(
            messages, model, temperature, max_tokens, tools=tools, **kwargs
        ):
            if isinstance(event, TextDelta):
                text_parts.append(event.text)
            elif isinstance(event, ToolCallDelta):
                tool_calls.append(event.tool_call)
            elif isinstance(event, UsageDelta):
                usage = event.usage
            yield event
            if isinstance(event, FinishDelta):
                pass
        result = CompletionResult(
            content="".join(text_parts),
            model=model,
            provider=self._inner.name,
            usage=usage,
            tool_calls=tool_calls,
        )
        self._recorder.record_llm_call(messages, model, result)

    async def health(self) -> bool:
        return await self._inner.health()


def record_provider(provider: BaseLLMProvider, recorder: TraceRecorder) -> BaseLLMProvider:
    """Wrap a provider so every LLM call is appended to ``recorder``."""
    return _RecordingProvider(provider, recorder)


class _SearchProtocol:
    """Anything DeepDive uses as a search backend has this shape."""

    async def search(self, query: str, max_results: int) -> list[SearchResult]: ...


class RecordingSearch:
    """Wraps a search backend; records every search call to the trace."""

    def __init__(self, inner: _SearchProtocol, recorder: TraceRecorder) -> None:
        self._inner = inner
        self._recorder = recorder

    def __getattr__(self, name: str):
        """Delegate optional capabilities to the wrapped backend.

        The CLI enables tracing automatically whenever ``-o`` is passed, so this
        wrapper is on the hot path by default. Without delegation, features that
        live on the concrete backend — like LocalCorpusClient.precheck, which
        refuses off-corpus questions — are silently invisible to the pipeline.
        """
        if name in ("_inner", "_recorder"):
            raise AttributeError(name)
        return getattr(self._inner, name)

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        results = await self._inner.search(query, max_results=max_results)
        self._recorder.record_search(query, max_results, results)
        return results


class _ScraperProtocol:
    async def fetch(self, url: str) -> ScrapedPage | None: ...


class RecordingScraper:
    """Wraps a scraper; records every fetched page to the trace."""

    def __init__(self, inner: _ScraperProtocol, recorder: TraceRecorder) -> None:
        self._inner = inner
        self._recorder = recorder

    async def fetch(self, url: str) -> ScrapedPage | None:
        page = await self._inner.fetch(url)
        self._recorder.record_scrape(url, page)
        return page


# Re-export the message-key helper so the replayer can use it consistently.
key_messages = _key_messages


# Make sure pydantic models that we serialize round-trip cleanly. (No-op import
# to surface any model-load errors at module import time rather than mid-research.)
_ = (BaseModel, CompletionResult, ScrapedPage, SearchResult)
