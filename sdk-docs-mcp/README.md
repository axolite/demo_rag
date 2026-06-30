# sdk-docs-mcp

A corpus-neutral **hybrid MCP engine** over Nordic/Zephyr SDK documentation. One
engine builds `.sqlite` indexes and serves them through one *or more* server
instances. Across **different SDKs** the surfaces stay isolated on purpose
(developers never mix SDKs in one project, and merging unrelated corpora would
pollute BM25/RRF ranking and collide same-named `CONFIG_*`/API symbols). Within a
single SDK, a server can **federate** several indexes and fuse their results.

| Server key | Index(es) | Corpus |
|---|---|---|
| `ncs-docs` | `ncs-1.6.1-resolved.sqlite` **+** `ncs-1.6.1-source.sqlite` | **federated** NCS 1.6.1: KB1 = resolved Sphinx **HTML** (real API signatures the RST stubs lack) and KB2 = the unified **RST + source code** (C/H, Kconfig, devicetree, …). One search fuses both, labelled by `source_kind ∈ {html, rst, code}`. Both built locally per the runbook (`../docs/build-ncs-1.6.1-doc.md`). |
| `bm-docs`  | `nrf-bm.sqlite`    | **sdk-nrf-bm** (nRF Baremetal) repo (`../sdk-nrf-bm/`) — single index, unchanged. |

The two NCS knowledge bases are **complementary**: KB1 carries the *rendered* API
surface (doxygen/breathe signatures); KB2 carries the prose **and** the real
C/Kconfig source it's drawn from, so a single query — e.g. *"can I run BLE legacy
and extended advertising at the same time?"* — returns **both** a doc hit and a
source-code hit (e.g. from `zephyr/subsys/bluetooth`), fused into one ranked list.
The standalone RST-only `ncs-docs` index it replaces has been **retired**.

Each index fuses three retrieval signals so both exact-symbol and conceptual
queries work against its *pinned* corpus:

| Signal | Store | Good at |
|---|---|---|
| **BM25** keyword | SQLite FTS5 (`tokenchars '_'`) | exact `CONFIG_*`, API names, file paths |
| **Dense vectors** | sqlite-vec (`jina-embeddings-v2-base-code`, cosine) | "how do I…" conceptual recall |
| **Xref graph** | `links` table (`:ref:`/`:doc:`/`:option:`/`:file:`) | jumping to related sections |

BM25 and vector results are merged with **Reciprocal Rank Fusion**. When a server
federates several indexes, each corpus's BM25 + vector candidates feed **one
namespaced RRF** (keyed by `(corpus, local_id)`), so the fused ranking is global.
The interface is *pointer-first*: `search_docs` returns locations + snippets; the
agent then `Read`s the real source (or calls `get_section`/`get_doc`) for exactness.

## Tools

Each server exposes the same four tools, namespaced by its key (`ncs-docs__…`,
`bm-docs__…`):

| Tool | Purpose |
|---|---|
| `search_docs(query, k=8, mode=hybrid\|keyword\|semantic, source=None)` | Find sections; returns `corpus`, `source_kind`, repo, file, anchor, breadcrumb, line range, citation, snippet. Append `*` in `keyword` mode for symbol-family prefix search (`CONFIG_BT*`). `source` filters by origin, e.g. `["code"]` (source-only) or `["rst","html"]` (docs-only). |
| `get_section(id)` | Full text of one section. `id` is a bare int (single index) or a self-describing `"corpus:local"` string (federated). |
| `get_doc(path, corpus=None)` | Full doc/source file by root-relative path; `corpus` pins which root to resolve against (each NCS corpus's root is the same west clone, so it serves rst/code/resolved alike). |
| `related(id)` | Resolved xref neighbours (outgoing + incoming) plus unresolved edges. Code sections carry no edges (v1) and return empties. |

## Build an index

The `.sqlite` files are reproducible — not magic. `--docs` and `--out` are always
required; `--format html` adds `--source-root`, `--format source` ingests both RST
and code from one west clone (`--docs` == `--code-root`). Use `python -u` so the
6-stage progress isn't swallowed by block buffering on a redirected stdout:

```bash
# sdk-nrf-bm (baremetal)
uv run --project sdk-docs-mcp python -u sdk-docs-mcp/build_index.py \
    --docs sdk-nrf-bm --out sdk-docs-mcp/nrf-bm.sqlite

# NCS 1.6.1 RESOLVED (KB1) — ingest a built Sphinx HTML tree instead of RST.
# --source-root is the west clone the HTML was built from: citation source + docs root.
uv run --project sdk-docs-mcp python -u sdk-docs-mcp/build_index.py \
    --format html --docs /c/ncs-docbuild/out/_build/html \
    --source-root /c/ncs-docbuild/src --out sdk-docs-mcp/ncs-1.6.1-resolved.sqlite

# NCS 1.6.1 SOURCE-TRUTH (KB2) — unified RST + symbol-chunked code from the SAME
# west clone (--docs and --code-root are the same clone). This is the heavy build.
# Fast first pass to validate the pipeline (~1–1.5 h, ~200 MB):
uv run --project sdk-docs-mcp python -u sdk-docs-mcp/build_index.py \
    --format source --docs /c/ncs-docbuild/src --code-root /c/ncs-docbuild/src \
    --no-tests --code-granularity file --out sdk-docs-mcp/ncs-1.6.1-source.sqlite
# Full production build (per-symbol anchors + samples + tests, ~5–7 h, ~0.8–1.1 GB):
uv run --project sdk-docs-mcp python -u sdk-docs-mcp/build_index.py \
    --format source --docs /c/ncs-docbuild/src --code-root /c/ncs-docbuild/src \
    --out sdk-docs-mcp/ncs-1.6.1-source.sqlite
```

`--format source` levers: `--code-granularity file` is one chunk per file (≈3–5×
fewer chunks, no symbol-precise anchors) vs the default `symbol`; `--no-tests`
drops `*/tests/*` (~−21% of C/H). Both NCS indexes are **built locally, not
committed** (KB2 is ~1 GB and resolves `get_doc` against the machine-local clone);
`ncs-1.6.1-source.sqlite` is gitignored.

The per-section `repo` field is the first path component under `--docs` (RST) or
the docset/scope label (HTML/source). The first build downloads the embedding
model (~640 MB) once via `fastembed`; subsequent builds reuse the cache. Extra
option: `--threads <n>`.

`--format html` parses the resolved Sphinx output (`html_chunker.py`): one
section per Sphinx section node, breathe API blocks (`<dl class="c function">`)
kept in the text and their `dt[id]` recorded as anchors, internal `<a
class="reference">` links resolved to concrete neighbours, and each rendered page
mapped back to its source `.rst` for citations (unique-suffix match per docset;
generated `kconfig`/`nrfx` cite the rendered page). See the runbook for the full
Docker build that produces the HTML.

`--format source` parses code with `code_chunker.py`: a pure-regex brace matcher
emits one chunk per top-level C/C++ symbol (function / struct / enum / typedef /
top-level `#define` / `*_DEFINE(...)`) with the **symbol name as the `anchor``;
Kconfig is chunked per `config`/`menuconfig` (`anchor=CONFIG_<NAME>`); devicetree /
CMake / yaml / linker / asm fall back to overlapping line windows. A per-file
`try/except` → line-window fallback means a parser miss never aborts the build.
Scope is positive-listed (`zephyr`, `nrf`, `nrfxlib`, `bootloader/mcuboot`,
`modules/hal/{nordic,cmsis,libmetal}`); the RST half is ingested from the same
clone, scoped to those repos. Both halves merge into one index with a
`source_kind ∈ {rst, code}` column.

## Run / wire up

Registered in the repo's `.mcp.json` — the server takes **one or more** index
paths (`nargs="+"`); listing several federates them behind one key:

```jsonc
"ncs-docs": {
  "command": "uv",
  "args": ["run", "--project", "sdk-docs-mcp", "sdk-docs-mcp",
           "sdk-docs-mcp/ncs-1.6.1-resolved.sqlite",
           "sdk-docs-mcp/ncs-1.6.1-source.sqlite"]
},
"bm-docs": {
  "command": "uv",
  "args": ["run", "--project", "sdk-docs-mcp", "sdk-docs-mcp", "sdk-docs-mcp/nrf-bm.sqlite"]
}
```

Each corpus opens read-only and resolves its docs root from
`meta.docs_root_relative` (relative for a portable snapshot, absolute for the
external west clone). A **missing or unreadable index is skipped with a warning**
rather than aborting the server — so `ncs-docs` runs as resolved-only until the
heavy `ncs-1.6.1-source.sqlite` is built. The corpus name is the filename stem's
last segment (`ncs-1.6.1-source → source`, `…-resolved → resolved`). The embedding
model loads lazily on the first semantic/hybrid query, and the query vector is
embedded once and reused across corpora.

## Layout

```
build_index.py            corpus -> <name>.sqlite (ingest_rst | ingest_html | ingest_code/source, shared embed/write tail)
sdk_docs_mcp/
  chunker.py              RST/MD section splitter + xref extractor (corpus-neutral)
  html_chunker.py         resolved-Sphinx-HTML section splitter + xref extractor
  code_chunker.py         C/Kconfig/dts/… symbol splitter (regex brace matcher)
  embed.py                fastembed wrapper (model pinned)
  store.py                meta helpers + open_corpus() (shared read/write seam, source_kind probe)
  server.py               FastMCP server: 4 tools + namespaced RRF + federation
tests/test_html_chunker.py  end-to-end checks for the --format html path
tests/test_code_chunker.py  code chunker + ingest_source + federated-server checks (no ONNX)
ncs-1.6.1-resolved.sqlite NCS resolved (HTML) index  — built locally per the runbook
ncs-1.6.1-source.sqlite   NCS unified RST+code index — built locally (gitignored, ~1 GB)
nrf-bm.sqlite             committed prebuilt baremetal index
```
