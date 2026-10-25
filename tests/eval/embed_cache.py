"""A disk-backed embedding cache for long measurement runs.

``DiskCachingEmbedder`` wraps an ``Embedder`` and stores each vector in a
sqlite3 file keyed by ``sha256(model name + "\\n" + text)``, so a run that is
killed and started again with the same directory re-embeds nothing it already
did, and separate runs (or variants) share vectors. Vectors are also kept in
memory. A query and an entry with the same text are cached apart, as
``embed_text`` and ``embed_entry`` are separate calls on the port.

Each new vector is committed as it arrives (embedding costs far more than a
commit), so a kill loses at most the vector being computed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from hivemind.domain.entry import EntryDraft
from hivemind.ports import Embedder

CACHE_FILE = "embeddings.sqlite3"


class DiskCachingEmbedder:
    """An ``Embedder`` that embeds each (model, text) once, ever, per cache directory."""

    def __init__(self, wrapped: Embedder, directory: str | Path) -> None:
        self._wrapped = wrapped
        self._by_key: dict[str, list[float]] = {}
        self.hits = 0
        self.misses = 0
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path / CACHE_FILE)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, vector TEXT)")
        self._db.commit()

    @property
    def dimension(self) -> int:
        return self._wrapped.dimension

    @property
    def model_name(self) -> str:
        return self._wrapped.model_name

    def entry_embeddable_text(self, draft: EntryDraft) -> str:
        return self._wrapped.entry_embeddable_text(draft)

    def close(self) -> None:
        self._db.close()

    def _key(self, kind: str, text: str) -> str:
        raw = f"{self.model_name}\n{self.dimension}\n{kind}\x00{text}"
        return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()

    def _lookup(self, key: str) -> list[float] | None:
        vector = self._by_key.get(key)
        if vector is None:
            row = self._db.execute("SELECT vector FROM vectors WHERE key = ?", (key,)).fetchone()
            if row is not None:
                vector = json.loads(row[0])
                self._by_key[key] = vector
        if vector is not None:
            self.hits += 1
        return vector

    def _store(self, key: str, vector: list[float]) -> None:
        self.misses += 1
        self._by_key[key] = vector
        self._db.execute(
            "INSERT OR REPLACE INTO vectors (key, vector) VALUES (?, ?)",
            (key, json.dumps(vector)),
        )
        self._db.commit()

    async def embed_text(self, text: str) -> list[float]:
        key = self._key("t", text)
        vector = self._lookup(key)
        if vector is None:
            vector = await self._wrapped.embed_text(text)
            self._store(key, vector)
        return vector

    async def embed_entry(self, draft: EntryDraft) -> list[float]:
        key = self._key("e", self.entry_embeddable_text(draft))
        vector = self._lookup(key)
        if vector is None:
            vector = await self._wrapped.embed_entry(draft)
            self._store(key, vector)
        return vector
