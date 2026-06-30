#!/usr/bin/env python
"""Self-contained checks for the source-code ingest + federation paths.

No external fixtures and **no ONNX**: it exercises the regex code chunker, the
``ingest_code`` / ``ingest_source`` front-ends, and the federated server end-to-
end on a synthetic two-corpus pair of v2 SQLite indexes built with deterministic
fake vectors (embedding is format-agnostic and already proven by the shipped
indexes). The query embedder is stubbed, so this runs in milliseconds offline.

Run:  uv run --project sdk-docs-mcp python sdk-docs-mcp/tests/test_code_chunker.py
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

import numpy as np
import sqlite_vec

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sdk_docs_mcp import EMBED_DIM  # noqa: E402
from sdk_docs_mcp.code_chunker import (  # noqa: E402
    CODE_SCOPE_DIRS, chunk_code_file, clean_code_for_embedding,
)


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

ADV_C = """/*
 * Copyright (c) 2021 Nordic Semiconductor ASA
 * SPDX-License-Identifier: LicenseRef-Nordic-5-Clause
 */
#include <bluetooth/bluetooth.h>

#define BT_LE_EXT_ADV_OPT 0

struct bt_le_ext_adv {
\tuint8_t handle;
};

/** @brief Create an extended advertising set. */
int bt_le_ext_adv_create(const struct bt_le_adv_param *param,
                         struct bt_le_ext_adv **adv)
{
\tif (param == NULL) {
\t\treturn -EINVAL;
\t}
\treturn 0;
}
"""

BT_KCONFIG = """menu "Bluetooth"

config BT_EXT_ADV
\tbool "Extended Advertising and Scanning support"
\thelp
\t  Enable extended (BT5) advertising alongside legacy advertising.

config BT_MAX_CONN
\tint "Maximum number of connections"
\tdefault 1

endmenu
"""

BOARD_DTS = """/dts-v1/;
#include <nordic/nrf52840_qiaa.dtsi>

/ {
\tmodel = "Nordic nRF52840 DK";
};

&uart0 {
\tstatus = "okay";
};
"""


# --------------------------------------------------------------------------- #
# 1. Code chunker
# --------------------------------------------------------------------------- #


def test_split_c() -> None:
    print("chunk_code_file (C symbols):")
    secs = chunk_code_file(ADV_C, "zephyr", "zephyr/subsys/bluetooth/host/adv.c", "c")
    by_anchor = {s.anchor: s for s in secs}

    check("bt_le_ext_adv_create" in by_anchor, "function captured by name")
    check("bt_le_ext_adv" in by_anchor, "struct captured by tag name")
    check("BT_LE_EXT_ADV_OPT" in by_anchor, "#define captured by name")

    fn = by_anchor["bt_le_ext_adv_create"]
    check(fn.header.startswith("int bt_le_ext_adv_create("), f"header is the signature ({fn.header!r})")
    check("@brief Create an extended" in fn.text, "preceding doc comment attached to the function")
    check(fn.line_start < fn.line_end, "function spans a 1-based line range")
    check(fn.breadcrumb.endswith("bt_le_ext_adv_create"), "breadcrumb ends with the symbol")
    check(fn.all_anchors == ["bt_le_ext_adv_create"], "primary anchor recorded in all_anchors")

    pre = [s for s in secs if s.anchor == ""]
    check(any("#include" in s.text for s in pre), "#include coalesced into a preamble chunk")
    print("  PASS\n")


def test_split_kconfig() -> None:
    print("chunk_code_file (Kconfig):")
    secs = chunk_code_file(BT_KCONFIG, "zephyr", "zephyr/subsys/bluetooth/Kconfig", "kconfig")
    by_anchor = {s.anchor: s for s in secs}

    check("CONFIG_BT_EXT_ADV" in by_anchor, "config entry anchored as CONFIG_<NAME>")
    check("CONFIG_BT_MAX_CONN" in by_anchor, "second config entry captured")
    ext = by_anchor["CONFIG_BT_EXT_ADV"]
    check(ext.header == "Extended Advertising and Scanning support",
          f"header is the prompt string (got {ext.header!r})")
    check("BT5" in ext.text, "help text kept in the chunk body")
    print("  PASS\n")


def test_split_window() -> None:
    print("chunk_code_file (window: dts):")
    secs = chunk_code_file(BOARD_DTS, "zephyr", "zephyr/boards/arm/x/x.dts", "window")
    check(len(secs) >= 1, "dts produced at least one window chunk")
    check(all(s.anchor == "" for s in secs), "window chunks carry no symbol anchor")
    check(secs[0].line_start == 1, "first window starts at line 1")
    print("  PASS\n")


def test_clean_code() -> None:
    print("clean_code_for_embedding:")
    cleaned = clean_code_for_embedding(ADV_C)
    check("Copyright" not in cleaned and "SPDX" not in cleaned, "SPDX/license header dropped")
    check("bt_le_ext_adv_create" in cleaned, "identifiers preserved verbatim")
    check("#include <bluetooth/bluetooth.h>" in cleaned, "includes preserved (no RST stripping)")
    print("  PASS\n")


# --------------------------------------------------------------------------- #
# 2. ingest_code / ingest_source
# --------------------------------------------------------------------------- #


def _make_clone(base: Path) -> Path:
    clone = base / "clone"
    (clone / "zephyr" / "doc").mkdir(parents=True)
    (clone / "zephyr" / "doc" / "adv.rst").write_text(
        ".. _adv:\n\nAdvertising\n===========\n\nLegacy and extended advertising.\n",
        encoding="utf-8")
    host = clone / "zephyr" / "subsys" / "bluetooth" / "host"
    host.mkdir(parents=True)
    (host / "adv.c").write_text(ADV_C, encoding="utf-8")
    (clone / "zephyr" / "subsys" / "bluetooth" / "Kconfig").write_text(BT_KCONFIG, encoding="utf-8")
    # a test file + an excluded third-party fork
    (clone / "zephyr" / "tests" / "bt").mkdir(parents=True)
    (clone / "zephyr" / "tests" / "bt" / "main.c").write_text("void test_main(void){}\n", encoding="utf-8")
    (clone / "modules" / "lib" / "matter").mkdir(parents=True)
    (clone / "modules" / "lib" / "matter" / "x.c").write_text("int matter_fn(void){return 0;}\n", encoding="utf-8")
    return clone


def test_ingest_code() -> None:
    print("ingest_code (triple + scope):")
    import build_index

    with tempfile.TemporaryDirectory() as d:
        clone = _make_clone(Path(d))
        sections, link_rows, embed_texts = build_index.ingest_code(
            clone, CODE_SCOPE_DIRS, include_tests=True)

        check(link_rows == [], "code emits no link rows in v1")
        check(len(embed_texts) == len(sections), "one embed text per section")
        check(all(getattr(s, "source_kind", None) == "code" for s in sections),
              "every code section tagged source_kind='code'")
        anchors = {s.anchor for s in sections}
        check("bt_le_ext_adv_create" in anchors, "C function symbol present")
        check("CONFIG_BT_EXT_ADV" in anchors, "Kconfig symbol present")
        check(not any("matter" in s.file_path for s in sections),
              "excluded fork (modules/lib/matter) never ingested")
        check(any(s.file_path == "zephyr/subsys/bluetooth/host/adv.c" for s in sections),
              "file_path is clone-relative for get_doc resolution")

        no_tests, _, _ = build_index.ingest_code(clone, CODE_SCOPE_DIRS, include_tests=False)
        check(not any("tests" in s.file_path for s in no_tests), "--no-tests drops */tests/*")
        print("  PASS\n")


def test_ingest_source_merge() -> None:
    print("ingest_source (merge: id-offset + kind distribution):")
    import build_index

    with tempfile.TemporaryDirectory() as d:
        clone = _make_clone(Path(d))
        sections, link_rows, embed_texts = build_index.ingest_source(
            clone, CODE_SCOPE_DIRS, include_tests=True)

        ids = [s.id for s in sections]
        check(ids == list(range(1, len(sections) + 1)), "merged id space is contiguous 1..N")
        kinds = {}
        for s in sections:
            kinds[s.source_kind] = kinds.get(s.source_kind, 0) + 1
        check(kinds.get("rst", 0) >= 1 and kinds.get("code", 0) >= 1,
              f"both rst and code kinds present ({kinds})")
        # code ids strictly follow rst ids (offset == number of rst sections)
        rst_max = max(s.id for s in sections if s.source_kind == "rst")
        code_min = min(s.id for s in sections if s.source_kind == "code")
        check(code_min == rst_max + 1, "code ids are offset directly past the rst block")
        check(len(embed_texts) == len(sections), "one embed text per merged section")
        print("  PASS\n")


# --------------------------------------------------------------------------- #
# 3. Federated server (synthetic 2-corpus indexes, deterministic fake vectors)
# --------------------------------------------------------------------------- #


def _shutdown(server) -> None:
    """Close every open corpus connection (Windows can't delete an open file)."""
    for c in server._CORPORA.values():
        try:
            c.db.close()
        except Exception:  # noqa: BLE001
            pass
    server._CORPORA.clear()
    server._embedder = None


def _fake_vec(seed: int) -> bytes:
    v = np.zeros(EMBED_DIM, dtype="<f4")
    v[seed % EMBED_DIM] = 1.0
    v[(seed * 7 + 1) % EMBED_DIM] = 0.5
    return v.tobytes()


def _write_index(path: Path, rows: list[dict], docs_root_abs: str, source_format: str) -> None:
    """Write a minimal v2 index (the real build's schema + inserts, fake vectors)."""
    import build_index

    db = sqlite3.connect(path)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.executescript(build_index.SCHEMA.format(dim=EMBED_DIM))
    for i, r in enumerate(rows, start=1):
        db.execute(
            "INSERT INTO sections(id, repo, file_path, anchor, breadcrumb, header, "
            "line_start, line_end, text, source_kind) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (i, r["repo"], r["file_path"], r["anchor"], r.get("breadcrumb", ""),
             r.get("header", ""), r.get("ls", 1), r.get("le", 1), r["text"], r["kind"]))
        db.execute("INSERT INTO fts_sections(rowid, text, header, anchor) VALUES (?,?,?,?)",
                   (i, r["text"], r.get("header", ""), r["anchor"]))
        db.execute("INSERT INTO vec_sections(section_id, embedding) VALUES (?,?)",
                   (i, _fake_vec(i + r.get("vseed", 0))))
    from sdk_docs_mcp.store import write_meta
    from sdk_docs_mcp import EMBED_MODEL, SCHEMA_VERSION
    write_meta(db, {
        "schema_version": str(SCHEMA_VERSION), "embed_model": EMBED_MODEL,
        "embed_dim": str(EMBED_DIM), "docs_root_relative": docs_root_abs,
        "section_count": str(len(rows)), "link_count": "0",
        "source_format": source_format,
    })
    db.commit()
    db.close()


def test_federation() -> None:
    print("federated server (RRF + prefixed ids + source filter + routing):")
    from sdk_docs_mcp import server

    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        clone = _make_clone(base)  # provides a real adv.c for get_doc
        clone_abs = clone.as_posix()

        resolved = base / "ncs-1.6.1-resolved.sqlite"
        source = base / "ncs-1.6.1-source.sqlite"
        _write_index(resolved, [
            {"repo": "zephyr", "file_path": "zephyr/doc/adv.rst", "anchor": "adv-api",
             "header": "Advertising API", "kind": "html",
             "text": "Extended advertising API: bt_le_ext_adv_create signature and params."},
        ], clone_abs, "html")
        _write_index(source, [
            {"repo": "zephyr", "file_path": "zephyr/doc/adv.rst", "anchor": "adv",
             "header": "Advertising", "kind": "rst",
             "text": "Legacy and extended advertising overview prose."},
            {"repo": "zephyr", "file_path": "zephyr/subsys/bluetooth/host/adv.c",
             "anchor": "bt_le_ext_adv_create", "header": "int bt_le_ext_adv_create(...)",
             "kind": "code", "ls": 14, "le": 22, "vseed": 100,
             "text": "/* create an extended advertising set */\n"
                     "int bt_le_ext_adv_create(const struct bt_le_adv_param *param) { return 0; }"},
        ], clone_abs, "source")

        server._CORPORA.clear()
        server.register([str(resolved), str(source)])
        try:
            check(set(server._CORPORA) == {"resolved", "source"},
                  f"both corpora registered by short name (got {set(server._CORPORA)})")
            check(server._multi(), "federation is in multi-corpus mode")

            # --- dual-source recall + prefixed ids -------------------------
            res = server.search_docs("advertising", k=10, mode="keyword")
            corpora_hit = {r["corpus"] for r in res["results"]}
            kinds_hit = {r["source_kind"] for r in res["results"]}
            check(corpora_hit == {"resolved", "source"}, f"hits from both corpora ({corpora_hit})")
            check(all(isinstance(r["id"], str) and ":" in r["id"] for r in res["results"]),
                  "multi-corpus ids are self-describing 'corpus:local'")
            check("html" in kinds_hit and ("rst" in kinds_hit or "code" in kinds_hit),
                  f"results labelled by source_kind ({kinds_hit})")

            # --- symbol search hits the code chunk -------------------------
            sym = server.search_docs("bt_le_ext_adv_create", mode="keyword")
            code_hits = [r for r in sym["results"] if r["source_kind"] == "code"]
            check(code_hits and code_hits[0]["anchor"] == "bt_le_ext_adv_create",
                  "keyword symbol search returns the code chunk with the symbol anchor")

            # --- source filter ---------------------------------------------
            only_code = server.search_docs("advertising", k=10, mode="keyword", source=["code"])
            check(only_code["results"] and all(r["source_kind"] == "code" for r in only_code["results"]),
                  "source=['code'] returns code only")
            only_docs = server.search_docs("advertising", k=10, mode="keyword", source=["rst", "html"])
            check(only_docs["results"] and all(r["source_kind"] in ("rst", "html") for r in only_docs["results"]),
                  "source=['rst','html'] returns docs only")

            # --- get_section round-trip on a 'corpus:local' id -------------
            cid = code_hits[0]["id"]
            sec = server.get_section(cid)
            check(sec.get("source_kind") == "code" and "bt_le_ext_adv_create" in sec.get("text", ""),
                  f"get_section('{cid}') returns full text + correct kind")

            # --- get_doc into the (synthetic) clone ------------------------
            doc = server.get_doc("zephyr/subsys/bluetooth/host/adv.c", corpus="source")
            check("bt_le_ext_adv_create" in doc.get("text", ""),
                  "get_doc resolves the code file against the corpus's clone root")
            check("error" not in server.get_doc("zephyr/doc/adv.rst"),
                  "get_doc with no corpus tries each root in turn")

            # --- hybrid path runs with a stubbed (no-ONNX) embedder --------
            class _FakeEmbedder:
                def embed_query(self, text):
                    return np.frombuffer(_fake_vec(2 + 100), dtype="<f4")  # ~ the code vector

            server._embedder = _FakeEmbedder()
            hyb = server.search_docs("extended advertising", k=10, mode="hybrid")
            check(hyb["results"] and all(":" in r["id"] for r in hyb["results"]),
                  "hybrid mode fuses BM25 + vectors across corpora (prefixed ids)")
        finally:
            _shutdown(server)
        print("  PASS\n")


def test_graceful_missing_index() -> None:
    print("graceful degradation (missing index skipped):")
    from sdk_docs_mcp import server

    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        clone = _make_clone(base)
        present = base / "ncs-1.6.1-resolved.sqlite"
        _write_index(present, [
            {"repo": "zephyr", "file_path": "zephyr/doc/adv.rst", "anchor": "adv",
             "header": "Advertising", "kind": "html", "text": "advertising"},
        ], clone.as_posix(), "html")

        server._CORPORA.clear()
        # the source index does not exist yet (not built) — must be skipped, not fatal
        server.register([str(present), str(base / "ncs-1.6.1-source.sqlite")])
        try:
            check(set(server._CORPORA) == {"resolved"},
                  "missing index skipped; present one still served")
            check(not server._multi(), "single surviving corpus -> bare-int id mode")
            r = server.search_docs("advertising", mode="keyword")
            check(r["results"] and isinstance(r["results"][0]["id"], int),
                  "degraded single-corpus search returns bare-int ids")
        finally:
            _shutdown(server)
        print("  PASS\n")


if __name__ == "__main__":
    test_split_c()
    test_split_kconfig()
    test_split_window()
    test_clean_code()
    test_ingest_code()
    test_ingest_source_merge()
    test_federation()
    test_graceful_missing_index()
    print("ALL CHECKS PASSED")
