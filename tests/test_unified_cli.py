from __future__ import annotations

import importlib
import json
from pathlib import Path

from global_rofl_collector.entrypoint import main


def test_global_entrypoint_routes_platform_status(tmp_path: Path, capsys) -> None:
    result = main(
        ["--project-root", str(tmp_path), "--platform", "NA1", "--json", "status"]
    )
    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["platform"] == "NA1"
    assert output["current_patch"] == "UNKNOWN"
    assert (tmp_path / "data" / "collector.sqlite3").is_file()
    assert not (tmp_path / "data" / "CN").exists()


def test_cn_entrypoint_routes_status_to_isolated_database(tmp_path: Path, capsys) -> None:
    cn_dir = tmp_path / "CN" / "replay-paired"
    result = main(["cn", "replay", "--status", "--data-dir", str(cn_dir)])
    assert result == 0
    assert "DOWNLOADS_ATTEMPTED=0" in capsys.readouterr().out
    assert (cn_dir / "collector.sqlite3").is_file()
    assert not (tmp_path / "data" / "collector.sqlite3").exists()


def test_project_help_exposes_cn_and_platform_choices(capsys) -> None:
    assert main(["--help"]) == 0
    output = capsys.readouterr().out
    assert "--platform" in output
    assert "collector cn --help" in output


def test_old_kr_imports_share_the_renamed_implementation() -> None:
    old = importlib.import_module("kr_rofl_collector.service")
    new = importlib.import_module("global_rofl_collector.service")
    assert old is new
