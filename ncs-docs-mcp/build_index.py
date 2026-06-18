#!/usr/bin/env python
"""Build ``index.sqlite`` from the frozen NCS 1.6.1 documentation snapshot.

One-time, deterministic build. Output is a single portable SQLite file holding
four logical stores (sections / FTS5 / sqlite-vec / xref links) plus a meta
table. Run from anywhere:

    uv run --project ncs-docs-mcp python ncs-docs-mcp/build_index.py
"""

from __future__ import annotations

import argparse
import posixpath
import sqlite3
import sys
import time
from pathlib import Path

import sqlite_vec

sys.path.insert(0, str(Path(__file__).parent))
from ncs_docs_mcp import EMBED_DIM, EMBED_MODEL, SCHEMA_VERSION  # noqa: E402
from ncs_docs_mcp.chunker import Section, chunk_file, clean_for_embedding, extract_links  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

SKIP_DIR_PARTS = {"_build", "_doxygen", ".git", "__pycache__"}

SCHEMA = """
CREATE TABLE sections(
    id INTEGER PRIMARY KEY,
    repo TEXT NOT NULL,
    file_path TEXT NOT NULL,
    anchor TEXT,
    breadcrumb TEXT,
    header TEXT,
    line_start INTEGER,
    line_end INTEGER,
    text TEXT
);

-- BM25 keyword search. tokenchars '_' keeps CONFIG_BOOTLOADER_MCUBOOT atomic.
-- External content (content='sections') avoids duplicating the text column.
CREATE VIRTUAL TABLE fts_sections USING fts5(
    text, header, anchor,
    content='sections', content_rowid='id',
    tokenize = "unicode61 tokenchars '_'"
);

-- Dense vectors. distance_metric=cosine is REQUIRED — vec0 defaults to L2.
CREATE VIRTUAL TABLE vec_sections USING vec0(
    section_id INTEGER,
    embedding float[{dim}] distance_metric=cosine
);

CREATE TABLE links(
    src_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                  -- ref | doc | option | file
    target_raw TEXT NOT NULL,
    resolved_id INTEGER                  -- NULL if external / unresolved
);
CREATE INDEX idx_links_src ON links(src_id);
CREATE INDEX idx_links_resolved ON links(resolved_id);

CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
"""


def discover_files(docs_root: Path) -> list[tuple[Path, str, str]]:
    """Return (abs_path, repo, posix_rel_path) for every .rst/.md doc."""
    out: list[tuple[Path, str, str]] = []
    for path in sorted(docs_root.rglob("*")):
        if path.suffix.lower() not in (".rst", ".md"):
            continue
        rel = path.relative_to(docs_root)
        if SKIP_DIR_PARTS & set(rel.parts):
            continue
        repo = rel.parts[0]
        out.append((path, repo, rel.as_posix()))
    return out


def build_doc_map(sections: list[Section]) -> dict[str, int]:
    """file_path (no suffix) -> id of that file's first section, for :doc: resolution."""
    doc_map: dict[str, int] = {}
    for sec in sections:
        key = sec.file_path.rsplit(".", 1)[0]
        if key not in doc_map:
            doc_map[key] = sec.id  # type: ignore[attr-defined]
    return doc_map


def resolve_doc(target: str, src_path: str, doc_map: dict[str, int]) -> int | None:
    if ":" in target:  # intersphinx (e.g. mcuboot:index) — external
        return None
    t = target.strip()
    candidates: list[str] = []
    if t.startswith("/"):
        stripped = t.lstrip("/")
        candidates.append(stripped)
        candidates.append(posixpath.join(src_path.split("/", 1)[0], stripped))
    else:
        candidates.append(posixpath.normpath(posixpath.join(posixpath.dirname(src_path), t)))
        candidates.append(t)
    for c in candidates:
        if c in doc_map:
            return doc_map[c]
    # Last resort: unique suffix match.
    tail = t.lstrip("/")
    hits = [v for k, v in doc_map.items() if k == tail or k.endswith("/" + tail)]
    return hits[0] if len(hits) == 1 else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--docs", type=Path, default=REPO_ROOT / "ncs-1.6.1-docs",
                    help="documentation root (default: <repo>/ncs-1.6.1-docs)")
    ap.add_argument("--out", type=Path, default=SCRIPT_DIR / "index.sqlite",
                    help="output SQLite path (default: ncs-docs-mcp/index.sqlite)")
    ap.add_argument("--threads", type=int, default=None, help="ONNX threads")
    args = ap.parse_args()

    docs_root = args.docs.resolve()
    out_path = args.out.resolve()
    if not docs_root.is_dir():
        ap.error(f"docs root not found: {docs_root}")

    t0 = time.time()
    files = discover_files(docs_root)
    print(f"[1/6] discovered {len(files)} doc files under {docs_root}")

    # --- chunk -------------------------------------------------------------
    sections: list[Section] = []
    for path, repo, rel in files:
        sections.extend(chunk_file(path, repo, rel))
    for i, sec in enumerate(sections, start=1):
        sec.id = i  # type: ignore[attr-defined]
    print(f"[2/6] chunked into {len(sections)} sections")

    # --- resolve cross-references -----------------------------------------
    anchor_map: dict[str, int] = {}
    for sec in sections:
        for a in sec.all_anchors:
            anchor_map.setdefault(a, sec.id)  # type: ignore[attr-defined]
    doc_map = build_doc_map(sections)

    link_rows: list[tuple[int, str, str, int | None]] = []
    for sec in sections:
        for link in extract_links(sec.text):
            if link.kind == "ref":
                resolved = anchor_map.get(link.target)
            elif link.kind == "doc":
                resolved = resolve_doc(link.target, sec.file_path, doc_map)
            else:
                resolved = None  # option / file have no target section
            link_rows.append((sec.id, link.kind, link.target, resolved))  # type: ignore[attr-defined]
    resolved_ct = sum(1 for r in link_rows if r[3] is not None)
    print(f"[3/6] extracted {len(link_rows)} xref edges ({resolved_ct} resolved)")

    # --- embed -------------------------------------------------------------
    from ncs_docs_mcp.embed import Embedder, to_blob

    print(f"[4/6] embedding with {EMBED_MODEL} (first run downloads the model)…")
    embedder = Embedder(threads=args.threads)
    embed_texts = [clean_for_embedding(sec.text) for sec in sections]
    vectors: list[bytes] = []
    done = 0
    for vec in embedder.embed_documents(embed_texts):
        vectors.append(to_blob(vec))
        done += 1
        if done % 500 == 0:
            print(f"      {done}/{len(sections)} embedded")
    assert len(vectors) == len(sections)

    # --- write -------------------------------------------------------------
    print(f"[5/6] writing {out_path}")
    if out_path.exists():
        out_path.unlink()
    db = sqlite3.connect(out_path)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.executescript(SCHEMA.format(dim=EMBED_DIM))

    db.executemany(
        "INSERT INTO sections(id, repo, file_path, anchor, breadcrumb, header, "
        "line_start, line_end, text) VALUES (?,?,?,?,?,?,?,?,?)",
        [(s.id, s.repo, s.file_path, s.anchor, s.breadcrumb, s.header,  # type: ignore[attr-defined]
          s.line_start, s.line_end, s.text) for s in sections],
    )
    db.executemany(
        "INSERT INTO fts_sections(rowid, text, header, anchor) VALUES (?,?,?,?)",
        [(s.id, s.text, s.header, s.anchor) for s in sections],  # type: ignore[attr-defined]
    )
    db.executemany(
        "INSERT INTO vec_sections(section_id, embedding) VALUES (?,?)",
        [(s.id, blob) for s, blob in zip(sections, vectors)],  # type: ignore[attr-defined]
    )
    db.executemany(
        "INSERT INTO links(src_id, kind, target_raw, resolved_id) VALUES (?,?,?,?)",
        link_rows,
    )

    docs_root_rel = posixpath.relpath(docs_root.as_posix(), out_path.parent.as_posix())
    meta = {
        "schema_version": str(SCHEMA_VERSION),
        "embed_model": EMBED_MODEL,
        "embed_dim": str(EMBED_DIM),
        "docs_root_relative": docs_root_rel,
        "section_count": str(len(sections)),
        "link_count": str(len(link_rows)),
    }
    db.executemany("INSERT INTO meta(key, value) VALUES (?,?)", list(meta.items()))

    db.commit()
    db.execute("VACUUM")
    db.commit()
    db.close()

    size_mb = out_path.stat().st_size / 1e6
    print(f"[6/6] done in {time.time() - t0:.1f}s — {size_mb:.1f} MB, "
          f"{len(sections)} sections, {len(link_rows)} links")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
