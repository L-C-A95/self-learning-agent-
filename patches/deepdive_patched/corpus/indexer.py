"""sqlite-vec corpus indexer.

Extracts text from PDF/MD/HTML/TXT, chunks it, embeds each chunk via Ollama
(default ``nomic-embed-text``), and stores everything in a sqlite database
with a vec0 virtual table for cosine search.

Table layout:
  documents(doc_id INTEGER PRIMARY KEY, source_path TEXT UNIQUE, indexed_at TEXT)
  chunks(chunk_id INTEGER PRIMARY KEY, doc_id INTEGER REFERENCES documents,
         text TEXT, offset_start INT, offset_end INT)
  vec_chunks(chunk_id INTEGER PRIMARY KEY, embedding FLOAT[N])  -- vec0 virtual

The indexer is async because the embedder is. Indexing is incremental — files
already in ``documents`` are skipped unless their mtime is newer.
"""

from __future__ import annotations

import math
import os
import sqlite3
import struct
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import sqlite_vec
import structlog
from actants import Embeddings

from deepdive.corpus.chunker import Chunk, chunk_text

log = structlog.get_logger(__name__)


@dataclass
class IndexHit:
    text: str
    source_path: str
    offset_start: int
    offset_end: int
    distance: float
    page: int | None = None
    """1-based page number in the source file, when the format has pages."""
    similarity: float | None = None
    """Cosine similarity in [-1, 1]. Interpretable; ``distance`` is not.

    A vector index always returns its top-k, even when the corpus has nothing to
    do with the question. Without an absolute threshold the pipeline happily
    builds a report out of the least-unrelated chunks — which is exactly how a
    question about thermal resistance got answered with Qwen training configs.
    """


def extract_text_with_pages(path: Path) -> tuple[str, list[tuple[int, int, int]]]:
    """Extract text plus page boundaries.

    Returns ``(text, [(page_no, start_offset, end_offset), ...])``. Formats
    without a page concept return an empty boundary list, which callers treat as
    "page unknown" rather than page 1.

    Page boundaries matter for citing a textbook: "传热学 p.87" is verifiable by
    a human in seconds, a character offset is not.
    """
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ImportError(
                "PDF indexing requires `pypdf`. Install with `pip install pypdf`."
            ) from exc
        reader = PdfReader(str(path))
        parts: list[str] = []
        pages: list[tuple[int, int, int]] = []
        pos = 0
        for page_no, page in enumerate(reader.pages, start=1):
            if parts:
                parts.append("\n\n")
                pos += 2
            start = pos
            body = page.extract_text() or ""
            parts.append(body)
            pos += len(body)
            pages.append((page_no, start, pos))
        return "".join(parts), pages
    return extract_text_plain(path), []


def extract_text_plain(path: Path) -> str:
    """Extract plain text from Markdown / HTML / plain text (no page concept)."""
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown", ".txt", ".rst"}:
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix in {".html", ".htm"}:
        try:
            from selectolax.parser import HTMLParser
        except ImportError:
            return path.read_text(encoding="utf-8", errors="replace")
        tree = HTMLParser(path.read_text(encoding="utf-8", errors="replace"))
        for tag in tree.css("script, style, nav, header, footer"):
            tag.decompose()
        body = tree.body
        return body.text(separator=" ", strip=True) if body else ""
    return ""


def extract_text(path: Path) -> str:
    """Extract plain text from PDF / Markdown / HTML / plain text.

    Returns an empty string for unrecognized formats. Best-effort — we don't
    try to preserve formatting; the goal is searchable text.
    """
    return extract_text_with_pages(path)[0]


def _pack(vector: list[float]) -> bytes:
    """Pack a float vector as little-endian f32 — sqlite-vec's wire format."""
    return struct.pack(f"{len(vector)}f", *vector)


def _read_stored_embed_model(db_path: Path) -> str | None:
    """Read the embedding model recorded when this index was built.

    Without this, query-side embedding silently falls back to the hardcoded
    default: index with ``bge-m3``, query with ``nomic-embed-text`` and the
    cosine search compares vectors from two different spaces.
    """
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'embed_model'"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


class CorpusIndex:
    """Async sqlite-vec corpus index. Use as an async context manager."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        embeddings: Embeddings | None = None,
        embed_model: str = "nomic-embed-text",
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # An index carries its own embedding model; respect it on the query side.
        self.embed_model = _read_stored_embed_model(self.db_path) or embed_model
        self.embeddings = embeddings or Embeddings(model=self.embed_model)
        self._dim: int | None = None  # set on first embed
        self._conn: sqlite3.Connection | None = None

    async def __aenter__(self) -> CorpusIndex:
        self._conn = self._open()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._open()
        return self._conn

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS documents ("
            " doc_id INTEGER PRIMARY KEY,"
            " source_path TEXT UNIQUE NOT NULL,"
            " indexed_at TEXT NOT NULL,"
            " mtime REAL NOT NULL"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS chunks ("
            " chunk_id INTEGER PRIMARY KEY,"
            " doc_id INTEGER NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,"
            " text TEXT NOT NULL,"
            " offset_start INTEGER NOT NULL,"
            " offset_end INTEGER NOT NULL"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS pages ("
            " doc_id INTEGER NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,"
            " page_no INTEGER NOT NULL,"
            " start_offset INTEGER NOT NULL,"
            " end_offset INTEGER NOT NULL,"
            " PRIMARY KEY (doc_id, page_no)"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta ("
            " key TEXT PRIMARY KEY,"
            " value TEXT NOT NULL"
            ")"
        )
        return conn

    async def _ensure_vec_table(self, dim: int) -> None:
        """Create the vec0 virtual table once we know the embedder's dimension."""
        if self._dim is not None:
            return
        # Defense in depth: ``dim`` comes from the embedder output and is always
        # an int in practice, but f-stringing into SQL is fragile — coerce
        # explicitly so any non-int slipping in raises here, not later.
        safe_dim = int(dim)
        if safe_dim <= 0:
            raise ValueError(f"embedding dimension must be positive, got {dim!r}")
        self._dim = safe_dim
        self.conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0("
            f"chunk_id INTEGER PRIMARY KEY, embedding FLOAT[{safe_dim}])"
        )

    async def index_file(self, path: str | Path) -> int:
        """Index one file. Returns the number of chunks added (0 if up-to-date)."""
        path = Path(path).resolve()
        if not path.exists() or not path.is_file():
            return 0
        # Record the embedding model once, before any early return, so a re-index
        # of an existing corpus still stamps it (and query-side search then uses
        # the right space).
        self.conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('embed_model', ?)",
            (self.embed_model,),
        )
        self.conn.commit()
        text, page_bounds = extract_text_with_pages(path)
        if not text.strip():
            return 0

        mtime = path.stat().st_mtime
        cur = self.conn.execute(
            "SELECT doc_id, mtime FROM documents WHERE source_path = ?", (str(path),)
        )
        row = cur.fetchone()
        if row is not None and row[1] >= mtime:
            return 0  # up-to-date
        if row is not None:
            # vec_chunks is a virtual table — FK cascade doesn't apply, so
            # we have to clean it up explicitly before dropping the chunks rows.
            old_chunk_ids = [
                r[0]
                for r in self.conn.execute(
                    "SELECT chunk_id FROM chunks WHERE doc_id = ?", (row[0],)
                )
            ]
            for chunk_id in old_chunk_ids:
                self.conn.execute("DELETE FROM vec_chunks WHERE chunk_id = ?", (chunk_id,))
            self.conn.execute("DELETE FROM chunks WHERE doc_id = ?", (row[0],))
            self.conn.execute("DELETE FROM pages WHERE doc_id = ?", (row[0],))
            self.conn.execute("DELETE FROM documents WHERE doc_id = ?", (row[0],))

        if page_bounds:
            # Chunk page by page so no chunk straddles a page boundary. A chunk
            # that started on p.86 and ran into p.87 would be cited as p.86 even
            # when the supporting sentence sits on p.87 — exactly the off-by-one
            # that makes a citation unverifiable by hand.
            chunks = []
            for _page_no, _start, _end in page_bounds:
                for piece in chunk_text(text[_start:_end]):
                    chunks.append(
                        Chunk(
                            text=piece.text,
                            offset_start=piece.offset_start + _start,
                            offset_end=piece.offset_end + _start,
                        )
                    )
        else:
            chunks = chunk_text(text)
        if not chunks:
            return 0

        embed_result = await self.embeddings.embed([c.text for c in chunks])
        vectors = embed_result.vectors
        if not vectors:
            return 0
        await self._ensure_vec_table(len(vectors[0]))

        self.conn.execute(
            "INSERT INTO documents (source_path, indexed_at, mtime) VALUES (?, ?, ?)",
            (str(path), datetime.now(UTC).isoformat(), mtime),
        )
        doc_id = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        if page_bounds:
            self.conn.executemany(
                "INSERT INTO pages (doc_id, page_no, start_offset, end_offset)"
                " VALUES (?, ?, ?, ?)",
                [(doc_id, no, s, e) for no, s, e in page_bounds],
            )

        for chunk, vec in zip(chunks, vectors, strict=True):
            cur = self.conn.execute(
                "INSERT INTO chunks (doc_id, text, offset_start, offset_end) VALUES (?, ?, ?, ?)",
                (doc_id, chunk.text, chunk.offset_start, chunk.offset_end),
            )
            chunk_id = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self.conn.execute(
                "INSERT INTO vec_chunks (chunk_id, embedding) VALUES (?, ?)",
                (chunk_id, _pack(vec)),
            )
        self.conn.commit()
        return len(chunks)

    def prune_missing(self, root: str | Path) -> int:
        """Drop documents under ``root`` whose file no longer exists.

        Deleting a PDF from the folder used to leave its chunks retrievable
        forever — the index kept answering from a file the user had removed.
        """
        root_str = str(Path(root).resolve())
        gone = [
            (row[0], row[1])
            for row in self.conn.execute("SELECT doc_id, source_path FROM documents")
            if row[1].startswith(root_str) and not Path(row[1]).exists()
        ]
        for doc_id, path in gone:
            chunk_ids = [
                r[0]
                for r in self.conn.execute(
                    "SELECT chunk_id FROM chunks WHERE doc_id = ?", (doc_id,)
                )
            ]
            for chunk_id in chunk_ids:
                self.conn.execute("DELETE FROM vec_chunks WHERE chunk_id = ?", (chunk_id,))
            self.conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            self.conn.execute("DELETE FROM pages WHERE doc_id = ?", (doc_id,))
            self.conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
            log.info("pruned_missing_document", source_path=path)
        if gone:
            self.conn.commit()
        return len(gone)

    async def index_directory(
        self,
        root: str | Path,
        *,
        extensions: tuple[str, ...] = (".pdf", ".md", ".markdown", ".txt", ".rst", ".html", ".htm"),
    ) -> AsyncIterator[tuple[Path, int]]:
        """Walk ``root`` and index every file with a supported extension.

        Yields ``(path, chunks_added)`` per file so callers can show progress.
        Documents whose file disappeared are pruned first.
        """
        root = Path(root).resolve()
        self.prune_missing(root)
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if not name.lower().endswith(extensions):
                    continue
                fpath = Path(dirpath) / name
                added = await self.index_file(fpath)
                yield fpath, added

    async def search(
        self, query: str, *, k: int = 5, min_similarity: float = 0.0
    ) -> list[IndexHit]:
        """Cosine-search the corpus. Returns the top-k hits (smaller distance = closer).

        ``min_similarity`` filters on absolute cosine similarity. Leave it at 0
        for the old behaviour; the pipeline sets a real threshold so that "the
        corpus has nothing about this" surfaces as zero hits instead of the
        least-bad chunks.
        """
        if self._dim is None:
            # No embeddings yet — try to detect from existing vec table
            cur = self.conn.execute("SELECT name FROM sqlite_master WHERE name = 'vec_chunks'")
            if cur.fetchone() is None:
                return []
        embed_result = await self.embeddings.embed([query])
        if not embed_result.vectors:
            return []
        qvec_list = list(embed_result.vectors[0])
        qvec = _pack(qvec_list)
        qnorm = math.sqrt(sum(v * v for v in qvec_list)) or 1.0
        rows = self.conn.execute(
            "SELECT vc.chunk_id, c.text, d.source_path, c.offset_start, c.offset_end,"
            " vc.distance, p.page_no, vc.embedding "
            "FROM vec_chunks AS vc "
            "JOIN chunks AS c ON c.chunk_id = vc.chunk_id "
            "JOIN documents AS d ON d.doc_id = c.doc_id "
            "LEFT JOIN pages AS p ON p.doc_id = c.doc_id "
            " AND c.offset_start >= p.start_offset AND c.offset_start < p.end_offset "
            "WHERE vc.embedding MATCH ? AND k = ? "
            "ORDER BY vc.distance",
            (qvec, k),
        ).fetchall()

        hits: list[IndexHit] = []
        for row in rows:
            sim = None
            blob = row[7]
            if blob:
                dim = len(blob) // 4
                vec = struct.unpack(f"{dim}f", blob)
                dot = sum(a * b for a, b in zip(qvec_list, vec))
                vnorm = math.sqrt(sum(v * v for v in vec)) or 1.0
                sim = dot / (qnorm * vnorm)
            if min_similarity and (sim is None or sim < min_similarity):
                continue
            hits.append(
                IndexHit(
                    text=row[1],
                    source_path=row[2],
                    offset_start=row[3],
                    offset_end=row[4],
                    distance=row[5],
                    page=row[6],
                    similarity=sim,
                )
            )
        return hits

    def stats(self) -> dict[str, int]:
        n_docs = self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        n_chunks = self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        return {"documents": n_docs, "chunks": n_chunks}
