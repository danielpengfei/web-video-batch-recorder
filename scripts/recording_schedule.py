"""Deterministic course start windows. Does not interrupt existing captures."""
import datetime as dt
import json
import os
import pathlib
from zoneinfo import ZoneInfo

DEFAULT_TZ = os.environ.get('RECORDER_TZ', 'Etc/UTC')

BASE = pathlib.Path(os.environ.get('RECORDER_DATA', './data'))
POLICY_PATH = BASE / 'recording-schedule.json'
PROTECTED_OUTPUT = BASE / 'output'


class StartWindowClosed(RuntimeError):
    pass


def require_start_allowed(output, policy_path=POLICY_PATH, protected_output=PROTECTED_OUTPUT, now=None):
    output, protected = pathlib.Path(output).resolve(), pathlib.Path(protected_output).resolve()
    if output != protected and protected not in output.parents:
        return
    try:
        policy = json.loads(pathlib.Path(policy_path).read_text(encoding='utf-8'))
        allowed = window_allows(policy, now or dt.datetime.now(dt.timezone.utc))
    except Exception as exc:
        raise StartWindowClosed('Recording schedule cannot be verified; no capture started') from exc
    if not allowed:
        raise StartWindowClosed('Recording start window closed or manually paused; current captures are not interrupted')


def window_allows(policy, now):
    if now.tzinfo is None:
        raise ValueError('An aware clock is required')
    if not policy.get('enabled') or policy.get('manual_hold', False):
        return False
    local = now.astimezone(ZoneInfo(policy.get('timezone', DEFAULT_TZ)))
    until = policy.get('temporary_until')
    if until:
        deadline = dt.datetime.fromisoformat(until)
        if deadline.tzinfo is None:
            raise ValueError('Temporary deadline must have a timezone')
        if now < deadline:
            return True
    return local.hour >= 22 or local.hour < 7


def temporary_deadline(now):
    if now.tzinfo is None:
        raise ValueError('An aware clock is required')
    local = now.astimezone(ZoneInfo(DEFAULT_TZ))
    deadline = local.replace(hour=7, minute=0, second=0, microsecond=0)
    return deadline if local < deadline else deadline + dt.timedelta(days=1)
