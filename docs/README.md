# 文档索引

> 本目录会随仓库发布。**任何具体业务内容（真实课程名、讲师、平台文档 ID、本机路径）都不允许出现在这里**，私有材料统一放仓库根的 `.private/`（已被 `.gitignore` 排除）。

## 阅读顺序

| 序 | 文件 | 什么时候看 |
|---:|---|---|
| 1 | [quickstart.md](quickstart.md) | 想把它跑起来 |
| 2 | [architecture.md](architecture.md) | 想改它，或想理解为什么这么设计 |
| 3 | [pitfalls.md](pitfalls.md) | 遇到诡异问题，或想少踩坑 |
| 4 | [troubleshooting.md](troubleshooting.md) | 已经出问题了，按现象查 |
| 5 | [roadmap.md](roadmap.md) | 想知道哪里还没做好、下一步能做什么 |
| 6 | [requirements.md](requirements.md) | 要判断自己的机器跑不跑得动、录制期间能干什么 |

## 各文档内容

| 文件 | 内容 |
|---|---|
| `quickstart.md` | 前置条件、目录结构、队列格式、第一次运行、时间窗配置、产物结构 |
| `architecture.md` | 设计原则、组件分工、单课录制时序、PlaybackAudit 判据、状态机、断点恢复、验收标准、安全边界、可复用 vs 需替换 |
| `pitfalls.md` | 15 条实测踩坑：音频参数、前台抢占、时间窗与 STOP、blocklist、状态不同步、媒体校验、硬链接统计、内核日志误读、长上下文衰减 |
| `requirements.md` | agent/模型依赖度（运行时不需要，接入后监督与纠错明显更好）、实测设备、最低配置估算、容量公式、录制期间的行为约束 |
| `troubleshooting.md` | 10 类现象按「看到了什么」查：启动即退、反复失败被屏蔽、前台被抢、录出来没声音、磁盘满、状态卡住、数量对不上 |

## 核心结论速查

**三个最容易踩的坑：**

1. **`screencapture -A` 才录系统声音**，`-g` 录麦克风，什么都不加等于没有音频轨。`-A` 在 man page 里查不到但有效。
2. **每日 07:00 定时任务会重建 `STOP` 文件**。白天临时录制时清掉 `manual_hold` 还不够，必须同时检查 `STOP`。
3. **连续失败 2 次的课会被 blocklist 剔除**，队列 `source` 变成 `guard-filtered`。之后跑多少次都不会碰它，得换回完整队列。

**验收三条件**（缺一不可）：能完整解码、能提取音轨、`volumedetect` 峰值超过静音阈值。

## 脱敏口径

- 真实 URL 用 `<PLATFORM_HOST>` `<FOLDER_ID>` `<FILE_ID>`
- 本机路径用 `<USER_HOME>`
- 机器标识用 `<SERIAL>` `<HARDWARE_UUID>`
- 被录内容用 `<COURSE_TITLE>` `<INSTRUCTOR>`
- 凭据一律 `<REDACTED>`

完整对照与发布前检查见仓库根的 `SECURITY.md`。

## 已知不足

完整清单见 [roadmap.md](roadmap.md)。最短版本：

- 抓取器不在本仓库（刻意如此，避免工具自己发现内容）
- 验收只判「能解码 + 有声音」，不判说话人身份与音画同步
- 前台点击依赖窗口几何，换浏览器/换主题可能失效
- 前台点击路径依赖的 `browser_focus.py` 未收录
- 时间窗起止写死为 `22:00–07:00`，时区已可配、起止未可配
