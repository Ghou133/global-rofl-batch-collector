from __future__ import annotations

import gzip
import hashlib
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import Config
from .db import Database
from .errors import IntegrityError
from .manifest import write_manifest
from .replay import verify_rofl


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _prefix(path: Path) -> bytes:
    with path.open("rb") as stream:
        return stream.read(4)


def _preserve_source(source: Path, backup: Path) -> None:
    backup.parent.mkdir(parents=True, exist_ok=True)
    if backup.is_file():
        if backup.stat().st_size != source.stat().st_size or _sha256(backup) != _sha256(source):
            raise IntegrityError(
                "ROFL_BACKUP_CONFLICT",
                f"Preservation backup differs from source: {backup}",
            )
        return
    temporary = backup.with_suffix(backup.suffix + ".partial")
    if temporary.exists():
        temporary.unlink()
    with source.open("rb") as incoming, temporary.open("xb") as outgoing:
        shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    if _sha256(temporary) != _sha256(source):
        raise IntegrityError("ROFL_BACKUP_HASH_MISMATCH", f"Backup copy failed: {backup}")
    os.rename(temporary, backup)


def _decode_gzip(source: Path, temporary: Path) -> None:
    if temporary.exists():
        temporary.unlink()
    try:
        with gzip.open(source, "rb") as incoming, temporary.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
            outgoing.flush()
            os.fsync(outgoing.fileno())
    except (OSError, EOFError) as exc:
        temporary.unlink(missing_ok=True)
        raise IntegrityError(
            "ROFL_HTTP_GZIP_CORRUPT",
            f"HTTP gzip wrapper failed CRC/decompression validation: {source}",
        ) from exc


def normalize_http_gzip_replays(
    db: Database,
    config: Config,
    dataset: Any,
    *,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """One-time, preservation-first migration from HTTP gzip bytes to RIOT bytes."""
    report = progress or (lambda _: None)
    dataset_id = int(dataset["id"])
    patch = str(dataset["patch_key"])
    rows = db.manifest_rows(dataset_id)
    normalized = 0
    already_raw = 0
    backups: list[str] = []

    for row in rows:
        final = config.data_dir / str(row["file_path"])
        backup = (
            config.data_dir
            / "KR"
            / patch
            / "quarantine"
            / "http-gzip"
            / f"{row['match_id']}.rofl.gz"
        )
        if not final.is_file():
            raise IntegrityError("ROFL_MISSING", f"Cannot normalize missing replay: {final}")

        magic = _prefix(final)
        if magic == b"RIOT":
            verification = verify_rofl(final)
            already_raw += 1
        elif magic.startswith(b"\x1f\x8b"):
            temporary = final.with_suffix(final.suffix + ".normalizing")
            _decode_gzip(final, temporary)
            verification = verify_rofl(temporary)
            if verification.game_version != str(row["game_version_exact"]):
                temporary.unlink(missing_ok=True)
                raise IntegrityError(
                    "ROFL_BUILD_MISMATCH",
                    f"Replay {row['match_id']} contains build {verification.game_version}",
                )
            _preserve_source(final, backup)
            os.replace(temporary, final)
            normalized += 1
            backups.append(backup.relative_to(config.data_dir).as_posix())
            report(f"NORMALIZED {normalized}: {row['match_id']}")
        else:
            raise IntegrityError(
                "ROFL_MALFORMED",
                f"Replay has neither RIOT nor HTTP gzip magic: {final}",
            )

        if verification.game_version != str(row["game_version_exact"]):
            raise IntegrityError(
                "ROFL_BUILD_MISMATCH",
                f"Replay {row['match_id']} contains build {verification.game_version}",
            )
        relative_backup = (
            backup.relative_to(config.data_dir).as_posix() if backup.is_file() else None
        )
        db.record_download(
            str(row["match_id"]),
            file_path=str(row["file_path"]),
            file_size=verification.file_size,
            sha256=verification.sha256,
            provider=str(row["provider"]),
            verification={
                **verification.as_dict(),
                "http_content_encoding_normalized": bool(relative_backup),
                "preserved_transport_backup": relative_backup,
            },
        )

    manifest = write_manifest(db, dataset_id, config.data_dir, patch)
    return {
        "patch": patch,
        "examined": len(rows),
        "normalized": normalized,
        "already_raw": already_raw,
        "preserved_backups": backups,
        "manifest": str(manifest),
    }
