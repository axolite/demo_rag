# zephyr_migration

A working repository supporting a migration onto the **nRF Connect SDK (NCS)** and
its **Bare Metal** variant. It bundles the documentation sources you need offline,
pins them to exact versions, and — the centerpiece — exposes them to Claude Code (and
any other MCP-capable tool) through a **hybrid, AI-queryable documentation server**.

The problem it solves: NCS 1.6.1 is a *frozen* target. Public AI tools (DeepWiki and
friends) track upstream `sdk-nrf`, not the pinned 1.6.1 snapshot, so they give answers
for the wrong version. This repo makes the **exact** docs searchable — by symbol *and*
by meaning — with citations back to the real source.

---

## What's in here

| Path | What it is |
|---|---|
| **`sdk-docs-mcp/`** | The corpus-neutral hybrid MCP documentation engine — see below. Ships a prebuilt index per corpus (`ncs-1.6.1.sqlite`, `ncs-1.6.1-resolved.sqlite`, `nrf-bm.sqlite`). |
| `ncs-1.6.1-docs/` | Frozen ~125 MB Sphinx doc snapshot of NCS **v1.6.1** (`zephyr/ nrf/ mcuboot/ nrfxlib/ tfm/`), pinned by commit in `MANIFEST.md`. |
| `docker/` | Pinned toolchain image + west-clone build script that render the snapshot into **resolved HTML** (real API reference), the input to `ncs-1.6.1-resolved.sqlite`. See `docs/build-ncs-1.6.1-doc.md`. |
| `sdk-nrf-bm/` | Local clone of `sdk-nrf-bm` (Bare Metal SDK) — the offline source of truth for headers, Kconfig, and samples, *and* the corpus behind the `bm-docs` server. Pinned + refreshable. |
| `docs/` | The recommendation that led to the server (`ideas/docs-access-recommendation.md`) and the full build runbook (`ncs-docs-mcp-build-guide.md`). |
| `.mcp.json` | Wires up `ncs-docs`, `ncs-docs-resolved`, and `bm-docs`, plus `deepwiki`, `mdn`, and `chrome-devtools`. |
| `sdk-nrf-bm.md` / `refresh-sdk-nrf-bm.sh` | Where to look in the Bare Metal clone, and how to refresh it. |

---

## The centerpiece: a hybrid documentation MCP engine

The "thing that indexes the docs and turns them into something you can ask questions"
is a **RAG-style hybrid retrieval engine**. One corpus-neutral engine builds a
**separate index per SDK** and serves each through its own MCP instance — `ncs-docs`
(NCS 1.6.1) and `bm-docs` (sdk-nrf-bm) — kept isolated so their `CONFIG_*`/API symbols
and xref graphs never bleed across SDK boundaries. Each index fuses three signals out of
one portable SQLite file so that both *exact-symbol* and *conceptual* queries work
against its pinned corpus:

| Signal | Store | Good at |
|---|---|---|
| **BM25 keyword** | SQLite FTS5 (`tokenchars '_'`) | exact `CONFIG_*`, API names, file paths |
| **Dense vectors** | sqlite-vec (`jina-embeddings-v2-base-code`, cosine) | "how do I…" conceptual recall |
| **Xref graph** | `links` table (`:ref:`/`:doc:`/`:option:`/`:file:`) | jumping to related sections |

Keyword and vector hits are merged with **Reciprocal Rank Fusion (RRF)**. The interface
is **pointer-first**: a search returns *locations* (repo / file / anchor / breadcrumb /
line range) plus a snippet, and the agent then reads the real RST for exactness — the
best of grep-the-source and semantic search.

**By the numbers:** NCS 1.6.1 — 1,824 files → 15,176 sections → 10,555 cross-reference
edges (6,245 resolved) → a 67 MB `ncs-1.6.1.sqlite`. sdk-nrf-bm — 195 files → 1,160
sections → 1,006 edges (430 resolved) → an 8 MB `nrf-bm.sqlite`.

**RST vs. resolved.** The NCS 1.6.1 RST snapshot is strong on prose but ~18% of its
files are doxygen *stub* pages — the real API reference (function signatures, struct
fields) is injected only by a Sphinx + breathe build. So a second index,
`ncs-1.6.1-resolved.sqlite` (served as `ncs-docs-resolved`), is built from the
**resolved HTML** and sits alongside the RST `ncs-docs`: same four tools, but it can
answer exact-API questions the stubs can't, while citations still point back to the
source `.rst`. Producing it is a one-time Docker build documented in
`docs/build-ncs-1.6.1-doc.md`.

### Tools it exposes

| Tool | Purpose |
|---|---|
| `search_docs(query, k=8, mode=hybrid\|keyword\|semantic)` | Find sections; returns repo, file, anchor, breadcrumb, line range, citation, snippet. Append `*` in `keyword` mode for symbol-family search (`CONFIG_BT*`). |
| `get_section(id)` | Full text of one section. |
| `get_doc(path)` | Full documentation file by repo-relative path (read fresh from disk). |
| `related(id)` | Resolved xref neighbours (outgoing + incoming) plus unresolved edges. |

---

## Quick start

Both servers are already registered in `.mcp.json`, so once you open this repo in Claude
Code and reload MCP servers, the four tools of each (`ncs-docs__…`, `bm-docs__…`) are
available — no setup, the indexes are committed.

To use them from the command line or rebuild an index:

```bash
# Run a server (what .mcp.json does) — one index per instance
uv run --project sdk-docs-mcp sdk-docs-mcp sdk-docs-mcp/ncs-1.6.1.sqlite   # ncs-docs
uv run --project sdk-docs-mcp sdk-docs-mcp sdk-docs-mcp/nrf-bm.sqlite      # bm-docs

# Rebuild an index from its corpus (--docs and --out are required; first run
# downloads the ~640 MB embedding model once, then it's cached)
uv run --project sdk-docs-mcp python -u sdk-docs-mcp/build_index.py \
    --docs ncs-1.6.1-docs --out sdk-docs-mcp/ncs-1.6.1.sqlite
uv run --project sdk-docs-mcp python -u sdk-docs-mcp/build_index.py \
    --docs sdk-nrf-bm --out sdk-docs-mcp/nrf-bm.sqlite
```

Each index is **reproducible, not magic** — `build_index.py` walks one `--docs` root,
chunks each RST/MD file into sections, extracts the xref graph, embeds each section, and
writes everything into the `--out` SQLite file. Because each corpus is frozen, this is a
one-time cost with no staleness or re-indexing machinery.

> The server opens the index read-only and resolves the docs root from a path stored in
> the index's `meta` table, so it works from any clone.

---

## Learn more

- **`docs/ncs-docs-mcp-build-guide.md`** — the complete build runbook and field notes:
  schema, chunking, the cross-reference graph, the embedding memory cliff (and the
  192 GB crash that motivated the fix), RRF, wiring, distribution, and verification.
  Read this to rebuild it — or rebuild it *differently* for another corpus.
- **`docs/build-ncs-1.6.1-doc.md`** — how the **resolved** index is produced: the
  pinned-toolchain Docker build that west-clones NCS v1.6.1 and renders the real
  API reference, then ingests that HTML (`--format html`) into `ncs-1.6.1-resolved.sqlite`.
- **`docs/ideas/docs-access-recommendation.md`** — why a hybrid MCP server, and why the
  alternatives (agentic grep, bare vector DB, DeepWiki) each fall short for a pinned
  snapshot.
- **`sdk-docs-mcp/README.md`** — the engine's own reference (build, wiring, layout).
