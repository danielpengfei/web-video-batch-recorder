#!/usr/bin/env python3
"""夜间无人值守守护：保活 + 卡死检测 + 失败换课 + 磁盘保护。

规则：
- batch_course.py 一旦某课失败会整体 break 退出 => 必须由本守护重启。
- 同一 URL 在本次守护内连续失败 2 次 => 加入 blocklist，后续用过滤后的队列，避免死磕单课堵住其余。
- 状态显示 recording 但 screencapture 子进程超过 GRACE 分钟不存在 => 判定卡死，SIGTERM 重启。
- 可用空间 < MIN_FREE_GB => SIGTERM 并退出，不写满盘。
"""
import json, os, re, shutil, signal, subprocess, sys, time, datetime, pathlib

TOOLS = pathlib.Path(os.environ.get('RECORDER_TOOLS', '.'))
NEW = pathlib.Path(os.environ.get('RECORDER_DATA', './data'))
OUT = NEW / 'output'
QUEUE = NEW / 'queue.json'
STATE = NEW / 'batch-state.json'
STOP = TOOLS / 'STOP'
GUARD_LOG = NEW / '无人值守-守护.log'
PY = '/usr/bin/python3'

POLL = 45
GRACE_MIN = 20         # recording 状态但无 screencapture 进程超过此值 => 卡死
                       # （验证/转码阶段没有采集进程属正常，故放宽到 20 分钟并豁免 ffmpeg 在跑的情况）
MIN_FREE_GB = 60
MAX_RESTARTS = 40      # 单夜上限，防止无限循环
MAX_FAILS_PER_URL = 2  # 同一课连续失败次数上限 => 转 blocklist


def log(msg):
    line = '%s %s' % (datetime.datetime.now().strftime('%m-%d %H:%M:%S'), msg)
    print(line, flush=True)
    GUARD_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(GUARD_LOG, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def read_state():
    try:
        return json.load(open(STATE, encoding='utf-8'))
    except Exception as e:
        log('读 state 失败: %r' % (e,))
        return None


def counts(state):
    c = {'completed': 0, 'pending': 0, 'recording': 0, 'failed': 0, 'other': 0}
    for v in (state or {}).get('items', {}).values():
        s = v.get('status')
        c[s if s in c else 'other'] += 1
    return c


def pgrep(pattern):
    try:
        out = subprocess.run(['pgrep', '-f', pattern], capture_output=True, text=True, timeout=15).stdout
        return [int(x) for x in out.split() if x.strip()]
    except Exception:
        return []


def windowserver_cpu():
    try:
        out = subprocess.run(['ps', '-Ao', 'pcpu,comm'], capture_output=True, text=True, timeout=15).stdout
        for line in out.splitlines():
            if 'WindowServer' in line:
                return line.split()[0]
    except Exception:
        pass
    return '?'


def free_gb():
    st = os.statvfs(str(NEW))
    return st.f_bavail * st.f_frsize / (1024 ** 3)


def build_queue(blocked):
    q = json.load(open(QUEUE, encoding='utf-8'))
    if not blocked:
        return QUEUE
    q['videos'] = [v for v in q['videos'] if v['url'] not in blocked]
    q['source'] = 'guard-filtered (blocked=%d)' % len(blocked)
    path = NEW / 'queue-guard.json'
    tmp = path.with_suffix('.tmp')
    json.dump(q, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def last_failed(state, since_ts):
    """返回本次守护运行以来最后一个 failed attempt 的 (url, title, 该课历史尝试数)。"""
    best = None
    for url, item in (state or {}).get('items', {}).items():
        for a in item.get('attempts') or []:
            if a.get('status') == 'failed' and (a.get('finished_at') or 0) >= since_ts:
                t = a.get('finished_at') or 0
                if best is None or t > best[0]:
                    best = (t, url, item.get('title'), len(item.get('attempts') or []))
    return (best[1], best[2], best[3]) if best else (None, None, 0)


def start_batch(queue_path, run_no):
    log_path = NEW / ('无人值守-运行%d-%s.log' % (run_no, datetime.datetime.now().strftime('%m%d-%H%M')))
    f = open(log_path, 'w', encoding='utf-8')
    p = subprocess.Popen(
        [PY, 'batch_course.py', '--queue', str(queue_path), '--state', str(STATE),
         '--output-dir', str(OUT), '--run', '--allow-foreground-click'],
        cwd=str(TOOLS), stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
    log('启动 batch pid=%d 队列=%s 日志=%s' % (p.pid, pathlib.Path(queue_path).name, log_path.name))
    return p


def diag(tag):
    state = read_state()
    c = counts(state)
    log('[诊断:%s] counts=%s WindowServer=%s%% 可用=%.0fGB screencapture=%s batch=%s' % (
        tag, c, windowserver_cpu(), free_gb(),
        pgrep('screencapture -v -V') or 'none', pgrep('batch_course.py --queue') or 'none'))
    for item in (state or {}).get('items', {}).values():
        if item.get('status') in ('recording', 'failed'):
            last = (item.get('attempts') or [{}])[-1]
            log('   %s | %s | attempts=%d | last=%s | %s' % (
                item.get('status'), item.get('title'), len(item.get('attempts') or []),
                last.get('status'), str(last.get('error'))[:160]))
    try:
        runs = sorted(NEW.glob('无人值守-运行*.log'), key=lambda p: p.stat().st_mtime)
        if runs:
            tail = runs[-1].read_text(encoding='utf-8', errors='replace').strip().splitlines()[-4:]
            for t in tail:
                log('   log| ' + t[:200])
    except Exception:
        pass


def remaining_work(state, blocked):
    """还有几课要录：pending + 未被 blocklist 的 failed。"""
    n = 0
    for url, item in (state or {}).get('items', {}).items():
        s = item.get('status')
        if s == 'pending' or (s == 'failed' and url not in blocked):
            n += 1
    return n


def main():
    started = time.time()
    run_no = int(os.environ.get('GUARD_RUN_BASE', '0'))
    restarts = 0
    blocked = set(os.environ.get('GUARD_BLOCK', '').split(',')) - {''}
    fails = {}
    proc = None
    stalled_since = None
    first = True
    log('守护启动 pid=%d POLL=%ds GRACE=%dmin MIN_FREE=%dGB' % (os.getpid(), POLL, GRACE_MIN, MIN_FREE_GB))

    start_at = os.environ.get('GUARD_START_AT')
    if start_at:
        now = datetime.datetime.now()
        h, m = [int(x) for x in start_at.split(':')]
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        wait_s = (target - now).total_seconds()
        log('等待到 %s 再开录（%.1f 小时）' % (target.strftime('%m-%d %H:%M'), wait_s / 3600))
        while time.time() < target.timestamp():
            if STOP.exists():
                log('等待期间检测到 STOP，退出')
                return 0
            time.sleep(min(300, max(5, target.timestamp() - time.time())))
        log('到达预约时间，开始调度')
    while True:
        if STOP.exists():
            log('检测到 STOP 文件，停止守护（不杀正在进行的录制）')
            return 0
        if free_gb() < MIN_FREE_GB:
            log('可用空间 %.0fGB 低于阈值，SIGTERM 当前 batch 并退出' % free_gb())
            for pid in pgrep('batch_course.py --queue'):
                try:
                    os.kill(pid, signal.SIGTERM)
                except Exception:
                    pass
            return 2

        state = read_state()
        c = counts(state)
        alive = pgrep('batch_course.py --queue')

        if first:
            diag('启动基线')
            first = False

        if not alive:
            stalled_since = None
            url, title, n_att = last_failed(state, started)
            if url:
                fails[url] = fails.get(url, 0) + 1
                log('失败记录: %s (本次守护第 %d 次, 历史尝试 %d 次)' % (title, fails[url], n_att))
                limit = 1 if n_att >= 5 else MAX_FAILS_PER_URL
                if fails[url] >= limit and url not in blocked:
                    blocked.add(url)
                    log('加入 blocklist（本次守护内暂不再重试，历史已失败 %d 次）: %s' % (n_att, title))
            if remaining_work(state, blocked) == 0:
                diag('收尾')
                log('没有待录课了，守护正常结束。completed=%d' % c['completed'])
                return 0
            if restarts >= MAX_RESTARTS:
                log('重启次数达到上限 %d，退出' % MAX_RESTARTS)
                return 3
            diag('重启前')
            run_no += 1
            restarts += 1
            try:
                proc = start_batch(build_queue(blocked), run_no)
            except Exception as e:
                log('启动 batch 失败: %r' % (e,))
            time.sleep(POLL)
            continue

        # batch 存活：卡死检测（验证/转码阶段没有采集进程是正常的，exempt）
        rec = [v for v in (state or {}).get('items', {}).values() if v.get('status') == 'recording']
        if rec and not pgrep('screencapture -v -V') and not pgrep('ffmpeg'):
            if stalled_since is None:
                stalled_since = time.time()
                log('警告: state=recording 但无 screencapture 进程，开始计时')
            elif (time.time() - stalled_since) / 60 >= GRACE_MIN:
                diag('疑似卡死')
                log('卡死确认（%.0f 分钟无采集进程），SIGTERM batch %s' % (
                    (time.time() - stalled_since) / 60, alive))
                for pid in alive:
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except Exception:
                        pass
                time.sleep(20)
                stalled_since = None
                continue
        else:
            stalled_since = None

        time.sleep(POLL)


if __name__ == '__main__':
    sys.exit(main())
