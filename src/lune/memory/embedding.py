"""Pinned, local-only E5 embeddings and bounded cosine retrieval."""

from __future__ import annotations

import importlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol, cast

import numpy as np
import numpy.typing as npt

from lune.memory.store import EMBEDDING_DIMENSIONS, MemoryStore
from lune.stt.model_manifest import (
    ModelPin,
    PinnedModelFile,
    check_model_manifest,
)

E5_MODEL_ID: Final[str] = "intfloat/multilingual-e5-small"
E5_MODEL_REVISION: Final[str] = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
E5_MODEL_PIN: Final[ModelPin] = ModelPin(
    model_id=E5_MODEL_ID,
    revision=E5_MODEL_REVISION,
    files=(
        PinnedModelFile(
            relative_path="model.safetensors",
            sha256="1a55775f53449dac10a2bcbc312469fac40b96d53198c407081a831f81c98477",
        ),
    ),
)
type FloatArray = npt.NDArray[np.float32]


class E5SetupRequired(RuntimeError):
    """Finite local setup failure that never includes a private path."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class EmbeddingEncoder(Protocol):
    @property
    def model_id(self) -> str: ...

    @property
    def revision(self) -> str: ...

    def encode_query(self, query: str) -> FloatArray: ...

    def encode_passages(self, passages: Sequence[str]) -> FloatArray: ...


class _SentenceTransformer(Protocol):
    def encode(
        self,
        sentences: list[str],
        *,
        normalize_embeddings: bool,
        convert_to_numpy: bool,
    ) -> object: ...


class LocalE5Encoder:
    """Lazy optional backend that can only load a verified local model directory."""

    def __init__(
        self,
        manifest_path: Path,
        *,
        loader: Callable[[Path], _SentenceTransformer] | None = None,
    ) -> None:
        self._manifest_path = manifest_path
        self._loader = loader or _load_sentence_transformer
        self._model: _SentenceTransformer | None = None

    @property
    def model_id(self) -> str:
        return E5_MODEL_ID

    @property
    def revision(self) -> str:
        return E5_MODEL_REVISION

    def encode_query(self, query: str) -> FloatArray:
        clean = _text(query, "query")
        return cast(FloatArray, self._encode([f"query: {clean}"])[0])

    def encode_passages(self, passages: Sequence[str]) -> FloatArray:
        if not passages:
            raise ValueError("at least one passage is required")
        prepared = [f"passage: {_text(item, 'passage')}" for item in passages]
        return self._encode(prepared)

    def _encode(self, values: list[str]) -> FloatArray:
        model = self._model
        if model is None:
            check = check_model_manifest(self._manifest_path, pin=E5_MODEL_PIN)
            if not check.ready or check.manifest is None:
                raise E5SetupRequired(check.reason)
            model = self._loader(check.manifest.model_root)
            self._model = model
        raw = model.encode(values, normalize_embeddings=True, convert_to_numpy=True)
        matrix = np.asarray(raw, dtype="<f4")
        if matrix.shape != (len(values), EMBEDDING_DIMENSIONS) or not np.isfinite(matrix).all():
            raise RuntimeError("E5 backend returned invalid embedding dimensions")
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        if np.any(norms == 0):
            raise RuntimeError("E5 backend returned a zero embedding")
        return cast(FloatArray, np.asarray(matrix / norms, dtype="<f4"))


@dataclass(frozen=True, slots=True)
class MemorySearchResult:
    id: str
    score: float
    category: str
    content: str = field(repr=False)


class E5MemoryRetriever:
    def __init__(self, store: MemoryStore, encoder: EmbeddingEncoder) -> None:
        self._store = store
        self._encoder = encoder

    @property
    def encoder(self) -> EmbeddingEncoder:
        return self._encoder

    def embed_passage(self, content: str) -> FloatArray:
        return cast(FloatArray, self._encoder.encode_passages((content,))[0])

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        threshold: float = 0.72,
        character_limit: int = 1_200,
    ) -> tuple[MemorySearchResult, ...]:
        if not 1 <= top_k <= 5:
            raise ValueError("top-k must be between one and five")
        if not -1.0 <= threshold <= 1.0:
            raise ValueError("cosine threshold must be between -1 and 1")
        if character_limit <= 0 or character_limit > 1_200:
            raise ValueError("memory character limit must be between one and 1,200")
        query_vector = _unit_vector(self._encoder.encode_query(query))
        scored = sorted(
            (
                (float(np.dot(query_vector, memory.embedding)), memory)
                for memory in self._store.list_memories()
            ),
            key=lambda item: (-item[0], item[1].id),
        )
        results: list[MemorySearchResult] = []
        used_characters = 0
        for score, memory in scored:
            if score < threshold or len(results) >= top_k:
                break
            if used_characters + len(memory.content) > character_limit:
                continue
            results.append(MemorySearchResult(memory.id, score, memory.category, memory.content))
            used_characters += len(memory.content)
        return tuple(results)


def _load_sentence_transformer(model_root: Path) -> _SentenceTransformer:
    try:
        module = importlib.import_module("sentence_transformers")
    except ImportError as error:
        raise E5SetupRequired("optional_dependency_missing") from error
    constructor = cast(Callable[..., _SentenceTransformer], module.SentenceTransformer)
    try:
        return constructor(
            str(model_root),
            local_files_only=True,
            trust_remote_code=False,
            model_kwargs={"use_safetensors": True},
        )
    except Exception as error:
        raise E5SetupRequired("model_load_failed") from error


def _text(value: str, label: str) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError(f"{label} cannot be empty")
    return clean


def _unit_vector(value: FloatArray) -> FloatArray:
    vector = np.asarray(value, dtype="<f4")
    if vector.shape != (EMBEDDING_DIMENSIONS,) or not np.isfinite(vector).all():
        raise RuntimeError("encoder returned an invalid query embedding")
    norm = float(np.linalg.norm(vector))
    if norm == 0.0:
        raise RuntimeError("encoder returned a zero query embedding")
    return cast(FloatArray, np.asarray(vector / norm, dtype="<f4"))


def memory_contents(results: Sequence[MemorySearchResult]) -> tuple[str, ...]:
    """Return only already-bounded text for PromptContext."""

    return tuple(result.content for result in results)
