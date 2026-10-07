"""Tests for wechat_import.py (run: python -m pytest examples/wechat-import -p no:cacheprovider)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import wechat_import as wi  # noqa: E402

CST = timezone(timedelta(hours=8))


def opts(**kwargs):
    defaults = {"tz": CST, "me": {"小明"}}
    defaults.update(kwargs)
    return wi.ParseOptions(**defaults)


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- parsing


def test_parse_time_variants():
    assert wi.parse_time("2024-05-01 12:00:01", CST) == datetime(2024, 5, 1, 12, 0, 1, tzinfo=CST)
    assert wi.parse_time("2024/5/1 9:05", CST) == datetime(2024, 5, 1, 9, 5, tzinfo=CST)
    assert wi.parse_time("2024年5月1日 09:05", CST) == datetime(2024, 5, 1, 9, 5, tzinfo=CST)
    assert wi.parse_time(1714536001, CST) == datetime(2024, 5, 1, 12, 0, 1, tzinfo=CST)
    assert wi.parse_time("1714536001000", CST) == datetime(2024, 5, 1, 12, 0, 1, tzinfo=CST)
    assert wi.parse_time("2024-05-01T04:00:01Z", CST) == datetime(
        2024, 5, 1, 4, 0, 1, tzinfo=timezone.utc
    )
    assert wi.parse_time("not a time", CST) is None


def test_txt_header_then_content_lines(tmp_path):
    path = write(
        tmp_path,
        "family.txt",
        "微信聊天记录\n\n"
        "2024-05-01 12:00:01 张三\n周五一起吃饭吧\n\n"
        "2024-05-01 12:01:00 小明\n好啊\n第二行\n\n"
        "2024-05-01 12:02:00 李四\n[图片]\n\n"
        "2024-05-01 12:03:00 李四\n李四撤回了一条消息\n",
    )
    messages = wi.load_messages([path], "auto", opts())
    assert [(m.sender, m.content, m.is_self) for m in messages] == [
        ("张三", "周五一起吃饭吧", False),
        ("小明", "好啊\n第二行", True),
    ]


def test_txt_name_then_time_header(tmp_path):
    path = write(tmp_path, "dm.txt", "张三 2024/5/1 12:00\n你好\n小明 2024/5/1 12:01\n在呢\n")
    messages = wi.load_messages([path], "txt", opts())
    assert [(m.sender, m.content) for m in messages] == [("张三", "你好"), ("小明", "在呢")]


def test_txt_single_line_style(tmp_path):
    path = write(
        tmp_path,
        "g.txt",
        "[2024-05-01 12:00:01] 张三: 明天 10:00 开会\n[2024-05-01 12:00:30] 小明：收到\n",
    )
    messages = wi.load_messages([path], "auto", opts())
    assert [(m.sender, m.content) for m in messages] == [
        ("张三", "明天 10:00 开会"),
        ("小明", "收到"),
    ]


def test_txt_content_line_with_date_is_not_a_header(tmp_path):
    blocks = []
    for i in range(12):
        blocks.append(f"2024-05-01 12:{i:02d}:00 张三\n消息{i}")
        blocks.append(f"2024-05-01 12:{i:02d}:30 小明\n回复{i}")
    blocks.append("2024-05-01 13:00:00 张三\n通知：\n会议改到 2024-06-03 10:00\n别迟到")
    path = write(tmp_path, "g.txt", "\n".join(blocks))
    messages = wi.load_messages([path], "txt", opts())
    assert messages[-1].sender == "张三"
    assert messages[-1].content == "通知：\n会议改到 2024-06-03 10:00\n别迟到"


def test_txt_without_headers_raises(tmp_path):
    path = write(tmp_path, "bad.txt", "just some text\nwithout headers\n")
    with pytest.raises(ValueError, match="no message headers"):
        wi.load_messages([path], "txt", opts())


def test_csv_with_wechat_style_columns(tmp_path):
    quote_xml = (
        "<msg><appmsg><title>好的，我带投影仪</title><type>57</type>"
        "<refermsg><displayname>张三</displayname><content>谁带投影仪？</content></refermsg>"
        "</appmsg></msg>"
    )
    rows = [
        ["localId", "Type", "IsSender", "CreateTime", "StrContent", "NickName", "Sender"],
        ["1", "1", "0", "1714536001", "周五聚餐？", "张三", "wxid_zhangsan"],
        ["2", "3", "0", "1714536002", "", "张三", "wxid_zhangsan"],
        ["3", "49", "1", "1714536003", quote_xml, "", "wxid_me"],
        ["4", "10000", "0", "1714536004", "张三拍了拍我", "", ""],
    ]
    import csv

    path = tmp_path / "chat.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)
    messages = wi.load_messages([path], "auto", opts(me=set()))
    assert len(messages) == 2
    assert messages[0].sender == "张三" and not messages[0].is_self
    assert messages[1].is_self
    assert messages[1].sender == "wxid_me"
    assert messages[1].content == "「回复 张三：谁带投影仪？」\n好的，我带投影仪"


def test_csv_missing_columns_suggests_map(tmp_path):
    path = write(tmp_path, "x.csv", "a,b\n1,2\n")
    with pytest.raises(ValueError, match="--map"):
        wi.load_messages([path], "csv", opts())


def test_json_with_column_map_and_dedup(tmp_path):
    record = {"ts": "2024-05-01 12:00:00", "who": "张三", "txt": "hi"}
    path = write(tmp_path, "c.json", json.dumps({"messages": [record, record]}))
    options = opts(column_map={"time": "ts", "sender": "who", "content": "txt"})
    messages = wi.load_messages([path], "auto", options)
    assert [(m.sender, m.content) for m in messages] == [("张三", "hi")]


def test_share_link_rendering():
    xml = "<msg><appmsg><title>年度报告</title><des>2024 总结</des></appmsg></msg>"
    assert wi.clean_content(xml, 49, keep_system=False, keep_media=False) == (
        "[分享] 年度报告 - 2024 总结"
    )


# --------------------------------------------------------------------------- identities


def test_peer_ids_are_safe_and_stable():
    registry = wi.PeerRegistry({}, {"李四": "lisi"})
    zhang = wi.ChatMessage(datetime.now(CST), "张三", "x")
    wx = wi.ChatMessage(datetime.now(CST), "王五", "x", sender_id="wxid_wangwu")
    li = wi.ChatMessage(datetime.now(CST), "李四", "x", sender_id="wxid_lisi")
    first = registry.peer_id(zhang)
    assert first.startswith("wx-") and wi._SAFE_ID_RE.match(first)
    assert registry.peer_id(zhang) == first
    assert registry.peer_id(wx) == "wx-wxid_wangwu"
    assert registry.peer_id(li) == "lisi"
    with pytest.raises(ValueError):
        wi.PeerRegistry({}, {"张三": "张三"})


def test_payload_marks_self_without_peer():
    registry = wi.PeerRegistry({}, {})
    me = wi.ChatMessage(datetime(2024, 5, 1, tzinfo=CST), "小明", "好的", is_self=True)
    other = wi.ChatMessage(datetime(2024, 5, 1, tzinfo=CST), "张三", "周五见")
    assert wi.to_payload(me, registry, "小明") == {
        "role": "user",
        "created_at": "2024-05-01T00:00:00+08:00",
        "content": "小明: 好的",
    }
    payload = wi.to_payload(other, registry, "小明")
    assert payload["content"] == "张三: 周五见" and payload["peer_id"].startswith("wx-")


# --------------------------------------------------------------------------- planning


def _msgs(spec):
    """spec: list of (day, count)."""
    out = []
    for day, count in spec:
        for i in range(count):
            when = datetime(2024, 5, day, 8, tzinfo=CST) + timedelta(seconds=i)
            out.append(wi.ChatMessage(when, "张三", "x"))
    return out


def test_plan_accumulates_small_days_and_splits_big_ones():
    groups = wi.plan_commits(_msgs([(1, 10), (2, 25), (3, 70)]), 30, 50, 10**6)
    assert [len(g) for g in groups] == [35, 50, 20]
    assert {m.time.day for m in groups[0]} == {1, 2}


def test_plan_splits_on_characters():
    messages = _msgs([(1, 4)])
    for m in messages:
        m.content = "x" * 10
    assert [len(g) for g in wi.plan_commits(messages, 1, 100, 25)] == [2, 2]


# --------------------------------------------------------------------------- importer


class AlreadyExistsError(Exception):
    pass


class FakeClient:
    def __init__(self, exists=False, task_status="completed"):
        self.exists = exists
        self.task_status = task_status
        self.calls = []
        self.batches = []

    def create_session(self, session_id, options=None):
        self.calls.append(("create", session_id, options))
        if self.exists:
            raise AlreadyExistsError(session_id)
        return {"session_id": session_id}

    def update_session_config(self, session_id, options=None):
        self.calls.append(("update", session_id, options))
        return {}

    def batch_add_messages(self, session_id, messages):
        assert len(messages) <= 100
        self.batches.append(messages)
        self.calls.append(("batch", session_id, len(messages)))
        return {"added": len(messages)}

    def commit_session(self, session_id, keep_recent_count=0, options=None):
        n = sum(1 for c in self.calls if c[0] == "commit") + 1
        self.calls.append(("commit", session_id, options))
        return {"status": "accepted", "task_id": f"t{n}", "archive_uri": f"a{n}"}

    def get_task(self, task_id):
        return {"status": self.task_status, "error": "boom"}


def make_importer(client, tmp_path, **kwargs):
    state = wi.ImportState(tmp_path / "state.json")
    defaults = {
        "my_name": "小明",
        "event_tags": ["source=wechat", "chat=wechat-test"],
        "memory_policy": None,
        "wait": True,
        "task_timeout": 5,
        "poll_interval": 0,
        "log": lambda _msg: None,
    }
    defaults.update(kwargs)
    return wi.Importer(client, "wechat-test", state, wi.PeerRegistry({}, {}), **defaults), state


def test_importer_batches_commits_and_disables_auto_commit(tmp_path):
    client = FakeClient()
    importer, state = make_importer(client, tmp_path)
    groups = wi.plan_commits(_msgs([(1, 150), (2, 40)]), 30, 300, 10**6)
    assert importer.run(groups) == 2
    assert client.calls[0] == ("create", "wechat-test", {"auto_commit_policy": None})
    kinds = [c[0] for c in client.calls]
    assert kinds == ["create", "batch", "batch", "commit", "batch", "commit"]
    commit_options = [c[2] for c in client.calls if c[0] == "commit"]
    assert commit_options[0] == {"event_tags": ["source=wechat", "chat=wechat-test"]}
    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert saved["imported_messages"] == 190
    assert saved["pending_commit"] is False
    assert [c["status"] for c in saved["commits"]] == ["completed", "completed"]


def test_rerun_skips_imported_messages(tmp_path):
    messages = _msgs([(1, 40)])
    importer, state = make_importer(FakeClient(), tmp_path)
    importer.run(wi.plan_commits(messages, 30, 300, 10**6))
    state2 = wi.ImportState(tmp_path / "state.json")
    newer = _msgs([(2, 5)])
    assert state2.new_messages(messages + newer) == newer


def test_existing_session_is_reused_and_pending_commit_resumed(tmp_path):
    client = FakeClient(exists=True)
    importer, state = make_importer(client, tmp_path)
    state.data["pending_commit"] = True
    importer.run([])
    kinds = [c[0] for c in client.calls]
    assert kinds == ["create", "update", "commit"]
    assert client.calls[1][2] == {"auto_commit_policy": None}


def test_failed_task_stops_import(tmp_path):
    importer, _ = make_importer(FakeClient(task_status="failed"), tmp_path)
    with pytest.raises(RuntimeError, match="failed"):
        importer.run(wi.plan_commits(_msgs([(1, 40), (2, 40)]), 30, 300, 10**6))


def test_max_commits_limits_run(tmp_path):
    client = FakeClient()
    importer, _ = make_importer(client, tmp_path)
    assert importer.run(wi.plan_commits(_msgs([(1, 40), (2, 40)]), 30, 300, 10**6), 1) == 1


def test_cli_dry_run(tmp_path, capsys):
    path = write(
        tmp_path, "群聊.txt", "2024-05-01 12:00:01 张三\n你好\n2024-05-01 12:01 小明\n在\n"
    )
    code = wi.main([str(path), "--me", "小明", "--dry-run", "--state-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "Messages to import: 2" in out
    assert "self (your memory space)" in out
    assert "session wechat-" in out
