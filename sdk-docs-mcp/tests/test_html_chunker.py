#!/usr/bin/env python
"""Self-contained checks for the resolved-HTML ingest path.

No external fixtures: builds a tiny Sphinx-3.3-style ``_build/html`` tree and a
matching west-clone source tree (incl. the ``bootloader/mcuboot`` layout) in a
temp dir, then exercises ``chunk_html_file`` and ``build_index.ingest_html``
end-to-end (everything *before* embedding, which is format-agnostic and already
proven by the shipped indexes).

Run:  uv run --project sdk-docs-mcp python sdk-docs-mcp/tests/test_html_chunker.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sdk_docs_mcp.html_chunker import chunk_html_file  # noqa: E402

# A doxygen-stub page once Sphinx+breathe has resolved it: the "API
# documentation" section now holds a real signature, with a ¶ headerlink to
# strip and an internal xref to a sibling page.
SECURE_SERVICES_HTML = """<!DOCTYPE html>
<html><head><title>Secure Services</title></head><body>
<div class="wy-nav-side">SIDEBAR NAV — must not be indexed</div>
<div role="main">
  <div class="section" id="secure-services">
    <h1>Secure Services<a class="headerlink" href="#secure-services">¶</a></h1>
    <p>Intro prose. See <a class="reference internal" href="../spm.html#spm-api">
       <span class="std std-ref">the SPM</span></a> for details.</p>
    <div class="section" id="api-documentation">
      <h2>API documentation<a class="headerlink" href="#api-documentation">¶</a></h2>
      <dl class="c function">
        <dt id="c.spm_request_read">
          <code class="descname">spm_request_read</code>
          (<em>void *destination</em>, <em>uint32_t addr</em>)
        </dt>
        <dd><p>Request a read of the non-secure address.</p></dd>
      </dl>
    </div>
  </div>
</div></body></html>"""

SPM_HTML = """<!DOCTYPE html>
<html><head><title>SPM</title></head><body><div role="main">
  <div class="section" id="spm-api"><h1>SPM API</h1><p>Secure Partition Manager.</p></div>
</div></body></html>"""

KCONFIG_HTML = """<!DOCTYPE html>
<html><head><title>CONFIG_FOO</title></head><body><div role="main">
  <div class="section" id="cmdoption-config-foo"><h1>CONFIG_FOO</h1>
  <p>A generated Kconfig option with no source .rst in the clone.</p></div>
</div></body></html>"""

# mcuboot's docset maps to bootloader/mcuboot/ in the west clone (not a top-level
# mcuboot/ as in the old snapshot) — exercises DOCSET_TO_SOURCE_TOP.
MCUBOOT_HTML = """<!DOCTYPE html>
<html><head><title>Design</title></head><body><div role="main">
  <div class="section" id="mcuboot-design"><h1>MCUboot Design</h1>
  <p>How the bootloader validates images.</p></div>
</div></body></html>"""

SECURE_SERVICES_RST = """.. _secure_services:

Secure Services
###############

Intro prose.

.. _api_documentation:

API documentation
*****************

.. doxygengroup:: secure_services
"""


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def test_chunk_html_file() -> None:
    print("chunk_html_file:")
    secs = chunk_html_file(SECURE_SERVICES_HTML, "nrf", "nrf/security/secure_services")
    by_anchor = {s.anchor: s for s in secs}

    check("secure-services" in by_anchor, "top section captured by id")
    check("api-documentation" in by_anchor, "nested section captured by id")

    api = by_anchor["api-documentation"]
    check(api.breadcrumb == "Secure Services > API documentation",
          f"breadcrumb nests titles (got {api.breadcrumb!r})")
    check(api.header == "API documentation", "header is the h2 text, ¶ stripped")
    check("c.spm_request_read" in api.all_anchors,
          "breathe dt[id] recorded as an API anchor")
    check("spm_request_read" in api.text and "non-secure address" in api.text,
          "real signature + description kept in section text")
    check("¶" not in api.text, "headerlink permalink stripped from text")

    top = by_anchor["secure-services"]
    check("SIDEBAR NAV" not in top.text, "non-main chrome excluded")
    check("nrf/spm#spm-api" in top.raw_links,
          f"relative internal xref resolved to docname#frag (got {top.raw_links!r})")
    print("  PASS\n")


def test_ingest_html() -> None:
    print("ingest_html (discover -> map -> resolve):")
    import build_index

    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        html = base / "_build" / "html"
        (html / "nrf" / "security").mkdir(parents=True)
        (html / "kconfig").mkdir(parents=True)
        (html / "mcuboot").mkdir(parents=True)
        (html / "nrf" / "security" / "secure_services.html").write_text(SECURE_SERVICES_HTML, encoding="utf-8")
        (html / "nrf" / "spm.html").write_text(SPM_HTML, encoding="utf-8")
        (html / "kconfig" / "index.html").write_text(KCONFIG_HTML, encoding="utf-8")
        (html / "mcuboot" / "design.html").write_text(MCUBOOT_HTML, encoding="utf-8")

        clone = base / "clone"
        rst = clone / "nrf" / "doc" / "nrf" / "security" / "secure_services.rst"
        rst.parent.mkdir(parents=True)
        rst.write_text(SECURE_SERVICES_RST, encoding="utf-8")
        (clone / "nrf" / "doc" / "nrf" / "spm.rst").write_text("SPM\n===\n", encoding="utf-8")
        mb_src = clone / "bootloader" / "mcuboot" / "docs" / "design.md"
        mb_src.parent.mkdir(parents=True)
        mb_src.write_text("# MCUboot Design\n", encoding="utf-8")

        sections, link_rows, embed_texts = build_index.ingest_html(html, clone)
        by_anchor = {s.anchor: s for s in sections}

        ss = by_anchor["secure-services"]
        check(ss.repo == "nrf", "mapped section repo = docset label")
        check(ss.file_path == "nrf/doc/nrf/security/secure_services.rst",
              f"docname suffix-matched to clone .rst (got {ss.file_path})")
        check(ss.line_start == 1,
              f"anchor line resolved via _-/-swap (.. _secure_services: at L1, got {ss.line_start})")

        mb = by_anchor["mcuboot-design"]
        check(mb.repo == "mcuboot" and mb.file_path == "bootloader/mcuboot/docs/design.md",
              f"mcuboot maps under bootloader/ in the clone layout (got {mb.repo}:{mb.file_path})")

        kc = by_anchor["cmdoption-config-foo"]
        check(kc.repo == "kconfig" and kc.file_path == "kconfig/index.html",
              f"unmapped docset cites rendered page (got {kc.repo}:{kc.file_path})")

        resolved = [r for r in link_rows if r[3] is not None]
        check(any(r[2] == "nrf/spm#spm-api" for r in resolved),
              "internal xref resolved to a concrete neighbour section id")
        check(len(embed_texts) == len(sections), "one embed text per section")
        print("  PASS\n")


def test_ingest_rst_unchanged() -> None:
    """Guard the main() refactor: the RST path still chunks + resolves xrefs."""
    print("ingest_rst (refactor regression):")
    import build_index

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "nrf").mkdir()
        (root / "nrf" / "a.rst").write_text(
            ".. _topic-a:\n\nTopic A\n=======\n\nSee :ref:`topic-b`.\n", encoding="utf-8")
        (root / "nrf" / "b.rst").write_text(
            ".. _topic-b:\n\nTopic B\n=======\n\nBody of B.\n", encoding="utf-8")

        sections, link_rows, embed_texts = build_index.ingest_rst(root)
        check(len(sections) == 2, "two RST sections chunked")
        check(len(embed_texts) == len(sections), "one embed text per section")
        resolved = [r for r in link_rows if r[3] is not None]
        check(any(r[1] == "ref" and r[2] == "topic-b" for r in resolved),
              ":ref: edge still resolves to the target section")
        print("  PASS\n")


if __name__ == "__main__":
    test_chunk_html_file()
    test_ingest_html()
    test_ingest_rst_unchanged()
    print("ALL CHECKS PASSED")
