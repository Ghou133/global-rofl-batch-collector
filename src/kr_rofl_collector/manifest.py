from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .db import Database


def manifest_path(data_dir: Path, patch: str) -> Path:
    return data_dir / "KR" / patch / "manifests" / "dataset_manifest.jsonl"


def _manifest_item(row: Any) -> dict[str, Any]:
    verification = json.loads(row["verification_json"])
    return {
        "match_id": row["match_id"],
        "game_id": row["game_id"],
        "region": row["platform"],
        "queue_id": row["queue_id"],
        "game_version": row["game_version_exact"],
        "patch": row["patch_key"],
        "game_creation": row["game_creation"],
        "game_duration": row["game_duration"],
        "challenger_count": row["challenger_count"],
        "grandmaster_count": row["grandmaster_count"],
        "master_count": row["master_count"],
        "known_apex_count": row["known_apex_count"],
        "highest_tier": row["highest_tier"],
        "source_quality": (
            "CHALLENGER_HEAVY"
            if row["challenger_count"] >= 5
            else "CHALLENGER_PRESENT"
            if row["challenger_count"] > 0
            else "GRANDMASTER_HEAVY"
            if row["grandmaster_count"] >= 5
            else "MASTER_HEAVY"
        ),
        "file": row["file_path"],
        "size": row["file_size"],
        "sha256": row["sha256"],
        "downloaded_at": row["downloaded_at"],
        "verified_at": row["verified_at"],
        "verification": verification,
        "provider": row["provider"],
    }


def write_manifest(db: Database, dataset_id: int, data_dir: Path, patch: str) -> Path:
    target = manifest_path(data_dir, patch)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    rows = db.manifest_rows(dataset_id)
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(
                json.dumps(
                    _manifest_item(row), ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                + "\n"
            )
        stream.flush()
        os.fsync(stream.fileno())

    seen: set[str] = set()
    with temporary.open("r", encoding="utf-8") as stream:
        for line in stream:
            item = json.loads(line)
            match_id = str(item["match_id"])
            if match_id in seen:
                raise ValueError(f"Duplicate manifest match_id: {match_id}")
            seen.add(match_id)
            file_path = data_dir / item["file"]
            if not file_path.is_file() or file_path.stat().st_size != item["size"]:
                raise ValueError(f"Manifest asset missing or size mismatch: {match_id}")
    if len(seen) != len(rows):
        raise ValueError("Manifest verification count mismatch")
    os.replace(temporary, target)
    return target

