# Remaining work — unified NCS 1.6.1 source-truth KB + federation

Status snapshot for the plan *"Unified, federated NCS v1.6.1 knowledge base
(doc + source code)"* (`terme-mon-id-e-cosmic-moonbeam`).

**The code is implemented, tested, and wired.** What's left is the gated,
machine-local **build** of the two NCS indexes and the **verification** that the
federation answers real queries — neither could run here because the west clone
(`C:\ncs-docbuild\src`) isn't present and the full build is ~5–7 h.

---

## Done (this change)

- `sdk_docs_mcp/code_chunker.py` — symbol-aware C/Kconfig/dts/… splitter (regex
  brace matcher, per-file fallback).
- `build_index.py` — `--format source` (merged RST+code), `--code-root`,
  `--no-tests`, `--code-granularity`; `source_kind` column; `SCHEMA_VERSION = 2`.
- `server.py` — federation: `_CORPORA` registry, per-corpus retrieval, namespaced
  RRF, `source` filter, `"corpus:local"` ids, per-corpus `get_doc`, `nargs="+"`,
  **graceful skip** of a missing/broken index.
- `store.py` — `PRAGMA` probe + `has_source_kind`/`source_format` (v1 indexes still open).
- `tests/test_code_chunker.py` — chunker + `ingest_source` + **no-ONNX federated
  server** checks. Both test suites green; `nrf-bm.sqlite` (v1) still opens.
- Wiring/docs — federated `ncs-docs` in `.mcp.json`, `ncs-1.6.1-source.sqlite`
  gitignored, README + runbook (`build-ncs-1.6.1-doc.md` Part D) updated.

## Current on-disk state (verified)

| Artifact | Present? | Action |
|---|---|---|
| west clone `C:\ncs-docbuild\src` | **absent** | build it (Step 1) |
| `sdk-docs-mcp/ncs-1.6.1-resolved.sqlite` (KB1) | **absent** | build it (Step 2) |
| `sdk-docs-mcp/ncs-1.6.1-source.sqlite` (KB2) | **absent** | build it (Step 3) |
| `sdk-docs-mcp/nrf-bm.sqlite` | present, committed | none |

Until KB1/KB2 exist, the federated `ncs-docs` server **degrades gracefully**: it
starts, warns about the missing index(es), and serves whatever is present (`bm-docs`
is unaffected throughout).

---

## What remains

### Step 1 — Create the commit-exact west clone  *(prerequisite, ~30–90 min, network)*

Needs Docker. Either run the orchestrator and stop after the HTML stage, or do the
clone-only Docker step from `docs/build-ncs-1.6.1-doc.md` (Part A2). The clone lands
in `C:\ncs-docbuild\src` and is the single source of truth for KB1 **and** KB2.

```powershell
# from repo root — builds the image + clones + renders HTML (also produces KB1 input)
.\build-resolved-docs.ps1 -SkipIndex
```

### Step 2 — Build KB1 (resolved HTML)  *(~45 min)*

```powershell
.\build-resolved-docs.ps1 -SkipImageBuild -SkipDocsBuild   # HTML already built -> just index
```
or directly:
```bash
uv run --project sdk-docs-mcp python -u sdk-docs-mcp/build_index.py \
    --format html --docs /c/ncs-docbuild/out/_build/html \
    --source-root /c/ncs-docbuild/src --out sdk-docs-mcp/ncs-1.6.1-resolved.sqlite
```

### Step 3 — Build KB2 (unified RST + source code)  *(the heavy one)*

**Fast first pass** — validate the whole pipeline end-to-end (~1–1.5 h, ~200 MB):
```bash
uv run --project sdk-docs-mcp python -u sdk-docs-mcp/build_index.py \
    --format source --docs /c/ncs-docbuild/src --code-root /c/ncs-docbuild/src \
    --no-tests --code-granularity file --out sdk-docs-mcp/ncs-1.6.1-source.sqlite
```
Smoke-test it (Step 5), then **full production build** — per-symbol anchors +
samples + tests (~5–7 h, ~0.8–1.1 GB), overwriting the fast index:
```bash
uv run --project sdk-docs-mcp python -u sdk-docs-mcp/build_index.py \
    --format source --docs /c/ncs-docbuild/src --code-root /c/ncs-docbuild/src \
    --out sdk-docs-mcp/ncs-1.6.1-source.sqlite
```
Tune with `--threads <n>`. KB2 is gitignored (rebuild-not-commit).

### Step 4 — Reload MCP servers

Reload MCP servers in Claude Code so `ncs-docs` picks up both indexes. (No
`.mcp.json` change needed — it already lists both, and the server skipped the
missing one before.)

### Step 5 — Verify the federation

Run the offline test suite first (proves the code path without a real build):
```bash
uv run --project sdk-docs-mcp python sdk-docs-mcp/tests/test_code_chunker.py
uv run --project sdk-docs-mcp python sdk-docs-mcp/tests/test_html_chunker.py
```

Then, against the live `ncs-docs` server, confirm each item (plan's verification +
runbook D3):

- [ ] **Dual-source recall:** `search_docs("legacy and extended advertising
      simultaneously", k=10)` returns **both** a doc hit (`source_kind ∈ {html,rst}`)
      **and** a `source_kind="code"` hit from `zephyr/subsys/bluetooth` — distinct `corpus`.
- [ ] **Symbol search:** `search_docs("bt_le_ext_adv_create", mode="keyword")` → a code
      chunk with `anchor == "bt_le_ext_adv_create"`.
- [ ] **CONFIG search:** `search_docs("CONFIG_BT_EXT_ADV", mode="keyword")` → a Kconfig
      chunk (`anchor="CONFIG_BT_EXT_ADV"`) **and** a doc hit.
- [ ] **`get_doc` into the clone:** `get_doc("zephyr/subsys/bluetooth/host/adv.c",
      corpus="source")` returns real source from `C:\ncs-docbuild\src`.
- [ ] **`get_section` round-trip:** a `"source:NNN"` id from search → full text + correct `source_kind`.
- [ ] **`source` filter:** `source=["code"]` → code only; `source=["rst","html"]` → docs only.
- [ ] **Backward-compat:** `bm-docs` behaves exactly as before (bare-int ids).

---

## Decisions / open items (need your call)

1. **`build-resolved-docs.ps1` commit hint.** I changed the docs to say KB1 is
   *built-locally, not committed* (matches the plan's artifact policy and git
   reality — it isn't tracked). The PowerShell script still prints a *"To commit
   the artifact: git add …"* hint at the end. Decide: keep KB1 local-only (drop the
   hint) **or** actually commit KB1. *(Not blocking; cosmetic contradiction only.)*
2. **`--no-tests` for the full build?** Default scope includes samples + tests
   (per the plan). `--no-tests` trims ~21% of C/H, ~1 h, ~150 MB if size/time is
   tight. Recommendation: keep tests in the production build.
3. **Tuning `--threads`** to the build host's core count to shave embed wall-time.

---

## Known limitations / future work (by design, v1)

- **Code emits no xref edges.** `related()` on a code section returns empties (the
  `#include` graph is different semantics from the Sphinx xref graph; symbol anchors
  + RRF already let the agent pivot). *Future:* match code `anchor` ↔ HTML API
  `dt[id]` anchors to bridge KB1↔KB2.
- **Regex C chunker is heuristic.** A few constructs (e.g. a function with a trailing
  `__attribute__((…))` before the body) can mis-pick the anchor; the per-file
  fallback + full-text FTS keep them searchable regardless. tree-sitter was
  deliberately avoided (no native Windows wheels).
- **Stale checkout.** Chunk text is frozen at build time; `get_doc` reads the live
  clone. The build note records the manifest rev (`v1.6.1`) + resolved shas for repro.
- **Windows console encoding.** Keep `build_index.py`'s module docstring (argparse
  `description`) cp1252-safe — non-cp1252 glyphs (`∈ → ≤ ×`) crash `--help` on a
  cp1252 console. ASCII-ify new docstring chars.
