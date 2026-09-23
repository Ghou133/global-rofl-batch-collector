from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from lol_collector.models import ReplayValidationStatus, normalize_patch


ROFL_FILENAME_RE: Final = re.compile(
    r"^(?:(?P<platform>[A-Za-z0-9_]+)-)?(?P<game_id>\d+)\.rofl$",
    re.IGNORECASE,
)
ROFL_VERSION_RE: Final = re.compile(r"^\d+\.\d+\.\d+\.\d+$")
TEXT_ERROR_PREFIXES: Final = (
    b"<!doctype",
    b"<html",
    b"<?xml",
    b"{",
    b"[",
)


@dataclass(frozen=True, slots=True)
class ReplayCandidate:
    source_path: Path
    game_id: str
    platform_id: str | None
    file_size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class ReplayValidation:
    status: ReplayValidationStatus
    file_size: int
    sha256: str | None
    game_version: str | None
    error_code: str | None = None
    error_message: str | None = None

    @property
    def valid(self) -> bool:
        return self.status is ReplayValidationStatus.VALIDATED


@dataclass(frozen=True, slots=True)
class ReplayArchiveResult:
    success: bool
    target_path: Path | None
    validation: ReplayValidation
    copied: bool
    error_code: str | None = None
    error_message: str | None = None


def default_replay_source_dirs() -> tuple[Path, ...]:
    documents = Path.home() / "Documents"
    return (
        documents / "League of Legends" / "Replays",
        documents / "League of Legends",
    )


def parse_replay_filename(path: Path) -> tuple[str, str | None] | None:
    match = ROFL_FILENAME_RE.fullmatch(path.name)
    if match is None:
        return None
    return match.group("game_id"), match.group("platform")


def discover_replays(
    source_dirs: tuple[Path, ...],
    *,
    excluded_root: Path | None = None,
) -> tuple[ReplayCandidate, ...]:
    excluded = excluded_root.resolve() if excluded_root is not None else None
    seen: set[Path] = set()
    candidates: list[ReplayCandidate] = []
    for source_dir in source_dirs:
        if not source_dir.is_dir():
            continue
        for path in source_dir.rglob("*"):
            if not path.is_file() or path.suffix.lower() != ".rofl":
                continue
            resolved = path.resolve()
            if resolved in seen or (excluded is not None and resolved.is_relative_to(excluded)):
                continue
            parsed = parse_replay_filename(resolved)
            if parsed is None:
                continue
            try:
                stat = resolved.stat()
            except OSError:
                continue
            game_id, platform_id = parsed
            seen.add(resolved)
            candidates.append(
                ReplayCandidate(
                    source_path=resolved,
                    game_id=game_id,
                    platform_id=platform_id,
                    file_size=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                )
            )
    candidates.sort(key=lambda item: (item.mtime_ns, str(item.source_path)))
    return tuple(candidates)


def validate_replay(path: Path) -> ReplayValidation:
    try:
        stat = path.stat()
        if not path.is_file():
            return _invalid(0, None, "NOT_A_FILE", "Replay path is not a regular file")
        digest = hashlib.sha256()
        header = b""
        with path.open("rb") as handle:
            header = handle.read(4096)
            digest.update(header)
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        return _invalid(0, None, "FILE_READ_ERROR", type(error).__name__)

    sha256 = digest.hexdigest()
    if stat.st_size <= 0:
        return _invalid(stat.st_size, sha256, "EMPTY_FILE", "Replay file is empty")
    stripped = header.lstrip().lower()
    if any(stripped.startswith(prefix) for prefix in TEXT_ERROR_PREFIXES):
        return _invalid(
            stat.st_size,
            sha256,
            "TEXT_ERROR_PAYLOAD",
            "Replay candidate is an HTML, XML, or JSON response",
        )
    if len(header) < 20:
        return _invalid(stat.st_size, sha256, "TRUNCATED_HEADER", "ROFL header is truncated")
    if header[:4] != b"RIOT":
        return _invalid(stat.st_size, sha256, "INVALID_MAGIC", "ROFL magic is not RIOT")
    if header[4:6] != b"\x02\x00":
        return _invalid(
            stat.st_size,
            sha256,
            "UNSUPPORTED_CONTAINER_VERSION",
            "ROFL container version is not the verified 02 00 format",
        )
    version_length = header[14]
    if not 1 <= version_length <= 64 or len(header) < 19 + version_length:
        return _invalid(
            stat.st_size,
            sha256,
            "INVALID_VERSION_HEADER",
            "ROFL version field is truncated or outside the safe bound",
        )
    try:
        game_version = header[15 : 15 + version_length].decode("ascii")
    except UnicodeDecodeError:
        return _invalid(
            stat.st_size,
            sha256,
            "INVALID_GAME_VERSION",
            "ROFL game version is not ASCII",
        )
    if ROFL_VERSION_RE.fullmatch(game_version) is None:
        return _invalid(
            stat.st_size,
            sha256,
            "INVALID_GAME_VERSION",
            "ROFL game version does not have four numeric components",
        )
    marker_start = 15 + version_length
    if header[marker_start : marker_start + 4] != b"\x01\x00\x00\x00":
        return _invalid(
            stat.st_size,
            sha256,
            "INVALID_HEADER_MARKER",
            "ROFL post-version header marker is invalid",
        )
    return ReplayValidation(
        status=ReplayValidationStatus.VALIDATED,
        file_size=stat.st_size,
        sha256=sha256,
        game_version=game_version,
    )


def replay_archive_path(
    archive_root: Path,
    game_id: str,
    game_version: str | None,
) -> Path:
    if not game_id.isdecimal():
        raise ValueError("game_id must contain decimal digits only")
    patch = normalize_patch(game_version or "") or "unknown"
    return archive_root / patch / f"{game_id}.rofl"


def archive_replay(source: Path, target: Path) -> ReplayArchiveResult:
    source_validation = validate_replay(source)
    if not source_validation.valid:
        return ReplayArchiveResult(
            success=False,
            target_path=None,
            validation=source_validation,
            copied=False,
            error_code=source_validation.error_code,
            error_message=source_validation.error_message,
        )

    if target.exists():
        existing = validate_replay(target)
        if existing.valid and existing.sha256 == source_validation.sha256:
            return ReplayArchiveResult(True, target, existing, False)
        return ReplayArchiveResult(
            success=False,
            target_path=target,
            validation=existing,
            copied=False,
            error_code="ARCHIVE_CONFLICT",
            error_message="An existing Replay already occupies the archive path",
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(f"{target.suffix}.partial")
    try:
        if partial.exists():
            partial.unlink()
        before = source.stat()
        with source.open("rb") as source_handle, partial.open("xb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            partial.unlink(missing_ok=True)
            return ReplayArchiveResult(
                False,
                None,
                source_validation,
                False,
                "SOURCE_CHANGED_DURING_COPY",
                "Replay source changed while it was being archived",
            )
        copied_validation = validate_replay(partial)
        if not copied_validation.valid or copied_validation.sha256 != source_validation.sha256:
            partial.unlink(missing_ok=True)
            return ReplayArchiveResult(
                False,
                None,
                copied_validation,
                False,
                "COPIED_REPLAY_INVALID",
                copied_validation.error_message or "Archived Replay hash differs from source",
            )
        os.replace(partial, target)
        final_validation = validate_replay(target)
    except OSError as error:
        partial.unlink(missing_ok=True)
        return ReplayArchiveResult(
            False,
            None,
            source_validation,
            False,
            "ARCHIVE_IO_ERROR",
            type(error).__name__,
        )
    if not final_validation.valid or final_validation.sha256 != source_validation.sha256:
        return ReplayArchiveResult(
            False,
            target,
            final_validation,
            True,
            "FINAL_REPLAY_INVALID",
            final_validation.error_message or "Final Replay hash differs from source",
        )
    return ReplayArchiveResult(True, target, final_validation, True)


def _invalid(
    file_size: int,
    sha256: str | None,
    error_code: str,
    error_message: str,
) -> ReplayValidation:
    return ReplayValidation(
        status=ReplayValidationStatus.INVALID,
        file_size=file_size,
        sha256=sha256,
        game_version=None,
        error_code=error_code,
        error_message=error_message,
    )
