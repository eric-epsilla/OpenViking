# Import WeChat chat history

English / [中文](README_CN.md)

Imports WeChat chat history **that you have already exported** (TXT, CSV, JSON or
JSONL) into OpenViking, producing per-period chat summaries with pending tasks and
long-term memories about you and each contact. Intended for personal note-taking.
The script only parses text exports; it never reads or decrypts WeChat databases.

## What it does

| WeChat | OpenViking |
|---|---|
| One group or 1:1 chat | One session (default `wechat-<hash of chat name>`) |
| Your messages | `role="user"`, no `peer_id` → memories in `viking://~/memories/` |
| Other people's messages | `role="user"` + stable `peer_id` → `viking://~/peers/{peer_id}/memories/` |
| Send time | `created_at`, so relative dates in event memories resolve correctly |
| A batch of days | One `commit`: archive summary (`.overview.md`) plus memory extraction |

Commits are day-aligned: quiet days accumulate until `--min-batch` (30) messages,
busy days split at `--max-batch` / `--max-commit-chars`. Auto-commit is disabled
for the session so the importer controls archive boundaries. Media placeholders
and system notices are dropped by default; quote replies and shared links in XML
are rendered as text. Progress is stored in
`~/.openviking/wechat-import/<session_id>.json`, so re-running resumes after an
interruption and later exports only add new messages.

## Usage

Requires a running server with VLM and embedding models, `pip install openviking-sdk`,
and a user key via `--url`/`--api-key`, `OPENVIKING_URL`/`OPENVIKING_API_KEY` or
`~/.openviking/ovcli.conf`.

```sh
# 1. Check parsing (no server needed)
python examples/wechat-import/wechat_import.py family.txt --chat family --me Ming --dry-run
# 2. Try a couple of commits
python examples/wechat-import/wechat_import.py family.txt --chat family --me Ming --max-commits 2
# 3. Import everything (re-running skips what is already imported)
python examples/wechat-import/wechat_import.py family.txt --chat family --me Ming
```

TXT exports may use `2024-05-01 12:00:01 Name` + content lines, `Name 2024/5/1 12:01`
+ content lines, or `[2024-05-01 12:02] Name: content`. CSV/JSON columns are detected
from common names (`CreateTime`, `StrTime`, `NickName`, `StrContent`, `IsSender`,
`Type`, ...); override them with `--map time=...,sender=...,content=...,is_self=...`.

See [README_CN.md](README_CN.md) for all options, how to inspect the results, and
privacy notes.

## Tests

```sh
pytest examples/wechat-import -p no:cacheprovider -o addopts=""
```
