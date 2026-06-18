"""Thin wrapper around fastembed, with the embedding model pinned.

jina-embeddings-v2-base-code needs no query/document prefixes, so the same
representation is used at build time (documents) and query time.

Two safeguards keep CPU embedding tractable and bounded in memory:

* **Length cap** — the embedding input is truncated to ``MAX_EMBED_CHARS``.
  Self-attention is O(seq²); a handful of giant no-blank-line tables would
  otherwise tokenize to the model's 8192-token max and, padded across a batch,
  demand >100 GB. The full text is still BM25-indexed verbatim, so exact-symbol
  recall is unaffected — only the vector sees a (generous) prefix.
* **Length-sorted batches** — fastembed pads each batch to its longest member,
  so embedding in length order keeps short docs in cheap batches and confines
  long sequences to a few small ones. Output order is restored before return.
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np

from . import EMBED_DIM, EMBED_MODEL

# ~1000 tokens of prose / ~1300 of code: bounds attention to a safe ceiling.
MAX_EMBED_CHARS = 4000
DEFAULT_BATCH = 16


class Embedder:
    def __init__(self, model_name: str = EMBED_MODEL, threads: int | None = None):
        # Imported lazily so the module is importable without warming the heavy
        # ONNX runtime (e.g. for unit tests of the chunker).
        from fastembed import TextEmbedding

        self.model_name = model_name
        self._model = TextEmbedding(model_name=model_name, threads=threads)

    @property
    def dim(self) -> int:
        return EMBED_DIM

    def embed_documents(self, texts: list[str], batch_size: int = DEFAULT_BATCH) -> list[np.ndarray]:
        """Return one float32 vector per input text, in the original order."""
        capped = [t[:MAX_EMBED_CHARS] for t in texts]
        order = sorted(range(len(capped)), key=lambda i: len(capped[i]))
        out: list[np.ndarray | None] = [None] * len(capped)
        sorted_texts = [capped[i] for i in order]
        for pos, vec in enumerate(self._model.embed(sorted_texts, batch_size=batch_size)):
            out[order[pos]] = np.asarray(vec, dtype=np.float32)
        return [v for v in out if v is not None]

    def embed_query(self, text: str) -> np.ndarray:
        vec = next(iter(self._model.query_embed(text[:MAX_EMBED_CHARS])))
        return np.asarray(vec, dtype=np.float32)


def to_blob(vec: Iterable[float]) -> bytes:
    """Serialize a vector to the little-endian float32 blob sqlite-vec expects."""
    return np.asarray(list(vec), dtype="<f4").tobytes()
