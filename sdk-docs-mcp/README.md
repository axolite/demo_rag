# sdk-docs-mcp

A corpus-neutral **hybrid MCP engine** over Nordic/Zephyr SDK documentation. One
engine builds and serves a **separate `.sqlite` index per SDK** — the indexes and
MCP surfaces stay isolated on purpose (developers never mix SDKs in one project,
and merging corpora would pollute BM25/RRF ranking, collide same-named
`CONFIG_*`/API symbols, and bleed the xref graph across SDK boundaries).

Indexes are served by one server instance each:

| Server key | Index | Corpus |
|---|---|---|
| `ncs-docs-resolved` | `ncs-1.6.1-resolved.sqlite` | the **resolved** Sphinx HTML build of NCS 1.6.1 — real API signatures the RST stubs lack; citations point back to the snapshot. Built via the runbook (`../docs/build-ncs-1.6.1-doc.md`). |
| `bm-docs`  | `nrf-bm.sqlite`    | **sdk-nrf-bm** (nRF Baremetal) repo (`../sdk-nrf-bm/`) |

The standalone RST-only `ncs-docs` index has been **retired**. Its RST snapshot is
being folded into a **unified source-code + RST** index (built with the
`--format`/`--code-root` path), which together with the resolved-HTML index forms two
complementary NCS knowledge bases: the resolved index carries the *rendered* API
surface (doxygen/breathe signatures), the unified index carries the prose **and** the
real C/Kconfig source it's drawn from.

Each index fuses three retrieval signals so both exact-symbol and conceptual
queries work against its *pinned* corpus:

| Signal | Store | Good at |
|---|---|---|
| **BM25** keyword | SQLite FTS5 (`tokenchars '_'`) | exact `CONFIG_*`, API names, file paths |
| **Dense vectors** | sqlite-vec (`jina-embeddings-v2-base-code`, cosine) | "how do I…" conceptual recall |
| **Xref graph** | `links` table (`:ref:`/`:doc:`/`:option:`/`:file:`) | jumping to related sections |

BM25 and vector results are merged with **Reciprocal Rank Fusion**. The interface
is *pointer-first*: `search_docs` returns locations + snippets; the agent then
`Read`s the real RST (or calls `get_section`/`get_doc`) for exactness.

## Tools

Each server exposes the same four tools, namespaced by its key (`ncs-docs__…`,
`bm-docs__…`):

| Tool | Purpose |
|---|---|
| `search_docs(query, k=8, mode=hybrid\|keyword\|semantic)` | Find sections; returns repo, file, anchor, breadcrumb, line range, citation, snippet. Append `*` in `keyword` mode for symbol-family prefix search (`CONFIG_BT*`). |
| `get_section(id)` | Full text of one section. |
| `get_doc(path)` | Full documentation file by repo-relative path. |
| `related(id)` | Resolved xref neighbours (outgoing + incoming) plus unresolved edges. |

## Build an index

The committed `.sqlite` files are reproducible — not magic. Each index is built
from one `--docs` root; `--docs` and `--out` are required. Use `python -u` so the
6-stage progress isn't swallowed by block buffering on a redirected stdout:

```bash
# sdk-nrf-bm (baremetal)
uv run --project sdk-docs-mcp python -u sdk-docs-mcp/build_index.py \
    --docs sdk-nrf-bm --out sdk-docs-mcp/nrf-bm.sqlite

# NCS 1.6.1 RESOLVED — ingest a built Sphinx HTML tree instead of RST.
# --source-root is the west clone the HTML was built from: citation source + docs root.
uv run --project sdk-docs-mcp python -u sdk-docs-mcp/build_index.py \
    --format html --docs /c/ncs-docbuild/out/_build/html \
    --source-root /c/ncs-docbuild/src --out sdk-docs-mcp/ncs-1.6.1-resolved.sqlite
```

The per-section `repo` field is the first path component under `--docs` (RST) or
the docset label (HTML). The first build downloads the embedding model
(~640 MB) once via `fastembed`; subsequent builds reuse the cache. Extra option:
`--threads <n>`.

`--format html` parses the resolved Sphinx output (`html_chunker.py`): one
section per Sphinx section node, breathe API blocks (`<dl class="c function">`)
kept in the text and their `dt[id]` recorded as anchors, internal `<a
class="reference">` links resolved to concrete neighbours, and each rendered page
mapped back to its source `.rst` for citations (unique-suffix match per docset;
generated `kconfig`/`nrfx` cite the rendered page). See the runbook for the full
Docker build that produces the HTML.

## Run / wire up

Registered in the repo's `.mcp.json` — one engine, different index files per
instance:

```jsonc
"ncs-docs-resolved": {
  "command": "uv",
  "args": ["run", "--project", "sdk-docs-mcp", "sdk-docs-mcp", "sdk-docs-mcp/ncs-1.6.1-resolved.sqlite"]
},
"bm-docs": {
  "command": "uv",
  "args": ["run", "--project", "sdk-docs-mcp", "sdk-docs-mcp", "sdk-docs-mcp/nrf-bm.sqlite"]
}
```

Each server opens its index read-only and resolves the docs root from
`meta.docs_root_relative` (relative to the index file), so it works from any
clone. The embedding model loads lazily on the first semantic/hybrid query.

## Layout

```
build_index.py            corpus -> <name>.sqlite (ingest_rst | ingest_html, shared embed/write tail)
sdk_docs_mcp/
  chunker.py              RST/MD section splitter + xref extractor (corpus-neutral)
  html_chunker.py         resolved-Sphinx-HTML section splitter + xref extractor
  embed.py                fastembed wrapper (model pinned)
  store.py                meta helpers + open_corpus() (shared read/write seam)
  server.py               FastMCP server: 4 tools + RRF
tests/test_html_chunker.py  end-to-end checks for the --format html path
ncs-1.6.1-resolved.sqlite NCS resolved (HTML) index — built locally per the runbook
nrf-bm.sqlite             committed prebuilt baremetal index
```
