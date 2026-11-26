"""Local, permission-respecting recorder for already-authorized pages."""
import re
import math
from recording_schedule import StartWindowClosed, require_start_allowed


def make_plan(state, clip_seconds=None):
    duration = state.get('duration', 0)
    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 0 < duration <= 14400:
        raise ValueError('Video duration must be finite, positive and no longer than four hours')
    if state.get('readyState', 0) < 2 or state.get('muted', True) or state.get('volume', 0) <= 0:
        raise ValueError('Video must be loaded and unmuted with nonzero volume')
    if state.get('error') or state.get('playError'):
        raise ValueError('Player already reports an error; no recording started')
    if clip_seconds is not None and (not math.isfinite(clip_seconds) or clip_seconds <= 0):
        raise ValueError('Clip duration must be finite and positive')
    content = duration if clip_seconds is None else min(duration, clip_seconds)
    return {'content_seconds': content, 'record_seconds': math.ceil(content) + 8,
            'full_course': content == duration, 'source_duration': duration}


class PlaybackAudit:
    """Conservative continuity checks; never a proof of perceptual AV quality."""
    def __init__(self, duration, content_seconds=None):
        self.duration = duration
        self.target = duration if content_seconds is None else content_seconds
        self.previous_position = 0.0
        self.previous_elapsed = 0.0
        self.goal = False
        self.source_ended = False

    def observe(self, state, elapsed):
        position = state.get('currentTime')
        if not isinstance(position, (float, int)) or not math.isfinite(position) or position < 0:
            raise RuntimeError('Invalid playback position')
        if not math.isfinite(elapsed) or elapsed < self.previous_elapsed:
            raise RuntimeError('Invalid playback clock')
        if abs(state.get('duration', 0) - self.duration) > 0.1:
            raise RuntimeError('Source duration changed')
        audit = state.get('audit') or {}
        if audit.get('seeks', 0) or audit.get('rate_changes', 0) or audit.get('mutes', 0):
            raise RuntimeError('Playback seek/rate/mute event observed')
        if abs(state.get('playbackRate', 1) - 1) > 0.001:
            raise RuntimeError('Playback rate changed')
        if not self.goal and (state.get('muted') or state.get('volume', 0) <= 0):
            raise RuntimeError('Playback became silent')
        delta = position - self.previous_position
        wall_delta = elapsed - self.previous_elapsed
        if delta < -0.25 or delta > wall_delta + 1.5:
            raise RuntimeError('Playback jump or rewind detected')
        if state.get('paused') and not state.get('ended') and not self.goal and position < self.target:
            raise RuntimeError('Unexpected playback pause')
        if state.get('ended'):
            if position < self.duration - 0.25 or elapsed < self.duration - 2:
                raise RuntimeError('Source ended before continuous playback could complete')
            self.source_ended = True
        if position >= self.target and elapsed >= self.target - 2:
            self.goal = True
        self.previous_position = position
        self.previous_elapsed = elapsed
        return {'content_complete': self.goal, 'source_ended': self.source_ended,
                'position': position, 'elapsed': elapsed}


def validate_course_url(url):
    if not re.fullmatch(r'https://[A-Za-z0-9.-]+\.[A-Za-z]{2,}/file/[A-Za-z0-9]+', url):
        raise ValueError('Expected an exact HTTPS file URL without query credentials')
    return url


def assess_media_logs(decode_log, audio_log, expected_seconds):
    duration_match = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', decode_log)
    duration = (int(duration_match[1]) * 3600 + int(duration_match[2]) * 60 + float(duration_match[3])) if duration_match else 0
    counts = re.findall(r'frame=\s*(\d+)', decode_log)
    frames = int(counts[-1]) if counts else 0
    peak_match = re.search(r'max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB', audio_log)
    peak = float(peak_match[1]) if peak_match else None
    reasons = []
    if duration < expected_seconds - 1.5:
        reasons.append('recording shorter than expected')
    if frames < 6:
        reasons.append('too few decoded video frames; needs review')
    if peak is None or peak <= -65:
        reasons.append('missing or effectively silent audio')
    return {'passed': not reasons, 'duration_seconds': duration, 'frames': frames,
            'audio_peak_dbfs': peak, 'reasons': reasons,
            'scope': 'decode and non-silence checks only; not speech identity or sync verification'}


# The OS/browser integration is exercised by test_live_full_course_recording.
import argparse
import json
import pathlib
import shutil
import signal
import subprocess
import sys
import time
import os
import uuid
import contextlib
import threading


@contextlib.contextmanager
def termination_guard():
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError('Recorder must run on the main thread for reliable termination handling')
    previous = signal.getsignal(signal.SIGTERM)
    def terminate(signum, frame):
        raise RuntimeError('SIGTERM requested; capture is incomplete and will be cleaned up')
    signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def capture_disk_guard(path, reserve=30 * 1024 ** 3):
    free = shutil.disk_usage(path).free
    if free < reserve:
        raise RuntimeError('Low disk space during capture; stopping before reserve is exhausted')
    return {'free_bytes': free, 'reserve_bytes': reserve}


def session_guard():
    """Read-only macOS session check; never unlock or synthesize input."""
    swift = '''import Foundation
import AppKit
import CoreGraphics
let s=CGSessionCopyCurrentDictionary() as? [String:Any] ?? [:]
let info:[String:Any] = ["screen_locked":s["CGSSessionScreenIsLocked"] ?? NSNull(),
 "on_console":s[kCGSessionOnConsoleKey as String] ?? NSNull(),
 "frontmost_bundle":NSWorkspace.shared.frontmostApplication?.bundleIdentifier ?? "unknown"]
let data=try JSONSerialization.data(withJSONObject:info)
print(String(data:data,encoding:.utf8)!)'''
    cache = pathlib.Path.home() / '.cache/recorder-swift-module-cache'
    try:
        result = subprocess.run(['swift', '-module-cache-path', str(cache), '-e', swift],
                                capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise ValueError('Session query failed')
        snapshot = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise RuntimeError('Cannot verify macOS session; no recording allowed') from exc
    if not isinstance(snapshot, dict) or snapshot.get('on_console') is not True or \
            'screen_locked' not in snapshot or not any(snapshot['screen_locked'] is x for x in (None, False, True)):
        raise RuntimeError('Cannot verify active macOS session')
    front = snapshot.get('frontmost_bundle')
    if not isinstance(front, str) or not re.fullmatch(r'[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+', front):
        raise RuntimeError('Cannot verify foreground macOS session')
    if snapshot['screen_locked'] is True or front == 'com.apple.loginwindow' or 'screensaver' in front.lower():
        raise RuntimeError('macOS session is locked or obscured; unlock manually before recording')
    return snapshot


def atomic_report(path, report):
    path = pathlib.Path(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
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


START_PLAYBACK = '''JSON.stringify((()=>{
const v=document.querySelector('video');
if(v.seeking||v.readyState<2) throw new Error('Video still seeking/loading before playback');
if(window.__capRemoveAudit) window.__capRemoveAudit();
const counts={seeks:0,rate_changes:0,mutes:0};let started=false;
const playing=()=>{started=true};
const seeking=()=>{if(started)counts.seeks++};
const rate=()=>{if(started&&Math.abs(v.playbackRate-1)>0.001)counts.rate_changes++};
const volume=()=>{if(started&&(v.muted||v.volume<=0))counts.mutes++};
const entries=[['playing',playing],['seeking',seeking],['ratechange',rate],['volumechange',volume]];
for(const [name,handler] of entries)v.addEventListener(name,handler);
window.__capAuditCounts=counts;
window.__capRemoveAudit=()=>{for(const [name,handler] of entries)v.removeEventListener(name,handler);
delete window.__capAuditCounts;delete window.__capRemoveAudit;};
v.play().catch(e=>window.__capPlayError=String(e));return {requested:true};
})())'''


PLAYER_STATE = '''JSON.stringify((()=>{
const v=document.querySelector('video');
if(!v) throw new Error('No video element in authorized course page');
return {title:document.title,paused:v.paused,currentTime:v.currentTime,duration:v.duration,
readyState:v.readyState,muted:v.muted,volume:v.volume,ended:v.ended,
playbackRate:v.playbackRate,width:v.videoWidth,height:v.videoHeight,
playError:window.__capPlayError||null,error:v.error?String(v.error.message):null,
seeking:v.seeking,audit:window.__capAuditCounts||null};
})())'''


def chrome_eval(url, expression, chrome_window_id=None):
    validate_course_url(url)
    if chrome_window_id is not None and (isinstance(chrome_window_id, bool) or
            not re.fullmatch(r'[1-9][0-9]*', str(chrome_window_id))):
        raise ValueError('Chrome window ID must be a positive integer')
    quoted = json.dumps(url)
    lines = ['tell application "Google Chrome"', 'considering case', 'repeat with w in windows']
    if chrome_window_id is not None:
        lines += ['if (id of w as text) is ' + json.dumps(str(chrome_window_id)) + ' then',
                  'if (count of tabs of w) is not 1 then error "Owned window tabs changed"']
    lines += ['repeat with t in tabs of w', 'if (URL of t) is ' + quoted + ' then',
              'if (URL of active tab of w) is not ' + quoted + ' then error "Authorized course tab is not active"',
              'return execute t javascript ' + json.dumps(expression, ensure_ascii=False), 'end if', 'end repeat']
    if chrome_window_id is not None:
        lines += ['error "Owned course URL changed"', 'end if']
    lines += ['end repeat', 'error "Authorized course tab not found"', 'end considering', 'end tell']
    script = '\n'.join(lines)
    result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout)


def select_capture_window(title, info):
    # macOS may ellipsize the middle of long Chrome window titles. Match only
    # a short unique marker on an owned page; never a shortened course name.
    marker = re.match(r'^\[RC-[0-9a-f]{12}\] ', title)
    if marker:
        matches = [w for w in info.get('windows', []) if w.get('title', '').startswith(marker.group(0))]
    else:
        matches = [w for w in info.get('windows', []) if w.get('title') == title or w.get('title', '').startswith(title + ' - ')]
    if not info.get('permission') or len(matches) != 1:
        raise RuntimeError('Need recording permission and exactly one matching course window; permission=' +
                           str(info.get('permission')) + ', matches=' + str(len(matches)))
    return matches[0]


def course_window(title):
    swift = '''import Foundation
import CoreGraphics
let all = CGWindowListCopyWindowInfo([.optionOnScreenOnly,.excludeDesktopElements], kCGNullWindowID) as? [[String:Any]] ?? []
let matches = all.filter { w in
 let owner = w[kCGWindowOwnerName as String] as? String ?? ""
 let layer = w[kCGWindowLayer as String] as? Int ?? -1
 return owner == "Google Chrome" && layer == 0
}.map { w -> [String:Any] in ["id":w[kCGWindowNumber as String] ?? 0,"title":w[kCGWindowName as String] ?? ""] }
let data = try JSONSerialization.data(withJSONObject:["permission":CGPreflightScreenCaptureAccess(),"windows":matches])
print(String(data:data,encoding:.utf8)!)'''
    cache = pathlib.Path.home() / '.cache/recorder-swift-module-cache'
    result = subprocess.run(['swift', '-module-cache-path', str(cache), '-e', swift], capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return select_capture_window(title, json.loads(result.stdout))


@termination_guard()
def record_course(url, output, clip_seconds=None, chrome_window_id=None, expected_capture_title=None):
    validate_course_url(url)

    expected_title = expected_capture_title

    def evaluate(expression):
        if chrome_window_id is not None and expected_title is not None:
            expression = '(()=>{if(document.title!==' + json.dumps(expected_title) + \
                ')throw new Error("Course document changed; no playback/restore allowed");return (' + expression + ');})()'
        return chrome_eval(url, expression, chrome_window_id=chrome_window_id)
    output = pathlib.Path(output).expanduser().resolve()
    if output.suffix.lower() != '.mov':
        raise ValueError('Output must use .mov')
    sidecar = output.with_suffix('.recording.json')
    if output.exists() or sidecar.exists():
        raise FileExistsError('Recording or its manifest already exists; refusing overwrite')
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise RuntimeError('ffmpeg is required for verification; no recording started')
    session = session_guard()
    initial = evaluate(PLAYER_STATE)
    if expected_capture_title is not None and initial['title'] != expected_capture_title:
        raise RuntimeError('Capture identity title changed during batch handoff')
    expected_title = initial['title']
    plan = make_plan(initial, clip_seconds)
    window = course_window(initial['title'])
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {'status': 'starting', 'url': url, 'file': str(output), 'initial': initial,
              'session_preflight': session,
              'plan': plan, 'window': window, 'samples': [],
              'disk_preflight': capture_disk_guard(output.parent),
              'audio_scope': 'system playback, not microphone; other apps must remain silent',
              'limitations': ['Automated checks do not verify speech identity or perceptual AV sync',
                              'Keep this Chrome window on the authorized course tab during recording']}
    with sidecar.open('x', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    process = None
    try:
        # Pause and seek before capture; playback only starts after the recorder is running.
        evaluate("JSON.stringify((()=>{const v=document.querySelector('video');v.pause();v.currentTime=0;v.playbackRate=1;window.__capPlayError=null;return {ok:true}})())")
        # Admission is checked at actual capture, never inside an existing recording.
        require_start_allowed(output)
        process = subprocess.Popen(['/usr/sbin/screencapture', '-v', '-V' + str(plan['record_seconds']),
                                    '-A', '-l' + str(window['id']), '-x', str(output)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        started = time.monotonic()
        report['recorder_pid'] = process.pid
        atomic_report(sidecar, report)
        time.sleep(1.2)
        if process.poll() is not None:
            raise RuntimeError('Recorder exited before playback')
        play_started = time.monotonic()
        evaluate(START_PLAYBACK)
        continuity = PlaybackAudit(plan['source_duration'], plan['content_seconds'])
        last_time = 0.0
        last_advance = time.monotonic()
        goal = False
        ended = False
        while process.poll() is None:
            time.sleep(2.5)
            session = session_guard()
            state = evaluate(PLAYER_STATE)
            # Preserve the exact failing state even when the continuity gate raises
            # before it can enter the successful-samples list. No rules relaxed.
            report['last_observation'] = {'elapsed': round(time.monotonic() - started, 3),
                                          'player': state, 'session': session}
            if state.get('playError') or state.get('error'):
                raise RuntimeError('Player reported an error: ' + str(state.get('playError') or state.get('error')))
            disk = capture_disk_guard(output.parent)
            progress = continuity.observe(state, time.monotonic() - play_started)
            report['samples'].append({'elapsed': round(time.monotonic()-started, 3), 'player': state, 'disk': disk, 'session': session})
            report['playback_audit'] = progress
            if state['currentTime'] > last_time + 0.15:
                last_time = state['currentTime']
                last_advance = time.monotonic()
            ended = progress['source_ended']
            if plan['full_course']:
                goal = ended and progress['content_complete']
            elif progress['content_complete']:
                goal = True
                evaluate("JSON.stringify((()=>{document.querySelector('video').pause();return {ok:true}})())")
            if not goal and time.monotonic() - last_advance > 15:
                raise RuntimeError('Playback stopped progressing for 15 seconds; recording is incomplete')
            if time.monotonic() - started > plan['record_seconds'] + 15:
                raise RuntimeError('Recorder exceeded its bounded runtime')
        stderr = process.communicate(timeout=10)[1]
        report['recorder_exit'] = process.returncode
        report['recorder_stderr'] = stderr[-4000:]
        report['source_ended_observed'] = ended
        report['content_goal_observed'] = goal
        if process.returncode != 0 or not output.exists() or not goal:
            raise RuntimeError('Recorder/player did not complete the requested content')
        verification_timeout = max(60, int(plan['content_seconds'] / 8))
        decode = subprocess.run([ffmpeg, '-nostdin', '-hide_banner', '-i', str(output), '-f', 'null', '-'],
                                capture_output=True, text=True, timeout=verification_timeout)
        audio = subprocess.run([ffmpeg, '-nostdin', '-hide_banner', '-i', str(output), '-map', '0:a:0',
                               '-af', 'volumedetect', '-f', 'null', '-'],
                               capture_output=True, text=True, timeout=verification_timeout)
        report['decode_log'] = decode.stderr[-10000:]
        report['audio_log'] = audio.stderr[-10000:]
        report['media'] = assess_media_logs(decode.stderr, audio.stderr, plan['record_seconds'])
        if decode.returncode or audio.returncode or not report['media']['passed']:
            raise RuntimeError('Media failed decode/non-silence checks; preserved for diagnosis')
        report['representative_frames'] = []
        for number, fraction in enumerate([0.08, 0.5, 0.9], 1):
            frame = output.with_name(output.stem + '.frame-' + str(number) + '.png')
            sample_time = round(plan['record_seconds'] * fraction, 2)
            result = subprocess.run([ffmpeg, '-nostdin', '-n', '-hide_banner', '-loglevel', 'error',
                                     '-ss', str(sample_time), '-i', str(output), '-frames:v', '1', str(frame)],
                                    capture_output=True, text=True, timeout=30)
            if result.returncode or not frame.exists():
                raise RuntimeError('Representative frame extraction failed')
            report['representative_frames'].append({'seconds': sample_time, 'path': str(frame)})
        report['status'] = 'validated'
        report['bytes'] = output.stat().st_size
    except StartWindowClosed as exc:
        report['status'] = 'not_started'
        report['schedule_blocked'] = True
        report['error'] = str(exc)
    except BaseException as exc:
        report['status'] = 'failed'
        report['error'] = str(exc)
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
    finally:
        try:
            body = "if(window.__capRemoveAudit)window.__capRemoveAudit();const v=document.querySelector('video');v.pause();v.currentTime=" + json.dumps(initial['currentTime']) + ";v.playbackRate=" + json.dumps(initial['playbackRate']) + ";"
            if not initial['paused']:
                body += "v.play().catch(e=>window.__capPlayError=String(e));"
            evaluate('JSON.stringify((()=>{' + body + 'return {ok:true}})())')
            report['restored'] = evaluate(PLAYER_STATE)
        except Exception as exc:
            report['restore_error'] = str(exc)
            report['status'] = 'failed'
        atomic_report(sidecar, report)
    return report


def main():
    parser = argparse.ArgumentParser(description='Record one already-open authorized page, then validate and restore playback state')
    parser.add_argument('--url', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--clip-seconds', type=float, default=None)
    args = parser.parse_args()
    try:
        report = record_course(args.url, args.output, args.clip_seconds)
    except Exception as exc:
        report = {'status': 'failed', 'error': str(exc)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'validated' else 1


if __name__ == '__main__':
    sys.exit(main())
