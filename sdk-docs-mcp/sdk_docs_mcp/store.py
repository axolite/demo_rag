"""Shared persistence helpers for the docs index ``.sqlite`` files.

Single source of truth for the ``meta`` table and for opening an index as a
read-only corpus. Reused by both ``build_index.py`` (write side) and
``server.py`` (read side) so the SQL lives in exactly one place.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import sqlite_vec


def read_meta(db: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    """Return ``meta[key]`` as text, or ``default`` if the key is absent."""
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def write_meta(db: sqlite3.Connection, mapping: Mapping[str, str]) -> None:
    """Upsert every ``key -> value`` pair into the ``meta`` table."""
    db.executemany(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        list(mapping.items()),
    )


@dataclass
class Corpus:
    """An opened, read-only docs index plus its resolved docs root."""

    db: sqlite3.Connection
    docs_root: Path
    embed_model: str | None
    # v2: whether ``sections`` carries the ``source_kind`` column, and the index's
    # declared ``source_format`` (rst | html | source). v1 indexes lack the column;
    # the server then derives a single default kind from ``source_format``.
    has_source_kind: bool = False
    source_format: str | None = None


def open_corpus(index_path: Path) -> Corpus:
    """Open ``index_path`` read-only, load sqlite-vec, and resolve the docs root.

    The docs root is reconstructed from ``meta.docs_root_relative`` (stored
    relative to the index file at build time, or absolute for the external west
    clone), so a clone works from anywhere. Raises if that meta key is missing —
    there is no SDK-specific fallback. A ``PRAGMA table_info`` probe keeps v1
    indexes (no ``source_kind`` column) openable.
    """
    db = sqlite3.connect(
        f"file:{index_path.as_posix()}?mode=ro", uri=True, check_same_thread=False
    )
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)

    rel = read_meta(db, "docs_root_relative")
    if rel is None:
        raise ValueError(
            f"{index_path} has no meta.docs_root_relative — rebuild it with build_index.py"
        )
    docs_root = (index_path.parent / rel).resolve()
    cols = {row[1] for row in db.execute("PRAGMA table_info(sections)").fetchall()}
    return Corpus(
        db=db,
        docs_root=docs_root,
        embed_model=read_meta(db, "embed_model"),
        has_source_kind="source_kind" in cols,
        source_format=read_meta(db, "source_format"),
    )
