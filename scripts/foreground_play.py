"""Consent-gated, exact-owned-window native play for authorized course capture."""
import re
import math
import json
import pathlib
import subprocess
import time
import record_course as recorder

# Native delivery is guarded again immediately before posting input. It never
# accepts an externally supplied window list and cannot click an arbitrary app.
NATIVE_CLICK = r'''import Foundation
import AppKit
import ApplicationServices
func fail(_ message:String)->Never { fputs(message+"\n",stderr); exit(1) }
let req=try JSONSerialization.jsonObject(with:Data(CommandLine.arguments.last!.utf8)) as! [String:Any]
guard let target=req["window_id"] as? UInt32, let x=req["x"] as? Double, let y=req["y"] as? Double,
 let marker=req["marker"] as? String, let width=req["outerWidth"] as? Double, let height=req["outerHeight"] as? Double,
 let windowX=req["windowX"] as? Double,let windowY=req["windowY"] as? Double,
 x.isFinite,y.isFinite,target>0 else { fail("Invalid native click request") }
guard AXIsProcessTrusted(), CGPreflightPostEventAccess() else { fail("Native input permission unavailable") }
guard let front=NSWorkspace.shared.frontmostApplication,front.bundleIdentifier=="com.google.Chrome" else { fail("Chrome is not frontmost") }
let all=CGWindowListCopyWindowInfo([.optionOnScreenOnly,.excludeDesktopElements],kCGNullWindowID) as? [[String:Any]] ?? []
let relevant=all.filter { ($0[kCGWindowOwnerPID as String] as? Int)==Int(front.processIdentifier) && ($0[kCGWindowLayer as String] as? Int)==0 && (($0[kCGWindowBounds as String] as? [String:NSNumber])?["Width"]?.doubleValue ?? 0)>300 }
guard let first=relevant.first,(first[kCGWindowNumber as String] as? UInt32)==target,
 (first[kCGWindowName as String] as? String ?? "").hasPrefix(marker) else { fail("Owned marked window is not frontmost") }
guard let b=first[kCGWindowBounds as String] as? [String:NSNumber] else { fail("Window bounds missing") }
let rect=CGRect(x:b["X"]?.doubleValue ?? .nan,y:b["Y"]?.doubleValue ?? .nan,width:b["Width"]?.doubleValue ?? 0,height:b["Height"]?.doubleValue ?? 0)
let p=CGPoint(x:x,y:y)
guard abs(rect.minX-windowX)<2,abs(rect.minY-windowY)<2,abs(rect.width-width)<2,abs(rect.height-height)<2,rect.contains(p) else { fail("Owned window moved/resized or click outside window") }
let old=CGEvent(source:nil)?.location
guard let down=CGEvent(mouseEventSource:nil,mouseType:.leftMouseDown,mouseCursorPosition:p,mouseButton:.left),
 let up=CGEvent(mouseEventSource:nil,mouseType:.leftMouseUp,mouseCursorPosition:p,mouseButton:.left) else { fail("Cannot create native mouse events") }
down.post(tap:.cghidEventTap);usleep(80000);up.post(tap:.cghidEventTap);usleep(150000)
if let old=old,let now=CGEvent(source:nil)?.location,abs(now.x-p.x)<2,abs(now.y-p.y)<2 { CGWarpMouseCursorPosition(old) }
let data=try JSONSerialization.data(withJSONObject:["clicked":true,"window_id":target])
print(String(data:data,encoding:.utf8)!)
'''

GEOMETRY = '''JSON.stringify((()=>{
const e=document.querySelector('xg-start.xgplayer-start');
if(!e)throw Error('No observed course start control');
const r=e.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;
const top=document.elementFromPoint(x,y);
return {title:document.title,hit_is_control:!!top&&(top===e||e.contains(top)),
 button:{x:r.x,y:r.y,width:r.width,height:r.height},
 viewport:{screenX,screenY,outerWidth,outerHeight,innerWidth,innerHeight}};
})())'''
RESET = '''JSON.stringify((()=>{const v=document.querySelector('video');
v.pause();v.playbackRate=1;v.currentTime=0;window.__capPlayError=null;
return {ok:true};})())'''


def prime_course_playback(url, owned, expected_title, allow_foreground=False, prior_focus=None):
    if allow_foreground is not True:
        raise PermissionError('Explicit foreground click consent required')
    recorder.validate_course_url(url)
    if set(owned) != {'window_id', 'tab_id'} or any(not re.fullmatch(r'[1-9][0-9]*', str(v)) for v in owned.values()):
        raise ValueError('Exact owned window/tab handles required')
    if not re.match(r'^\[RC-[0-9a-f]{12}\] ', expected_title):
        raise ValueError('Only uniquely marked worker pages may receive native input')
    wid, tid = str(owned['window_id']), str(owned['tab_id'])

    def run(script):
        p = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=20)
        if p.returncode:
            raise RuntimeError(p.stderr.strip() or 'Scoped Chrome action failed')
        return p.stdout.strip()

    def scoped(body):
        return '\n'.join(['tell application "Google Chrome"', 'considering case', 'repeat with w in windows',
            'if (id of w as text) is ' + json.dumps(wid) + ' then',
            'if (count of tabs of w) is not 1 then error "Worker tabs changed"',
            'if (id of active tab of w as text) is not ' + json.dumps(tid) + ' then error "Worker active tab changed"',
            'if (URL of active tab of w) is not ' + json.dumps(url) + ' then error "Worker URL changed"',
            body, 'end if', 'end repeat', 'error "Owned worker missing"', 'end considering', 'end tell'])

    def evaluate(expression):
        guard = '(()=>{if(document.title!==' + json.dumps(expected_title, ensure_ascii=False) + ')throw Error("Marked course identity changed");return (' + expression + ');})()'
        return json.loads(run(scoped('return execute active tab of w javascript ' + json.dumps(guard, ensure_ascii=False))))

    def reset():
        evaluate(RESET)
        deadline = time.monotonic() + 5
        while True:
            state = evaluate(recorder.PLAYER_STATE)
            if state.get('paused') and not state.get('seeking') and state.get('currentTime', 999) <= 0.25:
                return state
            if time.monotonic() >= deadline:
                raise RuntimeError('Could not pause and reset owned course after native interaction')
            time.sleep(0.15)

    previous = (prior_focus['bundle'] if prior_focus is not None else
                run('tell application "System Events" to get bundle identifier of first application process whose frontmost is true'))
    if not re.fullmatch(r'[A-Za-z0-9.-]+', previous):
        raise RuntimeError('Could not safely identify prior focus')
    initial = evaluate(recorder.PLAYER_STATE)
    recorder.make_plan(initial)
    receipt = {'status': 'starting', 'owned': dict(owned), 'method': 'consent-gated native play button', 'initial': initial}
    raised = False
    try:
        reset()
        if prior_focus is not None:
            # Page loading may outlive the user's current tab/app selection.
            # Snapshot again just before our brief native interaction.
            from browser_focus import capture_focus
            latest_focus = capture_focus()
            if latest_focus['window_id'] != wid:
                prior_focus = latest_focus
        receipt['raised'] = run(scoped('set index of w to 1\nactivate\nreturn "raised"'))
        raised = True
        time.sleep(0.3)
        cache = pathlib.Path.home() / '.cache/recorder-swift-module-cache'
        # Bounded focus retry (2026-11-24): the native click needs the owned
        # window frontmost at delivery. A brief focus loss (e.g. the agent chat
        # window) between the activate above and the click makes the Swift guard
        # fail with "not frontmost" BEFORE the click is posted, so no play/pause
        # toggle can occur and re-activate+retry is safe. Makes automatic course
        # transitions resilient to transient contention. Only pre-click
        # "not frontmost" failures retry; once the click is delivered, any
        # failure raises immediately (validation logic unchanged). Normal case
        # (Chrome frontmost) succeeds on the first iteration, identical to prior.
        click_attempts = 4
        last_err = 'unknown'
        for _ca in range(click_attempts):
            if _ca:
                run(scoped('set index of w to 1\nactivate\nreturn "raised"'))
                time.sleep(0.8)
            geometry = evaluate(GEOMETRY)
            plan = click_plan(geometry, expected_title)
            window = recorder.course_window(expected_title)
            plan['window_id'] = window['id']
            p = subprocess.run(['swift', '-module-cache-path', str(cache), '-e', NATIVE_CLICK, json.dumps(plan)], capture_output=True, text=True, timeout=40)
            if p.returncode:
                last_err = p.stderr.strip() or 'Native play button click failed'
                if 'not frontmost' not in last_err:
                    raise RuntimeError(last_err)
                continue
            native = json.loads(p.stdout)
            if native.get('clicked') is not True:
                raise RuntimeError('Native click did not return an explicit delivery receipt')
            receipt.update(native=native, click_plan=plan)
            time.sleep(0.9)
            after = evaluate(recorder.PLAYER_STATE)
            recorder.make_plan(after)
            if after.get('paused') or after.get('currentTime', 0) < 0.1 or abs(after.get('playbackRate', 1) - 1) > 0.001:
                raise RuntimeError('Native play did not produce verified unmuted advancing playback')
            receipt['after_click'] = after
            receipt['reset'] = reset()
            receipt['status'] = 'verified'
            return receipt
        raise RuntimeError('Native play button click failed after %d focus retries: %s' % (click_attempts, last_err))
    except BaseException:
        try:
            evaluate('JSON.stringify((()=>{document.querySelector("video").pause();return {ok:true}})())')
        except Exception:
            pass  # Never mutate a different/changed document to recover focus.
        raise
    finally:
        if raised:
            try:
                if prior_focus is not None:
                    from browser_focus import restore_focus
                    receipt['prior_focus'] = dict(prior_focus)
                    receipt['focus_restore'] = restore_focus(prior_focus, owned, expected_url=url)
                    if receipt['focus_restore'] in ('skipped_prior_tab_changed', 'skipped_prior_window_gone',
                                                    'skipped_prior_app_gone', 'skipped_owned_window_changed',
                                                    'skipped_owned_url_changed'):
                        raise RuntimeError('Original browser focus could not be safely restored')
                else:
                    restore = '\n'.join(['tell application "System Events"',
                        'set currentBundle to bundle identifier of (first application process whose frontmost is true)',
                        'if currentBundle is not "com.google.Chrome" then return "skipped_user_focus_changed"',
                        'if (count of (application processes whose bundle identifier is ' + json.dumps(previous) + ')) is 0 then return "skipped_prior_app_gone"',
                        'end tell', 'if (id of front window as text) is not ' + json.dumps(wid) + ' then return "skipped_user_window_changed"',
                        'tell application id ' + json.dumps(previous) + ' to activate', 'return "restored"'])
                    receipt['focus_restore'] = run(scoped(restore))
            except Exception as exc:
                receipt['focus_restore_error'] = str(exc)
                if receipt['status'] == 'verified':
                    raise RuntimeError('Could not restore brief foreground focus: ' + str(exc)) from exc


def click_plan(view, expected_title):
    marker = re.match(r'^\[RC-[0-9a-f]{12}\] ', expected_title)
    if not marker or view.get('title') != expected_title or view.get('hit_is_control') is not True:
        raise ValueError('Changed/occluded/unmarked course; no native input')
    button, viewport = view['button'], view['viewport']
    values = [button[k] for k in ('x', 'y', 'width', 'height')] + [viewport[k] for k in ('screenX', 'screenY', 'outerWidth', 'outerHeight', 'innerWidth', 'innerHeight')]
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        raise ValueError('Invalid control geometry')
    if not (button['width'] > 0 and button['height'] > 0 and viewport['innerWidth'] > 0 and viewport['innerHeight'] > 0
            and 0 <= button['x'] < button['x'] + button['width'] <= viewport['innerWidth']
            and 0 <= button['y'] < button['y'] + button['height'] <= viewport['innerHeight']
            and 0 <= viewport['outerWidth'] - viewport['innerWidth'] <= 16
            and 0 <= viewport['outerHeight'] - viewport['innerHeight'] <= 400):
        raise ValueError('Control outside visible viewport or unexpected browser zoom/chrome')
    return {'x': viewport['screenX'] + (viewport['outerWidth'] - viewport['innerWidth']) / 2 + button['x'] + button['width'] / 2,
            'y': viewport['screenY'] + viewport['outerHeight'] - viewport['innerHeight'] + button['y'] + button['height'] / 2,
            'marker': marker.group(),
            'windowX': viewport['screenX'], 'windowY': viewport['screenY'],
            'outerWidth': viewport['outerWidth'], 'outerHeight': viewport['outerHeight']}
