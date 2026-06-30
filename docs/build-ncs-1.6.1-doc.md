# Build the resolved NCS 1.6.1 documentation + a parallel resolved index

A runbook for producing the **fully resolved** NCS v1.6.1 documentation (real
API reference, not doxygen stubs) and ingesting it into a hybrid search index
(`ncs-1.6.1-resolved.sqlite`), sourced entirely from a fresh commit-exact `west`
clone.

## Why

NCS 1.6.1's **RST sources** are strong on prose but **starved on API reference**:
~18% of files (≈316/1,757) are doxygen *stub* pages whose real content — function
signatures, struct fields, parameters — is injected only by Sphinx + breathe **at
build time**, from C headers. Cross-references (`:ref:`/`:doc:`) are likewise
unresolved label strings until built.

This runbook builds the documentation the way Nordic builds it — with the real
2021 Sphinx toolchain, from a **fresh commit-exact `west` clone** of NCS v1.6.1 —
and ingests that **resolved HTML** into `ncs-1.6.1-resolved.sqlite` (KB1). It
answers exact-API questions the RST stubs can't, while citations point back to the
real source in that clone, preserving the pointer-first "go read the real source"
model. The same clone is the **single source of truth**: it feeds the HTML build,
the citation mapping, `get_doc`, *and* the unified RST+code index (KB2, Part D).
Both indexes are federated behind one `ncs-docs` MCP entity (Part C).

## Outcome

| Artifact | Committed? | Notes |
|---|---|---|
| `docker/ncs-1.6.1-docs.Dockerfile` | yes | pinned toolchain (doxygen 1.8.13, py3.8) |
| `docker/build-docs.sh`, `docker/constraints.txt` | yes | west clone + build; two-phase lockfile |
| resolved HTML (`_build/html/…`) | **no** | ~1–2 GB build artifact, host scratch only |
| `sdk-docs-mcp/ncs-1.6.1-resolved.sqlite` (KB1) | **no** | ~70 MB; built locally (cites the machine-local clone) |
| `sdk-docs-mcp/ncs-1.6.1-source.sqlite` (KB2) | **no** | ~0.8–1.1 GB; built locally, gitignored (Part D) |
| federated `ncs-docs` in `.mcp.json` | yes | one entity over KB1 + KB2 |

## Prerequisites

- **Docker** (Linux containers). Verified with Docker 29.x on Windows; the build
  runs entirely in an Ubuntu 18.04 container.
- A **writable scratch dir** for the west clone — the build creates it, and it
  doubles as the citation target + served source (see A2). No committed snapshot
  and no pre-existing `C:\ncs\v1.6.1` workspace are needed.
- **Network** at build time only: apt + PyPI (the 2021 pins) during the image
  and dependency install, GitHub during the west clone, and a one-time ~640 MB
  embedding-model download during indexing. The servers run fully offline.
- **Disk**: ~3–5 GB host scratch for the west workspace + `_build`. Pick a
  scratch root **outside** the repo, e.g. `C:\ncs-docbuild\` (`/c/ncs-docbuild`).

> Paths below use the git-bash `/c/...` form; Docker Desktop on Windows also
> accepts `-v C:\path:/mnt`. The build needs **no** local `C:\ncs\v1.6.1`
> workspace — sources are cloned fresh inside the container.

---

## Part A — Build resolved HTML in a pinned Linux container

### A1. Build the toolchain image

```bash
docker build -t ncs161-docs -f docker/ncs-1.6.1-docs.Dockerfile docker/
```

The image carries only the toolchain (doxygen 1.8.13 + mscgen 0.20 from bionic;
Python 3.8 from deadsnakes; cmake/ninja/west from pip). It is corpus-agnostic —
NCS itself is cloned at run time. Confirm in the build log:

```
doxygen --version   ->   1.8.13
```

If apt ever can't find bionic packages, the Dockerfile already falls back to
`old-releases.ubuntu.com`. If doxygen drifts off 1.8.13, compile 1.8.13 from
source in the image (see *Risks*).

### A2. Run the build (fresh west clone → writable output)

```bash
mkdir -p /c/ncs-docbuild/src /c/ncs-docbuild/out

docker run --rm -it \
  -v "$(pwd)/docker:/work:ro" \
  -v /c/ncs-docbuild/src:/src \
  -v /c/ncs-docbuild/out:/out \
  ncs161-docs
```

`docker/build-docs.sh` (the image's default command) then, inside the container:

1. **Clones fresh, commit-exact sources** — `west init -m sdk-nrf --mr v1.6.1`
   then `west update --narrow --fetch-opt=--filter=blob:none` (full project set,
   blobless). west checks out every project at the **exact commit the v1.6.1
   manifest pins** (nrf `651d785`, zephyr `a62ea8f`, nrfxlib `c5efbc8`, mcuboot
   `02afea3`, tfm `cb1e6c2`) — this clone is the **single source of truth** for
   the HTML build, the citation mapping, and `get_doc`. It persists in `/src`
   (host `C:\ncs-docbuild\src`), so re-runs don't re-fetch. These pins are the
   provenance record (formerly carried by the retired `ncs-1.6.1-docs/MANIFEST.md`).
2. **Installs the Python doc requirements** from the cloned sources (the six
   files the doc `CMakeLists` references). Two-phase repro — see A4.
3. **Configures + builds** all docsets:
   `cmake -GNinja -S nrf/doc -B /out/_build` then `cmake --build /out/_build`
   (= `ninja build-all`). **No `SPHINXOPTS_EXTRA=-W`** — warnings are not errors,
   which is what lets the 2021 build complete. Output lands under
   `/out/_build/html/{nrf,nrfx,nrfxlib,zephyr,mcuboot,kconfig}`.
4. **Captures** `pip freeze` → `/out/pip-freeze.txt` and the doxygen version.

The long pole is the `zephyr` docset; budget ~30–90 min wall depending on cores.
If a single docset fails, build only what you need (it pulls its own deps) and
index whatever completed — pass extra args straight through:

```bash
docker run --rm -it -v "$(pwd)/docker:/work:ro" \
  -v /c/ncs-docbuild/src:/src -v /c/ncs-docbuild/out:/out \
  ncs161-docs bash /work/build-docs.sh --target nrf-html-all --target nrfxlib-html-all
```

`nrf` + `nrfxlib` carry the bulk of the API gap, so a `zephyr`/`kconfig` failure
is non-fatal to the goal.

### A3. Verify the HTML is actually resolved

- Open `/out/_build/html/nrf/include/secure_services.html` (a former stub). Its
  *API documentation* section must now contain real signatures (`spm_request_*`),
  not just prose.
- Confirm `_sources/` exists under each docset (the theme sets
  `html_copy_source=True`). Not required by the ingest (it maps via output paths)
  but a good "the build really ran" signal.

### A4. Lock the toolchain for byte-stable rebuilds (two-phase)

The first build resolves the 2021 pins live. To make later builds reproducible:

```bash
cp /c/ncs-docbuild/out/pip-freeze.txt docker/constraints.txt
```

`build-docs.sh` installs with `-c constraints.txt` whenever that file is
non-empty, so subsequent builds use the exact same dependency set. Commit the
populated `constraints.txt`.

---

## Part B — Ingest resolved HTML into a parallel index

The embed / write / schema / meta phases are format-agnostic and reused as-is.
The new work is a parallel HTML ingest front-end, already implemented and
unit-tested (`sdk-docs-mcp/tests/test_html_chunker.py`).

### B1. What the ingest does (`sdk_docs_mcp/html_chunker.py` + `build_index.py`)

- **Chunking** (`chunk_html_file`, BeautifulSoup + lxml): one `Section` per
  Sphinx section node — handles both `<div class="section" id=…>` (docutils
  <0.17, what 1.6.1 emits) and `<section id=…>`. `breadcrumb` joins nested
  section titles; `header` is the heading text (¶ permalink stripped); `text` is
  the section's own content **including breathe API blocks**
  (`<dl class="c function">` with `<dt id="c.NAME">`), so real signatures are
  embedded *and* FTS-indexed. Each `dt[id]` is recorded as an extra `anchor`, so
  an API symbol becomes an xref target. Sidebar/nav chrome is excluded
  (`<div role="main">` scope).
- **Edges**: every `<a class="reference internal" href="page.html#id">` is
  normalized to a canonical `docname#fragment` target (relative paths resolved
  against the page, so cross-docset links resolve too) and stored in `links` —
  these point at **real** destinations, a graph the RST index only approximates.
- **Citations** (`SourceIndex` in `build_index.py`): the rendered output path
  *is* the Sphinx docname, so the docset-relative tail is unique-suffix-matched
  against the **west clone** — constrained to the docset's top folder so
  same-named pages can't collide across docsets (mcuboot lives under
  `bootloader/`). For a matched page the section cites its source `.rst`/`.md`;
  for explicit anchors the `.. _anchor:` line is looked up (with `-`/`_` swap) to
  restore line precision.
- **`meta.docs_root_relative`** holds the **clone's absolute path**, so `get_doc`
  serves the real source from the clone while `get_section` returns resolved text.
  Neither the HTML build nor the clone ships; both are local build inputs.

### B2. Per-docset citation policy

Because the clone is commit-exact (the manifest pins), citations are **line-exact
for every mapped docset** (no drift):

| Docset | Citation target | Notes |
|---|---|---|
| `nrf`, `nrfxlib`, `zephyr` | source `.rst` in the clone (exact) | suffix-match docname → `<repo>/…` |
| `mcuboot` | source `.rst`/`.md` in the clone (exact) | lives under `bootloader/mcuboot/` |
| `kconfig` | rendered page (`kconfig/<docname>.html`) | generated; no source file |
| `nrfx` | rendered page (`nrfx/<docname>.html`) | from `modules/hal/nordic` (out of mapped scope) |
| `tfm` | n/a | not built as a docset in this config |

(The docset→source-top mapping lives in `DOCSET_TO_SOURCE_TOP`; unmapped docsets
fall back to the rendered-page citation automatically.)

### B3. Build the index

```bash
uv run --project sdk-docs-mcp python -u sdk-docs-mcp/build_index.py \
    --format html \
    --docs /c/ncs-docbuild/out/_build/html \
    --source-root /c/ncs-docbuild/src \
    --out sdk-docs-mcp/ncs-1.6.1-resolved.sqlite
```

`--source-root` is the **west clone** (not a committed snapshot). The same
embedding profile applies (length-cap + length-sorted batching, ~1 GB RAM,
~45 min; the `embed.py` safeguards are in place). Expect a ~70 MB SQLite. Like KB2
it cites the machine-local clone, so it is **built locally, not committed**.
`build-resolved-docs.ps1` wires `--source-root` to the clone (`$srcDir`) automatically.

---

## Part C — Wire up the federated MCP instance

`.mcp.json` registers **one** federated `ncs-docs` (the rst-only `ncs-docs` and the
standalone `ncs-docs-resolved` are both retired/folded in):

```jsonc
"ncs-docs": {
  "command": "uv",
  "args": ["run", "--project", "sdk-docs-mcp", "sdk-docs-mcp",
           "sdk-docs-mcp/ncs-1.6.1-resolved.sqlite",
           "sdk-docs-mcp/ncs-1.6.1-source.sqlite"]
}
```

The server takes one *or more* index paths (`nargs="+"`) and fuses them. A
**missing index is skipped with a warning**, so this entry works as resolved-only
*before* KB2 (Part D) is built — no need to stage the `.mcp.json` change. Reload
MCP servers in Claude Code after each index is in place.

---

## Part D — Build the unified source-truth index (KB2) + federate

KB2 (`ncs-1.6.1-source.sqlite`) folds the NCS **RST docs** and the **source code**
(C/H, Kconfig, devicetree, …) into one index, ingested from the **same west clone**
Part A created. Together with KB1 (resolved HTML) it forms the two complementary
knowledge bases behind the single `ncs-docs` entity: KB1 = the *rendered* API
surface, KB2 = the prose **and** the real code it's drawn from.

### D1. What the ingest does (`code_chunker.py` + `build_index.py --format source`)

- **Symbol chunking** (`code_chunker.py`): a pure-regex brace matcher emits one
  chunk per top-level C/C++ construct (function / struct / union / enum / typedef /
  top-level `#define` / `*_DEFINE(...)` family) with the **symbol name as the
  `anchor`**; Kconfig is chunked per `config`/`menuconfig` (`anchor=CONFIG_<NAME>`,
  prompt string as the header); devicetree / CMake / yaml / linker / asm fall back
  to overlapping line windows. No tree-sitter — the stack stays pure-Python (no
  native Windows wheels), and a per-file `try/except` → line-window fallback means
  a parser miss never aborts the ~7k-file build.
- **Scope** is positive-listed (`CODE_SCOPE_DIRS`): `zephyr`, `nrf`, `nrfxlib`,
  `bootloader/mcuboot`, `modules/hal/{nordic,cmsis,libmetal}` — with mapped repo
  labels, so the big third-party forks (`modules/lib/{matter,openthread,…}`, the
  STM32 HAL) are never walked. Guards skip binaries/images (nrfxlib's `.a` blobs),
  files >1.5 MB, and NUL-byte (generated) files. The RST half is ingested from the
  same clone, scoped to those repos' doc trees.
- **Merge**: RST and code are chunked separately (each id-numbered `1..N`), then the
  code ids are offset past the RST block into one contiguous id space, and both
  carry a `source_kind ∈ {rst, code}` column. `meta.docs_root_relative` holds the
  clone's absolute path, so `get_doc` serves rst *and* code from the one root.

### D2. Build it

```bash
# Fast first pass — validate the whole pipeline (~1–1.5 h, ~200 MB):
uv run --project sdk-docs-mcp python -u sdk-docs-mcp/build_index.py \
    --format source --docs /c/ncs-docbuild/src --code-root /c/ncs-docbuild/src \
    --no-tests --code-granularity file --out sdk-docs-mcp/ncs-1.6.1-source.sqlite

# Full production build — per-symbol anchors + samples + tests (~5–7 h, ~0.8–1.1 GB):
uv run --project sdk-docs-mcp python -u sdk-docs-mcp/build_index.py \
    --format source --docs /c/ncs-docbuild/src --code-root /c/ncs-docbuild/src \
    --out sdk-docs-mcp/ncs-1.6.1-source.sqlite
```

`--docs` and `--code-root` are the **same** clone. Levers: `--code-granularity file`
(≈3–5× fewer chunks, no symbol anchors) and `--no-tests` (~−21% of C/H). KB2 is
~1 GB and resolves `get_doc` against the machine-local clone, so it is **gitignored
and rebuilt locally**, never committed.

### D3. Verify the federation

On `ncs-docs` (both indexes present), reload MCP servers, then:

1. **Dual-source recall:** `search_docs("legacy and extended advertising
   simultaneously", k=10)` returns **both** a doc hit (`source_kind ∈ {html, rst}`)
   **and** a code hit (`source_kind="code"`) from `zephyr/subsys/bluetooth`, each
   labelled by its `corpus`.
2. **Symbol search:** `search_docs("bt_le_ext_adv_create", mode="keyword")` → a code
   chunk with `anchor == "bt_le_ext_adv_create"`.
3. **CONFIG search:** `search_docs("CONFIG_BT_EXT_ADV", mode="keyword")` → a Kconfig
   chunk (`anchor="CONFIG_BT_EXT_ADV"`) plus a doc hit.
4. **Filter:** `source=["code"]` returns code only; `source=["rst","html"]` docs only.
5. **`get_doc` into the clone:** `get_doc("zephyr/subsys/bluetooth/host/adv.c",
   corpus="source")` returns real source from `C:\ncs-docbuild\src`.

The chunker + `ingest_source` + federated-server logic is covered offline (no ONNX)
by `sdk-docs-mcp/tests/test_code_chunker.py`:

```bash
uv run --project sdk-docs-mcp python sdk-docs-mcp/tests/test_code_chunker.py
```

---

## Verification

1. **API content present (the whole point).** On `ncs-docs` (filter
   `source=["html"]` for KB1): `search_docs("secure services API", mode=keyword)`
   returns a section whose text includes real `spm_request_*` signatures (the raw
   stub `.rst` in the clone has only the `.. doxygengroup::` directive).
2. **Doxygen-only symbol.** `search_docs("nrf_modem_init", mode=keyword)` hits a
   section carrying the real prototype.
3. **Resolved xrefs.** `related(id)` on a resolved section yields concrete
   neighbour sections, not just unresolved label strings.
4. **Provenance into the clone.** `get_doc(<file_path>)` returns the real source
   from the west clone (e.g. `nrf/doc/nrf/…` or `bootloader/mcuboot/docs/…`).
5. **Pristine inputs.** The clone lives in host scratch; the repo and any local
   `C:\ncs\v1.6.1` workspace are untouched — the build only writes under scratch.

The Part B code path (chunk → map → resolve → embed → write → FTS query) is
covered by `sdk-docs-mcp/tests/test_html_chunker.py`:

```bash
uv run --project sdk-docs-mcp python sdk-docs-mcp/tests/test_html_chunker.py
```

---

## Risks & fallbacks

- **2021 pip resolution drift** → capture `pip freeze` into `docker/constraints.txt`
  after the first good build and install with `-c` thereafter (Part A4).
- **bionic apt EOL** → Dockerfile falls back to `old-releases.ubuntu.com`.
- **doxygen not exactly 1.8.13 from apt** → compile 1.8.13 from source in the image.
- **A docset fails to build** → build per-docset (`--target nrf-html-all
  nrfxlib-html-all`); index whatever completed.
- **Network blocked for the west clone** → as a last resort, mount a local
  `C:\ncs\v1.6.1` workspace at `/src` instead of cloning. Be aware its checkout
  may have drifted from the manifest pins (the local zephyr was at `242ea14`,
  not the pin `a62ea8f`), which makes zephyr citations approximate; nrf/nrfxlib/
  mcuboot are unaffected.
- **Last resort** (only if the local build proves intractable): mirror Nordic's
  published 1.6.1 HTML and ingest it with the same `--format html` path.

## Effort & size

- HTML build: ~30–90 min wall; `_build` ~1–2 GB (not committed).
- KB1 index build: ~45 min; `ncs-1.6.1-resolved.sqlite` ~70 MB (built locally).
- KB2 index build: ~1–1.5 h (fast pass) to ~5–7 h (full); `ncs-1.6.1-source.sqlite`
  ~0.2–1.1 GB (built locally, gitignored). See Part D.
- New code: `html_chunker.py` + `build_index.py` ingest split + deps (~250 LOC),
  validated end-to-end on a synthetic Sphinx fixture before the real build.
