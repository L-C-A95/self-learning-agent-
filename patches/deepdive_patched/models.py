from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field, HttpUrl


class SearchQuery(BaseModel):
    text: str
    rationale: str | None = None


class SearchResult(BaseModel):
    url: HttpUrl
    title: str
    snippet: str
    source: str = "searxng"
    score: float = 0.0


class ScrapedPage(BaseModel):
    url: HttpUrl
    title: str
    text: str
    page: int | None = None
    """1-based page in the source document, when the format has pages (PDF)."""
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Citation(BaseModel):
    url: HttpUrl
    title: str
    page: int | None = None
    """1-based page in the source document, when known (local corpus / PDF)."""
    quote: str | None = None
    # Span-grounded: where in the source page the supporting text lives.
    # ``excerpt`` is the verbatim substring; ``offset_start``/``offset_end``
    # are character indices into the fetched page body. ``grounded`` is
    # True iff the validator confirmed excerpt == body[start:end].
    excerpt: str | None = None
    offset_start: int | None = None
    offset_end: int | None = None
    grounded: bool = False


class Claim(BaseModel):
    text: str
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    citations: list[Citation] = Field(default_factory=list)
    contradicts: list[str] = Field(default_factory=list)

    @property
    def grounded(self) -> bool:
        """A claim is grounded iff at least one of its citations is grounded."""
        return any(c.grounded for c in self.citations)


class ReportSection(BaseModel):
    heading: str
    body: str
    claims: list[Claim] = Field(default_factory=list)
    dropped: int = 0
    """Sentences removed by the synthesis audit (no citation / unsupported fact)."""


class ResearchReport(BaseModel):
    question: str
    summary: str
    sections: list[ReportSection] = Field(default_factory=list)
    sources: list[Citation] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    total_cost_usd: float = 0.0
    sentences_dropped: int = 0
    """Total sentences removed across summary and sections."""
