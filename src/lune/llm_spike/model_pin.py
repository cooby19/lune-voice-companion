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

LOCAL_LLM_PIN: Final[ModelPin | None] = None
"""Unset on purpose: no Q4 artifact has been authorised, downloaded or hashed yet."""


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
