"""GitHub documentation sync module."""

from __future__ import annotations

import fnmatch
import json
import logging
import logging.handlers
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import yaml

logger = logging.getLogger(__name__)


@dataclass
class SyncState:
    """Persisted sync state for one repository.

    Attributes:
        last_commit_sha: Last synced commit SHA.
        last_sync_time: Timestamp of last sync (ISO format).
        files_downloaded: Number of files downloaded in last sync.
    """

    last_commit_sha: Optional[str] = None
    last_sync_time: Optional[str] = None
    files_downloaded: int = 0


@dataclass
class RepoConfig:
    """Configuration for a single repository.

    Attributes:
        name: Repository in 'owner/repo' format.
        destination: Directory for raw downloaded files.
        normalized_destination: Directory for RAG-normalized files.
        within_hours: Skip if latest commit is older than this many hours.
        branch: Branch to sync (None = repo default).
        include_patterns: Glob patterns; only matching files are synced.
        exclude_patterns: Glob patterns; matching files are excluded.
        normalize_for_rag: Prepend metadata frontmatter and convert RST→MD.
    """

    name: str
    destination: str
    normalized_destination: Optional[str] = None
    within_hours: Optional[int] = None
    branch: Optional[str] = None
    include_patterns: List[str] = field(default_factory=list)
    exclude_patterns: List[str] = field(default_factory=list)
    normalize_for_rag: bool = False


def parse_repo(repo: str) -> Tuple[str, str]:
    """Parse 'owner/repo' string.

    Args:
        repo: Repository string in 'owner/repo' format.

    Returns:
        Tuple of (owner, repo_name).

    Raises:
        ValueError: If format is invalid or either segment is empty.
    """
    parts = repo.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Repository must be 'owner/repo', got: {repo!r}")
    return parts[0], parts[1]


def get_latest_commit(
    owner: str, repo: str, branch: Optional[str] = None
) -> Tuple[str, datetime]:
    """Fetch latest commit SHA and datetime from GitHub API.

    Args:
        owner: Repository owner.
        repo: Repository name.
        branch: Optional branch/ref to query.

    Returns:
        Tuple of (commit_sha, commit_datetime).

    Raises:
        RuntimeError: On HTTP errors, network failures, or empty repository.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/commits"
    params: Dict[str, Any] = {"per_page": 1}
    if branch:
        params["sha"] = branch
    try:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(
            f"GitHub API error for {owner}/{repo}: "
            f"{exc.response.status_code} {exc.response.reason}"
        ) from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"Network error fetching {owner}/{repo}: {exc}") from exc

    commits = r.json()
    if not commits:
        raise RuntimeError(f"Repository {owner}/{repo} has no commits")

    data = commits[0]
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
        SyncState instance, or empty SyncState on any read/parse failure.
    """
    if not path.exists():
        return SyncState()
    try:
        raw = json.loads(path.read_text())
        known_fields = set(SyncState.__dataclass_fields__)
        return SyncState(**{k: v for k, v in raw.items() if k in known_fields})
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("Could not load state from %s: %s — starting fresh", path, exc)
        return SyncState()


def save_state(path: Path, state: SyncState) -> None:
    """Persist sync state to disk.

    Args:
        path: Path to state file.
        state: SyncState to persist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.__dict__, indent=2))


def normalize_text(
    content: str,
    *,
    source_repo: str,
    source_path: str,
    commit_sha: str,
) -> str:
    """Normalize document text for RAG, prepending metadata frontmatter.

    Args:
        content: Raw file text.
        source_repo: Repository in 'owner/repo' format.
        source_path: File path within the repository.
        commit_sha: Commit SHA the file was fetched from.

    Returns:
        Normalized text with YAML frontmatter header.
    """
    ext = Path(source_path).suffix.lower().lstrip(".")
    source_type = ext if ext in ("md", "rst") else "unknown"

    header = (
        f"---\n"
        f"source_repo: {source_repo}\n"
        f"source_path: {source_path}\n"
        f"source_type: {source_type}\n"
        f"source_commit_sha: {commit_sha}\n"
        f"---\n\n"
    )

    body = content.strip()
    if source_type == "rst":
        body = _rst_to_md(body)

    return header + body + "\n"


def _rst_to_md(text: str) -> str:
    """Basic RST → Markdown conversion for common patterns."""
    underline_chars: Dict[str, str] = {
        "=": "#", "-": "##", "~": "###", "^": "####", '"': "#####", "'": "######",
    }
    lines = text.splitlines()
    result: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]

        # Section headings: text followed by underline of =, -, ~, etc.
        if i + 1 < len(lines):
            nxt = lines[i + 1]
            if (
                nxt
                and line.strip()
                and len(nxt) >= len(line.strip())
                and re.match(r'^[=\-~^"\'`#*+<>]+$', nxt)
            ):
                prefix = underline_chars.get(nxt[0], "#")
                result.append(f"{prefix} {line.strip()}")
                i += 2
                continue

        # .. code-block:: lang
        code_match = re.match(r"\.\.\s+code-block::\s*(.*)", line)
        if code_match:
            lang = code_match.group(1).strip()
            result.append(f"```{lang}")
            i += 1
            if i < len(lines) and not lines[i].strip():
                i += 1
            while i < len(lines) and (
                lines[i].startswith("   ") or lines[i].startswith("\t") or not lines[i].strip()
            ):
                result.append(re.sub(r"^   |\t", "", lines[i], count=1))
                i += 1
            result.append("```")
            continue

        # .. note:: / .. warning:: / .. tip::
        admonition = re.match(r"\.\.\s+(note|warning|tip|important)::(.*)", line, re.IGNORECASE)
        if admonition:
            kind = admonition.group(1).capitalize()
            extra = admonition.group(2).strip()
            result.append(f"> **{kind}:** {extra}")
            i += 1
            continue

        # :ref:`text <target>` and `text <url>`_
        line = re.sub(r":ref:`([^`<]+)\s+<([^>]+)>`", r"[\1](\2)", line)
        line = re.sub(r"`([^`]+)\s+<([^>]+)>`_", r"[\1](\2)", line)

        result.append(line)
        i += 1

    return "\n".join(result)


def _matches_patterns(
    path: str, include: List[str], exclude: List[str]
) -> bool:
    """Return True if path matches include patterns and not exclude patterns."""
    if include and not any(fnmatch.fnmatch(path, pat) for pat in include):
        return False
    if any(fnmatch.fnmatch(path, pat) for pat in exclude):
        return False
    return True


def _get_tree(owner: str, repo: str, sha: str) -> List[Dict[str, Any]]:
    """Fetch the full recursive blob list for a commit tree.

    Args:
        owner: Repository owner.
        repo: Repository name.
        sha: Commit or tree SHA.

    Returns:
        List of blob entries (dicts with 'path', 'sha', 'type', etc.).

    Raises:
        RuntimeError: On HTTP or network errors.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{sha}"
    try:
        r = requests.get(url, params={"recursive": "1"}, timeout=30)
        r.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(
            f"GitHub API error fetching tree: {exc.response.status_code}"
        ) from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"Network error fetching tree: {exc}") from exc

    data = r.json()
    if data.get("truncated"):
        logger.warning("Tree for %s/%s is truncated (very large repo)", owner, repo)
    return [item for item in data.get("tree", []) if item.get("type") == "blob"]


def _download_file(owner: str, repo: str, path: str, sha: str) -> bytes:
    """Download raw file content from GitHub.

    Args:
        owner: Repository owner.
        repo: Repository name.
        path: File path within the repository.
        sha: Commit SHA to fetch from.

    Returns:
        Raw file bytes.

    Raises:
        RuntimeError: On HTTP or network errors.
    """
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{sha}/{path}"
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(
            f"Failed to download {path}: {exc.response.status_code}"
        ) from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"Network error downloading {path}: {exc}") from exc
    return r.content


def sync_repo(cfg: RepoConfig) -> None:
    """Run a full sync cycle for one repository.

    Args:
        cfg: Repository configuration.

    Raises:
        ValueError: If repo name is invalid.
        RuntimeError: On GitHub API or network failures.
    """
    owner, repo_name = parse_repo(cfg.name)
    dest = Path(cfg.destination)
    state_path = dest / ".sync_state.json"
    state = load_state(state_path)

    sha, commit_dt = get_latest_commit(owner, repo_name, cfg.branch)

    if cfg.within_hours is not None:
        now = datetime.now(tz=timezone.utc)
        age_hours = (now - commit_dt).total_seconds() / 3600
        if age_hours > cfg.within_hours:
            logger.info(
                "%s: latest commit is %.1fh old (limit %dh) — skipping",
                cfg.name, age_hours, cfg.within_hours,
            )
            return

    if sha == state.last_commit_sha:
        logger.info("%s: already up-to-date at %s", cfg.name, sha[:12])
        return

    logger.info(
        "%s: syncing %s → %s",
        cfg.name,
        (state.last_commit_sha or "none")[:12],
        sha[:12],
    )

    blobs = _get_tree(owner, repo_name, sha)
    matching = [
        b for b in blobs
        if _matches_patterns(b["path"], cfg.include_patterns, cfg.exclude_patterns)
    ]
    logger.info("%s: %d matching files to download", cfg.name, len(matching))

    dest.mkdir(parents=True, exist_ok=True)
    norm_dest = Path(cfg.normalized_destination) if cfg.normalized_destination else None
    if norm_dest:
        norm_dest.mkdir(parents=True, exist_ok=True)

    downloaded = 0
    for blob in matching:
        file_path = blob["path"]
        try:
            content_bytes = _download_file(owner, repo_name, file_path, sha)
        except RuntimeError as exc:
            logger.warning("Skipping %s: %s", file_path, exc)
            continue

        out_path = dest / file_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(content_bytes)
        downloaded += 1

        if cfg.normalize_for_rag and norm_dest:
            try:
                text = content_bytes.decode("utf-8")
            except UnicodeDecodeError:
                logger.warning("Skipping normalization of %s (not UTF-8)", file_path)
                continue
            normalized = normalize_text(
                text,
                source_repo=cfg.name,
                source_path=file_path,
                commit_sha=sha,
            )
            norm_path = norm_dest / file_path
            norm_path.parent.mkdir(parents=True, exist_ok=True)
            norm_path.write_text(normalized, encoding="utf-8")

    new_state = SyncState(
        last_commit_sha=sha,
        last_sync_time=datetime.now(tz=timezone.utc).isoformat(),
        files_downloaded=downloaded,
    )
    save_state(state_path, new_state)
    logger.info("%s: done, %d files saved", cfg.name, downloaded)


def load_config(path: Path) -> Tuple[List[RepoConfig], Dict[str, Any]]:
    """Load YAML configuration file.

    Args:
        path: Path to YAML config file.

    Returns:
        Tuple of (list of RepoConfig, logging config dict).

    Raises:
        FileNotFoundError: If config file does not exist.
        KeyError: If a required field is missing from a repo entry.
    """
    raw = yaml.safe_load(path.read_text())
    logging_cfg: Dict[str, Any] = raw.get("logging", {})
    repos = [
        RepoConfig(
            name=entry["name"],
            destination=entry["destination"],
            normalized_destination=entry.get("normalized_destination"),
            within_hours=entry.get("within_hours"),
            branch=entry.get("branch"),
            include_patterns=entry.get("include_patterns", []),
            exclude_patterns=entry.get("exclude_patterns", []),
            normalize_for_rag=entry.get("normalize_for_rag", False),
        )
        for entry in raw.get("repos", [])
    ]
    return repos, logging_cfg


def setup_logging(cfg: Dict[str, Any]) -> None:
    """Configure logging from a config dict.

    Args:
        cfg: Logging configuration dict (keys: level, file, format, datefmt,
             max_bytes, backup_count).
    """
    level = getattr(logging, cfg.get("level", "INFO").upper(), logging.INFO)
    fmt = cfg.get("format", "%(asctime)s %(levelname)s %(name)s: %(message)s")
    datefmt = cfg.get("datefmt", "%Y-%m-%d %H:%M:%S")

    handlers: List[logging.Handler] = [logging.StreamHandler()]

    log_file = cfg.get("file")
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_path,
                maxBytes=cfg.get("max_bytes", 5_000_000),
                backupCount=cfg.get("backup_count", 3),
            )
        )

    logging.basicConfig(level=level, format=fmt, datefmt=datefmt, handlers=handlers)
