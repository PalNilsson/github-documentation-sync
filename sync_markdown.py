"""CLI wrapper for GitHub documentation sync."""

import argparse
import sys

from github_markdown_sync import get_latest_commit, parse_repo


def main() -> int:
    """Entry point for CLI.

    Returns:
        Exit code.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    args = parser.parse_args()

    try:
        owner, repo = parse_repo(args.repo)
        sha, dt = get_latest_commit(owner, repo)
        print(f"{repo}: {sha} ({dt.isoformat()})")
        return 0
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
