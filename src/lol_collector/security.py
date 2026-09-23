from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SecretScan:
    passed: bool
    findings: tuple[str, ...]


SECRET_PATTERNS = (
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{8,}", re.IGNORECASE),
    re.compile(r"Authorization\s*[:=]", re.IGNORECASE),
    re.compile(r'"(?:token|access_token|entitlementsToken|leagueSession)"\s*:\s*"(?!<redacted>)', re.IGNORECASE),
)


def scan_tree(root: Path) -> SecretScan:
    findings: list[str] = []
    if not root.exists():
        return SecretScan(True, ())
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".json", ".log", ".txt", ".sqlite3"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            findings.append(str(path))
            continue
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            findings.append(str(path))
    return SecretScan(not findings, tuple(findings))
