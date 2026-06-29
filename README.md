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
| **`sdk-docs-mcp/`** | The corpus-neutral hybrid MCP documentation engine — see below. Ships the prebuilt `nrf-bm.sqlite`; the NCS 1.6.1 indexes (resolved-HTML, and a unified source-code + RST KB) are built locally per the runbooks. |
| `ncs-1.6.1-docs/` | Frozen ~125 MB Sphinx doc snapshot of NCS **v1.6.1** (`zephyr/ nrf/ mcuboot/ nrfxlib/ tfm/`), pinned by commit in `MANIFEST.md`. |
| `docker/` | Pinned toolchain image + west-clone build script that render the snapshot into **resolved HTML** (real API reference), the input to `ncs-1.6.1-resolved.sqlite`. See `docs/build-ncs-1.6.1-doc.md`. |
| `sdk-nrf-bm/` | Local clone of `sdk-nrf-bm` (Bare Metal SDK) — the offline source of truth for headers, Kconfig, and samples, *and* the corpus behind the `bm-docs` server. Pinned + refreshable. |
| `docs/` | The current resolved-HTML build runbook (`build-ncs-1.6.1-doc.md`); plus the historical design rationale (`ideas/docs-access-recommendation.md`) and original RST build runbook (`ncs-docs-mcp-build-guide.md`). |
| `.mcp.json` | Wires up `ncs-docs-resolved` and `bm-docs`, plus `deepwiki`, `mdn`, and `chrome-devtools`. |
| `sdk-nrf-bm.md` / `refresh-sdk-nrf-bm.sh` | Where to look in the Bare Metal clone, and how to refresh it. |

---

## The centerpiece: a hybrid documentation MCP engine

The "thing that indexes the docs and turns them into something you can ask questions"
is a **RAG-style hybrid retrieval engine**. One corpus-neutral engine builds a
**separate index per SDK** and serves each through its own MCP instance — `bm-docs`
(sdk-nrf-bm) and the NCS 1.6.1 servers — kept isolated so their `CONFIG_*`/API symbols
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

**By the numbers:** sdk-nrf-bm — 195 files → 1,160 sections → 1,006 edges (430
resolved) → an 8 MB `nrf-bm.sqlite`. (The standalone NCS 1.6.1 RST index has been
**retired**; its corpus is being rebuilt into the unified KB below.)

**Two NCS knowledge bases, one search surface.** The NCS 1.6.1 RST snapshot is strong
on prose but ~18% of its files are doxygen *stub* pages — the real API reference
(function signatures, struct fields) is injected only by a Sphinx + breathe build. So
the NCS docs are moving to **two knowledge bases behind one federated search**: a
**resolved-HTML** index (`ncs-1.6.1-resolved.sqlite`, served as `ncs-docs-resolved`;
the *pretty* rendered docs) — a one-time Docker build documented in
`docs/build-ncs-1.6.1-doc.md` — and a **unified source-code + RST** index so answers
can be justified directly from the real C/Kconfig source. The retired RST-only index
is subsumed by the latter.

### Tools it exposes

| Tool | Purpose |
|---|---|
| `search_docs(query, k=8, mode=hybrid\|keyword\|semantic)` | Find sections; returns repo, file, anchor, breadcrumb, line range, citation, snippet. Append `*` in `keyword` mode for symbol-family search (`CONFIG_BT*`). |
| `get_section(id)` | Full text of one section. |
| `get_doc(path)` | Full documentation file by repo-relative path (read fresh from disk). |
| `related(id)` | Resolved xref neighbours (outgoing + incoming) plus unresolved edges. |

---

## Quick start

The servers are registered in `.mcp.json`, so once you open this repo in Claude Code
and reload MCP servers, the four tools of `bm-docs` are available (its index is
committed). The NCS 1.6.1 servers (`ncs-docs-resolved`, and the unified source+RST KB)
come online once their indexes are built locally per the runbooks.

To run a server or rebuild an index from the command line:

```bash
# Run a server (what .mcp.json does) — one index per instance
uv run --project sdk-docs-mcp sdk-docs-mcp sdk-docs-mcp/nrf-bm.sqlite      # bm-docs

# Rebuild an index from its corpus (--docs and --out are required; first run
# downloads the ~640 MB embedding model once, then it's cached)
uv run --project sdk-docs-mcp python -u sdk-docs-mcp/build_index.py \
    --docs sdk-nrf-bm --out sdk-docs-mcp/nrf-bm.sqlite
```

The NCS 1.6.1 indexes are produced by their own builds — the resolved-HTML index per
`docs/build-ncs-1.6.1-doc.md`, and the unified source-code + RST index via the
`--format`/`--code-root` path on `build_index.py`.

Each index is **reproducible, not magic** — `build_index.py` walks one `--docs` root,
chunks each RST/MD file into sections, extracts the xref graph, embeds each section, and
writes everything into the `--out` SQLite file. Because each corpus is frozen, this is a
one-time cost with no staleness or re-indexing machinery.

> The server opens the index read-only and resolves the docs root from a path stored in
> the index's `meta` table, so it works from any clone.

---

## Learn more

- **`docs/ncs-docs-mcp-build-guide.md`** — *(historical)* the original build runbook
  and field notes: schema, chunking, the cross-reference graph, the embedding memory
  cliff (and the 192 GB crash that motivated the fix), RRF, wiring, and verification.
  The engine field notes still apply; the RST-only `ncs-docs` wiring it describes is
  retired.
- **`docs/build-ncs-1.6.1-doc.md`** — how the **resolved** index is produced: the
  pinned-toolchain Docker build that west-clones NCS v1.6.1 and renders the real
  API reference, then ingests that HTML (`--format html`) into `ncs-1.6.1-resolved.sqlite`.
- **`docs/ideas/docs-access-recommendation.md`** — why a hybrid MCP server, and why the
  alternatives (agentic grep, bare vector DB, DeepWiki) each fall short for a pinned
  snapshot.
- **`sdk-docs-mcp/README.md`** — the engine's own reference (build, wiring, layout).
