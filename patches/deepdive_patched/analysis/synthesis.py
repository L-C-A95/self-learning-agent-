"""Sentence-level synthesis audit.

Span grounding proves a quote exists in the source. The support check proves a
claim does not add facts its quote lacks. Both operate on single claims — and a
report can still be wrong, because the prose around those claims is free-form
model output with citation numbers sprinkled on top. That seam is where we saw
`TPO (Truthful and Precise Output)` invented out of thin air, and an OpenAI
finding attributed to a Fudan team.

This module closes it with one rule:

    Every sentence must carry at least one resolvable citation, and must not
    mention any proper noun or number that its own cited claims do not contain.

Because ``entities()`` already extracts numbers, the same rule is also the
number-consistency check — a sentence claiming "25%" over a claim saying "12%"
fails on the number alone.

Set ``DEEPDIVE_ALLOW_UNCITED=1`` to keep uncited sentences that introduce no new
entities (lenient mode), instead of dropping every uncited sentence.
"""

from __future__ import annotations

import os
import re

import structlog

from deepdive.analysis.support import TERM_ALIASES, entities
from deepdive.corpus.chunker import _sentence_spans
from deepdive.models import Claim

log = structlog.get_logger(__name__)

_REF = re.compile(r"\[(\d{1,3})\]")
_LEADING_REFS = re.compile(r"\s*(?:\[\d{1,3}\]\s*)+")

DROP_ALL_UNCITED = os.environ.get("DEEPDIVE_DROP_ALL_UNCITED", "0") == "1"


_ATX_HEADING = re.compile(r"^#{1,6}[^\n]*\n?", re.MULTILINE)


def strip_headings(body: str, heading: str | None = None) -> str:
    """Drop markdown headings the model emitted inside a section body.

    The renderer already adds its own heading, and leaving the model's copy in
    would make "Contradictions" look like an unexplained entity to the audit.
    Models emit it two ways — as ``## Heading`` and as a bare ``Heading`` line —
    so both are removed.
    """
    body = _ATX_HEADING.sub("", body or "")
    if heading:
        lines = body.lstrip().splitlines()
        if lines and lines[0].strip().strip("#").strip().lower() == heading.strip().lower():
            body = "\n".join(lines[1:])
    return body.strip()


def _sentence_units(body: str) -> list[tuple[int, int]]:
    """Sentence spans, with trailing citation markers attached.

    Chinese convention puts the marker *after* the terminator — ``……。[1]`` —
    so a naive split leaves ``[1]`` orphaned at the head of the next sentence,
    which then looks uncited and gets dropped. Pull those markers back onto the
    sentence they belong to.
    """
    spans: list[list[int]] = [list(s) for s in _sentence_spans(body)]
    for i in range(len(spans) - 1):
        nxt_start, _nxt_end = spans[i + 1]
        match = _LEADING_REFS.match(body, nxt_start)
        if match and match.end() > nxt_start:
            spans[i][1] = match.end()
            spans[i + 1][0] = match.end()
    return [(s, e) for s, e in spans]


def _source_key(url) -> str:
    return str(url).split("#", 1)[0]


def claims_by_source(claims: list[Claim], source_order: dict[str, int]) -> dict[int, list[Claim]]:
    """Group claims by the source number used in the prompt / report body."""
    by_num: dict[int, list[Claim]] = {}
    for claim in claims:
        for cit in claim.citations:
            num = source_order.get(_source_key(cit.url))
            if num:
                by_num.setdefault(num, []).append(claim)
                break
    return by_num


def _allowed_bundle(cited: list[Claim]) -> tuple[set[str], str]:
    """Entities a body may mention while citing ``cited``, plus the raw text.

    The raw text is needed for term aliases: ``Biot`` is not an entity of the
    Chinese claim "毕渥数…", so the synonym has to be looked up in the source
    text rather than in the entity set.
    """
    ents: set[str] = set()
    parts: list[str] = []
    for claim in cited:
        ents |= entities(claim.text)
        parts.append(claim.text)
        for cit in claim.citations:
            if cit.excerpt:
                ents |= entities(cit.excerpt)
                parts.append(cit.excerpt)
            if cit.title:
                ents |= entities(cit.title)
                parts.append(cit.title)
    return {e.casefold() for e in ents}, "\n".join(parts).casefold()


def _mentions(entity: str, ents: set[str], blob: str) -> bool:
    cf = entity.casefold()
    if cf in ents:
        return True
    return any(alias in blob for alias in TERM_ALIASES.get(cf, ()))


def _strip_refs(raw: str, keep: set[int]) -> str:
    """Remove citation markers not in ``keep``, preserving everything else.

    Deleting a wrong number cannot corrupt the meaning, while leaving it in
    ships a citation the reader cannot resolve.
    """

    def _sub(match: re.Match[str]) -> str:
        return match.group(0) if int(match.group(1)) in keep else ""

    return _REF.sub(_sub, raw)


def _keep_paragraph_break(raw: str, kept: list[str]) -> None:
    """Preserve the blank line that followed a dropped sentence."""
    tail = raw[len(raw.rstrip()) :]
    if "\n\n" in tail:
        kept.append("\n\n")
    elif "\n" in tail and kept:
        kept.append("\n")


def audit_body(
    body: str,
    claims: list[Claim],
    source_order: dict[str, int],
    *,
    heading: str = "",
) -> tuple[str, list[dict]]:
    """Return ``(cleaned_body, drops)``. ``drops`` is for logs and the footer."""
    if not body or not body.strip():
        return body, []

    by_num = claims_by_source(claims, source_order)
    all_ents, all_blob = _allowed_bundle(claims)

    kept: list[str] = []
    drops: list[dict] = []
    uncited_kept = 0

    for start, end in _sentence_units(body):
        raw = body[start:end]
        text = raw.strip()
        if not text:
            continue

        refs = [int(n) for n in _REF.findall(text)]
        # Strip the markers before entity extraction: "[12]" would otherwise
        # contribute the number 12 as a "fact the claim must support".
        probe = _REF.sub(" ", text)
        valid = [n for n in refs if n in by_num]
        stray = [n for n in refs if n not in by_num]

        if not valid:
            # No usable citation. A wrong number is a citation defect, not a
            # factual one — the model wrote [5] against a single-source corpus.
            # Remove the bad markers and judge the sentence as uncited: it may
            # stand only if it says nothing new.
            novel = {e for e in entities(probe) if not _mentions(e, all_ents, all_blob)}
            note = f"uncited (bogus refs {stray})" if stray else "no citation"
            if novel or DROP_ALL_UNCITED:
                drops.append(
                    {
                        "heading": heading,
                        "reason": f"{note}, new facts: " + ", ".join(sorted(novel)[:5])
                        if novel
                        else note,
                        "text": text[:90],
                    }
                )
                _keep_paragraph_break(raw, kept)
            else:
                kept.append(_strip_refs(raw, set()))
                uncited_kept += 1
            continue

        cited: list[Claim] = []
        for n in valid:
            cited.extend(by_num[n])
        allowed_ents, allowed_blob = _allowed_bundle(cited)
        missing = sorted(e for e in entities(probe) if not _mentions(e, allowed_ents, allowed_blob))
        if missing:
            drops.append(
                {
                    "heading": heading,
                    "reason": "unsupported: " + ", ".join(missing[:5]),
                    "text": text[:90],
                }
            )
            _keep_paragraph_break(raw, kept)
            continue

        kept.append(_strip_refs(raw, set(valid)))

    cleaned = "".join(kept).strip()
    for d in drops:
        log.info("sentence_rejected", reason=d["reason"], heading=d["heading"], text=d["text"])
    if uncited_kept:
        log.info("uncited_kept", heading=heading, count=uncited_kept)
    return cleaned, drops
