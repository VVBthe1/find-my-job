from __future__ import annotations

import hashlib
import math
from typing import List

from langchain_core.embeddings import Embeddings


class HashEmbeddings(Embeddings):
    """Deterministic local embeddings for dry-run / tests (no API key)."""

    def __init__(self, dim: int = 64) -> None:
        self.dim = dim

    def _embed(self, text: str) -> List[float]:
        vec = [0.0] * self.dim
        tokens = text.lower().split()
        if not tokens:
            tokens = [""]
        for tok in tokens:
            digest = hashlib.sha256(tok.encode()).digest()
            for i in range(self.dim):
                vec[i] += (digest[i % len(digest)] - 128) / 128.0
        # L2 normalize
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> List[float]:
        return self._embed(text)
