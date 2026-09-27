"""The `trader` command. Its subcommands (run, report, smoke) arrive in M3 and M4 (docs/HANDOFF.md §18)."""

from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="trader",
        description="Daily pre-market trading agent for one Alpaca account.",
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
