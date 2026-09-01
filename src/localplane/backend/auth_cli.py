"""One-shot local master-secret initializer."""

from __future__ import annotations

import argparse
import sys

from localplane.backend.auth import (
    AuthenticationConfigurationError,
    initialize_master_secret,
)
from localplane.backend.config import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="localplane-auth")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("init", help="create the configured master secret once")
    args = parser.parse_args(argv)
    if args.command != "init":
        parser.error("unsupported command")
    try:
        token = initialize_master_secret(Settings.from_env().auth_secret_path)
    except (AuthenticationConfigurationError, FileExistsError, OSError) as exc:
        print(f"localplane-auth: {exc}", file=sys.stderr)
        return 1
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
