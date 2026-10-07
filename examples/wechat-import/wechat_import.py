#!/usr/bin/env python3
"""Import exported WeChat chat history into OpenViking for personal note-taking.

The script reads chat history that you have already exported (TXT, CSV, JSON or
JSONL), normalizes it, and replays it into one OpenViking session per chat:

* your own messages are added as ``role="user"`` without a ``peer_id`` so the
  extracted memories land in your own memory space;
* every other sender gets a stable ``peer_id`` so memories about them land in
  ``viking://user/{you}/peers/{peer_id}/memories``;
* messages are committed in day-aligned batches, so each commit produces an
  archive summary (key points, pending tasks) plus extracted memories.

Import progress is saved in a state file, so re-running the same command after
an interruption or with a newer export only imports new messages.

The script never reads or decrypts WeChat's own databases; it only parses text
exports you already have. Use ``--dry-run`` first to check how they are parsed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import unescape
from itertools import groupby
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

# ---------------------------------------------------------------------------
# Normalized message model
# ---------------------------------------------------------------------------

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")
_MAX_PEER_ID_LEN = 64
_BATCH_API_LIMIT = 100  # server-side max messages per batch request


@dataclass
class ChatMessage:
    time: datetime  # timezone-aware
    sender: str
    content: str
    sender_id: str = ""
    is_self: bool = False

    def fingerprint(self) -> str:
        raw = f"{self.time.isoformat()}\x1f{self.sender}\x1f{self.content}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Time parsing
# ---------------------------------------------------------------------------

_DATETIME_RE = re.compile(
    r"(?P<y>\d{4})\s*(?:[-/.]|年)\s*(?P<mo>\d{1,2})\s*(?:[-/.]|月)\s*(?P<d>\d{1,2})\s*日?"
    r"(?:[ T]+|\s*)(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?"
)
_TIME_TEXT = (
    r"\d{4}\s*(?:[-/.]|年)\s*\d{1,2}\s*(?:[-/.]|月)\s*\d{1,2}\s*日?"
    r"(?:[ T]+|\s*)\d{1,2}:\d{2}(?::\d{2})?"
)


def parse_tz(value: str) -> timezone:
    match = re.fullmatch(r"(?:UTC)?([+-])(\d{1,2}):?(\d{2})?", value.strip(), re.IGNORECASE)
    if not match:
        raise argparse.ArgumentTypeError(f"invalid timezone offset: {value!r} (e.g. +08:00)")
    sign = 1 if match.group(1) == "+" else -1
    offset = timedelta(hours=int(match.group(2)), minutes=int(match.group(3) or 0))
    return timezone(sign * offset)


def parse_time(value: Any, tz: timezone) -> Optional[datetime]:
    """Parse epoch seconds/milliseconds or common date-time strings."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        number = float(value)
        if number > 1e12:  # milliseconds
            number /= 1000
        if number <= 0:
            return None
        return datetime.fromtimestamp(number, tz)
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)
    except ValueError:
        pass
    match = _DATETIME_RE.search(text)
    if not match:
        return None
    parts = {k: int(v) for k, v in match.groupdict().items() if v is not None}
    try:
        return datetime(
            parts["y"],
            parts["mo"],
            parts["d"],
            parts["h"],
            parts["mi"],
            parts.get("s", 0),
            tzinfo=tz,
        )
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Content cleanup
# ---------------------------------------------------------------------------

# Numeric message types used by WeChat's own database (and most export tools).
_TEXT_TYPES = {1}
_APP_TYPES = {49}
_SYSTEM_TYPES = {10000, 10002}
_MEDIA_TYPE_PLACEHOLDERS = {
    3: "[图片]",
    34: "[语音]",
    42: "[名片]",
    43: "[视频]",
    47: "[表情]",
    48: "[位置]",
    50: "[通话]",
}
_SYSTEM_TYPE_NAMES = {"系统消息", "系统通知", "system", "sys"}
_MEDIA_TYPE_NAMES = {
    "图片": "[图片]",
    "image": "[图片]",
    "语音": "[语音]",
    "voice": "[语音]",
    "视频": "[视频]",
    "video": "[视频]",
    "表情": "[表情]",
    "动画表情": "[表情]",
    "emoji": "[表情]",
    "sticker": "[表情]",
    "名片": "[名片]",
    "位置": "[位置]",
    "location": "[位置]",
    "语音通话": "[通话]",
    "视频通话": "[通话]",
}
_PLACEHOLDER_RE = re.compile(
    r"^\[(图片|语音|视频|表情|动画表情|名片|位置|通话|语音通话|视频通话|Photo|Sticker|Voice|Video)\]$",
    re.IGNORECASE,
)
_SYSTEM_PATTERNS = (
    "撤回了一条消息",
    "recalled a message",
    "拍了拍",
    "加入了群聊",
    "加入群聊",
    "移出了群聊",
    "移出群聊",
    "修改群名为",
    "以上是打招呼的内容",
    "现在可以开始聊天了",
    "你已添加了",
    "开启了朋友验证",
    "与群里其他人都不是朋友关系",
)
_XML_TAG_TEMPLATE = r"<{tag}>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</{tag}>"


def _xml_tag(text: str, tag: str) -> str:
    match = re.search(_XML_TAG_TEMPLATE.format(tag=tag), text, re.DOTALL)
    return unescape(match.group(1).strip()) if match else ""


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_app_message(xml: str) -> str:
    """Turn a WeChat app-message XML payload (quote, link, file...) into text."""
    refer = re.search(r"<refermsg>(.*?)</refermsg>", xml, re.DOTALL)
    title = _xml_tag(xml, "title")
    if refer:
        quoted_name = _xml_tag(refer.group(1), "displayname")
        quoted = _xml_tag(refer.group(1), "content")
        if quoted.lstrip().startswith("<"):
            quoted = _xml_tag(quoted, "title") or "[消息]"
        prefix = f"「回复 {quoted_name}：{_clip(quoted, 80)}」" if quoted_name else ""
        return f"{prefix}\n{title}".strip()
    if not title:
        return ""
    description = _xml_tag(xml, "des")
    text = f"[分享] {title}"
    if description and description != title:
        text += f" - {_clip(description, 120)}"
    return text


def clean_content(
    content: str,
    msg_type: Any,
    *,
    keep_system: bool,
    keep_media: bool,
) -> Optional[str]:
    """Return message text worth importing, or None to drop the message."""
    text = (content or "").strip()
    type_key: Any = msg_type
    if isinstance(msg_type, str):
        stripped = msg_type.strip()
        type_key = int(stripped) if stripped.lstrip("-").isdigit() else stripped.lower()

    if type_key in _SYSTEM_TYPES or type_key in _SYSTEM_TYPE_NAMES:
        return (text or None) if keep_system else None
    media = _MEDIA_TYPE_PLACEHOLDERS.get(type_key) or _MEDIA_TYPE_NAMES.get(type_key)
    if media:
        return media if keep_media else None

    if text.startswith("<") and ("<msg" in text[:200] or "<?xml" in text[:200]):
        text = render_app_message(text)
    if not text:
        return None
    if _PLACEHOLDER_RE.match(text):
        return text if keep_media else None
    if not keep_system and any(pattern in text for pattern in _SYSTEM_PATTERNS):
        return None
    return text


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

# Candidate column names per field, in priority order (matched case-insensitively).
FIELD_ALIASES: dict[str, list[str]] = {
    "time": [
        "time",
        "createtime",
        "create_time",
        "strtime",
        "datetime",
        "timestamp",
        "date",
        "msgtime",
        "msg_time",
        "时间",
        "发送时间",
        "日期",
    ],
    "sender": [
        "remark",
        "备注",
        "nickname",
        "nick_name",
        "displayname",
        "display_name",
        "sendername",
        "sender_name",
        "talkername",
        "talker_name",
        "name",
        "发送人",
        "发送者",
        "昵称",
        "sender",
        "from",
    ],
    "sender_id": [
        "sender_id",
        "senderid",
        "sender_wxid",
        "wxid",
        "fromuser",
        "from_user",
        "微信号",
    ],
    "content": [
        "content",
        "strcontent",
        "msg",
        "message",
        "text",
        "body",
        "内容",
        "消息",
        "消息内容",
    ],
    "is_self": [
        "is_self",
        "isself",
        "issender",
        "is_sender",
        "is_send",
        "from_me",
        "fromme",
        "self",
        "是否自己",
        "是否发送",
    ],
    "type": ["type", "msgtype", "msg_type", "type_name", "typename", "类型", "消息类型"],
}
_TRUE_VALUES = {"1", "true", "yes", "y", "是", "self", "me"}


def _resolve_columns(columns: Iterable[str], overrides: dict[str, str]) -> dict[str, list[str]]:
    by_lower = {}
    for column in columns:
        by_lower.setdefault(str(column).strip().lower(), column)
    resolved: dict[str, list[str]] = {}
    for field, aliases in FIELD_ALIASES.items():
        if field in overrides:
            resolved[field] = [c.strip() for c in overrides[field].split("|") if c.strip()]
            continue
        resolved[field] = [by_lower[a] for a in aliases if a in by_lower]
    return resolved


def _first_value(row: dict[str, Any], columns: list[str]) -> Any:
    for column in columns:
        value = row.get(column)
        if value not in (None, ""):
            return value
    return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUE_VALUES


@dataclass
class ParseOptions:
    tz: timezone
    me: set[str]
    keep_system: bool = False
    keep_media: bool = False
    max_chars: int = 4000
    column_map: Optional[dict[str, str]] = None


def records_to_messages(
    records: Iterable[dict[str, Any]], options: ParseOptions, source: str
) -> Iterator[ChatMessage]:
    columns: Optional[dict[str, list[str]]] = None
    skipped_time = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        if columns is None:
            columns = _resolve_columns(record.keys(), options.column_map or {})
            missing = [f for f in ("time", "content") if not columns[f]]
            if missing:
                raise ValueError(
                    f"{source}: cannot find column(s) for {', '.join(missing)}; "
                    f"available columns: {', '.join(map(str, record.keys()))}. "
                    "Use --map, e.g. --map time=StrTime,content=StrContent,sender=NickName"
                )
        when = parse_time(_first_value(record, columns["time"]), options.tz)
        if when is None:
            skipped_time += 1
            continue
        sender = str(_first_value(record, columns["sender"]) or "").strip()
        sender_id = str(_first_value(record, columns["sender_id"]) or "").strip()
        is_self_value = _first_value(record, columns["is_self"])
        is_self = _truthy(is_self_value) if is_self_value is not None else False
        is_self = is_self or (sender in options.me) or (sender_id in options.me)
        content = clean_content(
            str(_first_value(record, columns["content"]) or ""),
            _first_value(record, columns["type"]),
            keep_system=options.keep_system,
            keep_media=options.keep_media,
        )
        if content is None:
            continue
        yield ChatMessage(
            time=when,
            sender=sender or (sender_id if sender_id else ("我" if is_self else "未知")),
            sender_id=sender_id,
            content=_truncate(content, options.max_chars),
            is_self=is_self,
        )
    if skipped_time:
        print(
            f"[warn] {source}: skipped {skipped_time} rows without a parsable time", file=sys.stderr
        )


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…[截断]"


def read_csv(text: str) -> list[dict[str, Any]]:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel
    return list(csv.DictReader(io.StringIO(text), dialect=dialect))


def read_json(text: str) -> list[dict[str, Any]]:
    data = json.loads(text)
    if isinstance(data, dict):
        # Accept {"messages": [...]} / {"data": [...]} style wrappers.
        for key in ("messages", "data", "items", "records", "chat", "list"):
            if isinstance(data.get(key), list):
                return data[key]
        raise ValueError("JSON object has no messages/data/items list")
    if not isinstance(data, list):
        raise ValueError("JSON must be a list of message objects")
    return data


def read_jsonl(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# TXT exports: a header line with time + sender, followed by content lines,
# or a single line "[time] sender: content".
_TXT_SINGLE_LINE = re.compile(
    rf"^\[?(?P<time>{_TIME_TEXT})\]?\s*(?P<sender>[^:：\[\]]{{1,40}}?)\s*[:：]\s*(?P<content>.+)$"
)
_TXT_HEADERS = (
    re.compile(rf"^\[?(?P<time>{_TIME_TEXT})\]?\s+(?P<sender>.{{1,60}}?)\s*[:：]?\s*$"),
    re.compile(rf"^(?P<sender>.{{1,60}}?)\s+[\[(（]?(?P<time>{_TIME_TEXT})[\])）]?\s*[:：]?\s*$"),
)


_TXT_STYLES = (_TXT_SINGLE_LINE, *_TXT_HEADERS)
# Below this many header lines every candidate sender is trusted; above it a
# sender must head at least two messages, so a content line that merely
# contains a date ("会议改到 2024-06-03 10:00") is not mistaken for a header.
_TXT_MIN_HEADERS_FOR_FILTER = 20


def _txt_header(line: str, style: re.Pattern[str], tz: timezone) -> Optional[tuple]:
    match = style.match(line.strip())
    if not match:
        return None
    when = parse_time(match.group("time"), tz)
    if when is None:
        return None
    sender = match.group("sender").strip()
    content = match.group("content") if style is _TXT_SINGLE_LINE else None
    return when, sender, content


def read_txt(text: str, options: ParseOptions, source: str) -> Iterator[ChatMessage]:
    lines = text.splitlines()
    # Pass 1: pick the header style used by this export and count its senders.
    candidates: dict[int, list[Optional[tuple]]] = {}
    for index, style in enumerate(_TXT_STYLES):
        candidates[index] = [_txt_header(line, style, options.tz) for line in lines]
    best = max(candidates, key=lambda i: sum(1 for h in candidates[i] if h))
    headers = candidates[best]
    sender_counts: dict[str, int] = {}
    for header in headers:
        if header:
            sender_counts[header[1]] = sender_counts.get(header[1], 0) + 1
    total = sum(sender_counts.values())
    if total == 0:
        if text.strip():
            raise ValueError(
                f"{source}: no message headers recognized. Expected lines such as "
                "'2024-05-01 12:00:01 张三' or '[2024-05-01 12:00] 张三: 内容'"
            )
        return

    def accepted(sender: str) -> bool:
        return (
            total < _TXT_MIN_HEADERS_FOR_FILTER
            or sender_counts[sender] >= 2
            or sender in options.me
        )

    # Pass 2: split into messages; lines before the first header (titles) are ignored.
    current: Optional[dict[str, Any]] = None

    def flush() -> Optional[ChatMessage]:
        if not current:
            return None
        body = "\n".join(current["lines"]).strip()
        content = clean_content(
            body, None, keep_system=options.keep_system, keep_media=options.keep_media
        )
        if content is None:
            return None
        sender = current["sender"]
        return ChatMessage(
            time=current["time"],
            sender=sender,
            content=_truncate(content, options.max_chars),
            is_self=sender in options.me,
        )

    for line, header in zip(lines, headers, strict=True):
        if header and accepted(header[1]):
            message = flush()
            if message:
                yield message
            when, sender, content = header
            current = {"time": when, "sender": sender, "lines": [content] if content else []}
        elif current is not None:
            current["lines"].append(line)
    message = flush()
    if message:
        yield message


def detect_format(path: Path, text: str) -> str:
    suffix = path.suffix.lower()
    if suffix in (".csv", ".tsv"):
        return "csv"
    if suffix in (".jsonl", ".ndjson"):
        return "jsonl"
    if suffix == ".json":
        return "json"
    if suffix in (".txt", ".md", ".log"):
        return "txt"
    head = text.lstrip()[:1]
    if head == "[":
        return "json"
    if head == "{":
        return "jsonl" if "\n{" in text.strip() else "json"
    return "txt"


def read_text_file(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def load_messages(paths: list[Path], fmt: str, options: ParseOptions) -> list[ChatMessage]:
    messages: list[ChatMessage] = []
    for path in paths:
        text = read_text_file(path)
        kind = detect_format(path, text) if fmt == "auto" else fmt
        if kind == "txt":
            messages.extend(read_txt(text, options, str(path)))
            continue
        reader: Callable[[str], list[dict[str, Any]]] = {
            "csv": read_csv,
            "json": read_json,
            "jsonl": read_jsonl,
        }[kind]
        messages.extend(records_to_messages(reader(text), options, str(path)))
    # Stable sort by time, then drop exact duplicates (overlapping exports).
    messages.sort(key=lambda m: m.time)
    seen: set[str] = set()
    unique = []
    for message in messages:
        fp = message.fingerprint()
        if fp not in seen:
            seen.add(fp)
            unique.append(message)
    return unique


# ---------------------------------------------------------------------------
# Identity helpers
# ---------------------------------------------------------------------------


def slugify_id(text: str, prefix: str) -> str:
    """Return an identifier matching OpenViking's [A-Za-z0-9_.@-] rule."""
    candidate = text.strip()
    if candidate and _SAFE_ID_RE.match(candidate) and candidate not in (".", ".."):
        ident = f"{prefix}{candidate}"
    else:
        ident = f"{prefix}{hashlib.sha1(candidate.encode('utf-8')).hexdigest()[:10]}"
    return ident[:_MAX_PEER_ID_LEN]


class PeerRegistry:
    """Assigns a stable peer_id per sender and remembers it across runs."""

    def __init__(self, known: dict[str, str], explicit: dict[str, str]):
        for name, peer_id in explicit.items():
            if not _SAFE_ID_RE.match(peer_id) or peer_id.count("@") > 1:
                raise ValueError(
                    f"peer map: {peer_id!r} for {name!r} must only use letters, digits, _ . @ -"
                )
        self.explicit = dict(explicit)
        self.mapping = dict(known)

    def peer_id(self, message: ChatMessage) -> str:
        for key in (message.sender_id, message.sender):
            if key and key in self.explicit:
                return self.explicit[key]
        key = message.sender_id or message.sender
        if key not in self.mapping:
            self.mapping[key] = slugify_id(key, "wx-")
        return self.mapping[key]


def to_payload(message: ChatMessage, peers: PeerRegistry, my_name: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "role": "user",
        "created_at": message.time.isoformat(),
    }
    if message.is_self:
        payload["content"] = f"{my_name}: {message.content}"
    else:
        payload["content"] = f"{message.sender}: {message.content}"
        payload["peer_id"] = peers.peer_id(message)
    return payload


# ---------------------------------------------------------------------------
# Commit planning
# ---------------------------------------------------------------------------


def plan_commits(
    messages: list[ChatMessage], min_batch: int, max_batch: int, max_chars: int
) -> list[list[ChatMessage]]:
    """Group messages into commits that end on day boundaries when possible.

    Days are accumulated until a group has at least ``min_batch`` messages; a
    single busy day is split once it exceeds ``max_batch`` messages or
    ``max_chars`` characters.
    """
    groups: list[list[ChatMessage]] = []
    current: list[ChatMessage] = []
    chars = 0
    for _day, day_messages in groupby(messages, key=lambda m: m.time.date()):
        for message in day_messages:
            if current and (len(current) >= max_batch or chars + len(message.content) > max_chars):
                groups.append(current)
                current, chars = [], 0
            current.append(message)
            chars += len(message.content)
        if len(current) >= min_batch:
            groups.append(current)
            current, chars = [], 0
    if current:
        groups.append(current)
    return groups


# ---------------------------------------------------------------------------
# Import state (resume / incremental imports)
# ---------------------------------------------------------------------------


class ImportState:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {
            "version": 1,
            "watermark_time": None,
            "watermark_fingerprints": [],
            "pending_commit": False,
            "imported_messages": 0,
            "peers": {},
            "commits": [],
        }
        if path.exists():
            self.data.update(json.loads(path.read_text(encoding="utf-8")))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def new_messages(self, messages: list[ChatMessage]) -> list[ChatMessage]:
        watermark = self.data.get("watermark_time")
        if not watermark:
            return messages
        mark = datetime.fromisoformat(watermark)
        seen = set(self.data.get("watermark_fingerprints") or [])
        return [
            m for m in messages if m.time > mark or (m.time == mark and m.fingerprint() not in seen)
        ]

    def mark_added(self, messages: list[ChatMessage]) -> None:
        last = messages[-1].time
        previous = self.data.get("watermark_time")
        same_second = (
            set(self.data.get("watermark_fingerprints") or [])
            if previous and datetime.fromisoformat(previous) == last
            else set()
        )
        same_second.update(m.fingerprint() for m in messages if m.time == last)
        self.data["watermark_time"] = last.isoformat()
        self.data["watermark_fingerprints"] = sorted(same_second)
        self.data["imported_messages"] += len(messages)
        self.data["pending_commit"] = True


# ---------------------------------------------------------------------------
# Importer
# ---------------------------------------------------------------------------


def _is_already_exists(error: Exception) -> bool:
    return type(error).__name__ in {"AlreadyExistsError", "ConflictError"}


class Importer:
    def __init__(
        self,
        client: Any,
        session_id: str,
        state: ImportState,
        peers: PeerRegistry,
        *,
        my_name: str,
        event_tags: list[str],
        memory_policy: Optional[dict[str, Any]],
        wait: bool,
        task_timeout: float,
        poll_interval: float = 3.0,
        log: Callable[[str], None] = print,
    ):
        self.client = client
        self.session_id = session_id
        self.state = state
        self.peers = peers
        self.my_name = my_name
        self.event_tags = event_tags
        self.memory_policy = memory_policy
        self.wait = wait
        self.task_timeout = task_timeout
        self.poll_interval = poll_interval
        self.log = log

    def ensure_session(self) -> None:
        # Disable auto-commit: this importer decides where archive boundaries go.
        options: dict[str, Any] = {"auto_commit_policy": None}
        if self.memory_policy:
            options["memory_policy"] = self.memory_policy
        try:
            self.client.create_session(self.session_id, options=options)
            self.log(f"Created session {self.session_id}")
        except Exception as error:  # noqa: BLE001 - SDK raises typed errors by name
            if not _is_already_exists(error):
                raise
            self.client.update_session_config(self.session_id, {"auto_commit_policy": None})
            self.log(f"Session {self.session_id} exists; continuing the import")
            if self.memory_policy:
                self.log(
                    "[note] memory policy is fixed at session creation; "
                    "use a new --session-id to change it"
                )

    def commit(self, label: str) -> None:
        result = (
            self.client.commit_session(
                self.session_id, keep_recent_count=0, options={"event_tags": self.event_tags}
            )
            or {}
        )
        task_id = result.get("task_id")
        record = {
            "label": label,
            "task_id": task_id,
            "archive_uri": result.get("archive_uri"),
            "status": result.get("status"),
            "committed_at": datetime.now(timezone.utc).isoformat(),
        }
        self.state.data["pending_commit"] = False
        self.state.data["commits"].append(record)
        self.state.save()
        if not task_id:
            self.log(f"  commit {label}: nothing to archive ({result.get('status')})")
            return
        self.log(f"  commit {label}: task {task_id}")
        if self.wait:
            record["status"] = self.wait_task(task_id)
            self.state.save()

    def wait_task(self, task_id: str) -> str:
        deadline = time.monotonic() + self.task_timeout
        status = "unknown"
        while time.monotonic() < deadline:
            task = self.client.get_task(task_id) or {}
            status = str(task.get("status", "unknown"))
            if status == "completed":
                self.log("    memory extraction completed")
                return status
            if status in ("failed", "cancelled"):
                raise RuntimeError(
                    f"commit task {task_id} {status}: {task.get('error') or task}. "
                    "Check the server log (model configuration, quota) and re-run; "
                    "already committed batches are not imported again."
                )
            time.sleep(self.poll_interval)
        self.log(
            f"    [warn] task {task_id} still {status} after {self.task_timeout:.0f}s; "
            "continuing (the server keeps processing it in the background)"
        )
        return status

    def run(self, groups: list[list[ChatMessage]], max_commits: Optional[int] = None) -> int:
        self.ensure_session()
        if self.state.data.get("pending_commit"):
            self.log("Committing messages left uncommitted by a previous run")
            self.commit("resume")
        done = 0
        for index, group in enumerate(groups, start=1):
            if max_commits is not None and done >= max_commits:
                self.log(f"Stopped after {done} commit(s) (--max-commits); re-run to continue")
                break
            first, last = group[0].time, group[-1].time
            label = f"{index}/{len(groups)} {first:%Y-%m-%d %H:%M} ~ {last:%Y-%m-%d %H:%M}"
            self.log(f"[{label}] {len(group)} messages")
            for start in range(0, len(group), _BATCH_API_LIMIT):
                chunk = group[start : start + _BATCH_API_LIMIT]
                payloads = [to_payload(m, self.peers, self.my_name) for m in chunk]
                self.client.batch_add_messages(self.session_id, payloads)
                self.state.mark_added(chunk)
                self.state.data["peers"] = self.peers.mapping
                self.state.save()
            self.commit(label)
            done += 1
        return done


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_map(value: Optional[str]) -> dict[str, str]:
    if not value:
        return {}
    result = {}
    for item in value.split(","):
        if "=" not in item:
            raise argparse.ArgumentTypeError(f"--map expects field=column, got {item!r}")
        field, column = item.split("=", 1)
        field = field.strip()
        if field not in FIELD_ALIASES:
            raise argparse.ArgumentTypeError(
                f"--map field must be one of {', '.join(FIELD_ALIASES)}; got {field!r}"
            )
        result[field] = column.strip()
    return result


def _parse_day(value: Optional[str]) -> Optional[date]:
    return date.fromisoformat(value) if value else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import exported WeChat chat history into OpenViking (one chat per run).",
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="exported file(s) of ONE chat; several files are merged",
    )
    parser.add_argument("--chat", help="chat name (default: first file name)")
    parser.add_argument("--session-id", help="OpenViking session id (default: derived from --chat)")
    parser.add_argument(
        "--me",
        action="append",
        default=[],
        help="your nickname/remark/wxid as it appears in the export (repeatable)",
    )
    parser.add_argument("--my-name", help="how your messages are labelled (default: first --me)")
    parser.add_argument("--format", default="auto", choices=["auto", "txt", "csv", "json", "jsonl"])
    parser.add_argument(
        "--map",
        type=_parse_map,
        default={},
        help="column overrides, e.g. time=StrTime,sender=NickName,content=StrContent",
    )
    parser.add_argument(
        "--tz",
        type=parse_tz,
        default=parse_tz("+08:00"),
        help="timezone of times without an offset (default +08:00)",
    )
    parser.add_argument("--since", type=_parse_day, help="only messages on/after YYYY-MM-DD")
    parser.add_argument("--until", type=_parse_day, help="only messages on/before YYYY-MM-DD")
    parser.add_argument(
        "--keep-system", action="store_true", help="keep system notices (recalls, pats, joins)"
    )
    parser.add_argument(
        "--keep-media", action="store_true", help="keep [图片]/[语音]/[表情] placeholders"
    )
    parser.add_argument("--max-message-chars", type=int, default=4000)
    parser.add_argument(
        "--min-batch",
        type=int,
        default=30,
        help="accumulate days until a commit has this many messages (default 30)",
    )
    parser.add_argument(
        "--max-batch",
        type=int,
        default=300,
        help="split a commit above this many messages (default 300)",
    )
    parser.add_argument(
        "--max-commit-chars",
        type=int,
        default=40000,
        help="split a commit above this many characters (default 40000)",
    )
    parser.add_argument(
        "--max-commits", type=int, help="stop after N commits (try a small import first)"
    )
    parser.add_argument(
        "--peer-map",
        type=Path,
        help='JSON file mapping sender name or wxid to peer_id, e.g. {"张三": "zhangsan"}',
    )
    parser.add_argument(
        "--no-peer-memory", action="store_true", help="do not write memories about other people"
    )
    parser.add_argument(
        "--memory-types", help="comma-separated memory types to extract, e.g. events,preferences"
    )
    parser.add_argument(
        "--no-wait", action="store_true", help="do not wait for each commit's memory extraction"
    )
    parser.add_argument("--task-timeout", type=float, default=1800)
    parser.add_argument(
        "--state-dir", type=Path, default=Path.home() / ".openviking" / "wechat-import"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="parse and show the plan without contacting the server",
    )
    parser.add_argument(
        "--export-normalized",
        type=Path,
        help="write parsed messages to this JSONL file for inspection",
    )
    parser.add_argument("--url", help="server URL (default: OPENVIKING_URL or ovcli.conf)")
    parser.add_argument(
        "--api-key", help="user API key (default: OPENVIKING_API_KEY or ovcli.conf)"
    )
    return parser


def print_summary(
    messages: list[ChatMessage],
    groups: list[list[ChatMessage]],
    peers: PeerRegistry,
    already_imported: int,
) -> None:
    senders: dict[tuple[str, bool], int] = {}
    for message in messages:
        key = (message.sender, message.is_self)
        senders[key] = senders.get(key, 0) + 1
    print(
        f"Messages to import: {len(messages)}"
        + (f" (skipped {already_imported} already imported)" if already_imported else "")
    )
    if messages:
        print(f"Time range: {messages[0].time:%Y-%m-%d %H:%M} ~ {messages[-1].time:%Y-%m-%d %H:%M}")
    print(f"Planned commits: {len(groups)}")
    print("Senders:")
    for (sender, is_self), count in sorted(senders.items(), key=lambda item: -item[1]):
        if is_self:
            target = "self (your memory space)"
        else:
            sample = next(m for m in messages if m.sender == sender and not m.is_self)
            target = f"peer_id={peers.peer_id(sample)}"
        print(f"  {sender:<20} {count:>6}  -> {target}")
    if messages and not any(m.is_self for m in messages):
        print(
            "[warn] none of the messages is marked as yours; pass --me <your nickname> "
            "so your own messages go to your memory space",
            file=sys.stderr,
        )


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    chat = args.chat or args.inputs[0].stem
    session_id = args.session_id or slugify_id(chat, "wechat-")
    if not _SAFE_ID_RE.match(session_id):
        print(
            f"--session-id must only use letters, digits, _ . @ -: {session_id!r}", file=sys.stderr
        )
        return 2

    options = ParseOptions(
        tz=args.tz,
        me=set(args.me),
        keep_system=args.keep_system,
        keep_media=args.keep_media,
        max_chars=args.max_message_chars,
        column_map=args.map,
    )
    try:
        messages = load_messages(args.inputs, args.format, options)
    except (ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"Failed to parse input: {error}", file=sys.stderr)
        return 2
    if args.since:
        messages = [m for m in messages if m.time.date() >= args.since]
    if args.until:
        messages = [m for m in messages if m.time.date() <= args.until]

    my_name = args.my_name or (args.me[0] if args.me else "我")
    state = ImportState(args.state_dir / f"{session_id}.json")
    explicit = json.loads(args.peer_map.read_text(encoding="utf-8")) if args.peer_map else {}
    peers = PeerRegistry(state.data.get("peers") or {}, explicit)
    pending = state.new_messages(messages)
    groups = plan_commits(pending, args.min_batch, args.max_batch, args.max_commit_chars)

    print(f"Chat: {chat}  ->  session {session_id}")
    print_summary(pending, groups, peers, len(messages) - len(pending))

    if args.export_normalized:
        with args.export_normalized.open("w", encoding="utf-8") as handle:
            for message in pending:
                handle.write(
                    json.dumps(to_payload(message, peers, my_name), ensure_ascii=False) + "\n"
                )
        print(f"Wrote normalized messages to {args.export_normalized}")
    if args.dry_run:
        for message in pending[:5]:
            print("  sample:", json.dumps(to_payload(message, peers, my_name), ensure_ascii=False))
        return 0
    if not groups and not state.data.get("pending_commit"):
        print("Nothing new to import.")
        return 0

    try:
        from openviking_sdk import SyncHTTPClient
    except ImportError:
        print(
            "openviking_sdk is not installed: pip install openviking-sdk (or openviking)",
            file=sys.stderr,
        )
        return 2

    memory_policy: dict[str, Any] = {}
    if args.no_peer_memory:
        memory_policy["peer"] = {"enabled": False}
    if args.memory_types:
        memory_policy["memory_types"] = [
            t.strip() for t in args.memory_types.split(",") if t.strip()
        ]
    client = SyncHTTPClient(url=args.url, api_key=args.api_key)
    importer = Importer(
        client,
        session_id,
        state,
        peers,
        my_name=my_name,
        event_tags=["source=wechat", f"chat={session_id}"],
        memory_policy=memory_policy or None,
        wait=not args.no_wait,
        task_timeout=args.task_timeout,
    )
    try:
        client.initialize()
        done = importer.run(groups, max_commits=args.max_commits)
    except KeyboardInterrupt:
        print("\nInterrupted; progress is saved, re-run the same command to continue.")
        return 130
    except RuntimeError as error:
        print(f"[error] {error}", file=sys.stderr)
        print("Progress is saved; fix the cause and re-run the same command.", file=sys.stderr)
        return 1
    finally:
        client.close()
    print(f"Done: {done} commit(s). Browse results with:")
    print(f"  ov ls viking://~/sessions/{session_id}/history/")
    print("  ov ls viking://~/memories/  and  ov ls viking://~/peers/")
    print('  ov find "<question>"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
