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
and ingests that **resolved HTML** into `ncs-1.6.1-resolved.sqlite` (served as
`ncs-docs-resolved`). It answers exact-API questions the RST stubs can't, while
citations point back to the real source in that clone, preserving the
pointer-first "go read the real source" model. The same clone is the **single
source of truth**: it feeds the HTML build, the citation mapping, and `get_doc`.

## Outcome

| Artifact | Committed? | Notes |
|---|---|---|
| `docker/ncs-1.6.1-docs.Dockerfile` | yes | pinned toolchain (doxygen 1.8.13, py3.8) |
| `docker/build-docs.sh`, `docker/constraints.txt` | yes | west clone + build; two-phase lockfile |
| resolved HTML (`_build/html/…`) | **no** | ~1–2 GB build artifact, host scratch only |
| `sdk-docs-mcp/ncs-1.6.1-resolved.sqlite` | yes | ~70 MB, the deliverable index |
| `ncs-docs-resolved` in `.mcp.json` | yes | second server instance |

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

- Open `/out/_build/html/nrf/security/secure_services.html` (a former stub). Its
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
~45 min; the `embed.py` safeguards are in place). Expect a ~70 MB SQLite,
committed (`*.sqlite` is `binary -diff` per `.gitattributes`). `build-resolved-docs.ps1`
wires `--source-root` to the clone (`$srcDir`) automatically.

---

## Part C — Wire up the new MCP instance

Already in `.mcp.json` (the rst-only `ncs-docs` has been retired):

```jsonc
"ncs-docs-resolved": {
  "command": "uv",
  "args": ["run", "--project", "sdk-docs-mcp", "sdk-docs-mcp",
           "sdk-docs-mcp/ncs-1.6.1-resolved.sqlite"]
}
```

No server code change — the four tools are corpus-neutral and read everything
from the index + `meta`. Reload MCP servers in Claude Code after the index is in
place.

---

## Verification

1. **API content present (the whole point).** On `ncs-docs-resolved`:
   `search_docs("secure services API", mode=keyword)` returns a section whose
   text includes real `spm_request_*` signatures (the raw stub `.rst` in the clone
   has only the `.. doxygengroup::` directive).
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
- Index build: ~45 min; `ncs-1.6.1-resolved.sqlite` ~70 MB (committed).
- New code: `html_chunker.py` + `build_index.py` ingest split + deps (~250 LOC),
  validated end-to-end on a synthetic Sphinx fixture before the real build.
