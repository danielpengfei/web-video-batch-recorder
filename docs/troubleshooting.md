# 故障排查

按**你看到的现象**查，不用按代码结构查。

## 1. 启动就退出，什么都没录

先看 `--status` 输出的 `stop_reason`：

| `stop_reason` | 含义 | 处理 |
|---|---|---|
| `stop_file` | 目录里存在 `STOP` 文件 | 检查是谁建的（手动暂停？定时任务？），移走或改名 |
| `schedule_window_closed` | 时间窗关闭或 `manual_hold=true` | 见下方「时间窗排查」 |
| `null` + `stop_reason` 为空 | 正常退出（没有待办） | 看 `counts.pending` 是不是 0 |

**时间窗排查四步**（顺序不能反）：

```bash
# 1) STOP 文件可能在 07:00 被定时任务重建
ls -la STOP

# 2) manual_hold 独立于时间窗
python3 -c "import json;print(json.load(open('recording-schedule.json'))['manual_hold'])"

# 3) temporary_until 是否覆盖当前时间
python3 -c "import json;print(json.load(open('recording-schedule.json'))['temporary_until'])"

# 4) 最后才看时间窗
date +%H   # hour >= 22 or hour < 7
```

## 2. 某一课反复失败，后续也不碰它了

`night_guard.py` 的规则：**同一 URL 连续失败 2 次就进 blocklist**，之后重建的队列里没有它。

```bash
grep '"source"' queue-guard.json
# "guard-filtered (blocked=1)"  ← 有课被屏蔽了
```

对比完整队列找差异：

```python
full = {v['url'] for v in json.load(open('queue.json'))['videos']}
guarded = {v['url'] for v in json.load(open('queue-guard.json'))['videos']}
print('被屏蔽:', full - guarded)
```

要重录就用 `queue.json`（未过滤），或手工把它从 blocklist 移除。

## 3. 失败原因怎么看

每课都有 sidecar：`<name>.recording.json`。关键字段：

```json
{
  "status": "failed",
  "error": "Native play button click failed after 4 focus retries: Chrome is not frontmost",
  "last_observation": {
    "elapsed": 42.1,
    "player": {...},
    "session": {...}
  }
}
```

`last_observation` 保住了失败瞬间的**确切状态**，比事后推断可靠。

常见 error 对照：

| error 片段 | 根因 | 处理 |
|---|---|---|
| `Chrome is not frontmost` | 前台被别的应用抢走 | 见下方第 4 条 |
| `Playback stopped progressing for N seconds` | 播放卡住（网络/缓冲/页面被节流） | 查网络；删掉该课重录 |
| `Playback seek/rate/mute event observed` | 播放器被动过 | 别在录制中操作播放器 |
| `Media failed decode/non-silence checks` | 音频轨为空或解码失败 | 看 sidecar 的 `audio_log` |
| `SIGTERM requested` | 进程被信号杀死（通常是人手中断） | 重跑 |
| `Cannot verify foreground macOS session` | 锁屏、屏幕遮挡或前台不可确认 | 解锁屏幕后重试 |

## 4. 前台反复被抢

排查顺序：

```bash
# 当前前台是谁
osascript -e 'tell application "System Events" to get name of first process whose frontmost is true'

# 有没有多余的浏览器实例
pgrep -fl "Google Chrome" | wc -l
```

**最常见的原因**：有脚本/工具在旁边开了新的浏览器实例（自动化工具、预览窗口、调试器）。它们会激活自己的窗口，把录制用的 Chrome 挤到后台。

**解法**：录制期间只用纯命令行工具（`curl` / `grep` / `sqlite3` / `ps`），它们的只读操作不会改变前台应用。

## 5. 录出来的文件没声音

按顺序查：

```bash
# ① 有没有音轨
ffprobe -v error -select_streams a -show_entries stream=codec_name -of csv=p=0 <file>.mov

# ② 有音轨的话，码率是多少（<10 kbps 基本是静音）
ffprobe -v error -show_entries format=bit_rate -of csv=p=0 <file>.mov

# ③ 分段扫全程电平（不要只看开头）
for t in $(seq 0 600 3600); do
  ffmpeg -hide_banner -nostdin -ss $t -t 60 -i <file>.mov -map 0:a:0 \
    -af volumedetect -f null - 2>&1 | grep -E "max_volume|mean_volume"
done
```

对照 [pitfalls.md](pitfalls.md) 第一节的四种成因（参数选错、无麦克风、片头静音误判、蓝牙接管）。

## 6. 录到一半磁盘满了

`capture_disk_guard` 每轮都会检查，剩余不足 30 GB 就抛错停止。这是保护，不是故障。

清理后重跑即可。已完成的课不会重录。

## 7. 状态卡在 `recording`

说明上次异常退出（停电、强杀）。工具启动时会跑 `assert_recovery_safe()`，如果发现残留会**拒绝启动**并提示手工清理。

确认没有残留进程后：

```python
item['status'] = 'pending'
item.pop('error', None)
```

`attempts` 数组保留，不要删——那是失败证据。

## 8. 同一个 URL 出现多份输出

不会发生。`run_batch` 每课用 `uuid4().hex` 拼文件名，且录制器拒绝覆盖已存在的输出或 sidecar。

如果目录里真的有多份，说明是多次重试留下的——**只有 state 指向的那份是有效的**。

## 9. `--status` 显示的数量和目录里的文件对不上

三个常见原因：

1. **`-sample-` 片段被算进去了**：sample 是验证用的几十秒片段，不是正式课
2. **exFAT 上的 `._*` 伪文件**：`find -name "*.mov"` 会把它们也数进来，必须加 `! -name '._*'`
3. **`.mp4` 也算了一份**：一课产出 mov + mp4 两个文件

对账基准应该用 `queue.json` 的条目数，不是目录文件数。

## 10. 想知道为什么排到某一课却跳过了

看 `run_batch` 的跳过逻辑：state 里该 URL 的 status 是 `completed` 就跳过，不重新录。

```python
python3 -c "
import json
s = json.load(open('batch-state.json'))
for url, it in s['items'].items():
    if '<关键词>' in it.get('title', ''):
        print(it.get('status'), it.get('title'))
"
```

要强制重录，先把状态改成 `pending`。
