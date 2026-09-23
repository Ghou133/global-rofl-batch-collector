"""The single installed entry point for global Riot and Tencent CN collection."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "cn":
        # Import lazily so global-region commands remain usable if a CN-only
        # dependency is temporarily unavailable in an existing installation.
        from click import ClickException

        from lol_collector.cli import app

        try:
            app(args=arguments[1:], prog_name="collector cn", standalone_mode=False)
        except ClickException as exc:
            exc.show()
            return exc.exit_code
        except SystemExit as exc:
            return int(exc.code or 0)
        return 0

    if arguments in (["--help"], ["-h"]):
        from .cli import build_parser

        build_parser().print_help()
        print("\nTencent CN: collector cn --help")
        return 0
    from .cli import main as global_main

    return global_main(arguments)
