"""Fail-closed pin for the local Qwen spike artifact.

The spike targets the official post-trained `Qwen/Qwen3.5-4B`. Which Q4 artifact is
actually used - an upstream community conversion or a locally produced quantization - is
still an open decision, so no revision or per-file checksum exists yet. The pin therefore
starts unestablished and every check fails closed until the user authorises a download and
the resulting files are hashed. `check_local_llm_manifest` reuses the hardened M2 verifier
rather than duplicating its symlink, permission, traversal and checksum policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

from lune.stt.model_manifest import (
    ManifestReason,
    ModelManifestCheck,
    ModelPin,
    PinnedModelFile,
    VerifiedModelManifest,
    check_model_manifest,
)

BASE_MODEL_ID: Final[str] = "Qwen/Qwen3.5-4B"
"""Official post-trained repository. The base model is the separate `-Base` repository."""

type LocalLLMManifestReason = Literal["pin_not_established"] | ManifestReason

UPSTREAM_REVISION: Final[str] = "851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a"
"""Upstream commit the local Q4 artifact was quantized from."""

QUANTIZATION: Final[str] = "mlx-lm 0.31.3, affine, 4 bits, group size 64"
"""How the artifact was produced.

The checksums below are for a locally produced file, not an upstream download, so they
only reproduce under the same converter and settings. `mlx-lm` drops the vision tower of
this vision-language checkpoint during conversion, which is why the artifact is a single
text-only 2.2 GB file rather than the 8.9 GB source.
"""

LOCAL_LLM_PIN: Final[ModelPin | None] = ModelPin(
    model_id=BASE_MODEL_ID,
    revision=UPSTREAM_REVISION,
    files=(
        PinnedModelFile(
            relative_path="chat_template.jinja",
            sha256="a4aee8afcf2e0711942cf848899be66016f8d14a889ff9ede07bca099c28f715",
        ),
        PinnedModelFile(
            relative_path="config.json",
            sha256="8591d683d8d132234766399897415b9427967c05e77c1c169a40de3e81177e62",
        ),
        PinnedModelFile(
            relative_path="model.safetensors",
            sha256="d58b87def677c7b3235e21cdca85289a162a5ba8949687b9573592e3c74cd477",
        ),
        PinnedModelFile(
            relative_path="model.safetensors.index.json",
            sha256="2e97890b24a47b9c290489efeb2b68b7e4dd4101360de80122165ebae4e0d6cf",
        ),
        PinnedModelFile(
            relative_path="tokenizer.json",
            sha256="06b9509352d2af50381ab2247e083b80d32d5c0aba91c272ca9ff729b6a0e523",
        ),
        PinnedModelFile(
            relative_path="tokenizer_config.json",
            sha256="95c557768e6b88a7128befc7bfd3c7de50e5d51af9b8b33a9f4dee0e04f99679",
        ),
    ),
)
"""`chat_template.jinja` is pinned on purpose.

It is the file that implements `enable_thinking=False`, so replacing it would re-enable
reasoning output without touching a single weight.
"""


@dataclass(frozen=True, slots=True)
class LocalLLMManifestCheck:
    reason: LocalLLMManifestReason
    manifest: VerifiedModelManifest | None = field(default=None, repr=False)

    @property
    def ready(self) -> bool:
        return self.reason == "ready" and self.manifest is not None

    @property
    def pin_established(self) -> bool:
        return self.reason != "pin_not_established"


def build_local_llm_pin(
    *,
    model_id: str,
    revision: str,
    files: tuple[PinnedModelFile, ...],
) -> ModelPin:
    """Construct a candidate pin once a Q4 artifact has been authorised and hashed."""

    return ModelPin(model_id=model_id, revision=revision, files=files)


def check_local_llm_manifest(
    manifest_path: Path,
    *,
    pin: ModelPin | None = LOCAL_LLM_PIN,
) -> LocalLLMManifestCheck:
    """Verify the local model manifest, refusing everything while the pin is unset."""

    if pin is None:
        return LocalLLMManifestCheck(reason="pin_not_established")
    delegated: ModelManifestCheck = check_model_manifest(manifest_path, pin=pin)
    return LocalLLMManifestCheck(reason=delegated.reason, manifest=delegated.manifest)
