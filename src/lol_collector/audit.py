from __future__ import annotations

import hashlib
import random
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AuditResult:
    seed: int
    requested: int
    checked: int
    passed: int
    failures: tuple[str, ...]


def audit_valid_matches(connection: sqlite3.Connection, run_id: int, seed: int = 0, limit: int = 20) -> AuditResult:
    rows = connection.execute("SELECT rm.match_id, m.summary_artifact_id, m.details_artifact_id FROM run_match rm JOIN match m ON m.id = rm.match_id WHERE rm.run_id = ? AND rm.target_valid = 1", (run_id,)).fetchall()
    selected = list(rows)
    random.Random(seed).shuffle(selected)
    selected = selected[:limit]
    failures: list[str] = []
    for row in selected:
        for field in ("summary_artifact_id", "details_artifact_id"):
            if row[field] is None:
                failures.append(f"match {row['match_id']}: {field} missing")
                continue
            artifact = connection.execute("SELECT filesystem_path, sha256, byte_size FROM raw_artifact WHERE id = ?", (row[field],)).fetchone()
            if artifact is None:
                failures.append(f"match {row['match_id']}: artifact row missing")
                continue
            path = Path(str(artifact["filesystem_path"]))
            if not path.is_file() or path.stat().st_size != int(artifact["byte_size"]) or hashlib.sha256(path.read_bytes()).hexdigest() != artifact["sha256"]:
                failures.append(f"match {row['match_id']}: artifact integrity failure")
    return AuditResult(seed, limit, len(selected), len(selected) - len(failures), tuple(failures))
