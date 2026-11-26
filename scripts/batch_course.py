"""Resumable capture of already-authorized web video. Default is read-only; --run opts into browser/recording.

Only the worker's new one-tab Chrome window is controlled. Other applications
must stay silent (system playback audio, never mic); do not change worker tabs.
MOV originals/failed attempts are retained. Smoke clips are samples, not courses.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import pathlib
import re
import subprocess
import sys
import time
import uuid

import record_course as recorder
from recording_schedule import StartWindowClosed, require_start_allowed

ROOT = pathlib.Path(__file__).resolve().parent
WORKER_LOCK = ROOT / '.batch-worker.lock'


def natural_key(text):
    return tuple((1, int(p)) if p.isdigit() else (0, p.casefold())
                 for p in re.split(r'(\d+)', text))


def validate_scan(folder):
    """Check saved evidence consistency, not authenticity or fresh remote state."""
    if not isinstance(folder, dict) or folder.get('scan_complete') is not True:
        raise ValueError('Folder requires scan_complete evidence')
    if folder.get('actual_url') != folder.get('url'):
        raise ValueError('Folder actual_url does not match scanned URL')
    entries = folder.get('entries')
    count = folder.get('entry_count')
    if not isinstance(entries, list):
        raise ValueError('Folder entries must be a list')
    if type(count) is not int or count != len(entries):
        raise ValueError('Folder entry_count does not match entries')
    pages = folder.get('scan_pages')
    if not isinstance(pages, list) or not pages:
        raise ValueError('Folder scan_pages evidence is missing')
    # Some collectors read an explicit empty-state instead of a scroll element.
    if (not entries and all(isinstance(p, dict) and p.get('empty') is True
                            and 'scroll' not in p
                            and all(type(p.get(key, 0)) is int and p.get(key, 0) == 0
                                    for key in ('collected', 'visible_rows')) for p in pages)
            and isinstance(folder.get('empty_state_readback'), str)
            and re.search(r'这是一个空文件夹|empty folder', folder['empty_state_readback'], re.I)):
        return
    if len(pages) < 2:
        raise ValueError('Folder scan needs stable bottom readback')
    previous = None
    collected = 0
    for page in pages:
        if not isinstance(page, dict):
            raise ValueError('Invalid scan page')
        seen, visible, scroll = page.get('collected'), page.get('visible_rows'), page.get('scroll')
        if (type(seen) is not int or type(visible) is not int
                or not 0 <= visible <= seen <= count or seen < collected or not isinstance(scroll, dict)):
            raise ValueError('Invalid scan counts/scroll evidence')
        values = [scroll.get(key) for key in ('before', 'after', 'max', 'height')]
        if not all(type(v) in (int, float) and math.isfinite(v) for v in values):
            raise ValueError('Invalid scan scroll numbers')
        before, after, maximum, height = values
        if not (0 <= before <= after <= maximum and height > 0):
            raise ValueError('Invalid scan scroll bounds')
        if ((previous is None and before != 0)
                or (previous is not None and (before != previous['after'] or maximum < previous['max']))):
            raise ValueError('Discontinuous scan scroll evidence')
        previous, collected = scroll, seen
    last, penultimate = pages[-1], pages[-2]
    if not (last['collected'] == penultimate['collected'] == count
            and last['scroll']['before'] == last['scroll']['after'] == last['scroll']['max']
            and penultimate['scroll']['after'] == penultimate['scroll']['max'] == last['scroll']['max']):
        raise ValueError('Folder scan did not reach a stable bottom/count')


def reachable_folders(data):
    """Follow exact folder IDs, never path-name uniqueness or year heuristics."""
    prefix = data['root_url'].split('/drive/folder/')[0]
    folder_pattern = re.escape(prefix) + r'/drive/folder/[A-Za-z0-9]+'
    entry_pattern = re.escape(prefix) + r'/(?:drive/folder|file)/[A-Za-z0-9]+'
    folders = {}
    for folder in data['folders']:
        validate_scan(folder)
        url, title, path = folder.get('url'), folder.get('title'), folder.get('folder_path')
        if not isinstance(url, str) or not re.fullmatch(folder_pattern, url) or url in folders:
            raise ValueError('Invalid or duplicate folder URL')
        if (not isinstance(title, str) or not title.strip() or not isinstance(path, list)
                or not all(isinstance(p, str) and p.strip() for p in path)):
            raise ValueError('Invalid folder title/path')
        for entry in folder['entries']:
            if (not isinstance(entry, dict) or not isinstance(entry.get('title'), str)
                    or not entry['title'].strip() or not isinstance(entry.get('url'), str)
                    or not re.fullmatch(entry_pattern, entry['url'])):
                raise ValueError('Invalid folder entry')
        folders[url] = folder
    pending = [(data['root_url'], [], None)]
    reached = set()
    while pending:
        url, path, title = pending.pop()
        if url not in folders:
            raise ValueError('Referenced child folder is unmounted/unscanned: ' + url)
        folder = folders[url]
        if folder['folder_path'] != path or (title is not None and folder['title'] != title):
            raise ValueError('Folder path/title does not match root entry graph: ' + url)
        if url in reached:
            continue
        reached.add(url)
        for entry in folder['entries']:
            if '/drive/folder/' in entry['url']:
                pending.append((entry['url'], path + [entry['title']], entry['title']))
    if reached != set(folders):
        raise ValueError('Inventory contains folders unreachable from root')
    return folders


def prepare_queue(data):
    if not isinstance(data, dict) or not re.fullmatch(
            r'https://[A-Za-z0-9.-]+\.[A-Za-z]{2,}/drive/folder/[A-Za-z0-9]+', data.get('root_url', '')):
        raise ValueError('Queue needs an exact HTTPS root folder URL')
    if not isinstance(data.get('folders'), list) or not isinstance(data.get('videos'), list):
        raise ValueError('Queue requires folders/videos lists')
    if not isinstance(data.get('inventory_complete'), bool):
        raise ValueError('Queue requires boolean inventory_complete')
    if data['inventory_complete']:
        if data.get('errors') != []:
            raise ValueError('Complete inventory requires an explicit empty errors list')
        if not any(isinstance(folder, dict) and folder.get('url') == data['root_url']
                   for folder in data['folders']):
            raise ValueError('Complete inventory is missing the exact root folder')
    memberships = None
    if data['inventory_complete']:
        folders = reachable_folders(data)
        memberships = {(entry['url'], entry['title'], tuple(folder['folder_path']))
                       for folder in folders.values() for entry in folder['entries']}
    rows = []
    for source in data['videos']:
        if not isinstance(source, dict):
            raise ValueError('Each video must be an object')
        aliases = source.get('aliases', [])
        if not isinstance(aliases, list):
            raise ValueError('Video entry aliases must be a list')
        for row in [source] + aliases:
            if not isinstance(row, dict) or row.get('url') != source.get('url'):
                raise ValueError('Video entry aliases require the same exact URL')
            recorder.validate_course_url(row.get('url', ''))
            if not isinstance(row.get('title'), str) or not row['title'].strip():
                raise ValueError('Video title is required')
            if not isinstance(row.get('folder_path'), list) or not all(
                    isinstance(part, str) and part.strip() for part in row['folder_path']):
                raise ValueError('folder_path must be a list of names')
            if memberships is not None and (row['url'], row['title'], tuple(row['folder_path'])) not in memberships:
                raise ValueError('Video identity does not match any reachable folder entry')
            rows.append({key: value for key, value in row.items() if key != 'aliases'})
    rows.sort(key=lambda v: (tuple(natural_key(p) for p in v['folder_path']), natural_key(v['title']), v['url']))
    unique = {}
    for row in rows:
        if row['url'] in unique:
            unique[row['url']]['aliases'].append(row)
        else:
            unique[row['url']] = dict(row, aliases=[])
    return dict(data, videos=list(unique.values()))


def atomic_json(path, data):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def completed_valid(item):
    try:
        attempt = item['attempts'][-1]
        output = pathlib.Path(attempt['output'])
        mp4 = output.with_suffix('.mp4')
        report = json.loads(output.with_suffix('.recording.json').read_text(encoding='utf-8'))
        receipt = report['batch_mp4']
        return bool(attempt['kind'] == 'full' and attempt['status'] == 'validated'
                    and report['status'] == 'validated' and report['url'] == item['url']
                    and report['file'] == str(output) and output.is_file() and output.stat().st_size > 0
                    and report['bytes'] == output.stat().st_size
                    and report['media']['passed'] is True and report['plan']['full_course'] is True
                    and report['source_ended_observed'] is True and report['content_goal_observed'] is True
                    and receipt['status'] == 'validated' and receipt['file'] == str(mp4)
                    and mp4.is_file() and mp4.stat().st_size > 0 and receipt['bytes'] == mp4.stat().st_size)
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return False


def ensure_disk(path, seconds):
    if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('Disk estimate requires finite positive seconds')
    path = pathlib.Path(path).resolve()
    while not path.exists():
        path = path.parent
    free = recorder.shutil.disk_usage(path).free
    required = 30 * 1024 ** 3 + math.ceil(seconds * 3_000_000 * 2)
    if free < required:
        raise RuntimeError('Low disk space: need 30GiB reserve plus two 3MB/sec copies; free=' + str(free) + ', required=' + str(required))
    return {'free_bytes': free, 'required_bytes': required}


def remux_mov(output):
    output = pathlib.Path(output).resolve()
    mp4 = output.with_suffix('.mp4')
    if mp4.exists():
        raise FileExistsError('MP4 already exists; refusing overwrite')
    sidecar = output.with_suffix('.recording.json')
    report = json.loads(sidecar.read_text(encoding='utf-8'))
    if report.get('status') != 'validated' or not report.get('media', {}).get('passed'):
        raise ValueError('Only validated MOV recordings can be remuxed')
    ensure_disk(mp4.parent, report['plan']['record_seconds'])
    ffmpeg = recorder.shutil.which('ffmpeg')
    if not ffmpeg:
        raise RuntimeError('ffmpeg is required')
    timeout = max(60, int(report['plan']['record_seconds'] / 4))
    result = subprocess.run([ffmpeg, '-nostdin', '-n', '-hide_banner', '-loglevel', 'error',
                             '-i', str(output), '-map', '0:v:0', '-map', '0:a:0', '-c', 'copy',
                             '-movflags', '+faststart', str(mp4)], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('Lossless remux failed (original retained): ' + result.stderr[-2000:])
    return verify_mp4(output, report, '-c copy')


def verify_mp4(output, report, provenance):
    mp4 = output.with_suffix('.mp4')
    ffmpeg = recorder.shutil.which('ffmpeg')
    if not ffmpeg:
        raise RuntimeError('ffmpeg is required')
    timeout = max(60, int(report['plan']['record_seconds'] / 4))
    decode = subprocess.run([ffmpeg, '-nostdin', '-hide_banner', '-xerror', '-i', str(mp4),
                             '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', '-'],
                            capture_output=True, text=True, timeout=timeout)
    audio = subprocess.run([ffmpeg, '-nostdin', '-hide_banner', '-xerror', '-i', str(mp4),
                            '-map', '0:a:0', '-af', 'volumedetect', '-f', 'null', '-'],
                           capture_output=True, text=True, timeout=timeout)
    media = recorder.assess_media_logs(decode.stderr, audio.stderr, report['plan']['record_seconds'])
    if decode.returncode or audio.returncode or not media['passed'] or not mp4.is_file():
        raise RuntimeError('MP4 playback decode/non-silence verification failed; all artifacts retained')
    receipt = {'status': 'validated', 'file': str(mp4), 'bytes': mp4.stat().st_size, 'media': media,
               'decode_log': decode.stderr[-10000:], 'audio_log': audio.stderr[-10000:], 'remux': provenance}
    report['batch_mp4'] = receipt
    atomic_json(output.with_suffix('.recording.json'), report)
    return receipt


def seed_legacy(queue, state_path, mov):
    """Explicit API: import a prior validated full MOV/MP4; never re-record it.

    Does not modify MOV/MP4 bytes. Adds MP4 validation to its existing manifest
    and persists batch completion. Caller must supply the real queue/state path.
    """
    mov = pathlib.Path(mov).expanduser().resolve()
    with worker_lock(WORKER_LOCK):
        report = json.loads(mov.with_suffix('.recording.json').read_text(encoding='utf-8'))
        url = report.get('url')
        if url not in {v['url'] for v in queue['videos']}:
            raise ValueError('Legacy recording URL is not in this exact queue')
        if not (report.get('status') == 'validated' and report.get('plan', {}).get('full_course') is True
                and report.get('source_ended_observed') is True and report.get('content_goal_observed') is True
                and report.get('media', {}).get('passed') is True and report.get('file') == str(mov)
                and mov.is_file() and mov.stat().st_size > 0 and report.get('bytes') == mov.stat().st_size):
            raise ValueError('Legacy MOV must have a validated complete matching manifest; no samples')
        state = load_state(queue, state_path, recover=True)
        item = state['items'][url]
        if item['status'] == 'completed' and completed_valid(item):
            return state
        if mov.with_suffix('.mp4').exists():
            receipt = verify_mp4(mov, report, 'existing MP4; verified without rewriting media')
        else:
            receipt = remux_mov(mov)
        item['attempts'].append({'kind': 'full', 'status': 'validated', 'output': str(mov),
                                 'legacy_import': True, 'mp4': receipt, 'finished_at': time.time()})
        if not completed_valid(item):
            raise RuntimeError('Legacy import failed completion gate')
        item.update(status='completed')
        item.pop('error', None)
        atomic_json(state_path, state)
        return state


def prepare_page(url, owned, marker):
    # A healthy 1080p file once exceeded the 45s ready budget (transient CDN
    # slowness; the same file probed to readyState=4 in about 8s minutes later).
    # Double the wait budget only; the ready condition (readyState>=2), the
    # login/permission block detection, and all downstream validation are unchanged.
    deadline = time.monotonic() + 90
    probe = '''JSON.stringify((()=>{
const v=document.querySelector('video');
const blocked=!!document.querySelector('input[type=password],input[autocomplete="one-time-code"]') ||
 /没有权限|无访问权限|无权访问|请求访问|access denied|permission denied|登录后|扫码登录|log in to|sign in to|文件已删除|文件不存在|文件已过期/i.test((document.body?.innerText||'').slice(0,1500));
return {blocked,ready:!!v&&v.readyState>=2,error:v?.error?String(v.error.message):null};
})())'''
    initial_navigation = True
    while True:
        status = owned_handle(owned, 'navigation' if initial_navigation else 'check', url=url)
        if status == 'present':
            initial_navigation = False
            state = recorder.chrome_eval(url, probe, chrome_window_id=owned['window_id'])
            if state.get('blocked') or state.get('error'):
                raise RuntimeError('Course page login/permission/player error; no auto-login or bypass')
            if state.get('ready'):
                break
        elif status != 'pending' or not initial_navigation:
            raise RuntimeError('Owned worker tab missing or navigation changed')
        if time.monotonic() >= deadline:
            raise RuntimeError('Course page did not load a ready video within 90 seconds')
        time.sleep(1)
    if owned_handle(owned, url=url) != 'present':
        raise RuntimeError('Owned worker tab missing or navigation changed')
    recorder.chrome_eval(url, 'JSON.stringify((()=>{document.title=' + json.dumps(marker) +
                         '+document.title;return {ok:true}})())', chrome_window_id=owned['window_id'])
    if owned_handle(owned, url=url) != 'present':
        raise RuntimeError('Owned worker tab missing or navigation changed')
    state = recorder.chrome_eval(url, recorder.PLAYER_STATE, chrome_window_id=owned['window_id'])
    if not state['title'].startswith(marker):
        raise RuntimeError('Unique worker capture title was not retained')
    recorder.make_plan(state)
    time.sleep(0.5)  # Let Chrome publish the new title to CoreGraphics.
    return state


def applescript(script):
    result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or 'Chrome AppleScript failed')
    return result.stdout.strip()


def owned_handle(owned, action='check', url=None):
    """Exact-ID access; 'navigation' alone permits initial blank URLs as pending."""
    # Only IDs returned by make new window are accepted, never titles/user tabs.
    for value in owned.values():
        if not re.fullmatch(r'[1-9][0-9]*', str(value)):
            raise ValueError('Invalid owned handle')
    lines = ['tell application "Google Chrome"', 'considering case', 'repeat with w in windows',
             'if (id of w as text) is ' + json.dumps(str(owned['window_id'])) + ' then',
             'repeat with t in tabs of w',
             'if (id of t as text) is ' + json.dumps(str(owned['tab_id'])) + ' then']
    if action == 'close':
        lines += ['close t', 'return "closed"']
    elif action in ('check', 'navigation'):
        if action == 'navigation' and url is None:
            raise ValueError('Navigation check requires an exact course URL')
        if url is not None:
            recorder.validate_course_url(url)
            lines += ['if (count of tabs of w) is not 1 then error "Owned window tabs changed"',
                      'if (id of active tab of w as text) is not ' + json.dumps(str(owned['tab_id'])) + ' then error "Owned active tab changed"']
            if action == 'navigation':
                lines += ['if (URL of t) is "about:blank" or (URL of t) is "chrome://newtab/" or (URL of t) is "chrome://new-tab-page/" then return "pending"']
            lines += ['if (URL of t) is not ' + json.dumps(url) + ' then error "Owned course URL changed (login or navigation)"']
        lines += ['return "present"']
    else:
        raise ValueError('Unknown owned handle action')
    lines += ['end if', 'end repeat', 'end if', 'end repeat', 'return "absent"', 'end considering', 'end tell']
    return applescript('\n'.join(lines))


@contextlib.contextmanager
def owned_window(url, on_created=None, prior_focus=None):
    """Publish verified creation IDs inside cleanup guard, before any checks/yield."""
    recorder.validate_course_url(url)
    created = applescript('\n'.join([
        'tell application "Google Chrome"', 'set w to make new window',
        'set URL of active tab of w to ' + json.dumps(url),
        'return (id of w as text) & ":" & (id of active tab of w as text)', 'end tell']))
    if not re.fullmatch(r'[1-9][0-9]*:[1-9][0-9]*', created):
        raise RuntimeError('Could not identify newly created Chrome window/tab; not closing anything')
    wid, tid = created.split(':')
    owned = {'window_id': wid, 'tab_id': tid}
    try:
        if on_created is not None:
            on_created(dict(owned))
        # Navigation may still be publishing its initial URL; only identity here.
        if owned_handle(owned) != 'present':
            raise RuntimeError('Newly created worker tab missing')
        if prior_focus is not None:
            from browser_focus import restore_focus
            restored = restore_focus(prior_focus, owned)
            if restored in ('skipped_prior_tab_changed', 'skipped_prior_window_gone',
                            'skipped_prior_app_gone', 'skipped_owned_window_changed',
                            'skipped_owned_url_changed'):
                raise RuntimeError('Original browser focus cannot be safely restored: ' + restored)
        yield owned
    finally:
        owned_handle(owned, 'close')
        if owned_handle(owned) != 'absent':
            raise RuntimeError('Could not verify exact worker tab closure; stopping')


@contextlib.contextmanager
def worker_lock(path):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+', encoding='utf-8') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Another course worker holds the lock; no process was killed') from exc
        try:
            handle.seek(0)
            handle.truncate()
            json.dump({'pid': os.getpid()}, handle)
            handle.flush()
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    # Do not unlink a lock inode: a waiting process may already hold it open.


def run_batch(queue, state_path, output_dir, limit=None, clip_seconds=None, stop_file=None, smoke_fixture=False, allow_foreground_click=False):
    queue = prepare_queue(queue)  # Direct API callers must pass the same scope gate as the CLI.
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0):
        raise ValueError('limit must be a positive integer')
    if clip_seconds is not None and (not isinstance(clip_seconds, (int, float)) or isinstance(clip_seconds, bool)
                                     or not math.isfinite(clip_seconds) or clip_seconds <= 0):
        raise ValueError('clip_seconds must be finite and positive')
    if smoke_fixture and not (limit is not None and limit <= 3 and clip_seconds is not None
                              and clip_seconds <= 30 and len(queue['videos']) <= 3):
        raise ValueError('Small smoke fixture requires <=3 videos, --limit 1..3 and --clip-seconds <=30')
    if not queue['inventory_complete'] and not smoke_fixture:
        raise ValueError('Inventory must be complete before --run (or explicitly use a small smoke fixture)')
    state_path = pathlib.Path(state_path).resolve()
    output_dir = pathlib.Path(output_dir).resolve()
    stop_file = pathlib.Path(stop_file) if stop_file else ROOT / 'STOP'
    with worker_lock(WORKER_LOCK):
        state = load_state(queue, state_path, recover=True)
        state['stop_reason'] = None
        atomic_json(state_path, state)
        attempted = 0
        for video in queue['videos']:
            if stop_file.exists():
                state['stop_reason'] = 'stop_file'
                break
            item = state['items'][video['url']]
            if item['status'] == 'completed' and completed_valid(item):
                continue
            if limit is not None and attempted >= limit:
                state['stop_reason'] = 'limit'
                break
            try:
                require_start_allowed(output_dir)
            except StartWindowClosed as exc:
                state['stop_reason'] = 'schedule_window_closed'
                state['schedule_message'] = str(exc)
                break
            prior_focus = None
            try:
                recorder.session_guard()
                if allow_foreground_click is True:
                    from browser_focus import capture_focus
                    prior_focus = capture_focus()
            except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                state['stop_reason'] = 'session_blocked'
                state['error'] = str(exc)
                break
            attempted += 1
            kind = 'full' if clip_seconds is None else 'sample'
            slug = re.sub(r'[^\w.-]+', '_', video['title'], flags=re.UNICODE)[:70].strip('._') or 'course'
            identity = hashlib.sha256(video['url'].encode()).hexdigest()[:12]
            output = output_dir / (slug + '-' + identity + '-' + kind + '-' + uuid.uuid4().hex + '.mov')
            attempt = {'kind': kind, 'clip_seconds': clip_seconds, 'status': 'recording',
                       'output': str(output), 'started_at': time.time(), 'owned_creation_pending': True}
            if prior_focus is not None:
                attempt['focus_before_creation'] = dict(prior_focus)
            item['attempts'].append(attempt)
            item.update(status='recording')
            item.pop('error', None)
            atomic_json(state_path, state)
            def on_created(owned):
                attempt['owned'] = dict(owned)
                attempt['owned_creation_pending'] = False
                # 2026-11-24 (user request): record on the LG external display at a
                # normal window size (1330x750), NOT full-screen. LG display is
                # 1440x3440 (portrait) at CGWindowList x [-1440, 0] (left of main).
                # A 1330x750 window at top-left x=-1385 y=-812 (bounds {-1385,-812,
                # -55,-62}) sits fully on the LG panel; x<0 is only reachable on the LG
                # display (main is x>=0). These exact bounds were verified in the
                # 2026-11-24 LG test (window landed on LG, PRIME_OK, native click armed
                # unmuted advancing playback). Full-screen on the main display was
                # reverted because it repeatedly re-wedged the WindowServer compositor
                # after the 2026-11-24 15:46 "UI disappeared" glitch (CPU 18%->38%,
                # capture 0 bytes). Synthetic clicks to the LG panel work while its
                # Space is active (a Space-routing change once broke them, see
                # 问题视频复核-46; the foreground_play focus-retry re-activates Chrome
                # and retries on "not frontmost"). The native-click guard compares live
                # CGWindowList bounds against the page viewport, so it adapts to this
                # size. Failing loudly here lets owned_window close the window.
                move = '\n'.join(['tell application "Google Chrome"', 'considering case',
                                  'repeat with w in windows',
                                  'if (id of w as text) is ' + json.dumps(str(owned['window_id'])) + ' then',
                                  'set bounds of w to {-1385, -812, -55, -62}',
                                  'return "moved"', 'end if',
                                  'end repeat', 'end considering',
                                  'error "owned window missing before move"', 'end tell'])
                mp = subprocess.run(['osascript', '-e', move], capture_output=True, text=True, timeout=20)
                if mp.returncode or mp.stdout.strip() != 'moved':
                    raise RuntimeError('Could not move owned course window to LG display: ' +
                                       (mp.stderr.strip() or mp.stdout.strip()))
                atomic_json(state_path, state)
            try:
                context = (owned_window(video['url'], on_created=on_created, prior_focus=prior_focus)
                           if prior_focus is not None else owned_window(video['url'], on_created=on_created))
                with context as owned:
                    marker = '[RC-' + uuid.uuid4().hex[:12] + '] '
                    attempt['capture_title_marker'] = marker
                    atomic_json(state_path, state)
                    player = prepare_page(video['url'], owned, marker)
                    plan = recorder.make_plan(player, clip_seconds)
                    attempt['disk_preflight'] = ensure_disk(output_dir, plan['record_seconds'])
                    if allow_foreground_click is True:
                        from foreground_play import prime_course_playback
                        attempt['interaction'] = prime_course_playback(video['url'], owned, player['title'],
                                                                       allow_foreground=True, prior_focus=prior_focus)
                        atomic_json(state_path, state)
                    report = recorder.record_course(video['url'], output, clip_seconds,
                                                    chrome_window_id=owned['window_id'],
                                                    expected_capture_title=player['title'])
                    if report.get('schedule_blocked') and not report.get('restore_error'):
                        raise StartWindowClosed(report.get('error', 'Recording start window closed'))
                    if report.get('status') != 'validated':
                        raise RuntimeError('Single recorder failed: ' + str(report.get('error', report)))
                    if not report.get('media', {}).get('passed') or not report.get('content_goal_observed'):
                        raise RuntimeError('Recording did not pass content/media validation')
                    if kind == 'full' and not (report.get('source_ended_observed') and report.get('plan', {}).get('full_course')):
                        raise RuntimeError('Full course end was not observed')
                    attempt['mp4'] = remux_mov(output)
                attempt.update(status='validated', finished_at=time.time(), owned_closed_verified=True)
                item['status'] = 'completed' if kind == 'full' else 'pending'
            except (Exception, KeyboardInterrupt) as exc:
                if 'owned' in attempt:
                    try:
                        attempt['owned_closed_verified'] = owned_handle(attempt['owned']) == 'absent'
                    except (Exception, KeyboardInterrupt):
                        attempt['owned_closed_verified'] = False
                if isinstance(exc, StartWindowClosed) and attempt.get('owned_closed_verified') is True:
                    attempt.update(status='not_started', error=str(exc), finished_at=time.time())
                    item['status'] = 'pending'
                    item.pop('error', None)
                    state['stop_reason'] = 'schedule_window_closed'
                    state['schedule_message'] = str(exc)
                else:
                    attempt.update(status='failed', error=str(exc), finished_at=time.time())
                    item.update(status='failed', error=str(exc))
                    state['stop_reason'] = 'failed'
                atomic_json(state_path, state)
                break
            atomic_json(state_path, state)
        atomic_json(state_path, state)
        return state


def assert_recovery_safe(state):
    """Refuse unresolved script-owned resources; never close/kill during recovery."""
    outputs = set()
    for item in state['items'].values():
        for attempt in item.get('attempts', []):
            if attempt.get('owned_creation_pending'):
                raise RuntimeError('Unresolved owned window creation requires manual cleanup; no handle was guessed')
            if 'owned' in attempt and attempt.get('owned_closed_verified') is not True:
                raise RuntimeError('Unverified old owned window/tab requires manual cleanup: '
                                   + json.dumps(attempt['owned'], ensure_ascii=False))
            if isinstance(attempt.get('output'), str) and attempt['output']:
                outputs.add(attempt['output'])
    if not outputs:
        return
    try:
        result = subprocess.run(['/bin/ps', '-ww', '-axo', 'pid=,args='],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError('Cannot verify old recorder processes; manual cleanup required') from exc
    if result.returncode or not result.stdout.strip():
        raise RuntimeError('Cannot verify old recorder processes; manual cleanup required')
    for line in result.stdout.splitlines():
        if not re.fullmatch(r'\s*[0-9]+\s+\S.*', line):
            raise RuntimeError('Invalid process snapshot; cannot verify old recorder processes')
        # Require this recorder's exact executable, flags and persisted output, not
        # a saved PID (which may be reused), a title, or a substring of user argv.
        match = re.fullmatch(r'\s*([1-9][0-9]*)\s+/usr/sbin/screencapture -v -V[0-9]+(?:\.[0-9]+)?'
                             r' -A -l[1-9][0-9]* -x (.+)', line)
        if match and match[2] in outputs:
            raise RuntimeError('Old recorder process ' + match[1] + ' still owns attempt output '
                               + match[2] + '; manual cleanup required; no process was killed')


def load_state(queue, path, recover=False):
    path = pathlib.Path(path)
    state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
        'version': 1, 'root_url': queue['root_url'], 'items': {}}
    if state.get('version') != 1 or state.get('root_url') != queue['root_url'] or not isinstance(state.get('items'), dict):
        raise ValueError('State does not belong to this root queue/version')
    if recover:
        assert_recovery_safe(state)  # Include historical items absent from today's queue, before mutation.
    for video in queue['videos']:
        item = state['items'].setdefault(video['url'], dict(video, status='pending', attempts=[]))
        item.update(video)
        if item.get('status') not in ('pending', 'recording', 'completed', 'failed'):
            raise ValueError('Unknown item status')
        if recover and item['status'] == 'recording':
            item.update(status='failed', error='interrupted recording; retained as incomplete')
            for attempt in item['attempts']:
                if attempt['status'] == 'recording':
                    attempt.update(status='failed', error=item['error'])
        if item['status'] == 'completed' and not completed_valid(item):
            item.update(status='failed', error='Completed artifact/validated manifest missing or invalid')
    return state


def summary(queue, state):
    items = [state['items'][v['url']] for v in queue['videos']]
    counts = {name: sum(item['status'] == name for item in items)
              for name in ('pending', 'recording', 'completed', 'failed')}
    next_item = next((item for item in items if item['status'] != 'completed'), None)
    return {'root_url': queue['root_url'], 'inventory_complete': queue['inventory_complete'],
            'folders': len(queue['folders']), 'videos': len(items), 'counts': counts,
            'validated_samples': sum(a.get('kind') == 'sample' and a.get('status') == 'validated'
                                     for item in items for a in item['attempts']),
            'stop_reason': state.get('stop_reason'),
            'next': {k: next_item[k] for k in ('url', 'title', 'folder_path', 'status')} if next_item else None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--queue', required=True, help='Parent-produced JSON inventory (never discovered or invented here)')
    parser.add_argument('--state', default=str(ROOT / 'batch-state.json'))
    parser.add_argument('--output-dir', default=str(ROOT / 'recordings'))
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--run', action='store_true', help='Explicit opt-in to real browser/capture')
    modes.add_argument('--status', action='store_true', help='Read-only compact JSON (default)')
    modes.add_argument('--dry-run', action='store_true', help='Same read-only summary, including next course')
    parser.add_argument('--limit', type=int, help='Maximum attempted jobs, not count including completed skips')
    parser.add_argument('--clip-seconds', type=float, help='Samples ONLY, never mark a full course completed')
    parser.add_argument('--smoke-fixture', action='store_true', help='Allow incomplete inventory only for <=3-video <=30s explicit samples')
    parser.add_argument('--allow-foreground-click', action='store_true', help='User-consented brief foreground native play click in each exact owned course window')
    parser.add_argument('--stop-file', default=str(ROOT / 'STOP'), help='Checked between jobs only; current job finishes safely')
    args = parser.parse_args(argv)
    try:
        queue = prepare_queue(json.loads(pathlib.Path(args.queue).read_text(encoding='utf-8')))
        if args.run:
            print('System playback audio only (no microphone): keep other applications silent; do not change tabs in the worker window.', file=sys.stderr)
            state = run_batch(queue, args.state, args.output_dir, limit=args.limit, clip_seconds=args.clip_seconds,
                              stop_file=args.stop_file, smoke_fixture=args.smoke_fixture, allow_foreground_click=args.allow_foreground_click)
        else:
            state = load_state(queue, args.state)
        result = summary(queue, state)
        result['mode'] = ('samples' if args.clip_seconds is not None else 'full') if args.run else 'status'
        print(json.dumps(result, ensure_ascii=False, separators=(',', ':')))
        return 1 if args.run and state.get('stop_reason') in ('failed', 'session_blocked') else 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False, separators=(',', ':')))
        return 1


if __name__ == '__main__':
    sys.exit(main())

