# 踩过的坑与根因

> 全部为实测结论，不是推测。每条给出「现象 → 根因 → 解法」，可直接对照排查。

## 一、音频：录出来没声音

### 1. `-A` 才是录系统声音，`-g` / `-G` 录麦克风

`recorder` 用 `screencapture -v -l<windowid>` 录指定窗口。音频参数有三个选择，行为完全不同：

| 参数 | 结果 |
|---|---|
| `-v` 什么都不加 | **完全没有音频轨** |
| `-v -A` | **录系统播放的声音**，AAC 18–64 kbps，峰值约 −5 dB ✅ |
| `-v -g` / `-v -G<id>` | 录**输入设备（麦克风）**；机器没麦克风就是 2 kbps 的静音轨 |

`-A` 在 `man screencapture` 和 `screencapture -h` 的选项列表里**都查不到**，但它存在且有效。macOS 上想要「录电脑里正在播放的声音」，这是唯一原生途径，不需要装 BlackHole。

**排查动作**：先确认调用方到底传了哪个参数（`grep -n screencapture <脚本>`），再用 `-A` / `-g` 各录 5 秒对比码率——2 kbps 是静音，>15 kbps 才有内容。

### 2. 桌面 Mac 没有内置麦克风

Mac Studio / Mac mini 的官方规格就是 **Built-In Microphone: No**。系统里查不到内置麦克风是正常现象，不是驱动坏了。

```bash
system_profiler SPAudioDataType | grep -B5 "Input Channels"
```

只剩虚拟设备（远程桌面软件的虚拟麦克风）时，要录人声就得接 USB 麦或连蓝牙耳机。

### 3. 只读文件开头会误判成静音

课程/会议录制常常前 5–23 分钟是片头或无人说话。只看前 120 秒的 `volumedetect` 会得出 −58 dB、−91 dB 的假结论。

正确做法是**分段扫全程**：

```bash
for t in $(seq 0 600 3600); do
  ffmpeg -hide_banner -nostdin -ss $t -t 60 -i in.mov -map 0:a:0 \
    -af volumedetect -f null - 2>&1 | grep -E "max_volume|mean_volume"
done
```

判读：峰值 −8 ~ −20 dB 正常；−90 dB 是真空轨；**只有开头一段 −90 而后面正常 = 片头静音，不是故障**。

### 4. 蓝牙接管输出会把 `-A` 变成静音

系统输出切到蓝牙耳机后，`-A` 抓系统声音可能录到全静音轨。

判断方法：看同目录有没有当时留的对照样本（如 `known-audible-control`），对比时间点是否卡在蓝牙设置前后。

**结论：录制期间不要连蓝牙耳机**，输出固定内建扬声器或 HDMI 显示器。

---

## 二、前台：Chrome 不在前台

### 5. 前台被抢 = 当前课直接判失败

录制流程需要在**自己的 Chrome 窗口**里点击原生播放按钮。点不到就失败：

```
Native play button click failed after 4 focus retries: Chrome is not frontmost
```

**根因**：任何其他操作把 Chrome 挤到后台都会触发。实测触发源包括：

- 手动切到游戏/其他应用
- **脚本自己启动新的浏览器实例**（`browser_exec` 之类工具会开独立 Chrome，抢走前台）
- 系统弹窗、通知中心、屏幕保护

**解法**：录制期间不要做任何会激活其他窗口的操作。如果必须查资料，用纯命令行（`curl`、`grep`、`sqlite3`），它们的只读操作不会改变前台应用。

### 6. 前台校验是「预检」不是「持续监控」

工具在录制**开始前**检查前台，录制中不强制。所以：

- 开始前抢前台 → 启动失败
- 开始后再切走 → 画面继续录，但如果窗口被最小化/遮挡，`-l<windowid>` 的窗口捕获仍正常，**音频也会继续**（`-A` 抓的是全局系统声音）

真正的风险在**播放中断**：切走后网页可能被节流，视频播放卡住 → 触发 `Playback stopped progressing for 15 seconds`。

---

## 三、调度：明明在时间窗内却不启动

### 7. 每日 07:00 会自动生成 STOP 文件

工具有两层门禁：时间窗 + `STOP` 文件。时间窗之外，**定时任务每天 07:00 还会主动写一个 STOP**：

```json
{"purpose": "schedule_cutoff", "token": "..."}
```

它和你手动创建的 `user_pause` 是两个不同来源，但都会阻止启动。

**排查顺序**（缺一不可）：

1. `STOP` 文件是否存在（可能被定时任务重建）
2. `recording-schedule.json` 的 `manual_hold` 是否为 `true`
3. `temporary_until` 是否覆盖当前时间
4. 最后才看时间窗 `hour >= 22 or hour < 7`

**踩坑实录**：某次白天要临时录制，清了 `manual_hold` 仍然秒退，连续两次失败才发现是早上 7 点自动重建的 STOP。

### 8. `manual_hold` 独立于时间窗

`window_allows()` 的逻辑是：

```python
if not policy.get('enabled') or policy.get('manual_hold', False):
    return False          # ← 先于时间窗判定
local = now.astimezone(ZoneInfo(policy['timezone']))
until = policy.get('temporary_until')
if until and now < deadline:
    return True
return local.hour >= 22 or local.hour < 7
```

所以「我在时间窗内」不等于「可以启动」。暂停时 `manual_hold` 会被一起设上，恢复时必须显式清掉。

### 9. 连续失败 2 次会被扔进 blocklist

`night_guard.py` 的规则：

```python
MAX_FAILS_PER_URL = 2   # 同一课连续失败 2 次 => 转 blocklist
```

被屏蔽的 URL 会从队列里剔除，队列文件的 `source` 字段变成：

```
guard-filtered (blocked=1)
```

**后果**：这一课从此「消失」，后续 `--run` 跑多少次都不会碰它。要重录必须换回未过滤的完整队列（`queue.json` 而非 `queue-guard.json`），或手动把它从 blocklist 里放出来。

**设计意图是好的**（避免一节课死磕堵住其余），但排查时容易误判成「平台上没有这节课」。

---

## 四、状态与队列

### 10. `failed` 不会自动重试

`run_batch` 的默认逻辑：

```python
item = state['items'].setdefault(video['url'], dict(video, status='pending', attempts=[]))
```

只有 state 里**不存在**的 URL 才会被建成 `pending`。已存在的 `failed` 保持原样。所以要重录必须先手工改状态：

```python
item['status'] = 'pending'
item.pop('error', None)
```

`attempts` 数组保留不删，作为历史证据。

### 11. 队列和 state 可能不同步

- `queue.json` 是抓取时的完整清单（含所有课）
- `queue-guard.json` 是过滤后的（去掉 blocklist）
- `batch-state.json` 是录制状态

三者的条目数可能都不一样。核对完整性时要拿 `queue.json` 做基准，不要用 guard 版。

---

## 五、媒体校验

### 12. 采样片段会污染「已完成」的判断

`--clip-seconds` 产的 sample 会以 `validated` 状态写进 state，文件名带 `-sample-`，时长只有几十秒到几分钟。

如果同步脚本按 `folder_path` 无差别搬运，**1 分钟的片段会被当正式课摆进课程目录**，制造「这课已经录好了」的错觉。

**解法**：同步时校验时长，或按文件名区分 `-full-` / `-sample-`。

### 13. 硬链接 + exFAT 的统计陷阱

把媒体库同步到 exFAT 外置盘后，`find -name "*.mov"` 数出来的数量会**翻倍**。

**根因**：macOS 为带扩展属性的文件在 exFAT 上生成 AppleDouble 元数据 `._xxx.mov`，它同样匹配 `*.mov`。

```
源 368 个 → 目标 find 得到 736（368 真 + 368 伪）
```

**解法**：统计时永远加 `! -name '._*'`。这些伪文件不占空间、不影响播放，但会让对账结论完全错掉。

同理，`du -sh` 在硬链接目录上会**重复计算**（每个目录各自算一份），判断真实占用要看 `df` 前后差值。

---

## 六、别把内核日志当硬件故障

### 14. `IOGPUCommandDescriptor::prepare` 报错 ≠ GPU 坏了

日志长这样：

```
kernel (IOGPUFamily) IOGPUCommandDescriptor::prepare(uint32_t): 
preparedBytes != requiredBytes : preparedBytes = <36823891968> requiredBytes = <36823810048>
```

差值只有几页（81920 字节 = 5 × 16 KB）。这是 MLX 申请的大 buffer 长度没按 16 KB 页对齐，IOGPU 向上取整后断言不相等——**只打日志，不中断执行**。

同时它在系统日志里是 `F`（Fatal）级别，极易被误读成硬件故障。

**验证 GPU 是否真坏的方法**：直接分配同尺寸大 buffer 做全量运算。

```python
import mlx.core as mx
# 单维 shape 受 int32 限制，要拆成二维
buf = mx.zeros((65536, 36 * 2**30 // 4 // 65536), dtype=mx.float32)
mx.eval(buf)
print(float(mx.sum(buf).item()))   # 0.0 且无异常 = GPU 正常
```

真故障另有标志：`.panic` 文件、`GPU hang` / `GPU restart` 日志、`kIOGPUCommandBufferCallbackErrorTimeout`。这些为 0 就别说 GPU 坏了。

---

## 七、性能：越录越慢

### 15. 长上下文的代价（如果接了本地 LLM）

录制本身不慢，但配套的模型分析会随上下文长度衰减。实测同一模型：

| prompt | 生成速度 |
|---:|---:|
| 695 tokens | 51.8 tok/s |
| 92 K | 5.4 tok/s |
| 152 K | 7.7 tok/s |
| 167 K | 9.2 tok/s |

同时 `prefix_cache_reconstruct` 从 4.7 秒涨到 20 秒再涨到 41 秒（随会话长度累积）。

**结论**：长时间会话要主动切断，不要指望 prefix cache 兜住一切。
