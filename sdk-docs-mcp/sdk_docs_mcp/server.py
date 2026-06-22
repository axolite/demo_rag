"""FastMCP server exposing one SDK docs index via hybrid retrieval.

Corpus-neutral: the index file (passed on the command line) decides which SDK
this process serves. Pointer-first: ``search_docs`` returns *locations* (repo /
file / anchor / breadcrumb / line range) plus a snippet, so the agent can
cheaply locate a section and then ``Read`` the real RST for exactness. Full text
is available on demand via ``get_section`` / ``get_doc``; the xref graph via
``related``.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Literal

from fastmcp import FastMCP

from . import EMBED_MODEL
from .store import open_corpus

RRF_K = 60          # Reciprocal Rank Fusion constant
FUSE_DEPTH = 50     # how deep each retriever goes before fusion
SNIPPET_CHARS = 320

# Cosmetic only: the two server processes are namespaced by their .mcp.json keys
# (``ncs-docs`` / ``bm-docs``), so a single generic name here is correct.
mcp = FastMCP("sdk-docs")

# Populated in main().
_DB: sqlite3.Connection
_DOCS_ROOT: Path
_embedder: Any = None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _get_embedder():
    global _embedder
    if _embedder is None:
        from .embed import Embedder

        _embedder = Embedder()
    return _embedder


def _fts_query(text: str) -> str | None:
    """Turn arbitrary user text into a safe FTS5 MATCH expression.

    Each term is quoted (so punctuation can't break MATCH syntax); a trailing
    ``*`` is preserved as an FTS prefix query (``CONFIG_BT*``). Terms are OR-ed
    so natural-language queries still retrieve — bm25 handles the ranking.
    """
    terms = re.findall(r"[A-Za-z0-9_]+\*?", text)
    if not terms:
        return None
    parts = [t if t.endswith("*") else f'"{t}"' for t in terms]
    return " OR ".join(parts)


def _keyword_ids(query: str, depth: int) -> list[int]:
    fts = _fts_query(query)
    if not fts:
        return []
    try:
        rows = _DB.execute(
            "SELECT rowid FROM fts_sections WHERE fts_sections MATCH ? "
            "ORDER BY bm25(fts_sections) LIMIT ?",
            (fts, depth),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [r[0] for r in rows]


def _semantic_ids(query: str, depth: int) -> list[int]:
    from .embed import to_blob

    vec = _get_embedder().embed_query(query)
    rows = _DB.execute(
        "SELECT section_id FROM vec_sections WHERE embedding MATCH ? "
        "ORDER BY distance LIMIT ?",
        (to_blob(vec), depth),
    ).fetchall()
    return [r[0] for r in rows]


def _rrf(*rankings: list[int]) -> list[int]:
    scores: dict[int, float] = {}
    for ranked in rankings:
        for rank, sid in enumerate(ranked, start=1):
            scores[sid] = scores.get(sid, 0.0) + 1.0 / (RRF_K + rank)
    return sorted(scores, key=lambda s: scores[s], reverse=True)


def _snippet(text: str) -> str:
    s = re.sub(r"\s+", " ", text).strip()
    return s[:SNIPPET_CHARS] + ("…" if len(s) > SNIPPET_CHARS else "")


def _load_meta(ids: list[int]) -> dict[int, dict[str, Any]]:
    if not ids:
        return {}
    qs = ",".join("?" * len(ids))
    rows = _DB.execute(
        f"SELECT id, repo, file_path, anchor, breadcrumb, header, "
        f"line_start, line_end, text FROM sections WHERE id IN ({qs})",
        ids,
    ).fetchall()
    out: dict[int, dict[str, Any]] = {}
    for r in rows:
        out[r[0]] = {
            "id": r[0], "repo": r[1], "file_path": r[2], "anchor": r[3],
            "breadcrumb": r[4], "header": r[5],
            "line_start": r[6], "line_end": r[7],
            "citation": f"{r[2]}#{r[3]}" if r[3] else f"{r[2]}:{r[6]}-{r[7]}",
            "snippet": _snippet(r[8]),
        }
    return out


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #


@mcp.tool
def search_docs(
    query: str,
    k: int = 8,
    mode: Literal["hybrid", "keyword", "semantic"] = "hybrid",
) -> dict[str, Any]:
    """Search this SDK's docs and return locations (not full text).

    Modes:
      * ``hybrid``   – BM25 + dense vectors fused with Reciprocal Rank Fusion (default).
      * ``keyword``  – BM25 only; best for exact ``CONFIG_*`` / API symbols.
                       Append ``*`` for symbol-family prefix search (``CONFIG_BT*``).
      * ``semantic`` – dense vectors only; best for conceptual "how do I…" queries.

    Each result has repo, file_path, anchor, breadcrumb, header, line range, a
    ``citation`` string, and a snippet. Use ``get_section(id)`` for full text.
    """
    k = max(1, min(k, 50))
    if mode == "keyword":
        ordered = _keyword_ids(query, FUSE_DEPTH)
    elif mode == "semantic":
        ordered = _semantic_ids(query, FUSE_DEPTH)
    else:
        ordered = _rrf(_keyword_ids(query, FUSE_DEPTH), _semantic_ids(query, FUSE_DEPTH))

    top = ordered[:k]
    meta = _load_meta(top)
    results = [meta[i] for i in top if i in meta]
    return {"query": query, "mode": mode, "count": len(results), "results": results}


@mcp.tool
def get_section(id: int) -> dict[str, Any]:
    """Return the full text and metadata for a section id from ``search_docs``."""
    r = _DB.execute(
        "SELECT id, repo, file_path, anchor, breadcrumb, header, "
        "line_start, line_end, text FROM sections WHERE id = ?",
        (id,),
    ).fetchone()
    if not r:
        return {"error": f"no section with id {id}"}
    return {
        "id": r[0], "repo": r[1], "file_path": r[2], "anchor": r[3],
        "breadcrumb": r[4], "header": r[5], "line_start": r[6], "line_end": r[7],
        "citation": f"{r[2]}#{r[3]}" if r[3] else f"{r[2]}:{r[6]}-{r[7]}",
        "text": r[8],
    }


@mcp.tool
def get_doc(path: str) -> dict[str, Any]:
    """Return a full documentation file by its repo-relative ``file_path``."""
    rel = Path(path.replace("\\", "/"))
    target = (_DOCS_ROOT / rel).resolve()
    try:
        target.relative_to(_DOCS_ROOT)  # block path traversal
    except ValueError:
        return {"error": "path escapes the docs root"}
    if not target.is_file():
        return {"error": f"file not found: {path}"}
    text = target.read_text(encoding="utf-8", errors="replace")
    return {"file_path": rel.as_posix(), "lines": text.count("\n") + 1, "text": text}


@mcp.tool
def related(id: int, limit: int = 20) -> dict[str, Any]:
    """Walk the Sphinx xref graph from a section to its resolved neighbours."""
    out_rows = _DB.execute(
        "SELECT l.kind, l.target_raw, l.resolved_id FROM links l "
        "WHERE l.src_id = ? AND l.resolved_id IS NOT NULL LIMIT ?",
        (id, limit),
    ).fetchall()
    in_rows = _DB.execute(
        "SELECT l.kind, l.src_id FROM links l WHERE l.resolved_id = ? LIMIT ?",
        (id, limit),
    ).fetchall()

    neighbor_ids = {r[2] for r in out_rows} | {r[1] for r in in_rows}
    meta = _load_meta(list(neighbor_ids))

    outgoing = [
        {"kind": kind, "target": tgt, **{k: meta[rid][k] for k in
         ("id", "file_path", "anchor", "breadcrumb", "header", "citation")}}
        for kind, tgt, rid in out_rows if rid in meta
    ]
    incoming = [
        {"kind": kind, **{k: meta[sid][k] for k in
         ("id", "file_path", "anchor", "breadcrumb", "header", "citation")}}
        for kind, sid in in_rows if sid in meta
    ]
    # Also surface unresolved edges as bare keywords (still useful to the agent).
    unresolved = _DB.execute(
        "SELECT kind, target_raw FROM links WHERE src_id = ? AND resolved_id IS NULL LIMIT ?",
        (id, limit),
    ).fetchall()
    return {
        "id": id,
        "outgoing": outgoing,
        "incoming": incoming,
        "unresolved": [{"kind": k, "target": t} for k, t in unresolved],
    }


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main() -> None:
    global _DB, _DOCS_ROOT
    ap = argparse.ArgumentParser(description="SDK docs hybrid MCP server")
    ap.add_argument("index", help="path to the docs index .sqlite to serve")
    args = ap.parse_args()

    index_path = Path(args.index).resolve()
    if not index_path.is_file():
        sys.exit(f"index not found: {index_path}\nBuild it first with build_index.py")

    corpus = open_corpus(index_path)
    _DB = corpus.db
    _DOCS_ROOT = corpus.docs_root

    if corpus.embed_model and corpus.embed_model != EMBED_MODEL:
        print(f"warning: index built with {corpus.embed_model}, server expects {EMBED_MODEL}",
              file=sys.stderr)

    mcp.run()


if __name__ == "__main__":
    main()
