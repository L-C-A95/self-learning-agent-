"""Token-aware chunking with character-offset preservation.

Each chunk records the byte offsets back into the source document so the
span-grounding validator can verify excerpts against the original file.

Sentence boundary detection handles English (terminator + whitespace + a
capitalised start) *and* Chinese (terminator with no whitespace, often followed
by a closing quote). The original implementation only understood `[.!?]` + space,
so a Chinese PDF — which has no spaces between sentences — degenerated into a
single chunk holding the whole book.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Chunk:
    text: str
    offset_start: int  # char offset in the source document
    offset_end: int


_CJK_TERMINATORS = "。！？；"
_EN_TERMINATORS = ".!?"
_CLOSERS = "”’」』）)]】》"
_EN_STARTERS = "\"'([「『“‘"

# English boundary heuristic — kept in sync with `_sentence_spans` below for
# backward compatibility with anything importing the old pattern.
_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(\[])")


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """Return ``[(start, end), ...]`` half-open spans that partition ``text``.

    A span ends right after its terminator (and any closing quote/bracket that
    follows). Whitespace between sentences is attached to the end of the
    preceding span, so callers may need to strip.
    """
    spans: list[tuple[int, int]] = []
    n = len(text)
    start = 0
    i = 0

    while i < n:
        ch = text[i]
        boundary: int | None = None

        if ch in _CJK_TERMINATORS:
            boundary = i + 1
            while boundary < n and text[boundary] in _CJK_TERMINATORS:
                boundary += 1
        elif ch == "…":
            boundary = i + 1
            while boundary < n and text[boundary] == "…":
                boundary += 1
        elif ch in _EN_TERMINATORS:
            j = i + 1
            if j < n and text[j] in _CLOSERS:
                j += 1
            if j >= n:
                boundary = n
            elif text[j].isspace():
                k = j
                while k < n and text[k].isspace():
                    k += 1
                if k >= n:
                    boundary = n
                elif text[k].isupper() or text[k] in _EN_STARTERS:
                    boundary = k

        if boundary is None:
            i += 1
            continue

        # A closing quote right after 。 belongs to the sentence we just closed:
        #   “他走了。”然后……
        while boundary < n and text[boundary] in _CLOSERS:
            boundary += 1

        spans.append((start, boundary))
        start = boundary
        i = boundary

    if start < n:
        spans.append((start, n))
    return spans


def _split_sentences(text: str) -> list[tuple[str, int]]:
    """Return [(sentence, start_offset)]. Offsets are into ``text``."""
    out: list[tuple[str, int]] = []
    for span_start, span_end in _sentence_spans(text):
        raw = text[span_start:span_end]
        sentence = raw.strip()
        if not sentence:
            continue
        lead = len(raw) - len(raw.lstrip())
        out.append((sentence, span_start + lead))
    return out


def _hard_split(
    sentences: list[tuple[str, int]], max_chars: int
) -> list[tuple[str, int]]:
    """Cut any single sentence longer than ``max_chars`` into fixed pieces.

    Without this, one over-long "sentence" (bad PDF extraction, a wall of text
    with no punctuation) becomes a chunk far bigger than the budget — the
    overflow check in ``chunk_text`` only fires when a *second* sentence arrives.
    """
    out: list[tuple[str, int]] = []
    for sentence, start in sentences:
        if len(sentence) <= max_chars:
            out.append((sentence, start))
            continue
        pos = 0
        while pos < len(sentence):
            piece = sentence[pos : pos + max_chars]
            out.append((piece, start + pos))
            pos += max_chars
    return out


def chunk_text(
    text: str,
    *,
    max_chars: int = 1500,
    overlap_chars: int = 200,
) -> list[Chunk]:
    """Split ``text`` into chunks of at most ``max_chars`` with sentence-aligned cuts.

    Each chunk overlaps the previous by ``overlap_chars`` so a relevant claim
    near a chunk boundary is still findable. Both bounds are character-based;
    we don't tokenize because the embedder will do that downstream.

    Invariant: every chunk's ``text`` equals ``source_text[offset_start:offset_end]``.
    Span-grounding relies on this — without it, an excerpt match against the
    chunk text wouldn't translate back to a slice of the original document.
    """
    if not text or not text.strip():
        return []
    sentences = _split_sentences(text)
    if not sentences:
        return []
    sentences = _hard_split(sentences, max_chars)

    chunks: list[Chunk] = []
    cur_start: int | None = None
    cur_end: int = 0

    def flush() -> None:
        nonlocal cur_start, cur_end
        if cur_start is not None and cur_end > cur_start:
            # Chunk text is the literal slice of the source — keeps the
            # `text == source[start:end]` invariant intact even after overlap.
            chunks.append(
                Chunk(
                    text=text[cur_start:cur_end],
                    offset_start=cur_start,
                    offset_end=cur_end,
                )
            )
        cur_start = None
        cur_end = 0

    for sent, start in sentences:
        sent_len = len(sent)
        sent_end = start + sent_len
        if cur_start is None:
            cur_start = start
            cur_end = sent_end
            continue
        # Would this sentence overflow the chunk?
        prospective = sent_end - cur_start
        if prospective > max_chars:
            flush()
            # Start a new chunk; back-track ``overlap_chars`` worth of text so
            # claims near the boundary are still findable in this chunk too.
            tail_start = max(0, start - overlap_chars)
            if sent_end - tail_start > max_chars:
                # The sentence nearly fills the budget already — carrying the
                # overlap would push the chunk past max_chars. Sentence-aligned
                # start wins over overlap.
                tail_start = start
            cur_start = tail_start
            cur_end = sent_end
        else:
            cur_end = sent_end
    flush()
    return chunks
