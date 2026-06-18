"""Hybrid MCP server over the NCS 1.6.1 documentation snapshot.

The index (``index.sqlite``) fuses three retrieval signals:

* **BM25** over an FTS5 table  -> exact ``CONFIG_*`` / API / path symbols
* **Dense vectors** in sqlite-vec -> conceptual "how do I ..." recall
* **Sphinx xref graph** in a ``links`` table -> ``:ref:``/``:doc:`` traversal

merged at query time with Reciprocal Rank Fusion.
"""

EMBED_MODEL = "jinaai/jina-embeddings-v2-base-code"
EMBED_DIM = 768
SCHEMA_VERSION = 1
