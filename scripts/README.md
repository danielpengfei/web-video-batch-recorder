# scripts/

从原始部署中脱敏后抽出的核心脚本。**只改了字符串常量、路径来源与一处目录名**，控制流、状态机、审计判据、验收标准均未变。

## 运行环境

```bash
export RECORDER_TOOLS="$PWD"          # 本目录（默认 .）
export RECORDER_DATA="$PWD/data"      # 队列/状态/输出所在目录（默认 ./data）
mkdir -p "$RECORDER_DATA"
```

| 环境变量 | 默认 | 用途 |
|---|---|---|
| `RECORDER_TOOLS` | `.` | 脚本与 `STOP` 文件所在目录 |
| `RECORDER_DATA` | `./data` | `queue.json` / `batch-state.json` / 输出目录 |

## 依赖

```
Python 3.9+
ffmpeg（必需，remux 与验收都靠它）
Google Chrome（AppleScript 驱动）
macOS（screencapture + AppleScript）
```

`ffmpeg` 缺失时录制器会**直接拒绝启动**，不会产出无法验收的文件。

## 脚本分工

| 文件 | 职责 |
|---|---|
| `batch_course.py` | 批量调度器，主入口（`--status` / `--dry-run` / `--run`） |
| `record_course.py` | 单课录制器（`screencapture` 调用 + `PlaybackAudit`） |
| `foreground_play.py` | 前台点击原生播放按钮（焦点重试） |
| `night_guard.py` | 守护进程，失败隔离与自动重启 |
| `recording_schedule.py` | 时间窗与暂停策略 |

## 脱敏改动清单

| 改动 | 原值 | 现值 |
|---|---|---|
| 硬编码路径 | `<USER_HOME>/Downloads/<业务目录>/...` | `os.environ['RECORDER_TOOLS']` / `['RECORDER_DATA']` |
| 平台域名正则 | `[A-Za-z0-9-]+\.<platform>\.<tld>` | `[A-Za-z0-9.-]+\.[A-Za-z]{2,}`（通用主机名） |
| 窗口标题标记 | `[<业务前缀>-<uuid12>]` | `[RC-<uuid12>]` |
| 输出目录名 | `<业务目录名>` | `output` |
| 平台名 | 正文/注释/docstring 里的平台品牌词 | 中性描述（`already-authorized page` / `web video`） |
| 内部模块前缀 | JS 注入变量 `__<业务前缀>*` | `__cap*` |
| 本机工具缓存路径 | `<USER_HOME>/<harness>/cache/.../<业务前缀>-swift-module-cache` | `~/.cache/recorder-swift-module-cache` |
| 时间窗时区 | 硬编码 `Asia/Shanghai` | `RECORDER_TZ`（默认 `Etc/UTC`），策略 JSON 可覆盖 |

`[RC-...]` 是打在受控窗口标题前的标记，用于在多个标签页里认出「这是我自己开的窗口」。改动只影响标记前缀，识别逻辑不变。

## 首次验证

```bash
export RECORDER_TOOLS="$PWD" RECORDER_DATA="$PWD/data"
mkdir -p "$RECORDER_DATA"

# 只读检查（默认行为，不会碰浏览器）
python3 batch_course.py --queue data/queue.json --state data/batch-state.json --status

# 预演
python3 batch_course.py --queue data/queue.json --state data/batch-state.json --dry-run
```

`queue.json` 需要自己按目标平台抓取生成，格式见 [../docs/quickstart.md](../docs/quickstart.md)。

## 与原版的差异

**只有上面列出的脱敏改动，外加一处修正**：`recording_schedule.py` 的 `PROTECTED_OUTPUT` 原先指向改名前的输出目录，导致时间窗闸门实际失效，已改为 `output`。业务逻辑其余部分未改。

原版里还有一批 `test_*.py`（约 30 个，覆盖调度、录制、焦点、时间窗），**未包含在本目录**——它们大量引用真实路径和平台 URL，脱敏成本高于收益。要补测试建议按当前接口重写。
