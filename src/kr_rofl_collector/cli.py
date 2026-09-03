from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import Config
from .errors import CollectorError
from .service import CollectorService


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def _print_probe(result: dict[str, Any]) -> None:
    ordered = (
        "RIOT_API_KEY",
        "KR_LADDER_API",
        "ASIA_MATCH_API",
        "CURRENT_PATCH",
        "LEAGUE_CLIENT",
        "LCU",
        "REPLAY_ACQUISITION",
        "KR_ACCOUNT_REQUIRED",
        "DATABASE",
        "STORAGE",
        "EXISTING_DATASET",
    )
    for key in ordered:
        print(f"{key}: {result.get(key, 'UNKNOWN')}")
    for route in result.get("REPLAY_ROUTES", []):
        print(
            f"  {route['route']}: {route['capability']} "
            f"(HTTP {route.get('http_status') or 'n/a'})"
        )


def _print_status(data: dict[str, Any]) -> None:
    if data.get("current_patch") == "UNKNOWN":
        print(data.get("message", "No dataset"))
        return
    lines = [
        ("current patch", data["current_patch"]),
        ("realm version", data["realm_version"]),
        ("players discovered", data["players_discovered"]),
        ("Challenger players", data["challenger_players"]),
        ("Grandmaster players", data["grandmaster_players"]),
        ("Master players", data["master_players"]),
        ("raw match discoveries", data["raw_match_discoveries"]),
        ("discovery edges", data["discovery_edges"]),
        ("unique matches", data["unique_matches"]),
        ("current-patch matches", data["current_patch_matches"]),
        ("eligible", data["eligible"]),
        ("queued", data["queued"]),
        ("downloading", data["downloading"]),
        ("downloaded", data["downloaded"]),
        ("verified", data["verified"]),
        ("unavailable", data["unavailable"]),
        ("failed retryable", data["failed_retryable"]),
        ("failed permanent", data["failed_permanent"]),
        ("total dataset size", _format_bytes(data["total_dataset_size"])),
        ("Challenger-heavy", data["challenger_heavy"]),
        ("Challenger-present", data["challenger_present"]),
        ("GM-heavy", data["gm_heavy"]),
        ("Master-heavy", data["master_heavy"]),
    ]
    for label, value in lines:
        print(f"{label}: {value}")
    print(f"database: {data['database']}")
    print(f"rofl root: {data['rofl_root']}")
    print(f"manifest: {data['manifest']}")
    print(f"reports: {data['reports']}")
    last_run = data.get("last_run")
    if last_run:
        print(
            f"last run: id={last_run['id']} command={last_run['command']} "
            f"status={last_run['status']} target={last_run.get('target_total') or 'n/a'}"
        )
    errors = data.get("latest_errors", [])
    print(f"latest errors: {len(errors)}")
    for error in errors[:5]:
        print(
            f"  id={error['id']} stage={error['stage']} code={error['code']} "
            f"retryable={bool(error['retryable'])}"
        )
    if "integrity_audit" in data:
        print(f"integrity audit: {data['integrity_audit']}")
        for error in data.get("integrity_errors", []):
            print(f"  {error}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="collector",
        description="KR current-patch high-elo Ranked Solo ROFL batch collector",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="project directory containing .env (default: current directory)",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("probe", help="test Riot API, patch, League Client, and replay route")
    run = commands.add_parser("run", help="collect until the patch dataset total reaches target")
    run.add_argument("--target", type=int, required=True, help="total verified replay target")
    status = commands.add_parser("status", help="show persistent dataset status")
    status.add_argument(
        "--verify-files", action="store_true", help="re-read every ROFL and verify structure/hash"
    )
    commands.add_parser(
        "normalize-http-gzip",
        help="preserve legacy HTTP wrappers and publish decoded RIOT containers",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = Config.load(args.project_root)
    try:
        with CollectorService(config, progress=lambda text: print(text, flush=True)) as service:
            if args.command == "probe":
                result = service.probe()
                if args.json:
                    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
                else:
                    _print_probe(result)
                return 0 if result.get("REPLAY_ACQUISITION") == "PASS" else 3
            if args.command == "run":
                result = service.run(args.target)
                if args.json:
                    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
                else:
                    print(result["status"])
                    print(f"CURRENT_PATCH: {result['patch']}")
                    print(f"VERIFIED: {result['verified']}/{result['target']}")
                    print(f"MANIFEST: {result['manifest']}")
                return 0
            if args.command == "normalize-http-gzip":
                result = service.normalize_http_gzip()
                if args.json:
                    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
                else:
                    print(f"NORMALIZED: {result['normalized']}")
                    print(f"ALREADY_RAW: {result['already_raw']}")
                    print(f"MANIFEST: {result['manifest']}")
                return 0
            result = service.status(verify_files=args.verify_files)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                _print_status(result)
            return 0
    except CollectorError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        if exc.code in {"API_KEY_MISSING", "LCU_LOGIN_REQUIRED", "LEAGUE_CLIENT_CLOSED"}:
            return 2
        return 3 if "REPLAY" in exc.code else 1
    except KeyboardInterrupt:
        print("INTERRUPTED: safe to rerun; completed files and database state were preserved")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
