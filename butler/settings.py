"""Validated profile settings; activity and pause restrict opted-in capture."""
import json
import os
import re
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_LEGACY_BASE_KEYS = frozenset({
    'timezone', 'tone', 'persistence', 'dailyLimit',
    'quietEnabled', 'quietStart', 'quietEnd',
    'activityEnabled', 'notificationsEnabled',
})

_BOOL_KEYS = (
    'quietEnabled', 'activityEnabled', 'notificationsEnabled', 'paused',
    'lifetimeAwarenessEnabled', 'applySystemControls',
    'suppressWarningNotifications', 'interimAssistantMessages',
    'busyAckNotifications', 'longRunningNotifications',
    'skillNudgesEnabled', 'gatewayRestartNotifications',
    'gatewayMessageTimestamps', 'naturalStatusStyle',
    'phoneEnabled', 'ownerRingEnabled', 'quietHoursBreakthrough',
    'inboundScreeningEnabled',
)

_E164_RE = re.compile(r'^\+[1-9]\d{6,14}$')
_AGENT_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')


def defaults():
    return dict(
        displayName='',
        timezone='',
        tone='Balanced',
        persistence='Occasional',
        dailyLimit=None,
        maxUnanswered=5,
        minWakeGapMinutes=1,
        quietEnabled=False,
        quietStart='',
        quietEnd='',
        activityEnabled=False,
        notificationsEnabled=False,
        paused=False,
        lifetimeAwarenessEnabled=True,
        applySystemControls=False,
        suppressWarningNotifications=True,
        toolProgressMode='off',
        interimAssistantMessages=False,
        busyAckNotifications=False,
        longRunningNotifications=False,
        backgroundProcessNotifications='error',
        memoryNotifications='off',
        skillNudgesEnabled=False,
        gatewayRestartNotifications=False,
        gatewayMessageTimestamps=True,
        naturalStatusStyle=True,
        phoneEnabled=False,
        ownerRingEnabled=False,
        ownerPhone='',
        quietHoursBreakthrough=False,
        maxRingsPerHour=3,
        minRingGapSeconds=120,
        inboundScreeningEnabled=False,
        livekitAgentName='butler-voice',
        callerName='Butler',
    )


def quiet_human_preset(current=None):
    """Recommended low-noise human texting posture for system load controls."""
    base = dict(defaults() if current is None else current)
    base.update(
        lifetimeAwarenessEnabled=True,
        applySystemControls=True,
        suppressWarningNotifications=True,
        toolProgressMode='off',
        interimAssistantMessages=False,
        busyAckNotifications=False,
        longRunningNotifications=False,
        backgroundProcessNotifications='error',
        memoryNotifications='off',
        skillNudgesEnabled=False,
        gatewayRestartNotifications=False,
        gatewayMessageTimestamps=True,
        naturalStatusStyle=True,
    )
    return base


def validate(data):
    base = defaults()
    # Preserve existing profile files saved before newer settings keys existed,
    # while still rejecting unknown keys or incomplete arbitrary dicts.
    if isinstance(data, dict) and _LEGACY_BASE_KEYS.issubset(data) and set(data).issubset(base):
        merged = dict(base)
        merged.update(data)
        data = merged
    if not isinstance(data, dict) or set(data) != set(base):
        raise ValueError('Expected exactly the supported settings')
    display_name = data['displayName']
    if not isinstance(display_name, str) or len(display_name) > 80 or any(ord(c) < 32 for c in display_name):
        raise ValueError('Invalid displayName')
    if not isinstance(data['timezone'], str) or len(data['timezone']) > 128:
        raise ValueError('Invalid timezone')
    if data['timezone']:
        if data['timezone'].startswith('/') or '..' in data['timezone']:
            raise ValueError('Invalid IANA timezone')
        try:
            ZoneInfo(data['timezone'])
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('Invalid IANA timezone') from None
    if data['tone'] not in ('Gentle', 'Supportive', 'Balanced', 'Direct', 'Strict'):
        raise ValueError('Invalid tone')
    if data['persistence'] not in ('Minimal', 'Occasional', 'Regular', 'Persistent'):
        raise ValueError('Invalid persistence')
    limit = data['dailyLimit']
    if limit is not None and (type(limit) is not int or not 0 <= limit <= 1000):
        raise ValueError('Daily limit must be 0–1000 or unset')
    for key, hi in (('maxUnanswered', 100), ('minWakeGapMinutes', 1440)):
        if type(data[key]) is not int or not 0 <= data[key] <= hi:
            raise ValueError(f'{key} must be an integer 0-{hi}')
    for key in _BOOL_KEYS:
        if type(data[key]) is not bool:
            raise ValueError(f'{key} must be boolean')
    if data['toolProgressMode'] not in ('off', 'new', 'all'):
        raise ValueError('Invalid toolProgressMode')
    if data['backgroundProcessNotifications'] not in ('off', 'error', 'concise', 'result', 'all'):
        raise ValueError('Invalid backgroundProcessNotifications')
    if data['memoryNotifications'] not in ('off', 'on', 'verbose'):
        raise ValueError('Invalid memoryNotifications')
    for key in ('quietStart', 'quietEnd'):
        if not isinstance(data[key], str) or (data[key] and not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', data[key])):
            raise ValueError(f'Invalid {key}')
    if data['quietEnabled'] and (not data['quietStart'] or not data['quietEnd'] or data['quietStart'] == data['quietEnd']):
        raise ValueError('Quiet hours require distinct start and end times')
    if not isinstance(data['ownerPhone'], str) or len(data['ownerPhone']) > 20:
        raise ValueError('Invalid ownerPhone')
    if data['ownerPhone'] and not _E164_RE.fullmatch(data['ownerPhone']):
        raise ValueError('ownerPhone must be E.164 (e.g. +14155551234) or empty')
    if type(data['maxRingsPerHour']) is not int or not 1 <= data['maxRingsPerHour'] <= 20:
        raise ValueError('maxRingsPerHour must be an integer 1-20')
    if type(data['minRingGapSeconds']) is not int or not 30 <= data['minRingGapSeconds'] <= 86400:
        raise ValueError('minRingGapSeconds must be an integer 30-86400')
    if not isinstance(data['livekitAgentName'], str) or not _AGENT_NAME_RE.fullmatch(data['livekitAgentName']):
        raise ValueError('Invalid livekitAgentName')
    if (not isinstance(data['callerName'], str) or not 1 <= len(data['callerName'].strip()) <= 64
            or any(ord(ch) < 32 for ch in data['callerName'])):
        raise ValueError('Invalid callerName')
    cleaned = dict(data)
    cleaned['displayName'] = display_name.strip()
    return cleaned


def load(directory):
    try:
        with (Path(directory) / 'settings.json').open(encoding='utf-8') as file:
            return validate(json.load(file))
    except FileNotFoundError:
        return defaults()


def save(directory, data):
    data = validate(data)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, path = tempfile.mkstemp(prefix='.settings-', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump(data, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(path, directory / 'settings.json')
    finally:
        if os.path.exists(path):
            os.unlink(path)
    return data
