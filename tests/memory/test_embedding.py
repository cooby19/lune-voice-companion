from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import numpy as np
import numpy.typing as npt
import pytest

import lune.memory.embedding as embedding_module
from lune.memory.embedding import (
    E5_MODEL_ID,
    E5_MODEL_PIN,
    E5_MODEL_REVISION,
    E5MemoryRetriever,
    LocalE5Encoder,
)
from lune.memory.store import EMBEDDING_DIMENSIONS, MemoryStore
from lune.stt.model_manifest import ModelManifestCheck, VerifiedModelManifest
from tests.memory.conftest import complete_turn

type FloatArray = npt.NDArray[np.float32]


class GoldenEncoder:
    model_id = E5_MODEL_ID
    revision = E5_MODEL_REVISION

    def encode_query(self, query: str) -> FloatArray:
        return _vector(int(query.rsplit("-", 1)[1]))

    def encode_passages(self, passages: Sequence[str]) -> FloatArray:
        return np.stack([_vector(int(item.rsplit("-", 1)[1])) for item in passages])


class RecordingModel:
    def __init__(self) -> None:
        self.values: list[list[str]] = []

    def encode(
        self,
        sentences: list[str],
        *,
        normalize_embeddings: bool,
        convert_to_numpy: bool,
    ) -> object:
        assert normalize_embeddings and convert_to_numpy
        self.values.append(sentences)
        return np.stack([_vector(0) for _item in sentences])


def test_compiled_e5_pin_uses_immutable_revision_and_safetensors() -> None:
    assert E5_MODEL_PIN.model_id == "intfloat/multilingual-e5-small"
    assert E5_MODEL_PIN.revision == "614241f622f53c4eeff9890bdc4f31cfecc418b3"
    assert E5_MODEL_PIN.files[0].relative_path == "model.safetensors"
    assert E5_MODEL_PIN.files[0].sha256 == (
        "1a55775f53449dac10a2bcbc312469fac40b96d53198c407081a831f81c98477"
    )


def test_local_encoder_uses_required_prefixes_and_verified_local_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_root = tmp_path / "model"
    model = RecordingModel()
    manifest = VerifiedModelManifest(E5_MODEL_ID, E5_MODEL_REVISION, model_root, ())
    monkeypatch.setattr(
        embedding_module,
        "check_model_manifest",
        lambda *_args, **_kwargs: ModelManifestCheck("ready", manifest),
    )
    loaded: list[Path] = []
    encoder = LocalE5Encoder(
        model_root / "manifest.json",
        loader=lambda root: (loaded.append(root), model)[1],
    )

    encoder.encode_query("weather")
    encoder.encode_passages(("sunny", "stadium"))

    assert loaded == [model_root]
    assert model.values == [["query: weather"], ["passage: sunny", "passage: stadium"]]


def test_ten_golden_queries_all_retrieve_the_expected_memory(store: MemoryStore) -> None:
    session_id = store.start_session("golden-session")
    source_turn = complete_turn(store, session_id, 1)
    retriever = E5MemoryRetriever(store, GoldenEncoder())
    for index in range(10):
        stored = store.add_memory(
            memory_id=f"memory-{index}",
            content=f"memory-{index}",
            category="stable_preference",
            importance=0.5,
            embedding=_vector(index),
            embedding_model=E5_MODEL_ID,
            embedding_revision=E5_MODEL_REVISION,
            source_turn_id=source_turn,
        )
        assert stored is not None

    hits = 0
    for index in range(10):
        results = retriever.search(f"query-{index}")
        hits += bool(results and results[0].id == f"memory-{index}")

    assert hits >= 8
    assert len(retriever.search("query-0")) <= 5


def _vector(index: int) -> FloatArray:
    vector = np.zeros(EMBEDDING_DIMENSIONS, dtype=np.float32)
    vector[index] = 1.0
    return vector
