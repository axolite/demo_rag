# Giving Claude Code Access to the NCS 1.6.1 Docs

**Date:** 2026-06-15
**Scope:** How best to expose `ncs-1.6.1-docs/` to Claude Code (and other LLM tools) for querying.
**Status:** Recommendation only — nothing in the repo was modified to produce this.

> **Historical (retained for reference).** This is the original recommendation that
> led to the hybrid MCP server. The implementation has since gone further than the
> single RST index proposed here: it now also indexes the **resolved HTML** docs and
> the NCS **source code**, and **combines** them into one federated searchable
> entity. The rationale below — why hybrid retrieval beats agentic grep, a bare
> vector DB, or DeepWiki for a pinned snapshot — still holds.

---

## 1. Findings — what's actually in the repo

| Property | Value |
|---|---|
| Docs root | `ncs-1.6.1-docs/` |
| Total size | ~126 MB |
| Sub-projects | `zephyr/`, `nrf/`, `mcuboot/`, `nrfxlib/`, `tfm/` |
| Total files | ~2,956 |
| **`.rst` source files** | **1,757** |
| RST text volume | ~11 MB / ~282 k lines / ~1.35 M words (≈ 2.5–3 M tokens) |
| Non-text assets | 374 PNG, 205 JPG, 171 SVG, 51 vsdx, 10 PDF (diagrams) |
| Distinct `CONFIG_*` symbols | **1,575** |
| Build system | Sphinx (multiple `conf.py`; a `_static/html/index.html` exists) |

Three structural facts drive the design:

1. **Clean section structure.** Every doc uses `.. _anchor:` labels plus underline headers
   (`####`, `****`). This makes it straightforward to chunk by section and emit **stable
   citation anchors**, e.g. `nrf/doc/.../mcuboot.rst#mcuboot_ncs`.

2. **1,575 distinct `CONFIG_*` symbols** (plus API names, sample names, file paths). Exact-token
   retrieval is essential — pure semantic embeddings routinely mangle tokens like
   `CONFIG_BOOTLOADER_MCUBOOT`. **This is the single biggest reason to go hybrid, not vector-only.**

3. **Sphinx cross-references everywhere** (`:ref:`, `:doc:`, `:option:`, `:file:`). These edges
   form a real **document graph (the "DAG")** that can be traversed — often more useful than raw
   cosine similarity.

The corpus is a **frozen 1.6.1 snapshot** — it does not change — so indexing is a one-time cost
with **no re-indexing / staleness concerns**.

### Already configured
`.mcp.json` currently wires up `deepwiki`, `mdn`, and `chrome-devtools`. Note that **DeepWiki tracks
upstream `sdk-nrf`, not this pinned 1.6.1 snapshot**, so it cannot give version-accurate answers for
this corpus.

---

## 2. Requirements (from clarifying questions)

- **Query style:** a **mix** of exact keyword/symbol lookups *and* conceptual "how do I…" questions.
- **Consumer:** must be **shared across tools/team**, not just this one Claude Code project.
- **Infrastructure appetite:** OK to stand up a **full MCP server / vector DB**.

These rule out a pure agentic-grep (QMD-style) approach on its own (no semantic recall) and a bare
vector DB on its own (loses exact symbols + ignores the xref graph).

---

## 3. Options considered

| Approach | Setup | Exact symbols (`CONFIG_*`) | Semantic recall | Staleness | Shareable | Verdict |
|---|---|---|---|---|---|---|
| **Agentic ripgrep + index** (QMD-style) | ~zero | Exact | Weak | n/a (reads source) | File-based | Great base, no semantics |
| **Vector DB / RAG only** | High | Fuzzy (misses tokens) | Strong | Re-index | Via server | Loses symbols + graph |
| **DeepWiki MCP** (already present) | none | — | Good | Tracks upstream, not 1.6.1 | Yes | Wrong version |
| **★ Hybrid MCP server (recommended)** | Medium | Exact (BM25) | Strong (vectors) | Build once | MCP for all tools | Best fit |

---

## 4. Recommendation — one hybrid MCP server that fuses all three ideas

Combine, don't choose: **QMD's "point at the source, let the agent Read it"** + **BM25 keyword** +
**dense vectors** + **the xref DAG**, behind a single MCP server the whole team mounts via `.mcp.json`.

### 4.1 Storage — a single portable SQLite file

One `index.sqlite` containing:

- **`FTS5` table** → BM25 keyword search. Nails `CONFIG_*`, API names, file paths.
- **`sqlite-vec` table** → dense embeddings. Handles "how do I configure the BLE controller?".
- **`links` table** → the Sphinx xref graph (the DAG).
- Keyword + vector results merged with **Reciprocal Rank Fusion (RRF)**.

One file, no separate DB process, trivially shareable (commit it, or rebuild from the script).

### 4.2 Embeddings — local, build once

Local model (e.g. `bge-small-en-v1.5` or `nomic-embed-text` via `fastembed`). Because the snapshot is
frozen, embed **once**, commit the DB, and never re-index. No API cost, fully offline.

### 4.3 MCP server — `FastMCP` (Python)

Pointer-first interface (returns locations + snippets, not giant blobs):

| Tool | Purpose |
|---|---|
| `search_docs(query, k, mode=hybrid\|keyword\|semantic)` | Returns repo, `file_path`, anchor, header breadcrumb, line range + snippet |
| `get_section(id)` | Full section text on demand |
| `get_doc(path)` | Full document on demand |
| `related(id)` | Walk the xref DAG to neighboring sections |

The pointer-first design lets Claude Code do a cheap semantic/keyword hop, then `Read` the actual RST
for exactness — the best of QMD + RAG.

### 4.4 Distribution

Ship a `build_index.py` (RST parser → section chunker → FTS5 / vec / links tables) plus the prebuilt
`index.sqlite`. Add one entry to `.mcp.json`:

```jsonc
"ncs-docs": {
  "command": "uv",
  "args": ["run", "ncs-docs-mcp", "ncs-1.6.1-docs/index.sqlite"]
}
```

Team members get the doc search automatically when they open the repo.

---

## 5. Why not the alternatives (short form)

- **QMD / ripgrep alone:** no semantic recall — fails the "mixed query style" requirement.
- **Bare vector DB:** loses on the 1,575 exact symbols and ignores the rich xref graph.
- **DeepWiki (already in `.mcp.json`):** tracks upstream `sdk-nrf`, not the pinned 1.6.1 snapshot —
  keep it for general repo Q&A, but it won't give version-accurate answers. 

---

## 6. Proposed build plan (when approved)

1. `build_index.py`
   - Walk `ncs-1.6.1-docs/**/*.rst`.
   - Chunk by section using `.. _label:` anchors + underline headers; record breadcrumb path.
   - Strip Sphinx directives for embedding text, but **preserve** code blocks and `CONFIG_*` tokens.
   - Extract `:ref:` / `:doc:` / `:option:` / `:file:` targets into the `links` table.
   - Populate `FTS5`, compute embeddings into `sqlite-vec`.
2. `ncs-docs-mcp` server (`FastMCP`) implementing the four tools above with RRF fusion.
3. Add the `ncs-docs` entry to `.mcp.json`; commit `index.sqlite` (or document the one-line rebuild).
4. Smoke test: a symbol query (`CONFIG_BOOTLOADER_MCUBOOT`), a conceptual query, and a `related()`
   graph hop.

---

*Generated by Claude Code. Findings based on read-only inspection of `ncs-1.6.1-docs/` and `.mcp.json`.*
