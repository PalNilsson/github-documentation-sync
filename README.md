# GitHub Documentation Sync

A lightweight, configuration-driven system for synchronizing documentation from GitHub repositories and preparing it for Retrieval-Augmented Generation (RAG).

This tool:
- Monitors one or more repositories
- Downloads only changed documentation files
- Supports both Markdown (`.md`) and reStructuredText (`.rst`)
- Normalizes documents into a unified, RAG-friendly format
- Maintains state to avoid redundant work

---

## 🚀 Features

### Efficient Sync
- Uses Git commit SHA caching to detect changes
- Compares Git trees to download only modified files
- Deletes locally removed files to stay in sync

### Multi-Repository Support
- YAML-based configuration
- Per-repo settings (branch, filters, frequency)

### Format Support
- Markdown (`.md`)
- reStructuredText (`.rst`)

### RAG-Ready Normalization
- Converts `.md` and `.rst` into a unified Markdown-like format
- Adds metadata headers for traceability

### Logging
- Structured logging
- Optional rotating log files
- Works well in cron jobs and pipelines

---

## 📦 Project Structure

```
github_markdown_sync.py   # Core library
sync_markdown.py          # CLI wrapper
repos.yaml                # User configuration
```

---

## ⚙️ Installation

### Requirements

- Python 3.10+
- `requests`
- `PyYAML`

Install dependencies:

```bash
pip install requests pyyaml
```

---

## 🧾 Configuration

Create a YAML configuration file:

```yaml
logging:
  level: INFO
  file: ./logs/github_doc_sync.log

repos:
  - name: eic/tutorial-analysis
    destination: ./data/tutorial-analysis/raw
    normalized_destination: ./data/tutorial-analysis/normalized
    within_hours: 24
    branch: main
    include_patterns:
      - "*.md"
      - "*.rst"
    exclude_patterns:
      - "drafts/*"
    normalize_for_rag: true
```

### Configuration Fields

#### Global

| Field | Description |
|------|-------------|
| logging.level | Log level (DEBUG, INFO, WARNING, ERROR) |
| logging.file | Optional log file |

#### Per Repository

| Field | Description |
|------|-------------|
| name | `owner/repo` |
| destination | Where raw files are stored |
| normalized_destination | Where normalized files are written |
| within_hours | Only sync if repo updated recently |
| branch | Optional branch (default = repo default) |
| include_patterns | File patterns to include |
| exclude_patterns | File patterns to exclude |
| normalize_for_rag | Enable normalization step |

---

## ▶️ Usage

Run the sync:

```bash
python sync_markdown.py --config repos.yaml
```

Optional:

```bash
python sync_markdown.py --config repos.yaml --log-level DEBUG
```

---

## 🔄 Sync Workflow

For each repository:

1. Fetch latest commit SHA
2. Compare with cached SHA
3. If unchanged → skip
4. If changed:
   - Compare Git trees
   - Download changed files
   - Delete removed files
   - Normalize documents
   - Update state file

---

## 🧠 Normalization for RAG

Each document is converted into a consistent format.

### Example Output

```text
---
source_repo: eic/tutorial-analysis
source_path: docs/example.rst
source_type: rst
source_commit_sha: abc123
---

# Example Title

Normalized content...
```

### What Gets Normalized

- Line endings
- Excess whitespace
- Markdown cleanup
- RST → Markdown-like conversion
  - Headings
  - Links
  - Code blocks

---

## 📁 Output Structure

```
data/
  tutorial-analysis/
    raw/
      docs/...
    normalized/
      docs/...
    .sync_state.json
```

---

## 🗂 State File

Each repo stores state in:

```
.sync_state.json
```

Example:

```json
{
  "last_commit_sha": "abc123",
  "last_sync_time": "2026-04-03T12:34:56Z",
  "files_downloaded": 12
}
```

---

## 🧪 Integration with RAG

This tool is designed to feed RAG pipelines:

- Use `normalized/` directory as input corpus
- Metadata headers enable traceability
- Incremental updates reduce re-indexing cost

---

## ⏱ Scheduling

Run daily via cron:

```bash
0 2 * * * /usr/bin/python /path/to/sync_markdown.py --config /path/to/repos.yaml
```

---

## 🔮 Future Improvements

- Parallel repo syncing
- Incremental embedding updates
- Direct vector DB integration
- HTML / PDF support

---

## 🧾 License

MIT License

---

## 🤝 Contributing

Contributions welcome — especially improvements to normalization and RAG integration.

