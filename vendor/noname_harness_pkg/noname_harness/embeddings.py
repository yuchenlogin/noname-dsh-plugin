"""Embeddings for semantic recall: an injectable protocol + a local default.

Semantic recall (vector similarity) complements FTS5 keyword search.  The
embedding function is **injectable**: a real embedding service (OpenAI,
Anthropic, a local model) plugs in behind the same protocol as a plugin, while
the default is a deterministic, network-free local embedding so the pipeline
works and is verifiable with zero external dependency.

The vector index is a *projection*, never the source of truth: it is rebuilt
from append-only events/evidence and can be discarded and recreated at any
time.  FTS5 remains the default recall; vectors are an opt-in enhancement.
"""

from __future__ import annotations

import hashlib
import math
from typing import Callable, Protocol, Sequence

# An embedding function maps text to a fixed-length float vector.
EmbeddingFn = Callable[[str], list[float]]


def cosine_similarity(first: Sequence[float], second: Sequence[float]) -> float:
    """Cosine similarity in pure Python (no numpy dependency)."""

    if len(first) != len(second):
        raise ValueError("embedding dimensions do not match")
    dot = sum(a * b for a, b in zip(first, second))
    norm_a = math.sqrt(sum(a * a for a in first))
    norm_b = math.sqrt(sum(b * b for b in second))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# A deterministic, network-free local embedding.
#
# It hashes token shingles into a fixed number of buckets (a "hashing trick"
# embedding) and L2-normalises.  It is NOT a semantic model -- it captures
# lexical overlap and character-n-gram similarity, which is enough to exercise
# and verify the semantic-recall pipeline deterministically.  A real embedding
# service replaces it behind the same protocol for true semantic similarity.
# ---------------------------------------------------------------------------

DEFAULT_DIMENSIONS = 256


def _token_shingles(text: str) -> list[str]:
    """Word tokens plus character 3-grams, lowercased."""

    lowered = text.lower()
    tokens = lowered.split()
    shingles = set(tokens)
    for token in tokens:
        for index in range(len(token) - 2):
            shingles.add(token[index : index + 3])
    return sorted(shingles)


def local_hash_embedding(text: str, *, dimensions: int = DEFAULT_DIMENSIONS) -> list[float]:
    """A deterministic local embedding for verifying the recall pipeline.

    This is a LEXICAL-OVERLAP measure, NOT a semantic model: it counts token
    and character-3-gram shingle occurrences into hash buckets and
    L2-normalises.  It captures *lexical* similarity only -- genuine
    paraphrases ("数据库查询超时" vs "DB latency") score ~0.  Use it to exercise
    and verify the recall pipeline deterministically with zero network; plug a
    real embedding service into the same protocol for true semantic recall.

    A plain count vector (no sign trick) is used so lexical overlap ranks
    correctly and cosine is always non-negative.
    """

    if dimensions < 8:
        raise ValueError("dimensions must be at least 8")
    vector = [0.0] * dimensions
    for shingle in _token_shingles(text):
        digest = hashlib.sha256(shingle.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % dimensions
        vector[bucket] += 1.0
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return vector
    return [value / norm for value in vector]
