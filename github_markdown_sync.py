"""Sync and normalize GitHub documentation."""

from __future__ import annotations

import fnmatch
import importlib
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

STATE_FILENAME = ".sync_state.json"


@dataclass(frozen=True)
class LoggingConfig:
    """Logging settings loaded from YAML."""

    level: str = "INFO"
    file: str | None = None
    format: str = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    datefmt: str = "%Y-%m-%d %H:%M:%S"
    max_bytes: int = 5_000_000
    backup_count: int = 3


# pylint: disable=too-many-instance-attributes
@dataclass(frozen=True)
class RepoConfig:
    """Configuration for one monitored repository."""

    name: str
    destination: Path
    normalized_destination: Path | None = None
    within_hours: int = 24
    branch: str | None = None
    include_patterns: list[str] = field(
        default_factory=lambda: ["*.md", "*.rst"]
    )
    exclude_patterns: list[str] = field(default_factory=list)
    normalize_for_rag: bool = True

    @staticmethod
    def from_dict(data: dict[str, Any], base_dir: Path) -> "RepoConfig":
        """Build a repository config from a dictionary."""
        if "name" not in data:
            raise ValueError("Each repo entry must contain a 'name' field.")
        if "destination" not in data:
            raise ValueError(
                "Each repo entry must contain a 'destination' field."
            )

        destination = Path(data["destination"])
        if not destination.is_absolute():
            destination = (base_dir / destination).resolve()

        normalized_destination = data.get("normalized_destination")
        if normalized_destination is not None:
            normalized_destination = Path(normalized_destination)
            if not normalized_destination.is_absolute():
                normalized_destination = (
                    base_dir / normalized_destination
                ).resolve()

        include_patterns = data.get("include_patterns", ["*.md", "*.rst"])
        exclude_patterns = data.get("exclude_patterns", [])
        if isinstance(include_patterns, str):
            include_patterns = [include_patterns]
        if isinstance(exclude_patterns, str):
            exclude_patterns = [exclude_patterns]

        return RepoConfig(
            name=str(data["name"]),
            destination=destination,
            normalized_destination=normalized_destination,
            within_hours=int(data.get("within_hours", 24)),
            branch=data.get("branch"),
            include_patterns=[str(pattern) for pattern in include_patterns],
            exclude_patterns=[str(pattern) for pattern in exclude_patterns],
            normalize_for_rag=bool(data.get("normalize_for_rag", True)),
        )


@dataclass(frozen=True)
class AppConfig:
    """Top-level application configuration."""

    logging: LoggingConfig = field(default_factory=LoggingConfig)
    repos: list[RepoConfig] = field(default_factory=list)

    @staticmethod
    def from_yaml(data: dict[str, Any], config_path: Path) -> "AppConfig":
        """Build an app config from parsed YAML data."""
        base_dir = config_path.parent.resolve()
        logging_cfg = data.get("logging", {}) or {}
        log_cfg = LoggingConfig(
            level=str(logging_cfg.get("level", "INFO")),
            file=logging_cfg.get("file"),
            format=str(
                logging_cfg.get(
                    "format",
                    "%(asctime)s %(levelname)s %(name)s: %(message)s",
                )
            ),
            datefmt=str(logging_cfg.get("datefmt", "%Y-%m-%d %H:%M:%S")),
            max_bytes=int(logging_cfg.get("max_bytes", 5_000_000)),
            backup_count=int(logging_cfg.get("backup_count", 3)),
        )

        repos_data = data.get("repos", [])
        if not isinstance(repos_data, list):
            raise ValueError("'repos' must be a list in the YAML config.")

        repos = [
            RepoConfig.from_dict(item, base_dir=base_dir)
            for item in repos_data
        ]
        return AppConfig(logging=log_cfg, repos=repos)


@dataclass(frozen=True)
class SyncState:
    """Persistent state for the latest successful sync."""

    last_commit_sha: str | None = None
    last_sync_time: str | None = None
    files_downloaded: int = 0


# pylint: disable=too-many-instance-attributes
@dataclass(frozen=True)
class SyncResult:
    """Summary of one sync run."""

    repo: str
    latest_commit_sha: str
    latest_commit_time: datetime
    downloaded_files: list[str]
    deleted_files: list[str]
    normalized_files: list[str]
    skipped: bool
    reason: str | None = None


@dataclass(frozen=True)
class RepoContext:
    """Resolved repository paths and names."""

    owner: str
    repo: str
    repo_name: str
    destination: Path
    normalized_destination: Path


def configure_logging(config: LoggingConfig) -> None:
    """Configure root logging."""
    level = getattr(logging, config.level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(fmt=config.format, datefmt=config.datefmt)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    if config.file:
        log_path = Path(config.file).expanduser().resolve()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=config.max_bytes,
            backupCount=config.backup_count,
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


def load_yaml_config(config_path: str | Path) -> AppConfig:
    """Load an application config from YAML."""
    yaml_module = importlib.import_module("yaml")
    path = Path(config_path).expanduser().resolve()
    data = yaml_module.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(
            "YAML config must contain a mapping at the top level."
        )
    return AppConfig.from_yaml(data, config_path=path)


def github_headers(token: str | None = None) -> dict[str, str]:
    """Build standard GitHub API headers."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "github-doc-sync",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def parse_repo(repo_arg: str) -> tuple[str, str]:
    """Parse owner/repo."""
    if "/" not in repo_arg:
        raise ValueError("Repository must be in the form owner/repo")
    owner, repo = repo_arg.split("/", 1)
    if not owner or not repo:
        raise ValueError("Repository must be in the form owner/repo")
    return owner, repo


def state_file_path(destination: Path) -> Path:
    """Return the JSON state path."""
    return destination / STATE_FILENAME


def load_state(destination: Path) -> SyncState:
    """Load sync state from disk."""
    path = state_file_path(destination)
    if not path.exists():
        return SyncState()

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return SyncState()

    return SyncState(
        last_commit_sha=data.get("last_commit_sha"),
        last_sync_time=data.get("last_sync_time"),
        files_downloaded=int(data.get("files_downloaded", 0)),
    )


def save_state(destination: Path, state: SyncState) -> None:
    """Save sync state to disk."""
    payload = asdict(state)
    state_file_path(destination).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def get_latest_commit(
    owner: str,
    repo: str,
    token: str | None = None,
    branch: str | None = None,
) -> tuple[str, datetime]:
    """Return the latest commit SHA and time."""
    headers = github_headers(token)
    with requests.Session() as session:
        if branch is None:
            meta_url = f"https://api.github.com/repos/{owner}/{repo}"
            meta = session.get(meta_url, headers=headers, timeout=30)
            meta.raise_for_status()
            branch = meta.json()["default_branch"]

        url = f"https://api.github.com/repos/{owner}/{repo}/commits"
        response = session.get(
            url,
            headers=headers,
            params={"sha": branch, "per_page": 1},
            timeout=30,
        )
        response.raise_for_status()
        commits = response.json()

    if not commits:
        raise RuntimeError("No commits found in repository.")

    commit = commits[0]
    commit_time = datetime.fromisoformat(
        commit["commit"]["committer"]["date"].replace("Z", "+00:00")
    )
    return commit["sha"], commit_time


def was_recent(commit_time: datetime, within_hours: int) -> bool:
    """Return True if the commit time is recent enough."""
    return (
        datetime.now(timezone.utc) - commit_time
    ) <= timedelta(hours=within_hours)


def get_recursive_tree(
    owner: str,
    repo: str,
    ref: str,
    token: str | None = None,
) -> dict[str, str]:
    """Return a path-to-blob-SHA mapping for a Git tree."""
    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{ref}"
    with requests.Session() as session:
        response = session.get(
            url,
            headers=github_headers(token),
            params={"recursive": "1"},
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()

    if data.get("truncated"):
        raise RuntimeError(
            "Git tree response was truncated; repository may be too large."
        )

    tree: dict[str, str] = {}
    for item in data.get("tree", []):
        if item.get("type") == "blob" and "path" in item and "sha" in item:
            tree[item["path"]] = item["sha"]
    return tree


def path_matches(
    path: str,
    include_patterns: list[str],
    exclude_patterns: list[str],
) -> bool:
    """Check whether a path matches include and exclude patterns."""
    included = any(
        fnmatch.fnmatch(path, pattern) for pattern in include_patterns
    )
    excluded = any(
        fnmatch.fnmatch(path, pattern) for pattern in exclude_patterns
    )
    return included and not excluded


def select_tracked_files(
    tree: dict[str, str],
    include_patterns: list[str],
    exclude_patterns: list[str],
) -> dict[str, str]:
    """Filter a tree down to tracked files."""
    return {
        path: sha
        for path, sha in tree.items()
        if path_matches(path, include_patterns, exclude_patterns)
    }


def normalize_document(
    content: str,
    source_path: str,
    repo_name: str,
    commit_sha: str,
) -> str:
    """Normalize a source document into a canonical text form."""
    suffix = Path(source_path).suffix.lower()
    if suffix == ".rst":
        body = normalize_rst_to_markdown(content)
        source_type = "rst"
    else:
        body = normalize_markdown(content)
        source_type = "md"

    header = [
        "---",
        f"source_repo: {repo_name}",
        f"source_path: {source_path}",
        f"source_type: {source_type}",
        f"source_commit_sha: {commit_sha}",
        "---",
        "",
    ]
    return "\n".join(header) + body.strip() + "\n"


def normalize_markdown(content: str) -> str:
    """Normalize Markdown content conservatively."""
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    text = text.lstrip("\ufeff")

    lines = text.split("\n")
    if len(lines) >= 3 and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                text = "\n".join(lines[index + 1:])
                break

    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def normalize_rst_to_markdown(content: str) -> str:
    """Convert common RST structures to Markdown-like text."""
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    text = text.lstrip("\ufeff")
    text = rst_inline_to_markdown(text)
    lines = text.split("\n")
    out: list[str] = []
    index = 0

    while index < len(lines):
        line = lines[index]
        next_line = lines[index + 1] if index + 1 < len(lines) else ""

        heading_level = rst_heading_level(line, next_line)
        if heading_level is not None:
            out.append("#" * heading_level + " " + line.strip())
            index += 2
            continue

        if line.rstrip().endswith("::"):
            prefix = line.rstrip()[:-2].rstrip()
            if prefix:
                out.append(prefix)
            code_block, consumed = consume_rst_literal_block(lines, index + 1)
            if code_block:
                out.append("")
                out.append("```text")
                out.extend(code_block)
                out.append("```")
                out.append("")
                index += 1 + consumed
                continue

        out.append(line)
        index += 1

    normalized = "\n".join(out)
    normalized = re.sub(r"[ \t]+\n", "\n", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip() + "\n"


def rst_heading_level(line: str, next_line: str) -> int | None:
    """Detect common RST section heading patterns."""
    stripped = line.strip()
    underline = next_line.strip()

    if not stripped or not underline:
        return None
    if len(underline) < len(stripped):
        return None
    if not re.fullmatch(r'(=+|-+|~+|\^+|"+|\*+|\++|#+)', underline):
        return None

    levels = {
        "=": 1,
        "-": 2,
        "~": 3,
        "^": 4,
        '"': 5,
        "*": 5,
        "+": 5,
        "#": 5,
    }
    return levels.get(underline[0], 5)


def consume_rst_literal_block(
    lines: list[str],
    start_index: int,
) -> tuple[list[str], int]:
    """Consume an indented literal block after ``::``."""
    block: list[str] = []
    consumed = 0

    while start_index + consumed < len(lines):
        if lines[start_index + consumed].strip() != "":
            break
        consumed += 1

    indent: int | None = None
    index = start_index + consumed

    while index < len(lines):
        line = lines[index]
        if line.strip() == "":
            if indent is None:
                consumed += 1
                index += 1
                continue
            block.append("")
            consumed += 1
            index += 1
            continue

        current_indent = len(line) - len(line.lstrip(" "))
        if indent is None:
            if current_indent == 0:
                break
            indent = current_indent

        if current_indent < indent:
            break

        block.append(line[indent:])
        consumed += 1
        index += 1

    return block, consumed


def rst_inline_to_markdown(text: str) -> str:
    """Convert common inline RST markup to Markdown-like text."""
    text = re.sub(r":\w+:`([^`]+)`", r"\1", text)
    text = re.sub(r"`([^`<]+?)\s*<([^>]+)>`_", r"[\1](\2)", text)
    text = re.sub(r"`([^`]+)`_", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"**\1**", text)
    return text


def download_file(
    owner: str,
    repo: str,
    ref: str,
    path: str,
    destination: Path,
) -> Path:
    """Download one file from GitHub raw content."""
    url = (
        f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/"
        f"{quote(path, safe='/')}"
    )
    out_path = destination / path
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with requests.Session() as session:
        response = session.get(
            url,
            headers={"User-Agent": "github-doc-sync"},
            timeout=60,
        )
        response.raise_for_status()
        out_path.write_bytes(response.content)

    return out_path


def write_normalized_document(
    repo_name: str,
    commit_sha: str,
    path: str,
    raw_text: str,
    normalized_destination: Path,
) -> Path:
    """Write a normalized document."""
    normalized_text = normalize_document(
        content=raw_text,
        source_path=path,
        repo_name=repo_name,
        commit_sha=commit_sha,
    )
    out_path = (normalized_destination / path).with_suffix(".md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(normalized_text, encoding="utf-8")
    return out_path


def delete_local_file(destination: Path, path: str) -> bool:
    """Delete a local file if it exists."""
    out_path = destination / path
    if out_path.exists() and out_path.is_file():
        out_path.unlink()
        return True
    return False


def _repo_context(repo_cfg: RepoConfig) -> RepoContext:
    """Resolve repository settings."""
    owner, repo_name = parse_repo(repo_cfg.name)
    destination = repo_cfg.destination
    normalized_destination = repo_cfg.normalized_destination
    if normalized_destination is None:
        normalized_destination = destination / "normalized"
    return RepoContext(
        owner=owner,
        repo=repo_name,
        repo_name=repo_cfg.name,
        destination=destination,
        normalized_destination=normalized_destination,
    )


def _tree_diff(
    old_tree: dict[str, str],
    new_tree: dict[str, str],
) -> tuple[list[str], list[str]]:
    """Return changed and deleted tracked file paths."""
    old_paths = set(old_tree)
    new_paths = set(new_tree)
    added_or_changed = sorted(
        path
        for path in new_paths
        if path not in old_tree or old_tree[path] != new_tree[path]
    )
    deleted = sorted(old_paths - new_paths)
    return added_or_changed, deleted


def _sync_downloads(
    context: RepoContext,
    commit_sha: str,
    paths: list[str],
    normalize_for_rag: bool,
    logger: logging.Logger,
) -> tuple[list[str], list[str]]:
    """Download changed files and normalize them."""
    downloaded_files: list[str] = []
    normalized_files: list[str] = []

    for path in paths:
        raw_output = download_file(
            context.owner,
            context.repo,
            commit_sha,
            path,
            context.destination,
        )
        downloaded_files.append(path)
        logger.debug(
            "Downloaded %s:%s -> %s",
            context.repo_name,
            path,
            raw_output,
        )

        if normalize_for_rag:
            raw_text = raw_output.read_text(encoding="utf-8", errors="replace")
            normalized_output = write_normalized_document(
                repo_name=context.repo_name,
                commit_sha=commit_sha,
                path=path,
                raw_text=raw_text,
                normalized_destination=context.normalized_destination,
            )
            relative_path = normalized_output.relative_to(
                context.normalized_destination
            )
            normalized_files.append(str(relative_path))
            logger.debug(
                "Normalized %s:%s -> %s",
                context.repo_name,
                path,
                normalized_output,
            )

    return downloaded_files, normalized_files


def _sync_deletions(
    context: RepoContext,
    paths: list[str],
    normalize_for_rag: bool,
    logger: logging.Logger,
) -> list[str]:
    """Delete removed files from raw and normalized destinations."""
    deleted_files: list[str] = []

    for path in paths:
        if delete_local_file(context.destination, path):
            deleted_files.append(path)
            logger.debug("Deleted local file %s:%s", context.repo_name, path)

        if normalize_for_rag:
            normalized_path = (
                context.normalized_destination / path
            ).with_suffix(".md")
            if normalized_path.exists() and normalized_path.is_file():
                normalized_path.unlink()
                logger.debug(
                    "Deleted normalized file %s:%s",
                    context.repo_name,
                    normalized_path,
                )

    return deleted_files


def sync_one_repo(
    repo_cfg: RepoConfig,
    token: str | None = None,
    logger: logging.Logger | None = None,
) -> SyncResult:
    """Synchronize one repository."""
    logger = logger or logging.getLogger(__name__)
    context = _repo_context(repo_cfg)
    context.destination.mkdir(parents=True, exist_ok=True)
    if repo_cfg.normalize_for_rag:
        context.normalized_destination.mkdir(parents=True, exist_ok=True)

    state = load_state(context.destination)
    latest_sha, latest_commit_time = get_latest_commit(
        owner=context.owner,
        repo=context.repo,
        token=token,
        branch=repo_cfg.branch,
    )

    logger.info(
        "Repo %s latest SHA=%s time=%s",
        context.repo_name,
        latest_sha,
        latest_commit_time.isoformat(),
    )

    if not was_recent(latest_commit_time, repo_cfg.within_hours):
        reason = (
            "Repository was not updated within the last "
            f"{repo_cfg.within_hours} hours."
        )
        logger.info("Skipping %s: %s", context.repo_name, reason)
        return SyncResult(
            repo=context.repo_name,
            latest_commit_sha=latest_sha,
            latest_commit_time=latest_commit_time,
            downloaded_files=[],
            deleted_files=[],
            normalized_files=[],
            skipped=True,
            reason=reason,
        )

    if state.last_commit_sha == latest_sha:
        reason = "Latest commit SHA matches cached state."
        logger.info("Skipping %s: %s", context.repo_name, reason)
        return SyncResult(
            repo=context.repo_name,
            latest_commit_sha=latest_sha,
            latest_commit_time=latest_commit_time,
            downloaded_files=[],
            deleted_files=[],
            normalized_files=[],
            skipped=True,
            reason=reason,
        )

    if state.last_commit_sha is None:
        old_tree: dict[str, str] = {}
        logger.info(
            "No prior state for %s; performing initial sync.",
            context.repo_name,
        )
    else:
        old_tree = select_tracked_files(
            get_recursive_tree(
                context.owner,
                context.repo,
                state.last_commit_sha,
                token,
            ),
            repo_cfg.include_patterns,
            repo_cfg.exclude_patterns,
        )

    new_tree = select_tracked_files(
        get_recursive_tree(context.owner, context.repo, latest_sha, token),
        repo_cfg.include_patterns,
        repo_cfg.exclude_patterns,
    )

    added_or_changed, deleted = _tree_diff(old_tree, new_tree)
    logger.info(
        "Repo %s: %d tracked files, %d changed/added, %d deleted",
        context.repo_name,
        len(new_tree),
        len(added_or_changed),
        len(deleted),
    )

    downloaded_files, normalized_files = _sync_downloads(
        context=context,
        commit_sha=latest_sha,
        paths=added_or_changed,
        normalize_for_rag=repo_cfg.normalize_for_rag,
        logger=logger,
    )
    deleted_files = _sync_deletions(
        context=context,
        paths=deleted,
        normalize_for_rag=repo_cfg.normalize_for_rag,
        logger=logger,
    )

    new_state = SyncState(
        last_commit_sha=latest_sha,
        last_sync_time=datetime.now(timezone.utc).isoformat(),
        files_downloaded=len(downloaded_files),
    )
    save_state(context.destination, new_state)

    logger.info(
        "Finished %s: downloaded=%d normalized=%d deleted=%d cache updated",
        context.repo_name,
        len(downloaded_files),
        len(normalized_files),
        len(deleted_files),
    )

    return SyncResult(
        repo=context.repo_name,
        latest_commit_sha=latest_sha,
        latest_commit_time=latest_commit_time,
        downloaded_files=downloaded_files,
        deleted_files=deleted_files,
        normalized_files=normalized_files,
        skipped=False,
        reason=None,
    )


def sync_from_config(
    config: AppConfig,
    token: str | None = None,
) -> list[SyncResult]:
    """Synchronize all repositories in a config."""
    configure_logging(config.logging)
    logger = logging.getLogger(__name__)
    results: list[SyncResult] = []

    if not config.repos:
        logger.warning("No repositories configured.")
        return results

    for repo_cfg in config.repos:
        try:
            results.append(sync_one_repo(repo_cfg, token=token, logger=logger))
        except (OSError, RuntimeError, requests.RequestException, ValueError):
            logger.exception("Failed to sync repository %s", repo_cfg.name)
            raise

    return results
