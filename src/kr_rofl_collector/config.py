from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigurationError


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and value[0:1] == value[-1:] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _path(root: Path, value: str | None, default: str) -> Path:
    result = Path(value or default).expanduser()
    return result if result.is_absolute() else (root / result)


@dataclass(frozen=True, slots=True)
class Config:
    project_root: Path
    data_dir: Path
    db_path: Path
    logs_dir: Path
    riot_api_key: str | None
    league_install_dir: Path
    replay_edge_base_url: str | None
    request_timeout: float
    api_min_interval: float
    api_max_retries: int
    history_count: int

    @classmethod
    def load(cls, project_root: Path | None = None) -> Config:
        root = (project_root or Path.cwd()).resolve()
        file_values = _read_dotenv(root / ".env")

        def setting(name: str, default: str | None = None) -> str | None:
            return os.environ.get(name, file_values.get(name, default))

        data_dir = _path(root, setting("COLLECTOR_DATA_DIR"), "data")
        return cls(
            project_root=root,
            data_dir=data_dir,
            db_path=_path(root, setting("COLLECTOR_DB_PATH"), "data/collector.sqlite3"),
            logs_dir=_path(root, setting("COLLECTOR_LOG_DIR"), "logs"),
            riot_api_key=(setting("RIOT_API_KEY") or "").strip() or None,
            league_install_dir=_path(
                root,
                setting("LEAGUE_INSTALL_DIR"),
                r"C:\Riot Games\League of Legends",
            ),
            replay_edge_base_url=(setting("REPLAY_EDGE_BASE_URL") or "").rstrip("/") or None,
            request_timeout=float(setting("COLLECTOR_REQUEST_TIMEOUT", "30") or "30"),
            api_min_interval=float(setting("RIOT_API_MIN_INTERVAL", "1.25") or "1.25"),
            api_max_retries=int(setting("RIOT_API_MAX_RETRIES", "6") or "6"),
            history_count=max(1, min(100, int(setting("MATCH_HISTORY_COUNT", "20") or "20"))),
        )

    def require_api_key(self) -> str:
        if not self.riot_api_key:
            raise ConfigurationError(
                "API_KEY_MISSING",
                "ACTION_REQUIRED: Please place Riot API key in .env",
            )
        return self.riot_api_key

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

