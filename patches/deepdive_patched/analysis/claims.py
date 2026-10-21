from __future__ import annotations

import structlog
from actants import LLM
from pydantic import BaseModel, Field

from deepdive.analysis.grounding import (
    GROUNDING_INSTRUCTIONS,
    _GroundedClaimList,
    ground_claim,
)
from deepdive.analysis.support import check_support, looks_like_table_fragment
from deepdive.models import Citation, Claim, ScrapedPage

log = structlog.get_logger(__name__)

_INSTRUCTIONS = """Extract the {max_claims} most important verifiable factual claims
from the text below. Each claim must be a self-contained statement.

Text:
{text}"""


class _ClaimList(BaseModel):
    claims: list[str] = Field(default_factory=list)


def _truncate(text: str, max_chars: int = 4000) -> str:
    return text if len(text) <= max_chars else text[:max_chars] + "…"


class ClaimExtractor:
    """Extract verifiable claims from a scraped page.

    By default uses span-grounded extraction: every claim must include a
    verbatim excerpt from the source, validated by substring match. This is
    the citation-honesty wedge — see docs/DIFFERENTIATION.md.

    Set ``ground=False`` to fall back to the v0.2 plain extraction (claims
    without source excerpts). Backward-compatible for legacy callers and tests.
    """

    def __init__(
        self,
        llm: LLM | None = None,
        max_claims: int = 8,
        *,
        ground: bool = True,
    ) -> None:
        self.llm = llm or LLM()
        self.max_claims = max_claims
        self.ground = ground

    async def extract(self, page: ScrapedPage) -> list[Claim]:
        if not page.text.strip():
            return []
        if self.ground:
            grounded = await self._extract_grounded(page)
            if grounded:
                return grounded
            # Fall through to legacy path if grounded extraction returned nothing
            # — better to hand a user ungrounded claims that the validator can
            # later flag than to drop the page silently.
            log.info("grounded_extract_empty_falling_back", url=str(page.url))
        return await self._extract_legacy(page)

    async def _extract_grounded(self, page: ScrapedPage) -> list[Claim]:
        prompt = GROUNDING_INSTRUCTIONS.format(
            text=_truncate(page.text), max_claims=self.max_claims
        )
        try:
            result = await self.llm.extract(prompt, _GroundedClaimList, temperature=0.2)
        except Exception as exc:
            log.info("grounded_extract_schema_failed", url=str(page.url), error=str(exc))
            return []
        out: list[Claim] = []
        for gc in result.claims[: self.max_claims]:
            if not gc.text or not gc.text.strip():
                continue
            if looks_like_table_fragment(gc.text):
                log.info("claim_table_fragment_rejected", url=str(page.url), claim=gc.text[:70])
                continue
            citation = Citation(
                url=page.url,
                title=page.title,
                excerpt=gc.excerpt or None,
                page=getattr(page, "page", None),
            )
            grounded = ground_claim(
                page.text,
                # Provisional confidence; adjusted below based on whether the
                # excerpt actually grounded against the source body.
                Claim(text=gc.text, citations=[citation], confidence=0.6),
            )
            # Grounded claims are higher-confidence than ungrounded ones — that's
            # the whole point of grounding. cross_reference() also nudges by
            # +0.1 per duplicate source, so we leave headroom for that.
            if grounded.grounded:
                # The excerpt exists in the page — but does it actually say what
                # the claim says? That second edge was missing entirely.
                _ok, _why = check_support(
                    grounded.text, grounded.citations[0].excerpt or ""
                )
                if _ok:
                    grounded.confidence = 0.8
                else:
                    log.info(
                        "claim_support_rejected",
                        url=str(page.url),
                        reason=_why,
                        claim=grounded.text[:80],
                    )
                    grounded = grounded.model_copy(
                        update={
                            "citations": [
                                c.model_copy(update={"grounded": False})
                                for c in grounded.citations
                            ],
                            "confidence": 0.4,
                        }
                    )
            out.append(grounded)
        return out

    async def _extract_legacy(self, page: ScrapedPage) -> list[Claim]:
        prompt = _INSTRUCTIONS.format(text=_truncate(page.text), max_claims=self.max_claims)
        raw: list[str] = []
        schema_failed = False
        try:
            result = await self.llm.extract(prompt, _ClaimList, temperature=0.2)
            raw = [c for c in result.claims if isinstance(c, str) and c.strip()]
        except Exception as exc:
            schema_failed = True
            log.info("claim_extract_schema_failed", url=str(page.url), error=str(exc))

        # Weaker local models (llama2, tinyllama) often ignore JSON schema
        # instructions and either raise on validation or return an empty list
        # while emitting a numbered list in prose. Fall back to line parsing
        # whenever the structured path produced nothing.
        if not raw:
            if not schema_failed:
                log.info("claim_extract_empty_using_fallback", url=str(page.url))
            try:
                completion = await self.llm.complete(prompt, temperature=0.2)
                raw = _parse_lines(completion.content)
            except Exception as exc2:
                log.warning("claim_extract_failed", url=str(page.url), error=str(exc2))
                return []

        raw = raw[: self.max_claims]
        if not raw:
            return []
        citation = Citation(url=page.url, title=page.title)
        return [Claim(text=c, citations=[citation], confidence=0.6) for c in raw]


def _parse_lines(text: str) -> list[str]:
    """Extract claim strings from a numbered/bulleted list in free text.

    Keeps only lines that actually started with a list marker (``1.``, ``-``, ``*``,
    ``•``) so we don't treat preamble prose like "Here are the claims:" as a claim.
    """
    out: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        had_marker = False
        if line[0] in "-*•":
            line = line[1:].strip()
            had_marker = True
        else:
            i = 0
            while i < len(line) and line[i].isdigit():
                i += 1
            if i > 0 and i < len(line) and line[i] in ".):":
                line = line[i + 1 :].strip()
                had_marker = True
        if not had_marker:
            continue
        if len(line) < 12:
            continue
        out.append(line)
    return out


def cross_reference(claims: list[Claim]) -> list[Claim]:
    """Merge claims from multiple sources. Same text → citations union → higher confidence."""
    buckets: dict[str, Claim] = {}
    for claim in claims:
        key = _normalize(claim.text)
        if key in buckets:
            existing = buckets[key]
            existing.citations = _dedupe_citations(existing.citations + claim.citations)
            existing.confidence = min(1.0, existing.confidence + 0.1)
        else:
            buckets[key] = claim.model_copy(deep=True)
    return sorted(buckets.values(), key=lambda c: c.confidence, reverse=True)


def _normalize(text: str) -> str:
    return " ".join(text.lower().strip().split())


def _dedupe_citations(citations: list[Citation]) -> list[Citation]:
    """One citation per (document, excerpt).

    Local-corpus citations differ only by a `#offset=` fragment, so keying on
    the full URL let the same sentence appear twice under one claim.
    """
    seen: dict[tuple[str, str], Citation] = {}
    for c in citations:
        key = (str(c.url).split("#", 1)[0], (c.excerpt or "").strip())
        seen.setdefault(key, c)
    return list(seen.values())
