"""Command-line wrapper for GitHub documentation synchronization."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from github_markdown_sync import load_yaml_config, sync_from_config


def main() -> int:
    """Run the CLI entry point.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(
        description="Synchronize Markdown and RST documentation from GitHub repositories."
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Path to a YAML config file describing repositories to monitor.",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("GITHUB_TOKEN"),
        help="Optional GitHub token. Defaults to GITHUB_TOKEN if set.",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help="Override the log level from the YAML config, for example INFO or DEBUG.",
    )
    args = parser.parse_args()

    try:
        config = load_yaml_config(args.config)

        if args.log_level:
            config = type(config)(
                logging=type(config.logging)(
                    level=args.log_level,
                    file=config.logging.file,
                    format=config.logging.format,
                    datefmt=config.logging.datefmt,
                    max_bytes=config.logging.max_bytes,
                    backup_count=config.logging.backup_count,
                ),
                repos=config.repos,
            )

        results = sync_from_config(config, token=args.token)

        for result in results:
            if result.skipped:
                print(f"{result.repo}: skipped ({result.reason})")
            else:
                print(
                    f"{result.repo}: downloaded={len(result.downloaded_files)} "
                    f"normalized={len(result.normalized_files)} "
                    f"deleted={len(result.deleted_files)} sha={result.latest_commit_sha}"
                )

        return 0

    except Exception as exc:
        logging.getLogger(__name__).exception("Sync failed")
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
