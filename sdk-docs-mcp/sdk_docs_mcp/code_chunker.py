"""Source-code section splitter for the unified NCS *source-truth* index.

The sibling chunkers see documentation prose; this one sees the **code** the
docs are drawn from — C/C++, Kconfig, devicetree, CMake, yaml, linker, asm —
taken from the same commit-exact ``west`` clone. It emits the identical
:class:`~sdk_docs_mcp.chunker.Section` contract so the rest of the pipeline
(embed -> write -> FTS -> vec -> RRF) is unchanged; orchestration lives in
``build_index.ingest_code``.

The design is deliberately **symbol-aware but heuristic**:

* **C/C++** (``_split_c``) — a pure-regex *brace matcher* that walks the file at
  brace-depth 0 (skipping strings, char literals, and ``//`` / ``/* */``
  comments) and emits one chunk per top-level construct — function, struct /
  union / enum, typedef, top-level ``#define``, and the ``*_DEFINE(...)`` macro
  family — with the **symbol name as a searchable ``anchor``**. Inter-symbol
  globals and ``#include``s coalesce into preamble chunks. No tree-sitter: macro-
  dense kernel code needs heuristics either way, FTS already gives exact-symbol
  recall, and this keeps the stack pure-Python (no native Windows wheels). A
  per-file ``try/except`` in the orchestrator falls back to a line window, so a
  parser miss never aborts a ~7k-file build.
* **Kconfig** (``_split_kconfig``) — one chunk per ``config`` / ``menuconfig``
  entry, ``anchor=CONFIG_<NAME>`` (the highest-value code chunk for an SDK
  assistant; the ``_`` FTS tokenizer keeps ``CONFIG_BT_EXT_ADV`` atomic).
* **everything else** (``_split_window``) — overlapping line windows; for ``.dts``
  a cheap ``label: node {`` header is recovered when present.

``clean_code_for_embedding`` strips the SPDX/license header and collapses blank
runs but keeps identifiers / ``CONFIG_*`` / code verbatim — it must *not* run the
RST role-stripping in ``chunker.clean_for_embedding``.
"""

from __future__ import annotations

import bisect
import os
import re
from pathlib import Path

from .chunker import Section, est_tokens

# --------------------------------------------------------------------------- #
# Scope + discovery
# --------------------------------------------------------------------------- #

# (clone-relative dir, repo label). The label is decoupled from the path's first
# component so nested repos get clean names (``bootloader/mcuboot`` -> mcuboot,
# ``modules/hal/nordic`` -> hal_nordic). Positive-listed: only these trees are
# walked, so the big third-party forks under modules/lib are never visited.
CODE_SCOPE_DIRS: list[tuple[str, str]] = [
    ("zephyr", "zephyr"),
    ("nrf", "nrf"),
    ("nrfxlib", "nrfxlib"),
    ("bootloader/mcuboot", "mcuboot"),
    ("modules/hal/nordic", "hal_nordic"),
    ("modules/hal/cmsis", "cmsis"),
    ("modules/hal/libmetal", "libmetal"),
]

# Pruned mid-walk. The fork excludes are defensive — they're outside the
# positive scope above, but guard against a caller widening it.
SKIP_CODE_DIR_PARTS = {
    ".git", "build", "_build", "_doxygen", "__pycache__", ".github",
    # defensive third-party-fork excludes (irrelevant to Nordic targets):
    "matter", "openthread", "loramac-node", "civetweb", "mbedtls", "st",
}

# Binaries / images / archives never carry searchable source (covers nrfxlib's
# ~234 prebuilt ``.a`` blobs).
SKIP_CODE_SUFFIXES = {
    ".a", ".o", ".obj", ".lib", ".so", ".dll", ".dylib", ".bin", ".hex", ".elf",
    ".img", ".out", ".map", ".d", ".pyc", ".pyd", ".exe",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp", ".pdf",
    ".zip", ".gz", ".tar", ".tgz", ".bz2", ".xz", ".7z", ".jar",
    ".ttf", ".woff", ".woff2", ".eot",
}

_C_SUFFIXES = {".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".hxx", ".cppm"}
_WINDOW_SUFFIXES = {
    ".dts", ".dtsi", ".overlay", ".cmake", ".yaml", ".yml", ".ld", ".s",
    ".S", ".asm", ".conf",
}
_WINDOW_NAMES = {"cmakelists.txt"}

# Guards against giant generated files (e.g. devicetree_generated.h) that would
# blow up chunking + embedding for no search value.
_MAX_FILE_BYTES = 1_500_000
_NUL_PROBE_BYTES = 8192


def _lang_for(name: str, suffix: str) -> str | None:
    """Map a filename to a chunker lane, or None to skip the file."""
    lower = name.lower()
    if lower.startswith("kconfig"):
        return "kconfig"
    if suffix in _C_SUFFIXES:
        return "c"
    if suffix in _WINDOW_SUFFIXES or lower in _WINDOW_NAMES:
        return "window"
    return None


def discover_code_files(
    code_root: Path, scope: list[tuple[str, str]], include_tests: bool = True
) -> list[tuple[Path, str, str, str]]:
    """Return ``(abs_path, repo_label, posix_rel, lang)`` for every code file.

    ``posix_rel`` is relative to ``code_root`` (the west clone), so ``get_doc``
    resolves it straight into the live checkout. Applies the dir/suffix excludes
    and the size + NUL-byte guards. ``include_tests=False`` drops ``*/tests/*``.
    """
    out: list[tuple[Path, str, str, str]] = []
    for sub, label in scope:
        base = code_root / sub
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_CODE_DIR_PARTS]
            if not include_tests:
                dirnames[:] = [d for d in dirnames if d != "tests"]
            for fn in sorted(filenames):
                suffix = Path(fn).suffix.lower()
                if suffix in SKIP_CODE_SUFFIXES:
                    continue
                lang = _lang_for(fn, suffix)
                if lang is None:
                    continue
                abs_path = Path(dirpath) / fn
                try:
                    if abs_path.stat().st_size > _MAX_FILE_BYTES:
                        continue
                    with open(abs_path, "rb") as fh:
                        if b"\x00" in fh.read(_NUL_PROBE_BYTES):
                            continue
                except OSError:
                    continue
                rel = abs_path.relative_to(code_root).as_posix()
                out.append((abs_path, label, rel, lang))
    return out


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

# Code packs more tokens per char than prose; cap sub-chunks so each one embeds
# whole (the embedder truncates at MAX_EMBED_CHARS=4000 ~ 1000 tokens).
_SUBSPLIT_TOKENS = 1000
_SUBSPLIT_TARGET_TOKENS = 800
_WINDOW_LINES = 120
_WINDOW_OVERLAP = 15


def _line_index(text: str) -> list[int]:
    """Offsets where each line starts (``[0]`` first); for char->line lookup."""
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _line_of(starts: list[int], pos: int) -> int:
    """1-based line number containing char offset ``pos``."""
    return bisect.bisect_right(starts, pos)


def _first_meaningful(lines: list[str]) -> str:
    for ln in lines:
        s = ln.strip()
        if s and not s.startswith(("//", "/*", "*", "#")):
            return s[:120]
    for ln in lines:
        if ln.strip():
            return ln.strip()[:120]
    return ""


def _subsplit(text: str, start_line: int) -> list[tuple[str, int, int]]:
    """Split an oversized body into ~target-token line windows.

    Returns ``(sub_text, line_start, line_end)``. Small bodies pass through as a
    single chunk. The full text is still FTS-indexed verbatim across the parts;
    this only bounds the *vector* input so a 50 KB function isn't silently
    embedded as its first 4 KB."""
    if est_tokens(text) <= _SUBSPLIT_TOKENS:
        return [(text, start_line, start_line + text.count("\n"))]
    lines = text.split("\n")
    out: list[tuple[str, int, int]] = []
    cur: list[str] = []
    cur_start = start_line
    cur_tokens = 0
    for off, ln in enumerate(lines):
        ltoks = est_tokens(ln) + 1
        if cur and cur_tokens + ltoks > _SUBSPLIT_TARGET_TOKENS:
            out.append(("\n".join(cur), cur_start, start_line + off - 1))
            cur, cur_tokens = [], 0
            cur_start = start_line + off
        cur.append(ln)
        cur_tokens += ltoks
    if cur:
        out.append(("\n".join(cur), cur_start, start_line + len(lines) - 1))
    return out


def _emit(
    out: list[Section], repo: str, rel: str, anchor: str, header: str,
    text: str, line_start: int,
) -> None:
    """Append one logical chunk, sub-splitting oversized bodies. Drops empties."""
    body = text.strip("\n")
    if not body.strip():
        return
    crumb_sym = anchor or header
    breadcrumb = f"{repo} > {rel} > {crumb_sym}" if crumb_sym else f"{repo} > {rel}"
    for sub_text, ls, le in _subsplit(body, line_start):
        if not sub_text.strip():
            continue
        out.append(Section(
            repo=repo, file_path=rel, anchor=anchor, breadcrumb=breadcrumb,
            header=header, line_start=ls, line_end=le, text=sub_text,
            all_anchors=[anchor] if anchor else [],
        ))


# --------------------------------------------------------------------------- #
# C / C++  (regex brace matcher)
# --------------------------------------------------------------------------- #

# Top-level unit classification. These run on the *signature* (text up to the
# first ``{``), whitespace-collapsed.
_DEFINE_RE = re.compile(r"#\s*define\s+([A-Za-z_]\w*)")
_TYPEDEF_FNPTR_RE = re.compile(r"\(\s*\*\s*([A-Za-z_]\w*)\s*\)")
_TYPEDEF_TAIL_RE = re.compile(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*;\s*$")
_TAG_RE = re.compile(r"\b(?:struct|union|enum|class)\s+([A-Za-z_]\w*)")
_TAG_TYPEDEF_TAIL_RE = re.compile(r"}\s*([A-Za-z_]\w*)\s*;")
_FUNC_RE = re.compile(r"([A-Za-z_]\w*)\s*\([^;{}]*\)\s*$")
_MACRO_CALL_RE = re.compile(r"^([A-Z][A-Z0-9_]*)\s*\(")
_FIRST_ARG_RE = re.compile(r"\(\s*([A-Za-z_]\w*)")
# Control keywords that look like a function head but aren't.
_CONTROL_KW = {"if", "for", "while", "switch", "return", "sizeof", "do", "else",
               "case", "defined", "static_assert", "_Static_assert"}


def _scan_top_level_units(text: str) -> list[tuple[int, int]]:
    """Char spans of top-level units, comment/string/preprocessor aware.

    A *unit* is a preprocessor directive (one logical line, continuations
    joined), a brace-delimited definition (with any trailing ``} Name;`` pulled
    in), or a ``;``-terminated statement at brace-depth 0. Whitespace- and
    comment-only gaps are not emitted — they attach to the following construct
    (doc comments) or to a preamble chunk."""
    n = len(text)
    i = 0
    depth = 0
    line_start = True
    unit_start: int | None = None
    units: list[tuple[int, int]] = []

    while i < n:
        c = text[i]
        # line comment
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        # block comment
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            line_start = False
            continue
        if c == "\n":
            line_start = True
            i += 1
            continue
        if c in " \t\r":
            i += 1
            continue
        # preprocessor directive (only at logical line start, depth 0)
        if c == "#" and line_start and depth == 0:
            j = i
            while j < n:
                if text[j] == "\\" and j + 1 < n and text[j + 1] == "\n":
                    j += 2
                    continue
                if text[j] == "\n":
                    break
                j += 1
            units.append((i, j))
            i = j
            unit_start = None
            line_start = True
            continue

        line_start = False
        if unit_start is None:
            unit_start = i

        # string / char literal
        if c == '"' or c == "'":
            q = c
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == q or text[j] == "\n":
                    j += 1 if text[j] == q else 0
                    break
                j += 1
            i = max(j, i + 1)
            continue
        if c == "{":
            depth += 1
            i += 1
            continue
        if c == "}":
            if depth > 0:
                depth -= 1
            i += 1
            if depth == 0:
                # pull in a trailing ``Name, Name2;`` (typedef struct {...} N;)
                k = i
                while k < n and text[k] in " \t\r\n":
                    k += 1
                m = re.match(r"[A-Za-z_][\w\s,\*\[\]]*;", text[k:])
                if m:
                    i = k + m.end()
                elif k < n and text[k] == ";":
                    i = k + 1
                units.append((unit_start, i))
                unit_start = None
            continue
        if c == ";":
            i += 1
            if depth == 0:
                units.append((unit_start, i))
                unit_start = None
            continue
        i += 1

    if unit_start is not None and text[unit_start:].strip():
        units.append((unit_start, n))
    return units


def _attached_comment_start(gap: str) -> int | None:
    """Offset within ``gap`` where a doc comment immediately preceding the next
    construct begins, or None. ``gap`` is the inter-unit text (only whitespace +
    comments)."""
    stripped = gap.rstrip()
    if not stripped:
        return None
    if stripped.endswith("*/"):
        start = stripped.rfind("/*")
        return start if start != -1 else None
    lines = gap.splitlines(keepends=True)
    j = len(lines) - 1
    while j >= 0 and lines[j].strip() == "":
        j -= 1
    k = j
    while k >= 0 and lines[k].lstrip().startswith("//"):
        k -= 1
    if k < j:  # at least one trailing // line
        return sum(len(lines[t]) for t in range(k + 1))
    return None


def _classify(seg: str, has_brace: bool) -> tuple[str, str, str]:
    """Return ``(kind, anchor, header)`` for one top-level unit.

    ``kind`` in {define, typedef, tag, function, macro, preamble}. ``header`` is a
    short signature/label for display."""
    head = seg.split("{", 1)[0]
    head_norm = re.sub(r"\s+", " ", head).strip()

    m = _DEFINE_RE.match(seg.lstrip())
    if m:
        return "define", m.group(1), head_norm[:160]
    if seg.lstrip().startswith("#"):
        return "preamble", "", ""

    first = head_norm.split("(", 1)[0].split()
    first_kw = first[0] if first else ""

    if first_kw == "typedef":
        if has_brace and re.search(r"\b(struct|union|enum)\b", head_norm):
            tm = _TAG_TYPEDEF_TAIL_RE.search(seg) or _TAG_RE.search(head_norm)
            return "tag", (tm.group(1) if tm else ""), head_norm[:160]
        fm = _TYPEDEF_FNPTR_RE.search(seg)
        if fm:
            return "typedef", fm.group(1), head_norm[:160]
        tm = _TYPEDEF_TAIL_RE.search(seg)
        return "typedef", (tm.group(1) if tm else ""), head_norm[:160]

    if has_brace and first_kw in ("struct", "union", "enum", "class"):
        tm = _TAG_RE.search(head_norm)
        anchor = tm.group(1) if tm else ""
        if not anchor:
            tt = _TAG_TYPEDEF_TAIL_RE.search(seg)
            anchor = tt.group(1) if tt else ""
        return "tag", anchor, head_norm[:160]

    if has_brace:
        fm = _FUNC_RE.search(head_norm)
        if fm and fm.group(1) not in _CONTROL_KW:
            return "function", fm.group(1), head_norm[:200]

    mc = _MACRO_CALL_RE.match(head_norm)
    if mc:
        macro = mc.group(1)
        am = _FIRST_ARG_RE.search(seg)
        arg = am.group(1) if am else ""
        anchor = arg if (arg and not arg.isupper()) else macro
        return "macro", anchor, head_norm[:160]

    return "preamble", "", ""


def _split_c(text: str, repo: str, rel: str) -> list[Section]:
    starts = _line_index(text)
    units = _scan_top_level_units(text)
    out: list[Section] = []

    pre_start: int | None = None      # open preamble run [char span]
    pre_end = 0
    prev_end = 0

    def flush_preamble() -> None:
        nonlocal pre_start, pre_end
        if pre_start is not None and text[pre_start:pre_end].strip():
            _emit(out, repo, rel, "", "", text[pre_start:pre_end],
                  _line_of(starts, pre_start))
        pre_start = None

    for s, e in units:
        seg = text[s:e]
        kind, anchor, header = _classify(seg, "{" in seg)
        if kind == "preamble":
            if pre_start is None:
                pre_start = s
            pre_end = e
            prev_end = e
            continue
        # named construct: attach a preceding doc comment, then flush preamble
        chunk_start = s
        gap = text[prev_end:s]
        cstart = _attached_comment_start(gap)
        if cstart is not None:
            doc_start = prev_end + cstart
            if pre_start is not None and doc_start > pre_start:
                pre_end = doc_start  # preamble keeps text before the doc comment
            chunk_start = doc_start
        flush_preamble()
        _emit(out, repo, rel, anchor, header, text[chunk_start:e],
              _line_of(starts, chunk_start))
        prev_end = e
    flush_preamble()
    return out


# --------------------------------------------------------------------------- #
# Kconfig
# --------------------------------------------------------------------------- #

_KCONFIG_ENTRY_RE = re.compile(r"^\s*(menuconfig|config)\s+([A-Za-z0-9_]+)")
# A new entry/section boundary that ends the current config's body.
_KCONFIG_BREAK_RE = re.compile(
    r"^\s*(menuconfig|config|menu|endmenu|choice|endchoice|if|endif|source|rsource|osource)\b")
_KCONFIG_PROMPT_RE = re.compile(
    r'^\s*(?:bool|tristate|string|int|hex|prompt)\b[^"\n]*"([^"]*)"', re.MULTILINE)


def _split_kconfig(text: str, repo: str, rel: str) -> list[Section]:
    lines = text.splitlines()
    n = len(lines)
    out: list[Section] = []
    gap_start = 0  # line index of accumulating non-config text
    i = 0
    while i < n:
        m = _KCONFIG_ENTRY_RE.match(lines[i])
        if not m:
            i += 1
            continue
        # flush preceding non-config text (menus, comments) as windows
        if i > gap_start:
            out.extend(_window_sections(lines[gap_start:i], gap_start + 1, repo, rel))
        name = m.group(2)
        j = i + 1
        while j < n and not _KCONFIG_BREAK_RE.match(lines[j]):
            j += 1
        body = "\n".join(lines[i:j])
        pm = _KCONFIG_PROMPT_RE.search(body)
        header = pm.group(1) if pm else name
        _emit(out, repo, rel, f"CONFIG_{name}", header, body, i + 1)
        i = j
        gap_start = j
    if n > gap_start:
        out.extend(_window_sections(lines[gap_start:n], gap_start + 1, repo, rel))
    return out


# --------------------------------------------------------------------------- #
# Line-window fallback (dts / cmake / yaml / ld / asm, and parser fallback)
# --------------------------------------------------------------------------- #

_DTS_NODE_RE = re.compile(r"^\s*([\w-]+\s*:\s*)?[\w@,-]+\s*\{")


def _window_sections(
    lines: list[str], start_line: int, repo: str, rel: str
) -> list[Section]:
    """Overlapping fixed-size windows over ``lines`` (1-based ``start_line``)."""
    out: list[Section] = []
    n = len(lines)
    if n == 0 or not any(ln.strip() for ln in lines):
        return out
    step = _WINDOW_LINES - _WINDOW_OVERLAP
    for off in range(0, n, step):
        chunk = lines[off:off + _WINDOW_LINES]
        if not any(ln.strip() for ln in chunk):
            continue
        header = ""
        for ln in chunk:
            if _DTS_NODE_RE.match(ln):
                header = ln.strip()[:120]
                break
        if not header:
            header = _first_meaningful(chunk)
        ls = start_line + off
        _emit(out, repo, rel, "", header, "\n".join(chunk), ls)
        if off + _WINDOW_LINES >= n:
            break
    return out


def _split_window(text: str, repo: str, rel: str) -> list[Section]:
    return _window_sections(text.splitlines(), 1, repo, rel)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def chunk_code_file(text: str, repo: str, rel: str, lang: str) -> list[Section]:
    """Split one source file into :class:`Section`s by language lane.

    Raising is allowed: the orchestrator wraps this per-file and falls back to a
    line window, so a chunker bug never aborts the build."""
    if lang == "c":
        return _split_c(text, repo, rel)
    if lang == "kconfig":
        return _split_kconfig(text, repo, rel)
    return _split_window(text, repo, rel)


def chunk_code_file_safe(text: str, repo: str, rel: str, lang: str) -> tuple[list[Section], bool]:
    """``chunk_code_file`` with the per-file safety net.

    Returns ``(sections, fell_back)``. On any parser exception (or an empty
    symbol result for a non-empty C file) it falls back to a line window so the
    file's text stays searchable."""
    try:
        secs = chunk_code_file(text, repo, rel, lang)
    except Exception:
        return _split_window(text, repo, rel), True
    if not secs and text.strip():
        return _split_window(text, repo, rel), True
    return secs, False


def chunk_file_whole(text: str, repo: str, rel: str) -> list[Section]:
    """One chunk per file (``--code-granularity file``), oversize sub-split only.

    A fast first-pass lever: skips symbol parsing entirely, trading symbol-precise
    anchors for ~3–5× fewer chunks."""
    lines = text.splitlines()
    out: list[Section] = []
    _emit(out, repo, rel, "", _first_meaningful(lines), text, 1)
    return out


def clean_code_for_embedding(text: str) -> str:
    """Reduce source to embedder input: drop the SPDX/license header, collapse
    blank runs. Identifiers / ``CONFIG_*`` / code are preserved verbatim."""
    m = re.match(r"\s*(/\*.*?\*/|(?:[ \t]*//.*\n)+)", text, re.DOTALL)
    if m and re.search(r"SPDX|Copyright|Licensed under", m.group(), re.IGNORECASE):
        text = text[m.end():]
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
