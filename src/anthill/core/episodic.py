"""Episodic memory — find past asks similar to a new one.

When the king asks something new, the most useful thing the nation can
do *before* planning is check: have we seen something like this before?
If yes, the past successful plan + outputs become hints for Scout.

We use TF-IDF cosine similarity, not embeddings. Reasons:

1. Zero extra API dependency. The nation already runs against DeepSeek/
   MiniMax; adding an embedding API doubles the failure surface.
2. Deterministic. Same inputs always produce same scores. Easy to test.
3. Cheap. A few hundred past asks indexed in memory in milliseconds.
4. Good enough at the scale Anthill currently operates. Embeddings
   are a v0.2 upgrade when history grows past 10k entries.

The output is a small set of high-similarity entries with their
outcomes, not raw text. Scout reads them as worked examples.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from anthill.core.history import HistoryEntry


_TOKEN_RE = re.compile(r"[一-鿿]|[a-zA-Z0-9_]+")


def tokenize(text: str) -> list[str]:
    """Tokenise for TF-IDF.

    Each CJK character is its own token (Chinese has no spaces, so
    character-level works better than word-level), Latin tokens are
    lowercased and split on word boundaries. Numbers are kept.
    """
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


@dataclass
class SimilarPast:
    """A past ask that is similar to the current one, with its outcomes."""

    entry: HistoryEntry
    score: float


class TfidfIndex:
    """A small in-memory TF-IDF index over a corpus of texts.

    Built from scratch (no sklearn dep). Idf is computed from the corpus
    at construction; the same idf is reused for new query vectors so the
    similarity score is meaningful.
    """

    def __init__(self, corpus_texts: Iterable[str]) -> None:
        docs = [tokenize(t) for t in corpus_texts]
        self._n_docs = len(docs)
        self._idf: dict[str, float] = {}
        if self._n_docs:
            df: Counter[str] = Counter()
            for doc in docs:
                df.update(set(doc))
            for term, count in df.items():
                # +1 smoothing so unseen terms during query do not blow up
                self._idf[term] = math.log((self._n_docs + 1) / (count + 1)) + 1.0
        # Precompute document vectors
        self._doc_vectors: list[dict[str, float]] = [self._vectorize(doc) for doc in docs]

    def _vectorize(self, tokens: list[str]) -> dict[str, float]:
        if not tokens:
            return {}
        tf = Counter(tokens)
        n = len(tokens)
        vector = {term: (count / n) * self._idf.get(term, 0.0) for term, count in tf.items()}
        return vector

    def query(self, text: str, top_k: int = 3) -> list[tuple[int, float]]:
        """Return [(doc_index, similarity), ...] sorted by similarity desc."""
        if not self._doc_vectors:
            return []
        q_vec = self._vectorize(tokenize(text))
        if not q_vec:
            return []
        results: list[tuple[int, float]] = []
        for i, doc_vec in enumerate(self._doc_vectors):
            if not doc_vec:
                continue
            score = _cosine(q_vec, doc_vec)
            if score > 0:
                results.append((i, score))
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    """Sparse cosine similarity."""
    # Iterate the smaller vector for the dot product
    if len(a) > len(b):
        a, b = b, a
    dot = sum(v * b.get(k, 0.0) for k, v in a.items())
    if dot == 0:
        return 0.0
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def find_similar(
    request: str,
    history: list[HistoryEntry],
    *,
    top_k: int = 3,
    min_score: float = 0.15,
) -> list[SimilarPast]:
    """Return the top-k past asks most similar to `request`.

    Filters out entries below min_score — better to return nothing than
    to feed Scout a barely-related "past example."
    """
    if not history:
        return []
    index = TfidfIndex(e.request for e in history)
    hits = index.query(request, top_k=top_k)
    out: list[SimilarPast] = []
    for idx, score in hits:
        if score < min_score:
            continue
        out.append(SimilarPast(entry=history[idx], score=score))
    return out


def format_similar_for_scout(similar: list[SimilarPast]) -> str:
    """Format hits as a context block Scout can read.

    Only includes the request and the task_type sequence of the plan —
    not the full outputs. Goal is to bias Scout's labelling toward the
    nation's existing vocabulary without flooding context.
    """
    if not similar:
        return ""
    lines: list[str] = ["Past similar asks in this nation:"]
    for i, hit in enumerate(similar, start=1):
        plan_types = " -> ".join(s.get("task_type", "?") for s in hit.entry.plan)
        lines.append(f'  {i}. "{hit.entry.request}"  (similarity {hit.score:.2f})')
        if plan_types:
            lines.append(f"     plan was: {plan_types}")
    return "\n".join(lines)
