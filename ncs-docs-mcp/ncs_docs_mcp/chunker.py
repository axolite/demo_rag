"""RST/Markdown section splitter and Sphinx cross-reference extractor.

The corpus is a frozen Sphinx documentation set. Every reStructuredText file
follows the same conventions we exploit here:

* ``.. _label:``            -> a global, stable citation anchor
* underline / overline      -> section headers whose *level* is defined by the
                               order adornment styles first appear in the file
                               (standard RST semantics, not a fixed ``#>*>=``)
* ``:ref:`` / ``:doc:`` /    -> cross-reference edges forming the doc graph
  ``:option:`` / ``:file:``

We chunk by section so every result carries a precise ``file#anchor`` +
line-range citation, and keep whole sections intact (jina-v2-code embeds up to
8192 tokens) unless a section is large enough to warrant a paragraph sub-split.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# RST adornment characters that may underline/overline a section title.
_ADORNMENT = set("""!"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~""")

# ``.. _some_label:`` (an explicit hyperlink target / Sphinx label).
_ANCHOR_RE = re.compile(r"^\.\.\s+_([\w.+-]+):\s*$")

# Layout-only directives whose marker line carries no conceptual content.
_LAYOUT_DIRECTIVE_RE = re.compile(
    r"^\s*\.\.\s+(contents|toctree|figure|image|include|highlight|"
    r"raw|tabularcolumns|sectionauthor|_static)::",
    re.IGNORECASE,
)
_DIRECTIVE_RE = re.compile(r"^\s*\.\.\s+[\w-]+::")
_OPTION_LINE_RE = re.compile(r"^\s+:[\w-]+:")

# Sphinx roles we treat as graph edges. ``:role:`Title <target>``` or ``:role:`target```.
_ROLE_RE = re.compile(r":(ref|doc|option|file):`([^`]+)`")
# Any inline role, for cleaning embedding text down to its display words.
_ANY_ROLE_RE = re.compile(r":[\w:+-]+:`([^`]+)`")

# Rough token estimate (chars/4) — only used as a sub-split guard, not for truncation.
_CHARS_PER_TOKEN = 4
_SUBSPLIT_TOKENS = 1500          # split sections larger than this
_SUBSPLIT_TARGET_TOKENS = 1200   # aim for sub-chunks around this size


@dataclass
class Section:
    """One indexed chunk: a section (or a paragraph-group sub-chunk of one)."""

    repo: str
    file_path: str            # POSIX, relative to the docs root
    anchor: str               # primary citation anchor ("" if none)
    breadcrumb: str           # "Title > Sub > Subsub" (includes own header)
    header: str
    line_start: int           # 1-based, inclusive
    line_end: int             # 1-based, inclusive
    text: str                 # raw section text (FTS-indexed verbatim)
    all_anchors: list[str] = field(default_factory=list)


@dataclass
class Link:
    kind: str                 # ref | doc | option | file
    target: str               # normalized target (label / path / symbol)


def est_tokens(s: str) -> int:
    return max(1, len(s) // _CHARS_PER_TOKEN)


# --------------------------------------------------------------------------- #
# Header detection
# --------------------------------------------------------------------------- #


def _is_adornment(line: str) -> bool:
    s = line.rstrip("\n")
    if len(s) < 3:
        return False
    first = s[0]
    return first in _ADORNMENT and all(c == first for c in s)


def _detect_header(lines: list[str], i: int) -> tuple[str, tuple[str, bool], int] | None:
    """If a section header starts at line ``i``, return (title, style, consumed).

    ``style`` = (adornment_char, has_overline); two styles are distinct iff this
    pair differs, matching how docutils assigns heading levels. ``consumed`` is
    the number of source lines the header occupies (2 for underline-only, 3 for
    overline+underline). Headers must sit at column 0 to avoid matching indented
    code, tables, or option blocks.
    """
    line = lines[i].rstrip("\n")

    # overline + title + underline
    if _is_adornment(line) and i + 2 < len(lines):
        title = lines[i + 1].rstrip("\n")
        under = lines[i + 2].rstrip("\n")
        if (
            title.strip()
            and not title.startswith(" ")
            and _is_adornment(under)
            and under[0] == line[0]
        ):
            return title.strip(), (line[0], True), 3

    # title + underline
    if line.strip() and not line.startswith(" ") and not _is_adornment(line):
        if i + 1 < len(lines):
            under = lines[i + 1].rstrip("\n")
            if _is_adornment(under) and len(under) >= len(line.rstrip()):
                return line.strip(), (under[0], False), 2

    return None


# --------------------------------------------------------------------------- #
# Section splitting
# --------------------------------------------------------------------------- #


@dataclass
class _RawSection:
    title: str
    level: int
    anchors: list[str]
    header_line: int          # 1-based line of the title
    body_start: int           # 1-based first line of the chunk (anchors/header)
    body_end: int = 0         # filled in later


def _split_rst(text: str, repo: str, rel_path: str) -> list[Section]:
    lines = text.splitlines()
    style_levels: dict[tuple[str, bool], int] = {}
    raws: list[_RawSection] = []
    pending_anchors: list[str] = []
    pending_anchor_start: int | None = None

    i = 0
    n = len(lines)
    while i < n:
        m = _ANCHOR_RE.match(lines[i])
        if m:
            if pending_anchor_start is None:
                pending_anchor_start = i + 1
            pending_anchors.append(m.group(1))
            i += 1
            continue

        hdr = _detect_header(lines, i)
        if hdr:
            title, style, consumed = hdr
            if style not in style_levels:
                style_levels[style] = len(style_levels) + 1
            level = style_levels[style]
            header_line = i + 1 + (1 if style[1] else 0)  # title line (1-based)
            body_start = pending_anchor_start if pending_anchor_start else header_line
            raws.append(_RawSection(title, level, pending_anchors, header_line, body_start))
            pending_anchors = []
            pending_anchor_start = None
            i += consumed
            continue

        # A non-anchor, non-header line clears any dangling anchors only if they
        # were not immediately followed by a header (keep them attached to the
        # next section, matching Sphinx, by NOT clearing here).
        i += 1

    if not raws:
        # No headers (e.g. a stub file): treat the whole file as one section.
        return _finalize(
            [_RawSection(Path(rel_path).stem, 1, [], 1, 1)], lines, repo, rel_path
        )

    # Close each section's body at the line before the next section's chunk start.
    for idx, r in enumerate(raws):
        r.body_end = (raws[idx + 1].body_start - 1) if idx + 1 < len(raws) else n

    return _finalize(raws, lines, repo, rel_path, style_levels)


def _finalize(
    raws: list[_RawSection],
    lines: list[str],
    repo: str,
    rel_path: str,
    style_levels: dict | None = None,
) -> list[Section]:
    for r in raws:
        if not r.body_end:
            r.body_end = len(lines)

    # Build breadcrumb paths from the level hierarchy.
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    for r in raws:
        while stack and stack[-1][0] >= r.level:
            stack.pop()
        breadcrumb = " > ".join(h for _, h in stack + [(r.level, r.title)])
        stack.append((r.level, r.title))

        body = "\n".join(lines[r.body_start - 1 : r.body_end]).strip("\n")
        primary_anchor = r.anchors[0] if r.anchors else ""

        for sub in _maybe_subsplit(body, r.body_start):
            sub_text, ls, le = sub
            sections.append(
                Section(
                    repo=repo,
                    file_path=rel_path,
                    anchor=primary_anchor,
                    breadcrumb=breadcrumb,
                    header=r.title,
                    line_start=ls,
                    line_end=le,
                    text=sub_text,
                    all_anchors=list(r.anchors),
                )
            )
    return sections


def _maybe_subsplit(body: str, body_start: int) -> list[tuple[str, int, int]]:
    """Yield (text, line_start, line_end). Splits only oversized sections."""
    if est_tokens(body) <= _SUBSPLIT_TOKENS:
        return [(body, body_start, body_start + body.count("\n"))]

    # Group paragraphs (blank-line separated) up to the target token budget,
    # tracking absolute line numbers so citations stay accurate.
    body_lines = body.split("\n")
    chunks: list[tuple[str, int, int]] = []
    cur: list[str] = []
    cur_start = body_start
    cur_tokens = 0

    def flush(end_line: int) -> None:
        nonlocal cur, cur_start, cur_tokens
        if cur:
            chunks.append(("\n".join(cur).strip("\n"), cur_start, end_line))
        cur = []
        cur_tokens = 0

    abs_line = body_start
    para: list[str] = []
    para_start = body_start
    for ln in body_lines:
        if ln.strip() == "":
            if para:
                ptoks = est_tokens("\n".join(para))
                if cur and cur_tokens + ptoks > _SUBSPLIT_TARGET_TOKENS:
                    flush(para_start - 1)
                    cur_start = para_start
                if not cur:
                    cur_start = para_start
                cur.extend(para)
                cur.append("")
                cur_tokens += ptoks
                para = []
            else:
                if cur:
                    cur.append("")
            abs_line += 1
            continue
        if not para:
            para_start = abs_line
        para.append(ln)
        abs_line += 1
    if para:
        ptoks = est_tokens("\n".join(para))
        if cur and cur_tokens + ptoks > _SUBSPLIT_TARGET_TOKENS:
            flush(para_start - 1)
            cur_start = para_start
        if not cur:
            cur_start = para_start
        cur.extend(para)
    flush(body_start + body.count("\n"))
    return chunks or [(body, body_start, body_start + body.count("\n"))]


# --------------------------------------------------------------------------- #
# Markdown (handful of .md files) — split on ATX headers.
# --------------------------------------------------------------------------- #


def _split_md(text: str, repo: str, rel_path: str) -> list[Section]:
    lines = text.splitlines()
    heads: list[tuple[int, int, str]] = []  # (line_idx0, level, title)
    in_fence = False
    for idx, ln in enumerate(lines):
        if ln.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            heads.append((idx, len(m.group(1)), m.group(2).strip()))
    if not heads:
        return _finalize([_RawSection(Path(rel_path).stem, 1, [], 1, 1)], lines, repo, rel_path)

    raws: list[_RawSection] = []
    for k, (idx0, level, title) in enumerate(heads):
        body_start = idx0 + 1
        body_end = heads[k + 1][0] if k + 1 < len(heads) else len(lines)
        r = _RawSection(title, level, [], body_start, body_start)
        r.body_end = body_end
        raws.append(r)
    return _finalize(raws, lines, repo, rel_path)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def chunk_file(path: Path, repo: str, rel_path: str) -> list[Section]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".md":
        return _split_md(text, repo, rel_path)
    return _split_rst(text, repo, rel_path)


def extract_links(text: str) -> list[Link]:
    """Pull ``:ref:``/``:doc:``/``:option:``/``:file:`` edges out of a section."""
    out: list[Link] = []
    seen: set[tuple[str, str]] = set()
    for kind, raw in _ROLE_RE.findall(text):
        target = raw.strip()
        m = re.search(r"<([^>]+)>", target)  # ``Title <target>`` form
        if m:
            target = m.group(1).strip()
        target = target.lstrip("~/")
        key = (kind, target)
        if target and key not in seen:
            seen.add(key)
            out.append(Link(kind=kind, target=target))
    return out


def clean_for_embedding(text: str) -> str:
    """Reduce a raw section to conceptual text for the embedder.

    Drops layout-only directive markers and their option lines, unwraps Sphinx
    roles to their display words, and strips light emphasis markup — while
    preserving code blocks and ``CONFIG_*`` symbols verbatim.
    """
    kept: list[str] = []
    skip_options = False
    for ln in text.split("\n"):
        if _LAYOUT_DIRECTIVE_RE.match(ln):
            skip_options = True
            continue
        if skip_options and (_OPTION_LINE_RE.match(ln) or ln.strip() == ""):
            continue
        skip_options = False
        if _ANCHOR_RE.match(ln) or _is_adornment(ln):
            continue  # hyperlink targets and header rules carry no prose
        kept.append(ln)
    body = "\n".join(kept)
    body = _ANY_ROLE_RE.sub(
        lambda m: m.group(1).split("<")[0].strip() or m.group(1), body
    )
    body = body.replace("``", "").replace("**", "")
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()
