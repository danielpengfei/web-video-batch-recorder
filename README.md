# web-video-batch-recorder

> **仅供技术探讨与个人学习使用。** 本仓库不含任何内容，也不含抓取器；不规避 DRM/付费墙；录什么由使用者自行确认授权。**个人非商业使用免费，商业使用需单独授权**，见 [NOTICE.md](NOTICE.md)。

本项目是一个面向个人、源码公开的**无人值守屏幕录制项目**：帮助个人把需要登录的在线视频学习内容**本地化**，断网、平台下架或授权到期之后仍可继续学习。

具体做法很朴素——给一份你自己准备的内容队列（JSON），它逐课打开页面、录制、remux、验收、记账。中断了能续；失败了能说清是哪一环断的。

它不是「随手录一段」的屏幕录制工具，而是**带验收的批量采集器**：录够时长不算完成，能完整解码 + 音轨有内容才算。

## 核心能力

| 能力 | 做法 |
|---|---|
| **断点恢复** | 队列（输入事实）与状态（运行结果）分离；重启只续没录的，已完成的跳过 |
| **PlaybackAudit** | 录制期间持续读播放器状态：`currentTime` 是否推进、是否被 seek/改速率/静音、是否 `source_ended` 且覆盖完整内容 |
| **sidecar 审计** | 每课留一份 `<name>.recording.json`：计划时长、窗口 ID、每秒采样、磁盘与前台状态、审计结果、验收日志。失败瞬间的状态保在里面 |
| **时间窗** | 只决定「什么时候允许**开新的一课**」，不打断正在跑的录制；带 `manual_hold` 软闸 |
| **验收三条件** | `ffmpeg` 能完整解码 → 能提取音轨 → `volumedetect` 峰值高于静音阈值。缺一即 failed |
| **拒绝覆盖** | 输出或 sidecar 已存在就抛错，不静默覆盖；失败产物保留作证据 |
| **单实例** | worker 锁保证同一输出目录只有一个录制进程 |

## 明确不做

- **本仓库不含抓取器。** 队列必须由外部生成（自己写爬虫或手工整理）。这是刻意设计：工具不自己发现内容，避免「录了不该录的」。
- 不绕过登录、权限或付费墙。它假设你已经有权访问目标页面，并且浏览器里处于已登录状态。
- 不验证内容语义：说话人身份、音画同步的感知质量不在验收范围内。

## 快速上手

```bash
git clone https://github.com/danielpengfei/web-video-batch-recorder.git
cd web-video-batch-recorder
export RECORDER_TOOLS="$PWD/scripts" RECORDER_DATA="$PWD/data"
mkdir -p "$RECORDER_DATA"

# 只读检查（默认行为，不碰浏览器）
python3 scripts/batch_course.py --queue data/queue.json --state data/batch-state.json --status
```

真跑需要 `--run`（显式 opt-in），第一次建议加 `--limit 1`。完整步骤、队列格式、产物体检见 [docs/quickstart.md](docs/quickstart.md)。

## 目录结构

```
web-video-batch-recorder/
├── README.md
├── CHANGELOG.md             每个版本修了什么坑
├── SECURITY.md              脱敏占位符表 + 发布前检查
├── LICENSE                  PolyForm Noncommercial 1.0.0
├── NOTICE.md                使用边界、版权与商业授权说明
├── docs/
│   ├── README.md            文档索引
│   ├── requirements.md      模型依赖度、最低配置、录制期行为约束
│   ├── quickstart.md        跑起来
│   ├── architecture.md      设计原则、时序、状态机、验收标准
│   ├── pitfalls.md          15 条实测踩坑
│   ├── troubleshooting.md   按现象排查
│   └── roadmap.md           已知不足与优化方向
└── scripts/
    ├── README.md
    ├── batch_course.py      批量调度器（主入口）
    ├── record_course.py     单课录制器 + PlaybackAudit
    ├── foreground_play.py   前台点击原生播放按钮
    ├── night_guard.py       守护进程（失败隔离、自动重启）
    └── recording_schedule.py 时间窗策略
```

## 环境要求

- macOS（依赖 `screencapture` 与 AppleScript）
- Python 3.9+
- `ffmpeg`（必需：remux 与验收都靠它；缺失时录制器直接拒绝启动）
- Chrome，且已登录目标站点
- 磁盘：约 1–3 GB／小时／课，整批建议预留成品体积的 2 倍

最低配置估算、容量公式，以及**录制期间不能做的事**（听音乐、玩游戏开声音、连蓝牙耳机、锁屏、动标签页、加载大模型……每条都对应一次废片）见 [docs/requirements.md](docs/requirements.md)。

**运行时无需 agent 辅助**：不给任何模型也能完整跑完一批，判据全在 DOM 状态与 `ffmpeg` 里。

但如果接入一个能读状态与日志的 agent（本地或云端模型均可），它能更早发现「进程还在、播放已停」「连续失败同一根因」「状态说完成但文件不在」这类问题，并在出错时直接做修复动作——实际效果明显好于纯定时脚本。详见 [docs/requirements.md](docs/requirements.md) 第一节。

## 配置

两个环境变量替代了所有硬编码路径：

| 变量 | 默认 | 用途 |
|---|---|---|
| `RECORDER_TOOLS` | `.` | 脚本与 `STOP` 文件所在目录 |
| `RECORDER_DATA` | `./data` | `queue.json` / `batch-state.json` / 输出目录 |
| `RECORDER_TZ` | `Etc/UTC` | 时间窗判定用的时区（也可在策略 JSON 里用 `timezone` 覆盖） |

## 安全边界

- 只操作自己创建的那个受控窗口句柄，不碰用户其他标签页
- 前台点击必须显式传 `--allow-foreground-click`，且只点自己窗口里的播放按钮
- 队列、状态、日志、录制产物、截图证据全部在 `.gitignore` 里

脱敏占位符与发布前检查见 [SECURITY.md](SECURITY.md)。

## 已知限制

见 [docs/roadmap.md](docs/roadmap.md)。其中两条现在就会咬人：前台点击路径依赖未收录的 `browser_focus.py`，以及验收只判「能解码 + 有声音」。

## 授权与免责

- **用途**：技术探讨与个人学习。本仓库不含内容、不含抓取器，不支持规避 DRM、付费墙或访问控制。
- **版权**：录到的内容版权归原权利人。「个人学习」不是版权豁免——请只对你有权访问且有权录制的内容使用。
- **授权**：源代码公开，条款为 [PolyForm Noncommercial 1.0.0](LICENSE)。个人、研究、非营利使用免费；**任何商业用途需单独书面授权**——邮件 [pengfei.lee17@gmail.com](mailto:pengfei.lee17@gmail.com) 或 [GitHub @danielpengfei](https://github.com/danielpengfei)。
- **措辞**：因为限制商业使用，严格意义上这是「源代码公开」而非 OSI 开源。
- **担保**：按原样提供，不附带任何担保；录制失败、数据丢失等后果自负。
- **关联**：与任何视频平台、课程提供方无关，未获其背书。

完整条款与中英双语说明见 [NOTICE.md](NOTICE.md)。
