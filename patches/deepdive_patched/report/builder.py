from __future__ import annotations

from actants import LLM
from pydantic import HttpUrl

from deepdive.analysis.support import is_cjk
from deepdive.analysis.synthesis import audit_body, strip_headings
from deepdive.models import Citation, Claim, ReportSection, ResearchReport

_SUMMARY_PROMPT = """Write a one-paragraph summary (3-5 sentences) that
answers the question using the claims below.

Rules:
- 3-5 sentences, no preamble
- EVERY sentence that states a fact MUST end with an inline citation [n],
  where n is the source number shown in brackets at the start of each claim line
- Do not state anything the claims do not support
{lang_rule}

Question: {question}

Claims:
{claims}

Summary:"""

_SECTION_PROMPT = """Write a {heading} section for a research report.
Use the claims below as factual basis.

Rules:
- 2-4 short paragraphs
- EVERY sentence that states a fact MUST end with an inline citation [n],
  where n is the source number shown in brackets at the start of each claim line
- Do not invent facts, do not expand abbreviations, do not attribute a claim
  to a different source than its own
{lang_rule}

Question: {question}
Section: {heading}

Claims:
{claims}

Section body:"""

_LABELS = {
    "zh": {
        "lang_rule": (
            "- 正文用中文写（即使原始材料是英文），专有名词、缩写、公式保持原文，"
            "引用编号 [n] 原样保留"
        ),
        "section_rejected": "_本节所有表述均未通过证据校验，已删除。原始声明见下方 Evidence 清单。_",
        "summary_rejected": "摘要未通过证据校验，已省略——原始声明与出处见下方 Evidence 清单。",
    },
    "en": {
        "lang_rule": "- Write the body in English; keep symbols and [n] markers unchanged",
        "section_rejected": (
            "_Every sentence in this section failed the evidence check and was removed. "
            "See the Evidence list below for the raw claims._"
        ),
        "summary_rejected": (
            "Summary omitted: it did not pass the evidence check. "
            "See the Evidence list below for the claims and their sources."
        ),
    },
}

_NO_CLAIMS_MESSAGE = (
    "No verifiable claims could be extracted from the searched sources. "
    "This can happen when search returned no results, when pages failed to "
    "scrape, or when the local model could not parse the content. Try "
    "rephrasing the question, switching search backend, or using a larger model."
)

_NO_CLAIMS_ZH = (
    "没有从资料里提取到可核对的结论。常见原因：①这个问题不在库内——"
    "库里最接近的内容相似度也没达到门槛；②资料是扫描件，抽不出文字；"
    "③问题太宽，加点限定词再问。"
)


def _source_key(url) -> str:
    """Document-level identity for a citation URL.

    Local-corpus chunks encode their position as ``#offset=N``, so two chunks of
    the same PDF would otherwise look like two different sources. Sources are
    per *document*; the page number travels on the citation instead.
    """
    return str(url).split("#", 1)[0]


class ReportBuilder:
    def __init__(self, llm: LLM | None = None) -> None:
        self.llm = llm or LLM()

    async def build(self, question: str, claims: list[Claim]) -> ResearchReport:
        if not claims:
            return ResearchReport(
                question=question,
                summary=_NO_CLAIMS_ZH if is_cjk(question) else _NO_CLAIMS_MESSAGE,
                sections=[],
                sources=[],
                total_cost_usd=0.0,
            )

        # Citations must reference the SOURCES list, not the claim index.
        # Previously [n] was the enumerated claim position, which never matched
        # the numbered Sources block (that produced dangling refs like [30] in a
        # report with 4 sources).
        source_order: dict[str, int] = {}
        for _c in claims:
            for _cit in _c.citations:
                source_order.setdefault(_source_key(_cit.url), len(source_order) + 1)

        def _primary_key(c: Claim) -> str:
            for _cit in c.citations:
                if _cit.grounded:
                    return _source_key(_cit.url)
            return _source_key(c.citations[0].url) if c.citations else ""

        numbered = [
            f"[{source_order[_primary_key(c)]}] {c.text}"
            for c in claims
            if c.citations and _primary_key(c) in source_order
        ]
        claims_block = "\n".join(numbered)

        # The reader asked in Chinese but the papers are in English; the report
        # should answer in the language of the question, not of the sources.
        _labels = _LABELS["zh"] if is_cjk(question) else _LABELS["en"]
        total_cost = 0.0

        summary_result = await self.llm.complete(
            _SUMMARY_PROMPT.format(
                question=question, claims=claims_block, lang_rule=_labels["lang_rule"]
            ),
            temperature=0.3,
        )
        total_cost += summary_result.cost_usd
        summary_text, summary_drops = audit_body(
            strip_headings(summary_result.content), claims, source_order, heading="Summary"
        )
        if summary_drops and not summary_text.strip():
            summary_text = _labels["summary_rejected"]

        sections: list[ReportSection] = []
        for heading in ("Background", "Key findings", "Contradictions & open questions"):
            r = await self.llm.complete(
                _SECTION_PROMPT.format(
                    question=question,
                    heading=heading,
                    claims=claims_block,
                    lang_rule=_labels["lang_rule"],
                ),
                temperature=0.3,
            )
            total_cost += r.cost_usd
            body, drops = audit_body(
                strip_headings(r.content, heading), claims, source_order, heading=heading
            )
            if drops and not body.strip():
                body = _labels["section_rejected"]
            sections.append(
                ReportSection(heading=heading, body=body, claims=claims, dropped=len(drops))
            )

        sources = self._collect_sources(claims, source_order)

        return ResearchReport(
            question=question,
            summary=summary_text.strip(),
            sections=sections,
            sources=sources,
            total_cost_usd=total_cost,
            sentences_dropped=len(summary_drops) + sum(s.dropped for s in sections),
        )

    @staticmethod
    def _collect_sources(
        claims: list[Claim], order: dict[str, int] | None = None
    ) -> list[Citation]:
        seen: dict[str, Citation] = {}
        for claim in claims:
            for c in claim.citations:
                key = _source_key(c.url)
                if key not in seen:
                    # Drop the `#offset=…` fragment: the Sources list points at
                    # the document, and the page number on each citation is the
                    # thing a reader can actually look up.
                    seen[key] = c.model_copy(update={"url": HttpUrl(key)})
        items = list(seen.items())
        if order:
            # Same ordering the prompt used, so [n] in the body resolves to
            # sources[n-1] and max(n) == len(sources).
            items.sort(key=lambda kv: order.get(kv[0], 10**6))
        return [c for _, c in items]
