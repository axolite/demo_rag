"""HTML section splitter and xref extractor for *resolved* Sphinx output.

The RST chunker (``chunker.py``) sees only the frozen source snapshot, where
~18% of the API reference is doxygen *stub* pages — the real signatures, struct
fields, and parameters are injected by Sphinx+breathe at build time. This module
ingests that **built** HTML instead, so the embedded/searchable text carries the
real API surface.

It mirrors ``chunker.py``'s model so the rest of the pipeline is unchanged: it
emits the same :class:`~sdk_docs_mcp.chunker.Section` dataclass, one per Sphinx
*section node*, with a ``breadcrumb`` of nested-section titles and an ``anchor``
for citation. Two HTML-specific facts are exploited:

* **breathe API blocks** (``<dl class="c function">`` / ``cpp …`` with
  ``<dt id="c.NAME">``) are kept in the section text *and* their ``dt[id]`` is
  recorded as an extra anchor, so an API symbol becomes an xref target.
* **internal references** (``<a class="reference internal" href="page.html#id">``)
  are resolved to a concrete ``docname#fragment`` target — these point at *real*
  destinations, a graph the RST index can only approximate.

Section ``repo``/``file_path`` are left as rendered-page defaults here; the
citation-to-source mapping (docname → snapshot ``.rst``) is applied by
``build_index.ingest_html`` where the snapshot path index lives.
"""

from __future__ import annotations

import posixpath
import re

from bs4 import BeautifulSoup
from bs4.element import Tag

from .chunker import Section

# A Sphinx "section" is a ``<section id=…>`` (docutils >=0.17) or, on the
# docutils <0.17 that NCS 1.6.1 ships, a ``<div class="section" id=…>``.
_SECTION_CLASS = "section"


def _is_section(node: object) -> bool:
    if not isinstance(node, Tag):
        return False
    if node.name == "section":
        return True
    return node.name == "div" and _SECTION_CLASS in (node.get("class") or [])


def _header_text(section: Tag) -> str:
    """Title text of a section node: its first direct ``h1..h6``, sans ¶ link."""
    for child in section.children:
        if isinstance(child, Tag) and child.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            return child.get_text(" ", strip=True)
    return ""


def _own_nodes(section: Tag) -> list[Tag]:
    """Direct children of ``section`` that are *not* themselves nested sections.

    Each section's text is its own content only; nested sections become their
    own :class:`Section`, exactly as the RST splitter ends a parent body where
    the next child section begins (so text is never double-counted)."""
    return [c for c in section.children if isinstance(c, Tag) and not _is_section(c)]


def _normalize(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _resolve_href(href: str, page_docname: str) -> str | None:
    """Turn an ``<a href>`` into a canonical ``docname#frag`` (or ``docname``).

    Returns ``None`` for external links and non-page assets. Relative ``.html``
    paths are normalized against the current page's directory, so cross-docset
    links (``../zephyr/foo.html#bar``) resolve too."""
    if href.startswith(("http://", "https://", "mailto:", "ftp:", "//")):
        return None
    frag = ""
    if "#" in href:
        href, frag = href.split("#", 1)
    if href == "":
        target_doc = page_docname  # same-page anchor
    else:
        if not href.endswith(".html"):
            return None  # _static asset, objects.inv, download, …
        base = posixpath.dirname(page_docname)
        target_doc = posixpath.normpath(posixpath.join(base, href[:-len(".html")]))
    return f"{target_doc}#{frag}" if frag else target_doc


def chunk_html_file(html: str, docset: str, page_docname: str) -> list[Section]:
    """Split one built HTML page into sections.

    ``page_docname`` is the page path relative to the HTML root without the
    ``.html`` suffix (e.g. ``nrf/security/secure_services``); ``docset`` is its
    first component. Each returned :class:`Section` carries two transient
    attributes consumed by ``build_index.ingest_html``:

    * ``raw_links`` — list of canonical ``docname#frag`` link targets.
    * ``docname``   — ``page_docname`` (the xref-resolution namespace)."""
    soup = BeautifulSoup(html, "lxml")

    # Drop chrome that would pollute text/anchors: nav, scripts, and the ¶
    # permalink anchors Sphinx appends inside every heading.
    for tag in soup.select("a.headerlink, script, style"):
        tag.decompose()

    # The RTD/NCS theme wraps article content in ``<div role="main">``; fall
    # back progressively so a bare page still yields something.
    root = soup.find(attrs={"role": "main"}) or soup.body or soup

    sections: list[Section] = []

    def emit(node: Tag, crumbs: list[str]) -> None:
        anchor = node.get("id") or ""
        header = _header_text(node)
        breadcrumb = " > ".join([*crumbs, header] if header else crumbs)
        own = _own_nodes(node)

        text = _normalize("\n".join(n.get_text("\n", strip=False) for n in own))

        # API symbols (breathe/c/cpp domain) + the section id are all anchors.
        all_anchors = [anchor] if anchor else []
        raw_links: list[str] = []
        for n in own:
            for dt in n.find_all("dt", id=True):
                all_anchors.append(dt["id"])
            for a in n.find_all("a", class_="reference"):
                href = a.get("href")
                if not href:
                    continue
                target = _resolve_href(href, page_docname)
                if target:
                    raw_links.append(target)

        sec = Section(
            repo=docset,
            file_path=f"{page_docname}.html",  # rendered-page default; remapped later
            anchor=anchor,
            breadcrumb=breadcrumb,
            header=header,
            line_start=0,
            line_end=0,
            text=text,
            all_anchors=list(dict.fromkeys(all_anchors)),
        )
        sec.raw_links = list(dict.fromkeys(raw_links))  # type: ignore[attr-defined]
        sec.docname = page_docname  # type: ignore[attr-defined]
        # Skip empty shells (e.g. a wrapper section that only holds children).
        if text or sec.all_anchors:
            sections.append(sec)

        next_crumbs = [*crumbs, header] if header else crumbs
        for child in node.children:
            if _is_section(child):
                emit(child, next_crumbs)

    top = [c for c in root.children if _is_section(c)]
    if top:
        for node in top:
            emit(node, [])
    else:
        # No section divs (a minimal page): treat the whole article as one.
        text = _normalize(root.get_text("\n", strip=False))
        if text:
            sec = Section(
                repo=docset,
                file_path=f"{page_docname}.html",
                anchor="",
                breadcrumb=_first_heading(soup),
                header=_first_heading(soup),
                line_start=0,
                line_end=0,
                text=text,
                all_anchors=[],
            )
            sec.raw_links = []  # type: ignore[attr-defined]
            sec.docname = page_docname  # type: ignore[attr-defined]
            sections.append(sec)

    return sections


def _first_heading(soup: BeautifulSoup) -> str:
    h = soup.find(["h1", "h2", "h3"])
    return h.get_text(" ", strip=True) if h else ""
