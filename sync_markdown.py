"""CLI wrapper for GitHub documentation sync."""

import argparse
import sys
from pathlib import Path

from github_markdown_sync import (
    get_latest_commit,
    load_config,
    parse_repo,
    setup_logging,
    sync_repo,
)


def main() -> int:
    """Entry point for CLI.

    Returns:
        Exit code (0 = success, 1 = error).
    """
    parser = argparse.ArgumentParser(description="Sync GitHub documentation for RAG.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--config",
        metavar="FILE",
        help="YAML config file for multi-repo sync",
    )
    group.add_argument(
        "--repo",
        metavar="OWNER/REPO",
        help="Single repository to query (prints latest commit)",
    )
    args = parser.parse_args()

    if args.config:
        try:
            repos, logging_cfg = load_config(Path(args.config))
        except FileNotFoundError:
            print(f"Error: config file not found: {args.config}", file=sys.stderr)
            return 1
        except (KeyError, TypeError) as exc:
            print(f"Error: invalid config: {exc}", file=sys.stderr)
            return 1

        setup_logging(logging_cfg)
        errors = 0
        for cfg in repos:
            try:
                sync_repo(cfg)
            except Exception as exc:  # pylint: disable=broad-except
                print(f"Error syncing {cfg.name}: {exc}", file=sys.stderr)
                errors += 1
        return 1 if errors else 0

    # --repo: one-shot commit lookup
    try:
        owner, repo = parse_repo(args.repo)
        sha, dt = get_latest_commit(owner, repo)
        print(f"{repo}: {sha} ({dt.isoformat()})")
        return 0
    except (ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
