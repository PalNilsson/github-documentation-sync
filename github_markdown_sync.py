
"""GitHub documentation sync module."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import requests


@dataclass
class SyncState:
    """Represents persisted sync state.

    Attributes:
        last_commit_sha: Last synced commit SHA.
        last_sync_time: Timestamp of last sync (ISO format).
        files_downloaded: Number of files downloaded in last sync.
    """

    last_commit_sha: Optional[str] = None
    last_sync_time: Optional[str] = None
    files_downloaded: int = 0


def parse_repo(repo: str) -> Tuple[str, str]:
    """Parse repository string.

    Args:
        repo: Repository string in 'owner/repo' format.

    Returns:
        Tuple of (owner, repo).

    Raises:
        ValueError: If format is invalid.
    """
    if "/" not in repo:
        raise ValueError("Repository must be owner/repo")
    owner, name = repo.split("/", 1)
    return owner, name


def get_latest_commit(owner: str, repo: str) -> Tuple[str, datetime]:
    """Fetch latest commit from GitHub.

    Args:
        owner: Repository owner.
        repo: Repository name.

    Returns:
        Tuple of (commit_sha, commit_datetime).
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/commits"
    r = requests.get(url, params={"per_page": 1}, timeout=30)
    r.raise_for_status()
    data = r.json()[0]
    sha = data["sha"]
    dt = datetime.fromisoformat(
        data["commit"]["committer"]["date"].replace("Z", "+00:00")
    )
    return sha, dt


def load_state(path: Path) -> SyncState:
    """Load sync state from disk.

    Args:
        path: Path to state file.

    Returns:
        SyncState instance.
    """
    if not path.exists():
        return SyncState()
    return SyncState(**json.loads(path.read_text()))


def save_state(path: Path, state: SyncState) -> None:
    """Save sync state to disk.

    Args:
        path: Path to state file.
        state: SyncState to persist.
    """
    path.write_text(json.dumps(state.__dict__, indent=2))


def normalize_text(content: str) -> str:
    """Normalize document text.

    Args:
        content: Raw text.

    Returns:
        Cleaned text.
    """
    return content.strip() + "\n"
