# 导入微信聊天记录

[English](README.md) / 中文

把**你已经导出的**微信聊天记录（TXT / CSV / JSON / JSONL）导入 OpenViking，自动生成每段时间的聊天纪要、待办，并按人沉淀长期记忆。适合个人整理信息。

脚本只负责解析和导入文本文件，**不会读取或解密微信本地数据库**。导出方式由你自己决定。

## 它做了什么

| 微信 | OpenViking |
|---|---|
| 一个群 / 一个 1:1 对话 | 一个 session（默认 `wechat-<聊天名哈希>`） |
| 你自己发的消息 | `role="user"`，不带 `peer_id` → 记忆写入你自己的空间 `viking://~/memories/` |
| 别人发的消息 | `role="user"` + 稳定的 `peer_id` → 关于此人的记忆写入 `viking://~/peers/{peer_id}/memories/` |
| 消息发送时间 | `created_at`（事件记忆里"明天""下周一"会据此换算成具体日期） |
| 每一批对话 | 一次 `commit`：归档并生成摘要（`.overview.md`，含待办），再提取记忆 |

- 按天切分 commit：消息少的日子会合并，直到达到 `--min-batch`（默认 30 条）；单日消息过多时按 `--max-batch` / `--max-commit-chars` 拆分。
- 会关闭该 session 的自动 commit，由脚本决定归档边界。
- 默认过滤噪音：`[图片]`、`[表情]`、`[语音]` 等占位符，以及撤回、拍一拍、入群等系统提示。引用回复和分享链接（XML 格式）会转成可读文本。
- 进度保存在 `~/.openviking/wechat-import/<session_id>.json`。中断后重跑同一命令会从断点继续；之后用更新的导出文件重跑，只会导入新增的消息。

## 准备

1. 一个运行中的 OpenViking 服务端，配置好 VLM 和 embedding 模型（见仓库 README 的 Quick start）。
2. 安装 SDK：`pip install openviking-sdk`（安装了 `openviking` 的话已经自带）。
3. 服务端地址和 **user key** 可以用以下任一方式提供：`--url` / `--api-key`、环境变量 `OPENVIKING_URL` / `OPENVIKING_API_KEY`、`~/.openviking/ovcli.conf`。

## 支持的格式

**TXT**：每条消息以"时间 + 发送人"开头，下面是正文（可以多行）。以下三种写法都能自动识别：

```text
2024-05-01 12:00:01 张三
周五一起吃饭吧？

张三 2024/5/1 12:01
好啊

[2024-05-01 12:02] 李四: 我带投影仪
```

**CSV / JSON / JSONL**：常见列名会自动识别，例如：

- 时间：`time` / `CreateTime` / `StrTime` / `时间`，支持秒或毫秒时间戳，以及各种日期字符串
- 发送人：`Remark` / `NickName` / `sender` / `发送人`
- 内容：`content` / `StrContent` / `msg` / `内容`
- 是否本人：`IsSender` / `is_self`
- 消息类型：`Type` / `type_name`

识别不了时，用 `--map` 指定列名：

```sh
--map time=StrTime,sender=NickName,content=StrContent,is_self=IsSender
```

JSON 可以是消息数组，也可以是 `{"messages": [...]}` 这类包装。

## 使用

**第一步：先 dry-run**，确认解析结果。会打印条数、时间范围、计划的 commit 数、每个发送人对应的 peer_id 和前 5 条样例，不连接服务端：

```sh
python examples/wechat-import/wechat_import.py 家庭群.txt --chat 家庭群 --me 小明 --dry-run
```

`--me` 填你在导出文件里显示的昵称、备注或 wxid，可以重复传多个。不传的话，你自己的消息会被当成别人的。

**第二步：小规模试跑**，看提取效果和模型开销：

```sh
python examples/wechat-import/wechat_import.py 家庭群.txt --chat 家庭群 --me 小明 --max-commits 2
```

**第三步：全量导入**。重跑同一命令即可，已导入的部分会自动跳过：

```sh
python examples/wechat-import/wechat_import.py 家庭群.txt --chat 家庭群 --me 小明
```

同一个聊天有多个导出文件（比如按月导出）时，可以一起传入，脚本会合并去重：

```sh
python examples/wechat-import/wechat_import.py 2024-*.txt --chat 项目群 --me 小明
```

## 查看结果

```sh
ov ls viking://~/sessions/<session_id>/history/              # 每次 commit 的归档
ov read viking://~/sessions/<session_id>/history/archive_001/.overview.md   # 纪要和待办
ov read viking://~/sessions/<session_id>/history/archive_001/memory_diff.json  # 本次记忆变更
ov ls viking://~/memories/                                    # 你自己的记忆
ov ls viking://~/peers/                                       # 每个联系人的记忆
ov find "张三上次答应了什么"
```

事件记忆都带有标签 `source=wechat` 和 `chat=<session_id>`，方便按聊天过滤。

## 常用参数

| 参数 | 说明 |
|---|---|
| `--chat` / `--session-id` | 聊天名 / 指定 session id（只能用字母、数字和 `_ . @ -`） |
| `--me` / `--my-name` | 你的昵称（用来识别本人消息）/ 你的消息在正文里显示的名字 |
| `--peer-map peers.json` | 自定义 peer_id，比如 `{"张三": "zhangsan"}`，让路径更好读 |
| `--since` / `--until` | 只导入某段日期（`YYYY-MM-DD`） |
| `--memory-types events,preferences` | 只提取这些记忆类型（建立 session 时生效） |
| `--no-peer-memory` | 不给他人建记忆，只保留你自己的记忆和聊天纪要 |
| `--keep-media` / `--keep-system` | 保留媒体占位符 / 系统提示 |
| `--tz` | 不带时区的时间按此时区解释，默认 `+08:00` |
| `--no-wait` | 不等待每次提取完成（更快，但下一批提取时可能还看不到上一批的记忆） |
| `--export-normalized out.jsonl` | 把解析结果写成文件，方便检查 |

默认每次 commit 后会等待记忆提取完成，再导入下一批。这样每批提取都能看到之前的记忆并在其基础上合并，效果更好。

## 隐私提示

- 记忆提取会把聊天内容发给你配置的模型服务。对隐私要求高时，可以用 Ollama 等本地模型。
- 默认的 `profile` 记忆可能会记录他人的健康、家庭等敏感信息。不需要时，用 `--memory-types events,preferences,entities` 或 `--no-peer-memory` 收窄范围。
- `memory_policy` 在 session 创建时确定。想改的话，换一个 `--session-id` 重新导入。

## 测试

```sh
pytest examples/wechat-import -p no:cacheprovider -o addopts=""
```
