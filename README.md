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
| **`ncs-docs-mcp/`** | The hybrid MCP documentation server — see below. Ships a prebuilt `index.sqlite`. |
| `ncs-1.6.1-docs/` | Frozen ~125 MB Sphinx doc snapshot of NCS **v1.6.1** (`zephyr/ nrf/ mcuboot/ nrfxlib/ tfm/`), pinned by commit in `MANIFEST.md`. |
| `sdk-nrf-bm/` | Local clone of `sdk-nrf-bm` (Bare Metal SDK) — the offline source of truth for headers, Kconfig, and samples. Pinned + refreshable. |
| `docs/` | The recommendation that led to the server (`ideas/docs-access-recommendation.md`) and the full build runbook (`ncs-docs-mcp-build-guide.md`). |
| `.mcp.json` | Wires up `ncs-docs`, plus `deepwiki`, `mdn`, and `chrome-devtools`. |
| `sdk-nrf-bm.md` / `refresh-sdk-nrf-bm.sh` | Where to look in the Bare Metal clone, and how to refresh it. |

---

## The centerpiece: `ncs-docs` — a hybrid documentation MCP server

The "thing that indexes the docs and turns them into something you can ask questions"
is a **RAG-style hybrid retrieval server**. It fuses three signals out of one portable
SQLite file so that both *exact-symbol* and *conceptual* queries work against the pinned
corpus:

| Signal | Store | Good at |
|---|---|---|
| **BM25 keyword** | SQLite FTS5 (`tokenchars '_'`) | exact `CONFIG_*`, API names, file paths |
| **Dense vectors** | sqlite-vec (`jina-embeddings-v2-base-code`, cosine) | "how do I…" conceptual recall |
| **Xref graph** | `links` table (`:ref:`/`:doc:`/`:option:`/`:file:`) | jumping to related sections |

Keyword and vector hits are merged with **Reciprocal Rank Fusion (RRF)**. The interface
is **pointer-first**: a search returns *locations* (repo / file / anchor / breadcrumb /
line range) plus a snippet, and the agent then reads the real RST for exactness — the
best of grep-the-source and semantic search.

**By the numbers:** 1,824 files → 15,176 sections → 10,555 cross-reference edges
(6,245 resolved) → a single 67 MB `index.sqlite`.

### Tools it exposes

| Tool | Purpose |
|---|---|
| `search_docs(query, k=8, mode=hybrid\|keyword\|semantic)` | Find sections; returns repo, file, anchor, breadcrumb, line range, citation, snippet. Append `*` in `keyword` mode for symbol-family search (`CONFIG_BT*`). |
| `get_section(id)` | Full text of one section. |
| `get_doc(path)` | Full documentation file by repo-relative path (read fresh from disk). |
| `related(id)` | Resolved xref neighbours (outgoing + incoming) plus unresolved edges. |

---

## Quick start

The server is already registered in `.mcp.json`, so once you open this repo in Claude
Code and reload MCP servers, the four tools are available — no setup, the index is
committed.

To use it from the command line or rebuild it:

```bash
# Run the server (what .mcp.json does)
uv run --project ncs-docs-mcp ncs-docs-mcp ncs-docs-mcp/index.sqlite

# Rebuild the index from the corpus (one-time ~45–50 min CPU embed;
# first run downloads the ~640 MB embedding model once)
uv run --project ncs-docs-mcp python ncs-docs-mcp/build_index.py
```

The index is **reproducible, not magic** — `build_index.py` walks the corpus, chunks
each RST/MD file into sections, extracts the xref graph, embeds each section, and writes
everything into `index.sqlite`. Because the corpus is frozen, this is a one-time cost
with no staleness or re-indexing machinery.

> The server opens the index read-only and resolves the docs root from a path stored in
> the index's `meta` table, so it works from any clone.

---

## Learn more

- **`docs/ncs-docs-mcp-build-guide.md`** — the complete build runbook and field notes:
  schema, chunking, the cross-reference graph, the embedding memory cliff (and the
  192 GB crash that motivated the fix), RRF, wiring, distribution, and verification.
  Read this to rebuild it — or rebuild it *differently* for another corpus.
- **`docs/ideas/docs-access-recommendation.md`** — why a hybrid MCP server, and why the
  alternatives (agentic grep, bare vector DB, DeepWiki) each fall short for a pinned
  snapshot.
- **`ncs-docs-mcp/README.md`** — the server's own reference.
