# ncs-docs-mcp

A hybrid MCP server over the frozen **NCS 1.6.1** documentation snapshot in
`../ncs-1.6.1-docs/`. It fuses three retrieval signals so both exact-symbol and
conceptual queries work against this *pinned* corpus (DeepWiki tracks upstream
`sdk-nrf`, not 1.6.1):

| Signal | Store | Good at |
|---|---|---|
| **BM25** keyword | SQLite FTS5 (`tokenchars '_'`) | exact `CONFIG_*`, API names, file paths |
| **Dense vectors** | sqlite-vec (`jina-embeddings-v2-base-code`, cosine) | "how do I…" conceptual recall |
| **Xref graph** | `links` table (`:ref:`/`:doc:`/`:option:`/`:file:`) | jumping to related sections |

Results from BM25 and vectors are merged with **Reciprocal Rank Fusion**. The
interface is *pointer-first*: `search_docs` returns locations + snippets; the
agent then `Read`s the real RST (or calls `get_section`/`get_doc`) for exactness.

## Tools

| Tool | Purpose |
|---|---|
| `search_docs(query, k=8, mode=hybrid\|keyword\|semantic)` | Find sections; returns repo, file, anchor, breadcrumb, line range, citation, snippet. Append `*` in `keyword` mode for symbol-family prefix search (`CONFIG_BT*`). |
| `get_section(id)` | Full text of one section. |
| `get_doc(path)` | Full documentation file by repo-relative path. |
| `related(id)` | Resolved xref neighbours (outgoing + incoming) plus unresolved edges. |

## Build the index

The committed `index.sqlite` is reproducible — not magic. To rebuild:

```bash
uv run --project ncs-docs-mcp python ncs-docs-mcp/build_index.py
```

The first run downloads the embedding model (~640 MB) once via `fastembed`;
subsequent runs reuse the cache. Options: `--docs <dir>`, `--out <file>`,
`--threads <n>`.

## Run / wire up

Registered in the repo's `.mcp.json`:

```jsonc
"ncs-docs": {
  "command": "uv",
  "args": ["run", "--project", "ncs-docs-mcp", "ncs-docs-mcp", "ncs-docs-mcp/index.sqlite"]
}
```

The server opens the index read-only and resolves the docs root from a path
stored in the index's `meta` table (relative to the index file), so it works
from any clone. The embedding model loads lazily on the first semantic/hybrid
query.

## Layout

```
build_index.py        corpus -> index.sqlite (one-time)
ncs_docs_mcp/
  chunker.py          RST/MD section splitter + xref extractor
  embed.py            fastembed wrapper (model pinned)
  server.py           FastMCP server: 4 tools + RRF
index.sqlite          committed prebuilt index
```
