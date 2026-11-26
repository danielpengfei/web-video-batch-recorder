# 快速开始

## 一、前置条件

| 项 | 要求 | 检查方式 |
|---|---|---|
| 系统 | macOS（依赖 `screencapture` 与 AppleScript） | `sw_vers` |
| Python | 3.9+ | `python3 -V` |
| ffmpeg | **必需**，用于 remux 与验收 | `which ffmpeg` |
| 浏览器 | Chrome（脚本用 AppleScript 驱动） | 已登录目标站点 |
| 磁盘 | 每课预留 30 GB（`capture_disk_guard` 的 reserve） | `df -h` |

> 没有 ffmpeg 时录制器会**直接拒绝启动**，不会录出一个无法验收的文件。

## 二、目录结构

```
recorder/
├── batch_course.py          批量调度器（主入口）
├── record_course.py         单课录制器
├── foreground_play.py       前台点击播放
├── night_guard.py           守护进程（可选）
├── recording_schedule.py    时间窗策略
├── browser_focus.py         窗口焦点辅助（未收录，见 docs/roadmap.md）
├── resume_stitch.py         分段拼接（未收录，见 docs/roadmap.md）
└── data/
    ├── queue.json           完整队列（外部抓取生成）
    ├── batch-state.json     录制状态
    └── out/                 录制输出目录
```

`queue.json` 与 `batch-state.json` **必须分离**：队列是输入事实，state 是运行结果。混在一起会导致无法重跑。

## 三、队列格式

```json
{
  "root_url": "<PLATFORM_HOST>/folder/<FOLDER_ID>",
  "root_title": "<COURSE_TITLE>",
  "inventory_complete": true,
  "videos": [
    {
      "url": "<PLATFORM_HOST>/file/<FILE_ID>",
      "title": "<COURSE_TITLE> 01.mp4",
      "folder_path": ["Chapter A", "Sub B"]
    }
  ]
}
```

`prepare_queue()` 会校验：

- 每个视频都有 `url` / `title` / `folder_path`
- `inventory_complete` 为 true（除非 `--smoke-fixture` 且样本 ≤ 3 条、≤ 30 秒）
- 文件夹可达性（`reachable_folders`）

**队列不由本工具生成。** 需要按目标平台自己写抓取器——这是刻意的设计，避免工具自己发现内容导致「录了不该录的」。

## 四、第一次跑

```bash
# 1) 只读检查，什么都不动（默认行为）
python3 batch_course.py --queue data/queue.json --state data/batch-state.json --status

# 2) 预演，打印下一课会做什么
python3 batch_course.py --queue data/queue.json --state data/batch-state.json --dry-run

# 3) 真跑
python3 batch_course.py \
  --queue data/queue.json \
  --state data/batch-state.json \
  --output-dir data/out \
  --run \
  --allow-foreground-click
```

**`--run` 是显式 opt-in**，不加它就是纯只读。`--allow-foreground-click` 允许工具把自己的 Chrome 窗口提到前台，点击窗口内的原生播放按钮。

建议第一次加 `--limit 1` 只跑一课，确认整条链路（录制 → remux → 验收 → state 标记）都通，再放量。

## 五、时间窗

`recording_schedule.py` 用一个 JSON 策略控制「什么时候允许**开新的一课**」：

```json
{
  "enabled": true,
  "timezone": "Etc/UTC",
  "manual_hold": false,
  "temporary_until": null,
  "night_start": "22:00",
  "cutoff": "07:00"
}
```

判定顺序：

1. `enabled == false` 或 `manual_hold == true` → 拒绝（**独立于时间窗**）
2. `temporary_until` 未过期 → 允许
3. 否则 `hour >= 22 or hour < 7` → 允许

> **只影响「开新的一课」，不打断正在进行的录制。**

时区默认取环境变量 `RECORDER_TZ`（默认 `Etc/UTC`），策略里的 `timezone` 优先。上面的 `22:00–07:00` 是代码里的常量，想换窗口得改 `recording_schedule.py`——见 [roadmap.md](roadmap.md)。

白天要临时录制时，`temporary_until` 是最省事的开关：

```python
d['temporary_until'] = (now + timedelta(hours=8)).isoformat()
```

## 六、守护进程（可选）

`night_guard.py` 负责周期唤醒和失败隔离：

```bash
python3 night_guard.py
```

行为：

- 检查 `remaining_work()`，没活就退出
- 同一 URL 连续失败 2 次 → 加入 blocklist，重建 `queue-guard.json`
- 每轮把 WindowServer CPU（合成器负载）记进守护日志，作事后判据；磁盘余量不足则直接 SIGTERM + 退出
- 用 `--session` 隔离，避免和手动启动的 worker 冲突

**它会把被屏蔽的课从队列里删掉**。排查「平台上明明有这课却没录」时，先看队列的 `source` 字段是不是 `guard-filtered`。

## 七、录制产物

每课产出三样东西：

```
<name>.mov                原始录制
<name>.mp4                remux 直通封装（h264 不重新编码）
<name>.recording.json     sidecar 审计报告
```

sidecar 里记录了：播放器初始状态、录制计划、窗口 ID、每秒采样（含磁盘与前台状态）、PlaybackAudit 结果、验收日志。

**排查任何失败，先看 sidecar 的 `last_observation` 和 `error`**——它保住了失败瞬间的确切状态。

## 八、录完要验什么

```bash
# 时长对不对
ffprobe -v error -show_entries format=duration -of csv=p=0 <file>.mov

# 音轨有没有内容
ffmpeg -hide_banner -nostdin -i <file>.mov -map 0:a:0 \
  -af volumedetect -f null - 2>&1 | grep -E "max_volume|mean_volume"
```

判读标准见 [pitfalls.md](pitfalls.md) 第一节：峰值 −8 ~ −20 dB 正常，−90 dB 是空轨。

## 九、不要做的事

- ❌ 录制期间启动其他浏览器实例或工具（会抢前台，当前课直接失败）
- ❌ 录制期间播放音乐/视频/语音通话（`-A` 会把它们混进课程音频）
- ❌ 连蓝牙耳机后录制（`-A` 可能录成静音）
- ❌ 把 `queue.json` 放在会被工具写的地方（它是输入事实，不该被改）
