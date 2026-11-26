# 架构与流程

## 一、要解决的问题

把一个需要登录、需要人点播放、时长几十分钟到几小时的网页视频，**无人值守地批量录成完整文件**，并且：

- 录完能自证完整性（时长对得上、音轨有内容、解码不报错）
- 中断能续（不从头再来）
- 失败可归因（是前台被抢、播放卡住、还是磁盘不够）
- 全程可审计（每条录制留一份 sidecar 报告）

不是屏幕录制工具，是**「带验收的批量采集器」**。

## 二、设计原则

| 原则 | 落地方式 |
|---|---|
| **绝不猜内容** | 队列必须由外部抓取生成，工具只消费，不自己发现/发明 |
| **只操作自己的窗口** | 通过 `owned_window` 记录窗口句柄，只对这个句柄 eval 和截图，不碰用户其他标签 |
| **先录后播** | 录制进程启动并确认存活 1.2 秒后，才触发播放，避免漏开头 |
| **拒绝覆盖** | 输出文件或 sidecar 已存在就直接抛错，绝不静默覆盖 |
| **状态由证据决定** | 只有 `decode + volumedetect + remux` 全过才标 `completed` |
| **单实例** | `worker_lock` 保证同一输出目录只有一个 worker |

## 三、组件分工

```
┌─────────────────────────────────────────────────────────┐
│ night_guard.py        守护进程（可选）                    │
│  · 定时/周期唤醒 → 检查 remaining_work                    │
│  · 连续失败 2 次的 URL 进 blocklist，重建过滤后的队列      │
│  · 检查 WindowServer CPU、磁盘余量，异常则不启动           │
└───────────────────────┬─────────────────────────────────┘
                        │ 启动
┌───────────────────────▼─────────────────────────────────┐
│ batch_course.py       批量调度器（主入口）                │
│  · prepare_queue      队列结构校验                        │
│  · load_state         状态加载 + 断点恢复安全检查          │
│  · run_batch          逐课循环：跳过已完成 → 调录制器      │
│  · owned_window       创建/复用受控 Chrome 窗口            │
│  · remux_mov          .mov → .mp4 直通封装                │
│  · verify_mp4         ffmpeg 解码 + volumedetect 验收      │
└───────────────────────┬─────────────────────────────────┘
                        │ 每课
┌───────────────────────▼─────────────────────────────────┐
│ record_course.py      单课录制器（核心）                  │
│  · make_plan           由播放器元数据算录制秒数            │
│  · course_window       定位目标窗口 ID                    │
│  · Popen screencapture -v -V<n> -A -l<win> -x            │
│  · PlaybackAudit       连续性审计（seek/rate/mute/推进）   │
│  · session_guard       会话安全（锁屏、前台、屏幕状态）     │
│  · capture_disk_guard  磁盘余量护栏                       │
└───────────────────────┬─────────────────────────────────┘
                        │ 需要前台点击时
┌───────────────────────▼─────────────────────────────────┐
│ foreground_play.py    前台点击播放                        │
│  · prime_course_playback  焦点重试 → 点击原生播放按钮      │
│  · click_plan             由窗口几何算点击坐标             │
└─────────────────────────────────────────────────────────┘

配置层
  recording_schedule.py   时间窗与暂停策略（STOP 之外的软闸）
```

## 四、单课录制时序

```
1  校验 URL / 输出不存在 / ffmpeg 可用
2  session_guard()            读当前会话状态（锁屏？前台？）
3  evaluate(PLAYER_STATE)     读 title / duration / paused / muted / rate
4  make_plan()                由元数据算 record_seconds
5  course_window(title)       定位窗口 ID
6  写 sidecar  {status: starting, plan, window, session_preflight, ...}
   ────────────────────────────────────────
7  video.pause(); currentTime=0; rate=1      ← 做准备，不播
8  require_start_allowed()                   ← 时间窗闸门（真正 capture 前）
9  Popen screencapture -v -V<n> -A -l<win> -x <out>
10 sleep 1.2 → 检查进程还活着（死了就抛错，不进播放）
11 evaluate(START_PLAYBACK)                  ← 此刻才开始播
   ────────────────────────────────────────
12 循环（每 2.5 秒）:
     · session_guard()            会话是否仍然安全
     · evaluate(PLAYER_STATE)     读实时状态
     · playError / error 非空 → 抛错
     · capture_disk_guard()       余量不足 → 抛错
     · PlaybackAudit.observe()    连续性判定
     · currentTime 推进 > 0.15 秒 → 刷新 last_advance
     · source_ended && content_complete → 达标
13 达标或录制器退出 → 停播、收尾
14 remux .mov → .mp4
15 verify_mp4：解码 + volumedetect（不足则 failed，保留证据）
16 state 标记 completed / failed，写 sidecar 终态
```

**关键点**：第 8 步的时间窗检查放在第 9 步之前，而不是录制中途。已开始的录制不会被中途掐断——这是刻意的，宁可录完，也不要产生半截文件。

## 五、PlaybackAudit：判定「这一课真的播完了」

录制时长够了不代表内容完整。审计器同时看三件事：

| 维度 | 判据 |
|---|---|
| **播放推进** | `currentTime` 是否持续增长；15 秒没动 → `Playback stopped progressing` |
| **事件洁净度** | 是否出现 `seeking` / `playbackRate != 1` / `muted`（用户或脚本动过播放器） |
| **内容覆盖** | `source_ended` 且已播时长覆盖 `content_seconds` |

任一不满足 → 判 failed，**保留半截文件作为证据**，不删除。

## 六、状态机

```
            ┌──────────┐
            │ pending  │ ← 只有 state 里不存在的 URL 才会自动变成这个
            └────┬─────┘
                 │ 开始录制
            ┌────▼──────┐
            │ recording │
            └────┬──────┘
        ┌────────┴────────┐
        │                 │
   ┌────▼─────┐     ┌─────▼────┐
   │completed │     │  failed  │
   └──────────┘     └────┬─────┘
                         │ 手工重置
                    ┌────▼─────┐
                    │ pending  │
                    └──────────┘
```

**注意**：`failed` 不会自动重试。`run_batch` 用 `setdefault` 建条目，已存在的 `failed` 原样保留。要重录必须显式改状态：

```python
item['status'] = 'pending'
item.pop('error', None)      # attempts 不删，作为历史
```

## 七、断点恢复

`load_state(recover=True)` 之前先跑 `assert_recovery_safe()`，检查：

- state 里有没有 `recording` 状态的条目（说明上次异常退出）
- 对应的 recorder 进程是否还活着
- 输出文件是否被别的进程占用

**只有确认没有正在进行的录制**，才允许继续。这是为了防止两个 worker 同时写同一个输出。

## 八、验收标准（completed 的三个必要条件）

```
1. decode.returncode == 0         ffmpeg 能完整解码
2. audio.returncode == 0          能提取音轨
3. media['passed'] == True        volumedetect 峰值超过静音阈值
```

三条全过才 `completed`，否则 `failed` 并写 `decode_log` / `audio_log` 到 sidecar。

**工具明确不验证的**：说话人身份、音画同步的感知质量。sidecar 的 `limitations` 字段写明了这一点。

## 九、安全边界

| 边界 | 实现 |
|---|---|
| 不碰用户的标签页 | 只操作 `owned_window` 记录的窗口句柄 |
| 不越权点击 | `--allow-foreground-click` 是显式 opt-in，且只点自己窗口里的播放按钮 |
| 不静默覆盖 | 输出或 sidecar 存在即抛错 |
| 不删证据 | 失败文件保留，仅在人工确认后清理 |
| 不猜测内容 | 队列只能来自外部抓取，工具不自己爬 |

## 十、可复用的部分 vs 需要替换的部分

| 可复用 ✅ | 需替换 ⚠️ |
|---|---|
| 状态机与断点恢复 | 具体平台的 DOM 选择器（`PLAYER_STATE` 的取值方式） |
| PlaybackAudit 审计逻辑 | 窗口定位策略（不同浏览器/播放器不同） |
| remux + verify 验收链 | 时间窗策略（按自己的作息改） |
| 队列/state 分离设计 | 抓取器（本仓库不含，需按目标平台自写） |
| sidecar 审计格式 | 平台域名/路径常量 |
