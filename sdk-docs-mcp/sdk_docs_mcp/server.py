"""FastMCP server exposing one *or more* SDK docs indexes via hybrid retrieval.

Corpus-neutral and **federated**: each index file passed on the command line is
opened as a corpus, and ``search_docs`` fuses results across all of them into one
ranked list labelled by ``corpus`` + ``source_kind``. One index = today's
behavior (bare-int ids, single root); the federated NCS pair (resolved HTML +
unified RST/code source) becomes a single searchable entity.

Pointer-first: ``search_docs`` returns *locations* (corpus / repo / file / anchor
/ breadcrumb / line range) plus a snippet, so the agent can cheaply locate a
section and then ``Read`` the real source. Full text is available on demand via
``get_section`` / ``get_doc``; the xref graph via ``related``.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fastmcp import FastMCP

from . import EMBED_MODEL
from .store import open_corpus

RRF_K = 60          # Reciprocal Rank Fusion constant
FUSE_DEPTH = 50     # how deep each retriever goes before fusion
SNIPPET_CHARS = 320

# Cosmetic only: the server process is namespaced by its .mcp.json key
# (``ncs-docs`` / ``bm-docs``), so a single generic name here is correct.
mcp = FastMCP("sdk-docs")


@dataclass
class OpenCorpus:
    """One opened index in the federation, with its display name + default kind."""

    name: str
    db: sqlite3.Connection
    docs_root: Path
    has_source_kind: bool
    source_format: str | None
    embed_model: str | None
    default_kind: str   # kind to report for v1 indexes lacking the column


# Insertion-ordered registry; first entry is the default for bare-int routing.
_CORPORA: dict[str, OpenCorpus] = {}
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


def _multi() -> bool:
    return len(_CORPORA) > 1


def _first() -> OpenCorpus:
    return next(iter(_CORPORA.values()))


def _id_str(name: str, local_id: int) -> int | str:
    """Self-describing id across corpora; bare int when there's only one."""
    return f"{name}:{local_id}" if _multi() else local_id


def _parse_id(raw: int | str) -> tuple[OpenCorpus, int]:
    """Resolve an id from a tool arg to its ``(corpus, local_id)``.

    ``"name:local"`` routes to that corpus; a bare int (or numeric string) goes
    to the sole/first corpus — the shape single-index callers always receive."""
    if isinstance(raw, str) and ":" in raw:
        name, local = raw.split(":", 1)
        corpus = _CORPORA.get(name)
        if corpus is None:
            raise KeyError(name)
        return corpus, int(local)
    return _first(), int(raw)


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


def _keyword_ids(corpus: OpenCorpus, query: str, depth: int) -> list[int]:
    fts = _fts_query(query)
    if not fts:
        return []
    try:
        rows = corpus.db.execute(
            "SELECT rowid FROM fts_sections WHERE fts_sections MATCH ? "
            "ORDER BY bm25(fts_sections) LIMIT ?",
            (fts, depth),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [r[0] for r in rows]


def _semantic_ids(corpus: OpenCorpus, query_blob: bytes, depth: int) -> list[int]:
    rows = corpus.db.execute(
        "SELECT section_id FROM vec_sections WHERE embedding MATCH ? "
        "ORDER BY distance LIMIT ?",
        (query_blob, depth),
    ).fetchall()
    return [r[0] for r in rows]


def _filter_by_kind(corpus: OpenCorpus, ids: list[int], source: list[str] | None) -> list[int]:
    """Keep only ids whose ``source_kind`` is in ``source`` (order preserved).

    Applied to the <=FUSE_DEPTH candidates *before* fusion, so top-k fills with
    matches. A v1 corpus (no column) has one kind: keep all or none."""
    if not source:
        return ids
    if not corpus.has_source_kind:
        return ids if corpus.default_kind in source else []
    if not ids:
        return ids
    id_qs = ",".join("?" * len(ids))
    kind_qs = ",".join("?" * len(source))
    rows = corpus.db.execute(
        f"SELECT id FROM sections WHERE id IN ({id_qs}) AND source_kind IN ({kind_qs})",
        (*ids, *source),
    ).fetchall()
    keep = {r[0] for r in rows}
    return [i for i in ids if i in keep]


def _rrf(rankings: list[list[tuple[str, int]]]) -> list[tuple[str, int]]:
    """Reciprocal Rank Fusion over namespaced ``(corpus_name, local_id)`` keys."""
    scores: dict[tuple[str, int], float] = {}
    for ranked in rankings:
        for rank, key in enumerate(ranked, start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank)
    return sorted(scores, key=lambda k: scores[k], reverse=True)


def _snippet(text: str) -> str:
    s = re.sub(r"\s+", " ", text).strip()
    return s[:SNIPPET_CHARS] + ("…" if len(s) > SNIPPET_CHARS else "")


def _citation(file_path: str, anchor: str, ls: int, le: int) -> str:
    return f"{file_path}#{anchor}" if anchor else f"{file_path}:{ls}-{le}"


def _select_cols(corpus: OpenCorpus) -> str:
    kind = "source_kind" if corpus.has_source_kind else f"'{corpus.default_kind}'"
    return ("id, repo, file_path, anchor, breadcrumb, header, "
            f"line_start, line_end, text, {kind} AS source_kind")


def _row_to_meta(corpus: OpenCorpus, r: tuple, *, full_text: bool) -> dict[str, Any]:
    body_key = "text" if full_text else "snippet"
    body = r[8] if full_text else _snippet(r[8])
    return {
        "id": _id_str(corpus.name, r[0]),
        "corpus": corpus.name,
        "source_kind": r[9],
        "repo": r[1], "file_path": r[2], "anchor": r[3],
        "breadcrumb": r[4], "header": r[5],
        "line_start": r[6], "line_end": r[7],
        "citation": _citation(r[2], r[3], r[6], r[7]),
        body_key: body,
    }


def _load_meta(corpus: OpenCorpus, ids: list[int]) -> dict[int, dict[str, Any]]:
    if not ids:
        return {}
    qs = ",".join("?" * len(ids))
    rows = corpus.db.execute(
        f"SELECT {_select_cols(corpus)} FROM sections WHERE id IN ({qs})", ids
    ).fetchall()
    return {r[0]: _row_to_meta(corpus, r, full_text=False) for r in rows}


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #


@mcp.tool
def search_docs(
    query: str,
    k: int = 8,
    mode: Literal["hybrid", "keyword", "semantic"] = "hybrid",
    source: list[Literal["html", "rst", "code"]] | None = None,
) -> dict[str, Any]:
    """Search this SDK's docs (federated across all open corpora) and return
    locations — not full text.

    Modes:
      * ``hybrid``   – BM25 + dense vectors fused with Reciprocal Rank Fusion (default).
      * ``keyword``  – BM25 only; best for exact ``CONFIG_*`` / API symbols.
                       Append ``*`` for symbol-family prefix search (``CONFIG_BT*``).
      * ``semantic`` – dense vectors only; best for conceptual "how do I…" queries.

    ``source`` filters by where a hit came from — ``["code"]`` for source-only,
    ``["rst","html"]`` for docs-only. Each result has ``corpus`` + ``source_kind``
    labels, repo, file_path, anchor, breadcrumb, line range, a ``citation``, and a
    snippet. ``id`` is ``"corpus:local"`` when several corpora are served, else a
    bare int. Use ``get_section(id)`` for full text.
    """
    k = max(1, min(k, 50))
    query_blob: bytes | None = None
    if mode in ("hybrid", "semantic"):
        from .embed import to_blob

        query_blob = to_blob(_get_embedder().embed_query(query))

    rankings: list[list[tuple[str, int]]] = []
    for corpus in _CORPORA.values():
        if mode != "semantic":
            kw = _filter_by_kind(corpus, _keyword_ids(corpus, query, FUSE_DEPTH), source)
            rankings.append([(corpus.name, i) for i in kw])
        if mode != "keyword":
            assert query_blob is not None
            sem = _filter_by_kind(corpus, _semantic_ids(corpus, query_blob, FUSE_DEPTH), source)
            rankings.append([(corpus.name, i) for i in sem])

    ordered = _rrf(rankings)[:k]

    # Batch the metadata fetch per corpus, then re-assemble in fused order.
    by_corpus: dict[str, list[int]] = {}
    for name, local in ordered:
        by_corpus.setdefault(name, []).append(local)
    meta = {name: _load_meta(_CORPORA[name], ids) for name, ids in by_corpus.items()}
    results = [meta[name][local] for name, local in ordered if local in meta.get(name, {})]
    return {"query": query, "mode": mode, "count": len(results), "results": results}


@mcp.tool
def get_section(id: int | str) -> dict[str, Any]:
    """Return the full text and metadata for a section id from ``search_docs``."""
    try:
        corpus, local = _parse_id(id)
    except (KeyError, ValueError):
        return {"error": f"bad section id: {id!r}"}
    r = corpus.db.execute(
        f"SELECT {_select_cols(corpus)} FROM sections WHERE id = ?", (local,)
    ).fetchone()
    if not r:
        return {"error": f"no section with id {id}"}
    return _row_to_meta(corpus, r, full_text=True)


@mcp.tool
def get_doc(path: str, corpus: str | None = None) -> dict[str, Any]:
    """Return a full documentation/source file by its root-relative ``file_path``.

    For the NCS federation every corpus's root is the same west clone, so this
    serves rst, code, and resolved-HTML citations alike. Pass ``corpus`` to pin
    which root to resolve against; otherwise each open corpus is tried in turn.
    """
    rel = Path(path.replace("\\", "/"))
    if corpus is not None:
        c = _CORPORA.get(corpus)
        if c is None:
            return {"error": f"unknown corpus: {corpus}"}
        candidates = [c]
    else:
        candidates = list(_CORPORA.values())

    last_err = "file not found"
    for c in candidates:
        target = (c.docs_root / rel).resolve()
        try:
            target.relative_to(c.docs_root)  # block path traversal
        except ValueError:
            last_err = "path escapes the docs root"
            continue
        if target.is_file():
            text = target.read_text(encoding="utf-8", errors="replace")
            return {
                "file_path": rel.as_posix(), "corpus": c.name,
                "lines": text.count("\n") + 1, "text": text,
            }
        if not c.docs_root.exists():
            last_err = f"source checkout not available at {c.docs_root}"
    return {"error": f"{last_err}: {path}"}


@mcp.tool
def related(id: int | str, limit: int = 20) -> dict[str, Any]:
    """Walk the xref graph from a section to its resolved neighbours.

    Code sections carry no edges (v1), so this returns empties for them — fine."""
    try:
        corpus, local = _parse_id(id)
    except (KeyError, ValueError):
        return {"error": f"bad section id: {id!r}"}
    db = corpus.db
    out_rows = db.execute(
        "SELECT l.kind, l.target_raw, l.resolved_id FROM links l "
        "WHERE l.src_id = ? AND l.resolved_id IS NOT NULL LIMIT ?",
        (local, limit),
    ).fetchall()
    in_rows = db.execute(
        "SELECT l.kind, l.src_id FROM links l WHERE l.resolved_id = ? LIMIT ?",
        (local, limit),
    ).fetchall()

    neighbor_ids = {r[2] for r in out_rows} | {r[1] for r in in_rows}
    meta = _load_meta(corpus, list(neighbor_ids))

    keys = ("id", "corpus", "file_path", "anchor", "breadcrumb", "header", "citation")
    outgoing = [
        {"kind": kind, "target": tgt, **{k: meta[rid][k] for k in keys}}
        for kind, tgt, rid in out_rows if rid in meta
    ]
    incoming = [
        {"kind": kind, **{k: meta[sid][k] for k in keys}}
        for kind, sid in in_rows if sid in meta
    ]
    unresolved = db.execute(
        "SELECT kind, target_raw FROM links WHERE src_id = ? AND resolved_id IS NULL LIMIT ?",
        (local, limit),
    ).fetchall()
    return {
        "id": _id_str(corpus.name, local),
        "outgoing": outgoing,
        "incoming": incoming,
        "unresolved": [{"kind": k, "target": t} for k, t in unresolved],
    }


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def _corpus_name(stem: str, taken: set[str]) -> str:
    """Short, unique display name from an index filename stem.

    ``ncs-1.6.1-source`` -> ``source``, ``…-resolved`` -> ``resolved``,
    ``nrf-bm`` -> ``bm``; falls back to the full stem on collision."""
    short = stem.rsplit("-", 1)[-1]
    return short if short not in taken else stem


def register(index_paths: list[str]) -> None:
    """Open each index as a corpus (skipping missing/broken ones with a warning).

    Graceful degradation is deliberate: the federated ``ncs-docs`` stays useful as
    resolved-only until the multi-hour source index is built, and a stale path
    never takes the whole server down."""
    for raw in index_paths:
        path = Path(raw).resolve()
        if not path.is_file():
            print(f"warning: index not found, skipping: {path}", file=sys.stderr)
            continue
        try:
            c = open_corpus(path)
        except Exception as e:  # noqa: BLE001 — never let one bad index abort startup
            print(f"warning: could not open {path}: {e}", file=sys.stderr)
            continue
        name = _corpus_name(path.stem, set(_CORPORA))
        default_kind = "html" if c.source_format == "html" else "rst"
        _CORPORA[name] = OpenCorpus(
            name=name, db=c.db, docs_root=c.docs_root,
            has_source_kind=c.has_source_kind, source_format=c.source_format,
            embed_model=c.embed_model, default_kind=default_kind,
        )
        if c.embed_model and c.embed_model != EMBED_MODEL:
            print(f"warning: {name} built with {c.embed_model}, server expects {EMBED_MODEL}",
                  file=sys.stderr)
        print(f"corpus '{name}': {path.name} "
              f"(source_format={c.source_format}, root={c.docs_root})", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description="SDK docs hybrid MCP server")
    ap.add_argument("index", nargs="+", help="one or more docs index .sqlite files to federate")
    args = ap.parse_args()

    register(args.index)
    if not _CORPORA:
        sys.exit("no indexes could be opened — build one first with build_index.py")

    mcp.run()


if __name__ == "__main__":
    main()
