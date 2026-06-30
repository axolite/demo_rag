#!/usr/bin/env python
"""Build a docs index ``.sqlite`` from a Nordic/Zephyr SDK docs snapshot.

One-time, deterministic build. Output is a single portable SQLite file holding
four logical stores (sections / FTS5 / sqlite-vec / xref links) plus a meta
table. Two ingest front-ends share one embed/write/meta tail:

* ``--format rst`` (default) — the frozen RST/MD source snapshot. The corpus is
  whatever ``--docs`` points at; the per-section ``repo`` is the first path
  component under that root. E.g.::

      uv run --project sdk-docs-mcp python -u sdk-docs-mcp/build_index.py \\
          --docs sdk-nrf-bm --out sdk-docs-mcp/nrf-bm.sqlite

* ``--format html`` — the *resolved* Sphinx HTML build (real API signatures from
  breathe). ``--docs`` is the ``_build/html`` tree; ``--source-root`` is the
  **west clone** the HTML was built from (a commit-exact NCS v1.6.1 workspace),
  used both to map each rendered page back to its source ``.rst``/``.md``
  (citations) and as the docs root the server serves. E.g.::

      uv run --project sdk-docs-mcp python -u sdk-docs-mcp/build_index.py \\
          --format html --docs C:/ncs-docbuild/out/_build/html \\
          --source-root C:/ncs-docbuild/src --out sdk-docs-mcp/ncs-1.6.1-resolved.sqlite
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import sqlite3
import sys
import time
from pathlib import Path

import sqlite_vec

sys.path.insert(0, str(Path(__file__).parent))
from sdk_docs_mcp import EMBED_DIM, EMBED_MODEL, SCHEMA_VERSION  # noqa: E402
from sdk_docs_mcp.chunker import Section, chunk_file, clean_for_embedding, extract_links  # noqa: E402
from sdk_docs_mcp.html_chunker import chunk_html_file  # noqa: E402
from sdk_docs_mcp.store import write_meta  # noqa: E402

SKIP_DIR_PARTS = {"_build", "_doxygen", ".git", "__pycache__"}
# HTML output dirs/files that carry no indexable doc content.
SKIP_HTML_DIR_PARTS = {"_static", "_sources", "_images", "_downloads", "__pycache__"}
SKIP_HTML_NAMES = {"genindex.html", "search.html", "py-modindex.html", "objects.inv"}

# Maps an HTML docset to the top-level folder of its source repo in the west
# clone. mcuboot lives under ``bootloader/`` in the NCS west workspace. Generated
# (``kconfig``) and module-sourced (``nrfx`` ← modules/hal/nordic) docsets have no
# mapped source and cite their rendered page instead.
DOCSET_TO_SOURCE_TOP = {
    "nrf": "nrf", "zephyr": "zephyr", "nrfxlib": "nrfxlib", "mcuboot": "bootloader",
}
# Dirs pruned while indexing source paths (the clone carries large .git/build trees).
_SKIP_WALK_DIRS = {".git", "_build", "_doxygen", "build", "__pycache__"}

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


# --------------------------------------------------------------------------- #
# RST ingest (the frozen source snapshot)
# --------------------------------------------------------------------------- #


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


def ingest_rst(docs_root: Path) -> tuple[list[Section], list[tuple], list[str]]:
    """Chunk + cross-reference the RST snapshot. Returns (sections, link_rows, embed_texts)."""
    files = discover_files(docs_root)
    print(f"[1/6] discovered {len(files)} doc files under {docs_root}")

    sections: list[Section] = []
    for path, repo, rel in files:
        sections.extend(chunk_file(path, repo, rel))
    for i, sec in enumerate(sections, start=1):
        sec.id = i  # type: ignore[attr-defined]
    print(f"[2/6] chunked into {len(sections)} sections")

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

    embed_texts = [clean_for_embedding(sec.text) for sec in sections]
    return sections, link_rows, embed_texts


# --------------------------------------------------------------------------- #
# HTML ingest (the resolved Sphinx build)
# --------------------------------------------------------------------------- #


def discover_html_files(html_root: Path) -> list[tuple[Path, str, str]]:
    """Return (abs_path, docset, page_docname) for every content .html page.

    ``page_docname`` is the path under the HTML root without ``.html`` — the
    docname Sphinx renders to, and the namespace we resolve xrefs in."""
    out: list[tuple[Path, str, str]] = []
    for path in sorted(html_root.rglob("*.html")):
        rel = path.relative_to(html_root)
        if SKIP_HTML_DIR_PARTS & set(rel.parts):
            continue
        if rel.name in SKIP_HTML_NAMES:
            continue
        docset = rel.parts[0]
        out.append((path, docset, rel.with_suffix("").as_posix()))
    return out


class SourceIndex:
    """Maps a rendered docname back to its source file in the west clone.

    The rendered output path *is* the Sphinx docname, but the clone stores files
    at their full repo path (``nrf/doc/nrf/foo.rst``, ``bootloader/mcuboot/docs/
    foo.md``), so we suffix-match the docset-relative tail against the clone —
    constrained to the docset's top folder so same-named pages in other docsets
    can't collide. Only the mapped top folders are walked (``.git``/build dirs
    pruned), so indexing a multi-GB clone stays cheap."""

    def __init__(self, source_root: Path, tops):
        self.root = source_root
        self.by_top: dict[str, list[tuple[str, str]]] = {}  # top -> [(nosuffix, rel)]
        self._lines: dict[str, list[str]] = {}
        for top in sorted({t for t in tops if t}):
            top_dir = source_root / top
            if not top_dir.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(top_dir):
                dirnames[:] = [d for d in dirnames if d not in _SKIP_WALK_DIRS]
                for fn in filenames:
                    if fn.lower().endswith((".rst", ".md")):
                        rel = (Path(dirpath) / fn).relative_to(source_root).as_posix()
                        self.by_top.setdefault(top, []).append((rel.rsplit(".", 1)[0], rel))

    def match(self, docset: str, page_docname: str) -> str | None:
        top = DOCSET_TO_SOURCE_TOP.get(docset)
        if not top or top not in self.by_top:
            return None
        tail = page_docname.split("/", 1)[1] if "/" in page_docname else page_docname
        hits = [rel for nosuffix, rel in self.by_top[top]
                if nosuffix == tail or nosuffix.endswith("/" + tail)]
        return hits[0] if len(hits) == 1 else None

    def anchor_line(self, rel: str, anchor: str) -> int:
        """1-based line of ``.. _anchor:`` in the matched source, else 0."""
        if not anchor:
            return 0
        lines = self._lines.get(rel)
        if lines is None:
            try:
                lines = (self.root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                lines = []
            self._lines[rel] = lines
        # An explicit label may be rendered as a hyphenated id (and vice versa).
        cands = {anchor, anchor.replace("-", "_"), anchor.replace("_", "-")}
        pat = re.compile(r"^\.\.\s+_(" + "|".join(re.escape(c) for c in cands) + r"):\s*$")
        for i, line in enumerate(lines, start=1):
            if pat.match(line):
                return i
        return 0


def build_html_maps(sections: list[Section]) -> tuple[dict[str, int], dict[str, int]]:
    """(anchor_map, doc_map) for resolving HTML xrefs.

    ``anchor_map`` keys both ``docname#frag`` (precise) and bare ``frag``
    (cross-page fallback); ``doc_map`` keys ``docname`` -> first section."""
    anchor_map: dict[str, int] = {}
    doc_map: dict[str, int] = {}
    for sec in sections:
        dn = getattr(sec, "docname", "")
        if dn:
            doc_map.setdefault(dn, sec.id)  # type: ignore[attr-defined]
        for a in sec.all_anchors:
            anchor_map.setdefault(f"{dn}#{a}", sec.id)  # type: ignore[attr-defined]
            anchor_map.setdefault(a, sec.id)  # type: ignore[attr-defined]
    return anchor_map, doc_map


def resolve_html_edge(target: str, anchor_map: dict[str, int], doc_map: dict[str, int]) -> int | None:
    if "#" in target:
        dn, frag = target.split("#", 1)
        if frag:
            return anchor_map.get(f"{dn}#{frag}") or anchor_map.get(frag)
        return doc_map.get(dn)
    return doc_map.get(target)


def ingest_html(html_root: Path, source_root: Path) -> tuple[list[Section], list[tuple], list[str]]:
    """Chunk the resolved HTML, map citations to source, resolve xrefs."""
    files = discover_html_files(html_root)
    print(f"[1/6] discovered {len(files)} HTML pages under {html_root}")
    src = SourceIndex(source_root, DOCSET_TO_SOURCE_TOP.values())

    sections: list[Section] = []
    mapped_files = 0
    for path, docset, page_docname in files:
        html = path.read_text(encoding="utf-8", errors="replace")
        secs = chunk_html_file(html, docset, page_docname)
        src_rel = src.match(docset, page_docname)
        if src_rel:
            mapped_files += 1
            for s in secs:
                s.repo = docset  # clean label (nrf/zephyr/nrfxlib/mcuboot)
                s.file_path = src_rel  # clone-relative source path
                s.line_start = s.line_end = src.anchor_line(src_rel, s.anchor)
        sections.extend(secs)
    for i, sec in enumerate(sections, start=1):
        sec.id = i  # type: ignore[attr-defined]
    print(f"[2/6] chunked into {len(sections)} sections "
          f"({mapped_files}/{len(files)} pages mapped to source .rst)")

    anchor_map, doc_map = build_html_maps(sections)
    link_rows: list[tuple[int, str, str, int | None]] = []
    for sec in sections:
        for target in getattr(sec, "raw_links", []):
            resolved = resolve_html_edge(target, anchor_map, doc_map)
            link_rows.append((sec.id, "ref", target, resolved))  # type: ignore[attr-defined]
    resolved_ct = sum(1 for r in link_rows if r[3] is not None)
    print(f"[3/6] extracted {len(link_rows)} xref edges ({resolved_ct} resolved)")

    # HTML text is already plain prose; the embedder caps length itself.
    embed_texts = [sec.text for sec in sections]
    return sections, link_rows, embed_texts


# --------------------------------------------------------------------------- #
# Shared tail: embed -> write -> meta
# --------------------------------------------------------------------------- #


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--docs", type=Path, required=True,
                    help="root to index: an RST snapshot, or a _build/html tree for --format html")
    ap.add_argument("--out", type=Path, required=True,
                    help="output SQLite path (e.g. sdk-docs-mcp/ncs-1.6.1-resolved.sqlite)")
    ap.add_argument("--format", choices=("rst", "html"), default="rst",
                    help="source format: rst snapshot (default) or resolved Sphinx html")
    ap.add_argument("--source-root", type=Path, default=None,
                    help="west clone the html was built from: used for html citation "
                         "mapping + as the served docs root (required with --format html)")
    ap.add_argument("--threads", type=int, default=None, help="ONNX threads")
    args = ap.parse_args()

    docs_root = args.docs.resolve()
    out_path = args.out.resolve()
    if not docs_root.is_dir():
        ap.error(f"docs root not found: {docs_root}")

    t0 = time.time()
    if args.format == "html":
        if args.source_root is None:
            ap.error("--source-root is required with --format html")
        source_root = args.source_root.resolve()
        if not source_root.is_dir():
            ap.error(f"source root not found: {source_root}")
        sections, link_rows, embed_texts = ingest_html(docs_root, source_root)
        meta_root = source_root  # the west clone; get_doc serves its source files
    else:
        sections, link_rows, embed_texts = ingest_rst(docs_root)
        meta_root = docs_root

    # --- embed -------------------------------------------------------------
    from sdk_docs_mcp.embed import Embedder, to_blob

    print(f"[4/6] embedding with {EMBED_MODEL} (first run downloads the model)…")
    embedder = Embedder(threads=args.threads)
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

    if args.format == "html":
        # The source root is the external west clone; store its ABSOLUTE path so
        # get_doc resolves into the live checkout (a relative path would escape the
        # repo as ../../…). store.open_corpus does index.parent / value, which
        # yields the absolute path unchanged.
        docs_root_value = meta_root.as_posix()
    else:
        docs_root_value = posixpath.relpath(meta_root.as_posix(), out_path.parent.as_posix())
    meta = {
        "schema_version": str(SCHEMA_VERSION),
        "embed_model": EMBED_MODEL,
        "embed_dim": str(EMBED_DIM),
        "docs_root_relative": docs_root_value,
        "section_count": str(len(sections)),
        "link_count": str(len(link_rows)),
        "source_format": args.format,
    }
    if args.format == "html":
        meta["build_note"] = (
            "Resolved Sphinx HTML built from a fresh commit-exact NCS v1.6.1 west "
            "clone; citations map each rendered page back to its source .rst/.md in "
            "that clone (docs_root_relative holds the clone's absolute path). mcuboot "
            "lives under bootloader/; kconfig/nrfx have no mapped source and cite the "
            "rendered page."
        )
    write_meta(db, meta)

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
