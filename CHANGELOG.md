# 变更记录

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号用 [语义化版本](https://semver.org/lang/zh-CN/)。

只记录「行为变了什么、修掉了什么问题」，不复述实现细节。踩坑的完整根因见 [docs/pitfalls.md](docs/pitfalls.md)。

## [0.1.0] — 2026-11-26

首次公开。授权条款为 **PolyForm Noncommercial 1.0.0**：个人非商业使用免费，商业使用需单独授权；使用边界与版权声明见 [NOTICE.md](NOTICE.md)。

### 包含

- `batch_course.py`：队列校验、状态机、断点恢复、单实例锁、remux 与验收
- `record_course.py`：单课录制 + `PlaybackAudit` 连续性审计 + sidecar 审计报告
- `foreground_play.py`：原生播放按钮点击（需 `--allow-foreground-click`）
- `night_guard.py`：守护进程，卡死检测、失败隔离、重启上限、磁盘下限
- `recording_schedule.py`：时间窗闸门（只拦「开新的一课」，不打断已开始的）
- 文档：quickstart / architecture / pitfalls / troubleshooting / requirements / roadmap

### 已修的坑（都来自实测废片）

- **音频轨**：`screencapture -v` 不带 `-A` 就没有音频轨，`-g/-G` 录的是麦克风而不是系统声音；`-A` 不在 man page 里但有效。
- **外接屏点击失效**：某些外接屏上合成的点击事件到不了网页 DOM，前台与坐标校验全部通过也没用；修法是建窗后把窗口移到主屏。
- **自动切课失败**：切课瞬间前台不可控，原生点击被拒；改为有界焦点重试（仅点击前「非前台」失败时重试）。
- **就绪超时误判**：45 秒没到 `readyState=2` 被判失败，实际只是 CDN 慢；等待放宽到 90 秒，验收逻辑不动。
- **坏源文件**：源文件本身「不支持的格式」或只有几秒时，验收会正确拒绝并保留证据，不删半截文件。
- **状态机卡死**：上次失败时留下未验证关闭的自有窗口，会导致下次启动的 preflight 拒绝继续；补了明确的清理与验证步骤。
- **采集写出 0 字节**：GPU/合成器被其他负载占满时会写不出帧；判据不能看「文件在不在」（`screencapture` 录完才落盘），要看抓帧能否出数据、`currentTime` 是否推进。

### 已知边界

- 抓取器不包含（队列必须外部生成）
- 验收只判「能解码 + 有声音」，不判说话人身份与音画同步
- 前台点击依赖窗口几何，换浏览器或缩放可能失效
- 约 30 个 `test_*.py` 未纳入（引用真实路径与真实 URL）

完整清单见 [docs/roadmap.md](docs/roadmap.md)。
