from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from lol_collector.models import (
    ArtifactType,
    JsonValue,
    sanitize_json,
    schema_fingerprint,
    utc_now,
)


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    artifact_type: ArtifactType
    owner_type: str
    owner_id: str
    fetched_at: str
    endpoint_template: str
    http_status: int
    byte_size: int
    sha256: str
    filesystem_path: str
    schema_hash: str
    artifact_version: int = 1


class ArtifactSecurityError(RuntimeError):
    pass


class RawArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def write_json(
        self,
        *,
        artifact_type: ArtifactType,
        owner_type: str,
        owner_id: str,
        endpoint_template: str,
        http_status: int,
        payload: JsonValue,
        artifact_version: int = 1,
    ) -> ArtifactRecord:
        clean = sanitize_json(payload)
        encoded = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return self._write(
            artifact_type=artifact_type,
            owner_type=owner_type,
            owner_id=owner_id,
            endpoint_template=endpoint_template,
            http_status=http_status,
            encoded=encoded,
            schema_hash=schema_fingerprint(clean),
            artifact_version=artifact_version,
        )

    def write_named_json(self, filename: str, payload: JsonValue) -> Path:
        relative = Path(filename)
        if relative.name != filename or relative.suffix.lower() != ".json":
            raise ValueError("artifact filename must be a plain JSON filename")
        clean = sanitize_json(payload)
        encoded = json.dumps(clean, ensure_ascii=False, indent=2).encode("utf-8")
        if b"Authorization" in encoded or b"Bearer " in encoded:
            raise ArtifactSecurityError("credential-shaped content rejected")
        target = self.root / relative
        partial = target.with_suffix(".partial")
        with partial.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        partial.replace(target)
        return target

    def _write(
        self,
        *,
        artifact_type: ArtifactType,
        owner_type: str,
        owner_id: str,
        endpoint_template: str,
        http_status: int,
        encoded: bytes,
        schema_hash: str,
        artifact_version: int,
    ) -> ArtifactRecord:
        if b"Authorization" in encoded or b"Bearer " in encoded:
            raise ArtifactSecurityError("credential-shaped content rejected")
        digest = hashlib.sha256(encoded).hexdigest()
        relative = Path(artifact_type.value.lower()) / f"{owner_id}-{artifact_version}-{digest[:16]}.json"
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(".partial")
        with partial.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        partial.replace(target)
        stat = target.stat()
        return ArtifactRecord(
            artifact_type=artifact_type,
            owner_type=owner_type,
            owner_id=owner_id,
            fetched_at=utc_now().isoformat(),
            endpoint_template=endpoint_template,
            http_status=http_status,
            byte_size=stat.st_size,
            sha256=digest,
            filesystem_path=str(target),
            schema_hash=schema_hash,
            artifact_version=artifact_version,
        )
