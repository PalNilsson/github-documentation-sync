# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Linting and type checking
flake8 *.py
pyright *.py
pylint *.py

# Multi-repo sync via config file
python sync_markdown.py --config repos.yaml

# One-shot: print latest commit for a single repo
python sync_markdown.py --repo owner/repo
```

There is no test suite or build step — this is a pure Python project with no compilation.

## Architecture

Two-file library/CLI split:

- **`github_markdown_sync.py`** — core library: GitHub API calls, state persistence, file download, normalization, config loading
- **`sync_markdown.py`** — thin CLI wrapper; `--config` for full sync, `--repo` for a quick one-shot commit lookup

### Key data flow (`sync_repo`)

1. Load YAML config (`repos.example.yaml` as template) via `load_config()`
2. For each `RepoConfig`: call `get_latest_commit()` via GitHub REST API
3. Apply `within_hours` check — skip if latest commit is older than configured threshold
4. Compare SHA against cached state in `{destination}/.sync_state.json` — skip if unchanged
5. Fetch full file tree via `GET /repos/{owner}/{repo}/git/trees/{sha}?recursive=1`
6. Filter blobs with `_matches_patterns()` (include/exclude glob patterns)
7. Download each matching file from `raw.githubusercontent.com`
8. If `normalize_for_rag: true`, write a second copy to `normalized_destination` with YAML frontmatter (`source_repo`, `source_path`, `source_type`, `source_commit_sha`) and basic RST→MD conversion
9. Persist new `SyncState` (dataclass: `last_commit_sha`, `last_sync_time`, `files_downloaded`)

### State management

Each repo's destination directory gets a `.sync_state.json` file. `load_state()` tolerates missing files, malformed JSON, and unknown fields (schema evolution) — all cases return an empty `SyncState` so the first run always does a full sync.

### Normalization

`normalize_text()` prepends a YAML frontmatter block and, for `.rst` files, runs `_rst_to_md()` which handles section underlines, `.. code-block::`, admonitions, and `:ref:`/backtick links.

## Dependencies

- Python 3.10+
- `requests` — GitHub API
- `PyYAML` — config parsing

No `requirements.txt` or `pyproject.toml` — install manually: `pip install requests pyyaml`.
