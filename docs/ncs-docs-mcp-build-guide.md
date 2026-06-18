# Building a Hybrid Docs MCP Server — Build Guide & Field Notes

**What this is.** A complete, self-contained record of how the `ncs-docs` MCP
server was built, so it can be rebuilt (or rebuilt *differently*, for another
corpus) from scratch with no prior context. It is a **runbook** first — do these
steps, in this order — with each non-obvious decision framed as a **transferable
principle** so it carries over to other documentation sets and embedding models.

It deliberately spends most of its words on the things you cannot re-derive by
staring at the code: the failure modes, the silent defaults, and the order in
which things must happen. The boilerplate is summarized; the gotchas are
exhaustive.

> **Scope note.** The concrete subject is the **nRF Connect SDK (NCS) 1.6.1**
> documentation snapshot under `ncs-1.6.1-docs/` — a *frozen*
> ~125 MB Sphinx corpus (`zephyr/ nrf/ mcuboot/ nrfxlib/ tfm/`). "Frozen" is
> load-bearing: it means **index once, never re-index**, which removes all
> staleness/incremental-update machinery from the design.

---

## Table of contents

1. [Global summary (read this first)](#1-global-summary-read-this-first)
2. [The shape of the solution & why](#2-the-shape-of-the-solution--why)
3. [Environment & exact versions](#3-environment--exact-versions)
4. [The SQLite index: schema & the two silent defaults that bite](#4-the-sqlite-index-schema--the-two-silent-defaults-that-bite)
5. [Chunking RST: section structure, anchors, breadcrumbs](#5-chunking-rst-section-structure-anchors-breadcrumbs)
6. [Cross-reference graph extraction & resolution](#6-cross-reference-graph-extraction--resolution)
7. [Embeddings: the 192 GB crash and how to never hit it](#7-embeddings-the-192-gb-crash-and-how-to-never-hit-it)
8. [The server: hybrid retrieval, RRF, and query sanitizing](#8-the-server-hybrid-retrieval-rrf-and-query-sanitizing)
9. [Wiring into Claude Code via uv + .mcp.json](#9-wiring-into-claude-code-via-uv--mcpjson)
10. [Distribution: committing a 67 MB binary index](#10-distribution-committing-a-67-mb-binary-index)
11. [Verification: the smoke tests that actually prove it works](#11-verification-the-smoke-tests-that-actually-prove-it-works)
12. [Windows / shell / tooling gotchas hit along the way](#12-windows--shell--tooling-gotchas-hit-along-the-way)
13. [Full rebuild checklist](#13-full-rebuild-checklist)
14. [If you change one thing — ripple table](#14-if-you-change-one-thing--ripple-table)

---

## 1. Global summary (read this first)

The server answers documentation queries against a **pinned** doc snapshot by
**fusing three retrieval signals** out of one portable SQLite file:

| Signal | Store | Owns | Tech |
|---|---|---|---|
| Exact keyword/symbol | FTS5 (`bm25`) | `CONFIG_*`, API names, file paths | built into SQLite |
| Conceptual recall | dense vectors | "how do I …?" prose questions | `sqlite-vec` + a local embedding model |
| Document graph | `links` table | `:ref:`/`:doc:` traversal | plain table |

Keyword and vector results are merged with **Reciprocal Rank Fusion (RRF)**. The
interface is **pointer-first**: search returns *locations* (repo / file / anchor
/ breadcrumb / line range) + a snippet, and the agent then reads the real source
for exactness. A Python **FastMCP** server exposes four tools: `search_docs`,
`get_section`, `get_doc`, `related`.

**Pipeline:** `build_index.py` walks the corpus → `chunker.py` splits each file
into sections + extracts xref edges → `embed.py` vectorizes each section →
everything is written to `index.sqlite` (committed) → `server.py` opens it
read-only and serves queries.

**The five things that will waste your day if you don't know them:**

1. **FTS5 splits on `_` by default**, shattering `CONFIG_BOOTLOADER_MCUBOOT`
   into three useless tokens. Fix: `tokenize = "unicode61 tokenchars '_'"`.
2. **`sqlite-vec` `vec0` defaults to L2 distance, not cosine.** You must declare
   `embedding float[768] distance_metric=cosine` or your "semantic" ranking is
   silently Euclidean over un-normalized vectors.
3. **Batch embedding can try to allocate 192 GB.** Transformer self-attention is
   O(sequence²); a few giant tables tokenize to the model's 8192-token max, and
   the whole batch pads to that length. Fix: **cap the embedding input length**
   and **sort batches by length**. (Full autopsy in §7.)
4. **Redirected stdout is block-buffered**, so a long build looks frozen. Use
   `python -u`, and remember a downstream `grep` re-buffers too.
5. **It is CPU-slow and that's normal.** ~15k sections on a ~12-core CPU took
   **~45–50 minutes** to embed. Plan for it; it's a one-time cost.

Concrete result for this corpus: **1,824 files → 15,176 sections → 10,555 xref
edges (6,245 resolved) → a 67 MB `index.sqlite`.**

---

## 2. The shape of the solution & why

**Summary:** Hybrid beats pure-vector and pure-grep because the corpus has *both*
exact symbols (which embeddings mangle) *and* conceptual prose (which keyword
search misses), *plus* a real cross-reference graph that is often more useful
than cosine similarity. One SQLite file keeps it portable and process-free.

Three structural facts about Sphinx docs drove every decision:

- **Clean section structure.** Every RST file uses `.. _anchor:` labels and
  underline/overline headers. This yields *stable citation anchors*
  (`file.rst#anchor`) and a natural chunk boundary.
- **Dense exact symbols.** ~1,575 distinct `CONFIG_*` options plus API names and
  file paths. Pure semantic embeddings routinely corrupt these tokens — this is
  the single biggest reason to be hybrid, not vector-only.
- **Cross-references everywhere** (`:ref:`, `:doc:`, `:option:`, `:file:`). These
  form a traversable graph ("the DAG").

**Transferable principle.** Before choosing a retrieval architecture, characterize
the corpus along two axes: *how much exact-token matching matters* and *how much
explicit link structure exists*. High on either axis → do not go vector-only.

**Why one SQLite file.** No separate vector-DB process, trivially shareable (commit
it or rebuild from one script), and FTS5 + `sqlite-vec` + a plain table cover all
three signals in the same file. For a frozen corpus this is close to ideal.

---

## 3. Environment & exact versions

**Summary:** A `uv`-managed Python 3.12 project. The dependency stack is small but
version-sensitive in two places (`sqlite-vec`, `fastembed`); the versions below
are known-good.

| Component | Version used | Notes |
|---|---|---|
| Python | 3.12.12 | via `uv` |
| uv | 0.9.28 | runner + lockfile |
| fastmcp | 3.4.2 | server framework (`from fastmcp import FastMCP`) |
| mcp | 1.27.2 | protocol lib (pulled in by fastmcp); **has no `__version__` attr** — query via `importlib.metadata` |
| fastembed | 0.8.0 | ONNX embedding runner; downloads models from HuggingFace |
| sqlite-vec | 0.1.9 | loadable SQLite extension; `serialize_float32`, `load` helpers |
| onnxruntime | 1.27.0 | CPU inference backend |
| numpy | 2.4.6 | vector handling |

`pyproject.toml` essentials (the part that matters):

```toml
[project.scripts]
ncs-docs-mcp = "ncs_docs_mcp.server:main"   # this is what `uv run ncs-docs-mcp` resolves

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["ncs_docs_mcp"]
```

**Gotcha — verifying versions.** `import mcp; mcp.__version__` raises
`AttributeError`. Use:

```python
import importlib.metadata as m
m.version("mcp")        # "1.27.2"
```

**Transferable principle.** Pin the embedding model name *and* the dimension in
one place (here, `ncs_docs_mcp/__init__.py`: `EMBED_MODEL`, `EMBED_DIM`,
`SCHEMA_VERSION`). The DB schema, the build, and the server all read from it, and
a `meta` row records what the index was actually built with so the server can warn
on mismatch.

---

## 4. The SQLite index: schema & the two silent defaults that bite

**Summary:** One file, four logical stores (`sections`, `fts_sections`,
`vec_sections`, `links`) plus a `meta` key/value table. Two virtual-table
declarations contain defaults that are *wrong* for this use case and fail
silently — get these exactly right or everything "works" while returning garbage
rankings.

```sql
CREATE TABLE sections(
    id INTEGER PRIMARY KEY,
    repo TEXT, file_path TEXT, anchor TEXT,
    breadcrumb TEXT, header TEXT,
    line_start INTEGER, line_end INTEGER, text TEXT
);

-- (1) tokenchars '_' keeps CONFIG_BOOTLOADER_MCUBOOT as ONE token.
-- content='sections' = "external content": FTS stores only its index, reading
-- column values back from `sections` by rowid. Saves duplicating ~11 MB of text.
CREATE VIRTUAL TABLE fts_sections USING fts5(
    text, header, anchor,
    content='sections', content_rowid='id',
    tokenize = "unicode61 tokenchars '_'"
);

-- (2) distance_metric=cosine is REQUIRED — vec0 defaults to L2.
CREATE VIRTUAL TABLE vec_sections USING vec0(
    section_id INTEGER,
    embedding float[768] distance_metric=cosine
);

CREATE TABLE links(
    src_id INTEGER, kind TEXT,            -- kind ∈ ref|doc|option|file
    target_raw TEXT, resolved_id INTEGER  -- NULL if external/unresolved
);
CREATE INDEX idx_links_src ON links(src_id);
CREATE INDEX idx_links_resolved ON links(resolved_id);

CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
```

### Silent default #1 — FTS5 tokenization

`unicode61` (the default tokenizer) treats `_` as a separator. Without
`tokenchars '_'`, `CONFIG_BOOTLOADER_MCUBOOT` indexes as `config`, `bootloader`,
`mcuboot` — so an exact-symbol search returns every doc mentioning any of those
words, and the *whole premise* of hybrid (nailing exact symbols) collapses. The
fix costs one clause and is invisible if you don't test for it.

> Consequence you must embrace: because `_` is now a *token character*,
> `BOOTLOADER` will **not** sub-match `CONFIG_BOOTLOADER_MCUBOOT`. Symbol-family
> search must use an FTS **prefix** query (`CONFIG_BT*`). This is handled in the
> server (§8).

### Silent default #2 — vec0 distance metric

`sqlite-vec`'s `vec0` defaults to **L2 (Euclidean)** distance. If you write
`embedding float[768]` and then `ORDER BY distance`, you are ranking by Euclidean
distance over *un-normalized* embeddings — which is not what "semantic similarity"
means and gives subtly wrong results that look plausible. Declaring
`distance_metric=cosine` makes ranking magnitude-invariant (verified: identical
text scores cosine self-distance ≈ 0). Either declare cosine, or L2-normalize
every vector before insert (then L2 and cosine coincide). We chose the former.

### Populating external-content FTS5

With `content='sections'`, you still must feed the index, matching `rowid` to the
section id:

```python
db.executemany(
    "INSERT INTO fts_sections(rowid, text, header, anchor) VALUES (?,?,?,?)",
    [(s.id, s.text, s.header, s.anchor) for s in sections],
)
```

Queries then `SELECT rowid FROM fts_sections WHERE fts_sections MATCH ?` and join
back to `sections` by id.

### The `meta` table earns its keep

Store at least: `schema_version`, `embed_model`, `embed_dim`,
`docs_root_relative`, and counts. The two that matter operationally:

- `embed_model` lets the server warn if the index was built with a different
  model than the server expects (dimension/semantics mismatch → silent garbage).
- `docs_root_relative` (e.g. `../ncs-1.6.1-docs`) is stored **relative to the
  index file's directory**, so `get_doc` can locate source files from any clone
  without absolute paths baked in.

**Transferable principle.** Any virtual-table / extension you adopt: *look up its
defaults for tokenization and distance, and write a test that would fail if the
default were in force.* These libraries fail open (return results), not closed.

---

## 5. Chunking RST: section structure, anchors, breadcrumbs

**Summary:** Split each file into sections at header boundaries, where header
*level* is defined by the **order adornment styles first appear in the file**
(true reStructuredText semantics — not a fixed `# > * > =` ranking). Attach
preceding `.. _label:` anchors to the section they introduce, build a breadcrumb
from the live header stack, and record 1-based inclusive line ranges for
citations. Sub-split only sections large enough to matter.

### Header detection (the subtle part)

RST does **not** fix which underline character means which level. Each document
establishes its own order: the first adornment *style* seen is level 1, the next
new style is level 2, and so on. A "style" is the pair `(char, has_overline)` —
an overline+underline `#` is a different level from an underline-only `#`.

```python
# A header is: optional overline, a title at column 0, then an underline of the
# same char at least as long as the title. Column-0 requirement avoids matching
# indented code, list-tables, and option blocks.
def _detect_header(lines, i):
    # ... returns (title, (char, has_overline), consumed_lines) or None

style_levels = {}                        # (char, overline) -> level, in first-seen order
if style not in style_levels:
    style_levels[style] = len(style_levels) + 1
level = style_levels[style]
```

Verified on `nrf/doc/nrf/ug_bootloader.rst`: levels came out as
`Secure bootloader chain` (1) → `Architecture` (2) → `First-stage immutable
bootloader` (3) → `Pre-signed variants` (4), exactly matching the document.

**Gotcha — false positives.** Tables (`===` grid borders), transitions (a lone
rule with blank lines around it), and code can look like adornments. Two cheap
guards eliminate nearly all of them: require the title and adornment at **column
0**, and require the underline length **≥ title length**.

### Anchors attach forward

`.. _label:` is a hyperlink target for *the next* section. Accumulate pending
anchors as you scan; when a header appears, all pending anchors belong to it
(multiple anchors can stack on one section). The **first** anchor becomes the
primary citation; all are kept for `:ref:` resolution.

### Breadcrumbs from a stack

Maintain a stack of `(level, title)`. At each new section, pop entries with
`level >= current`, then the breadcrumb is the remaining ancestors plus the
current title joined by `" > "`. This produced, e.g.,
`Secure bootloader chain > Architecture > Flash memory partitions > MCUboot partitions`.

### Sub-splitting oversized sections

The chosen embedding model handles 8192 tokens, so **keep whole sections as one
chunk** by default — that preserves context and citation stability. Only when a
section exceeds ~1,500 estimated tokens (`len/4` heuristic) do we split on
paragraph (blank-line) boundaries into ~1,200-token sub-chunks, **carrying the
same anchor/breadcrumb** and adjusting line ranges so citations stay accurate.

> Caveat: a pathological section with *no blank lines* (a giant generated table)
> stays as one big chunk here. That's fine for storage; §7 explains why the
> embedder must still defend itself against such chunks.

### Cleaning text for embedding (only)

Two different texts come out of a section:

- **Raw text** → stored in `sections.text` and FTS-indexed *verbatim* (so exact
  symbols and code are searchable).
- **Cleaned text** → fed to the embedder only. We drop layout-only directive
  markers (`.. contents::`, `.. toctree::`, `.. figure::`, …) and their option
  lines, drop `.. _anchor:` lines and header rules, unwrap Sphinx roles to their
  display words (`:ref:`Title <tgt>`` → `Title`), and strip `**`/``` `` ```
  emphasis — **while preserving code and `CONFIG_*` tokens**.

**Transferable principle.** Index the raw text for lexical search; embed a cleaned
version for semantics. Never let one representation serve both — the requirements
conflict (exactness vs. denoising).

### Markdown

A handful of `.md` files are split on ATX (`#`) headers, skipping fenced code
blocks so `#` inside ``` blocks isn't mistaken for a heading.

---

## 6. Cross-reference graph extraction & resolution

**Summary:** Regex out `:ref:`/`:doc:`/`:option:`/`:file:` roles, normalize the
target, and resolve `:ref:` against a global anchor registry and `:doc:` against a
file-path map. Keep unresolved edges with `resolved_id = NULL` rather than
dropping them — they're still useful as keywords and as a signal.

```python
_ROLE_RE = re.compile(r":(ref|doc|option|file):`([^`]+)`")
# target may be "Title <real_target>"; take what's inside <>, strip ~ and leading /
```

Resolution happens in a **second pass**, after *all* sections exist and have ids,
because Sphinx labels are **global across the whole doc set**:

- `:ref:` → look up the target in `anchor → section_id` (built from every
  section's anchors). Resolves cleanly (6,245 of 10,555 edges here).
- `:doc:` → resolve a doc path against a `file_path(no-suffix) → first-section-id`
  map. Try the path relative to the source file's directory, then relative to the
  sub-project root, then a unique-suffix match. Intersphinx targets containing
  `:` (e.g. `mcuboot:index`) are external → `NULL`.
- `:option:` (mostly `CONFIG_*`) and `:file:` have no target section → `NULL`,
  but the raw target is still stored.

**Gotcha — resolution is best-effort and that's OK.** ~40% of edges stay
unresolved (intersphinx, autodoc domains, external files). Storing them as `NULL`
is deliberate: `related()` surfaces them as bare keywords, which still helps the
agent.

**Transferable principle.** Build identifier→node maps in a pass that runs *after*
all nodes are assigned stable ids; cross-references in docs are forward- and
backward-pointing and global, so single-pass resolution will miss most of them.

---

## 7. Embeddings: the 192 GB crash and how to never hit it

**Summary:** The first build ran for **85 minutes and then crashed** trying to
allocate **192 GB** in a single matmul. Cause: transformer attention is
O(sequence²), a few enormous sections hit the model's 8192-token ceiling, and
fastembed pads the *entire batch* to the longest member. The fix is two lines of
discipline — **cap the embedding input length** and **embed in length-sorted
batches** — which also makes the build dramatically faster and drops peak RAM
from ~15 GB to ~1 GB.

### The crash, decoded

```
onnxruntime ... FAIL ... FusedMatMul ... Failed to allocate memory for
requested buffer of size 206158430208
```

`206,158,430,208 bytes = 64 × 8192² × 12 × 4`:

| Factor | Value | Meaning |
|---|---|---|
| 64 | batch size | fastembed default |
| 8192² | 67,108,864 | attention is sequence-length **squared** |
| 12 | heads | model attention heads |
| 4 | bytes | float32 |

Even one 8192-token document in a 64-doc batch pads *all 64* to 8192, producing a
192 GB attention buffer. It crashed *late* (85 min in) precisely because it only
blew up when it reached a batch containing such a monster section — and all the
in-memory vectors computed up to that point were lost (the index is written only
at the end).

### The fix

```python
MAX_EMBED_CHARS = 4000      # ~1000–1500 tokens; bounds attention regardless of section size
DEFAULT_BATCH   = 16

def embed_documents(self, texts, batch_size=DEFAULT_BATCH):
    capped = [t[:MAX_EMBED_CHARS] for t in texts]                 # 1) hard length cap
    order  = sorted(range(len(capped)), key=lambda i: len(capped[i]))  # 2) sort by length
    out = [None] * len(capped)
    for pos, vec in enumerate(self._model.embed([capped[i] for i in order], batch_size=batch_size)):
        out[order[pos]] = np.asarray(vec, dtype=np.float32)       # 3) restore original order
    return out
```

Why each piece matters:

1. **Length cap.** Bounds the worst-case sequence so attention can't explode. The
   vector only needs a representative prefix; **the full text is still BM25-indexed
   verbatim**, so exact-symbol recall is untouched. This is the key insight that
   makes truncation safe here.
2. **Length-sorted batches.** fastembed pads each batch to its longest member, so
   grouping similar lengths means short docs sit in cheap batches and only a few
   small batches carry the long sequences. This is the single biggest *speed* win,
   independent of the crash fix.
3. **Restore order.** Must map results back to input order before insert, or
   vectors misalign with section ids — a silent, catastrophic correctness bug.

Result: peak RAM ~15 GB → **~1 GB**; no OOM; build completes.

### The model & why

`jinaai/jina-embeddings-v2-base-code` (768-dim, 8192-token context, **no
query/document prefix needed**). Chosen because (a) 8192 context lets whole
sections embed coherently and (b) it's trained on code + technical text, which
NCS prose is saturated with. In this hybrid design the embedder is **not**
responsible for exact symbols (FTS5 owns those) — only conceptual recall — so
"code-aware + long-context" matters more than raw leaderboard rank.

Alternatives, for the record: `nomic-embed-text-v1.5` (768/8192, but needs
`search_query:`/`search_document:` prefixes — best fallback); `bge-small-en-v1.5`
(384/512, fast but 512 context forces aggressive sub-chunking);
`jina-embeddings-v3`/`gte-large`/`arctic-embed-l` (1024-dim, 1–2 GB, overkill).

### Build-time reality

- **First run downloads the model once** (~640 MB) from HuggingFace into
  `…/Temp/fastembed_cache/`. Subsequent runs reuse it. On Windows you'll see a
  benign symlink warning ("activate Developer Mode") and an HF "unauthenticated
  requests" warning — both ignorable.
- **Embedding ~15k sections on a ~12-core CPU took ~45–50 minutes.** This is just
  slow; it is not stuck. CPU-bound, ~1 GB RAM, no GPU. Budget for it.

**Transferable principle.** Any transformer-batch embedding step has a quadratic
memory cliff. *Before* a long run: bound the per-item sequence length and
length-sort the batch. And write the index incrementally or checkpoint if a
multi-hour embed is at risk of losing work on a late failure.

---

## 8. The server: hybrid retrieval, RRF, and query sanitizing

**Summary:** A FastMCP server opens the index **read-only**, lazily loads the
embedding model on the first semantic/hybrid query, and exposes four pointer-first
tools. The two non-trivial pieces are **Reciprocal Rank Fusion** (combine BM25 and
vector rankings without comparing their incomparable scores) and an **FTS query
sanitizer** (turn arbitrary user text into a MATCH expression that can't throw).

### Opening the index (read-only + extension)

```python
db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
db.enable_load_extension(True)
sqlite_vec.load(db)
db.enable_load_extension(False)
```

`check_same_thread=False` because the MCP server may touch the connection from
different threads; read-only mode is safe for that here.

### RRF — fuse rankings, not scores

BM25 scores (negative, lower=better) and vector distances (0…2 for cosine) are not
comparable. RRF sidesteps this by using only **rank position**:

```python
RRF_K = 60; FUSE_DEPTH = 50      # each retriever returns its top ~50
score(id) = Σ over retrievers of 1 / (RRF_K + rank_in_that_retriever)   # rank is 1-based
# sort ids by score desc, take top k
```

`mode=keyword`/`semantic` simply bypass one retriever. `k` is clamped (1…50).

### FTS query sanitizing (don't let users crash MATCH)

Raw user text fed to `MATCH` can throw on punctuation. Tokenize to words (keeping
a trailing `*` for prefix search), quote each term, and OR them:

```python
terms = re.findall(r"[A-Za-z0-9_]+\*?", text)
parts = [t if t.endswith("*") else f'"{t}"' for t in terms]   # CONFIG_BT* stays a prefix token
return " OR ".join(parts)                                      # OR = recall; bm25 handles ranking
```

OR (not AND) maximizes recall on natural-language queries; bm25 down-weights
common words so ranking still favors the rare, meaningful terms. Wrap the
`MATCH` in `try/except sqlite3.OperationalError` and return empty on failure.

### Vector KNN

```python
SELECT section_id FROM vec_sections WHERE embedding MATCH ? ORDER BY distance LIMIT ?
# ? = sqlite_vec.serialize_float32(query_vec)  (little-endian float32 blob)
```

### The four tools (pointer-first)

| Tool | Returns |
|---|---|
| `search_docs(query, k=8, mode=hybrid\|keyword\|semantic)` | locations: repo, file_path, anchor, breadcrumb, header, line range, a `citation` string, snippet — **not** full text |
| `get_section(id)` | full section text from the DB |
| `get_doc(path)` | full file, **read fresh from disk** via the `meta` docs-root |
| `related(id)` | resolved outgoing + incoming xref neighbours, plus unresolved edges as keywords |

**Gotcha — path traversal.** `get_doc` resolves the path under the docs root and
calls `target.relative_to(docs_root)`; if that raises `ValueError`, the path
escaped the root → reject. Verified that `../../../etc/passwd` is blocked.

**Gotcha — lazy model load.** The first hybrid/semantic call pays a ~10–20 s
one-time model load. Keyword queries never load the model. This keeps startup
instant and keyword-only workflows model-free.

**Transferable principle.** When fusing heterogeneous retrievers, fuse **ranks**
(RRF), never raw scores. And always sanitize free text before it reaches a query
DSL (`MATCH`, Lucene, etc.) — assume it will contain syntax metacharacters.

---

## 9. Wiring into Claude Code via uv + .mcp.json

**Summary:** A `[project.scripts]` console entry point + `uv run --project` is all
it takes. `uv` auto-syncs the local package into its venv on first run.

```jsonc
// .mcp.json (repo root; cwd for the server = repo root)
"ncs-docs": {
  "command": "uv",
  "args": ["run", "--project", "ncs-docs-mcp", "ncs-docs-mcp", "ncs-docs-mcp/index.sqlite"]
}
```

- The third `ncs-docs-mcp` is the **console script** (from `[project.scripts]`),
  not the directory — `uv run` resolves it after syncing the project.
- The index path is **relative to the repo root** (Claude Code launches MCP
  servers with cwd = the project root where `.mcp.json` lives).
- The server itself derives the **docs root** from the `meta` table
  (`docs_root_relative`, resolved against the index file's directory), so no doc
  paths are hard-coded.

**Verification that this actually launches** (this is the piece a plan can't prove
on paper — do it): drive a real MCP stdio handshake — `initialize` →
`notifications/initialized` → `tools/list` → `tools/call` — and confirm all four
tools appear and a call returns. Done here; it works end-to-end.

**Gotcha.** The new server only appears after Claude Code **reloads MCP servers**
(restart, or re-read `.mcp.json`).

---

## 10. Distribution: committing a 67 MB binary index

**Summary:** Commit the prebuilt `index.sqlite` so teammates get search instantly,
but tell git it's an opaque binary, and never commit the venv.

- `.gitattributes` (repo root): `*.sqlite binary -diff` — git won't try to
  diff/merge the blob.
- `ncs-docs-mcp/.gitignore`: exclude `.venv/` (here ~210 MB), `__pycache__/`,
  `*.pyc`, `*.egg-info/`. **Commit `uv.lock`** for reproducible installs.
- The committed set is exactly: the four `ncs_docs_mcp/*.py`, `build_index.py`,
  `pyproject.toml`, `uv.lock`, `README.md`, `.gitignore`, and `index.sqlite`.

**Decision point — 67 MB is bigger than expected.** The original plan estimated
30–40 MB assuming 5–10k chunks; the real corpus produced **15,176** sections.
~15k × 768 × 4 bytes ≈ 47 MB of vectors alone, plus text and FTS. If a 67 MB
binary in git is unacceptable, the alternative is to **gitignore the index** and
have each teammate run the one-line rebuild (cost: the ~45–50 min embed, once).

**Transferable principle.** Index size scales with *chunk count × dimension × 4*.
Estimate chunk count from the corpus *before* committing to "commit the binary" —
section-level chunking of a large doc set produces far more chunks than file-level
intuition suggests.

---

## 11. Verification: the smoke tests that actually prove it works

**Summary:** Three tests, one per signal, plus boundary checks. If these pass, the
pipeline is sound end-to-end.

| # | Query | Mode | Expectation | Proves |
|---|---|---|---|---|
| 1 | `CONFIG_BOOTLOADER_MCUBOOT` | keyword | bootloader sections, symbol intact | FTS `tokenchars '_'` works |
| 2 | "how do I enable the secure bootloader chain?" | hybrid | `ug_bootloader#ug_bootloader` in top-k | embeddings + RRF + cosine work |
| 3 | `related(<that id>)` | — | reaches `immutable_bootloader` & `upgradable_bootloader` | xref graph resolved correctly |

Plus boundary checks that caught real issues earlier: `CONFIG_BT_*` prefix family
returns results; semantic-only finds the Bluetooth controller reference;
`get_section`/`get_doc` return text; and `get_doc("../../../etc/passwd")` is
rejected.

**Counts to sanity-check after a build:** `sections`, `fts_sections`,
`vec_sections` row counts must be **equal** (one vector + one FTS row per section).
If they differ, the order-restoration or insert loop is broken.

**Transferable principle.** Write one verification per retrieval signal, and one
that exercises the *fusion*. A green "it returns something" is not proof; assert on
*which* document ranks where.

---

## 12. Windows / shell / tooling gotchas hit along the way

**Summary:** None are conceptual, all cost time. Listed so you skip the rediscovery.

- **`bash` working directory persists across tool calls.** A `cd foo` in one
  command affects the next. Use **absolute paths** (`/c/Users/.../ncs-1.6.1-docs`)
  rather than relying on cwd.
- **Default command timeout is 120 s.** A bare `sleep 180` is killed at ~120 s
  (exit 143 = SIGTERM). Set an explicit longer timeout for waits.
- **Redirected stdout is block-buffered.** A long-running build writing progress
  to a file shows *nothing* until the buffer fills or the process exits. Run
  Python with `-u`. **And** note that piping through `grep` re-buffers — so
  `python -u ... | grep ...` still hides progress until grep flushes.
- **`bc` is not present.** Use `wc -l`/`awk` or Python for arithmetic.
- **`tasklist` memory column contains NUL bytes / non-breaking spaces**, which
  makes `grep` say "Binary file matches". Pipe through `tr -d '\0'` if you need
  clean text. For CPU/liveness checks, PowerShell
  `(Get-Process python | Measure-Object CPU -Sum).Sum` is reliable.
- **HuggingFace symlink warning on Windows** (degraded cache, more disk) is
  harmless; activating Developer Mode silences it but isn't required.
- **A background build's in-memory results are lost on crash.** The index is only
  written at the end, so an 85-minute embed that crashes at minute 85 yields
  nothing. (Reinforces the §7 "bound it before you run it" rule.)

---

## 13. Full rebuild checklist

Do this, in order, to reproduce from nothing:

1. **Confirm the corpus** exists at `ncs-1.6.1-docs/` and is the frozen snapshot
   (commits pinned in its `MANIFEST.md`).
2. **Create the project** `ncs-docs-mcp/` with `pyproject.toml` (console script +
   hatchling) and `ncs_docs_mcp/__init__.py` pinning `EMBED_MODEL`/`EMBED_DIM`/
   `SCHEMA_VERSION`.
3. **`uv sync`** (or let `uv run` do it) — installs fastmcp, fastembed,
   sqlite-vec, numpy into `ncs-docs-mcp/.venv`.
4. **Write the four modules**: `chunker.py` (§5), `embed.py` (§7 — *with the cap
   and length-sort*), `server.py` (§8), and `build_index.py` (§4 schema + 6-phase
   build).
5. **Sanity-test the chunker** on one known file (e.g. `ug_bootloader.rst`):
   verify section count, breadcrumbs, line ranges, and a couple of extracted
   links — *before* the expensive embed.
6. **Sanity-test the embedder** on ~80 sample sections: confirm 768-dim float32
   out, order preserved (self-cosine ≈ 1.0), and bounded memory.
7. **Run `build_index.py`** with `python -u`. Expect a ~640 MB model download on
   first run and **~45–50 min** of CPU embedding for ~15k sections. Watch RAM stays
   ~1 GB (if it climbs to 10s of GB, the cap isn't engaged — stop and fix).
8. **Verify the index**: equal row counts in `sections`/`fts_sections`/
   `vec_sections`; `meta` populated; docs root resolves.
9. **Run the three smoke tests** (§11) against the server functions directly.
10. **Wire `.mcp.json`** (§9) and **drive a real MCP stdio handshake** to confirm
    `uv run` launches it and lists all four tools.
11. **Add `.gitattributes` + `.gitignore`**, decide commit-the-index vs. rebuild
    (§10), and commit the intended file set only.

---

## 14. If you change one thing — ripple table

**Summary:** The parts are coupled through the embedding dimension, the model
identity, and the chunking granularity. This table says what else must move.

| If you change… | You must also… |
|---|---|
| **Embedding model** | Update `EMBED_MODEL`+`EMBED_DIM`; the `vec0` `float[N]` must match the new dim; check whether the model needs query/document **prefixes** (jina-v2-code doesn't; nomic/e5 do); rebuild the whole index. |
| **Embedding dimension** | Change the schema `float[N]`; rebuild; index size scales linearly. |
| **`MAX_EMBED_CHARS` / batch size** | Re-check peak RAM (∝ batch × max_seq²). Bigger = more context but quadratic memory risk. |
| **Tokenizer (`tokenchars`)** | Re-test exact-symbol smoke #1; changing it can re-break `CONFIG_*`. |
| **Distance metric** | If you drop `distance_metric=cosine`, you must L2-normalize vectors at insert time, or rankings change silently. |
| **Chunk granularity** (sub-split thresholds) | Chunk count (→ index size, build time) and citation precision both move; re-run all smoke tests. |
| **Corpus is no longer frozen** | You now need re-indexing/staleness handling — a different design (incremental build, content hashing). The whole "index once, commit it" simplification no longer holds. |
| **Docs root location** | It's stored in `meta.docs_root_relative` relative to the index file — rebuild, or update that row, so `get_doc` keeps working. |

---

*Written after implementing `ncs-docs-mcp`. Every number and failure mode here was
observed during that build, not assumed: the 192 GB allocation, the ~45–50 min
embed, 15,176 sections / 10,555 edges / 67 MB index, and the passing smoke tests.*
