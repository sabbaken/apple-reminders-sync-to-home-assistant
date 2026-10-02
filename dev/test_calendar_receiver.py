#!/usr/bin/python3
"""Synthetic calendar receiver smoke test on the dev HA only; no EventKit reads.

Run `make calendar-e2e`; a fresh token is minted internally. This only replaces
snapshots for source_id=rhs-calendar-smoke-test, leaving other sources intact.
"""
import sys, time, datetime as dt
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import reminders_ha_sync as rhs
from tests.test_calendar import snapshot
import subprocess
# Mint fresh dev token without printing it.
token = subprocess.check_output(['make', '-s', 'ha-token'], text=True).strip()
ha = rhs.HomeAssistant('http://127.0.0.1:8124', token)
flow = ha._request('POST', '/api/config/config_entries/flow', {'handler':'apple_calendar_sync'})
if flow.get('type') == 'form':
    result = ha._request('POST', '/api/config/config_entries/flow/' + flow['flow_id'], {})
    assert result['type'] == 'create_entry', result
else:
    assert flow.get('reason') == 'already_configured', flow
payload = snapshot()
payload['source_id'] = 'rhs-calendar-smoke-test'
for c in payload['calendars']: c['name'] = 'RHS Calendar smoke test'
# Keep all events within today's window for state and queries.
today = dt.datetime.now(dt.timezone.utc).date()
payload['range_start'] = (today - dt.timedelta(days=1)).isoformat() + 'T00:00:00Z'
payload['range_end'] = (today + dt.timedelta(days=365)).isoformat() + 'T00:00:00Z'
payload['calendars'][0]['events'][0]['start'] = today.isoformat()
payload['calendars'][0]['events'][1]['start'] = today.isoformat() + 'T13:00:00Z'
payload['calendars'][0]['events'][1]['end'] = today.isoformat() + 'T14:00:00Z'
payload['calendars'][0]['events'][0]['end'] = (today + dt.timedelta(days=1)).isoformat()
for attempt in range(20):
    try:
        result = ha._request('POST','/api/apple_calendar_sync/snapshot', payload)
        break
    except rhs.UserError as e:
        if '503' not in str(e): raise
        time.sleep(1)
else: raise AssertionError('receiver did not start')
assert result == {'calendars': 2, 'events': 2}, result
for attempt in range(20):
    states = ha._request('GET', '/api/states')
    calendars = [s for s in states if s['entity_id'].startswith('calendar.') and 'RHS Calendar smoke test' in s.get('attributes',{}).get('friendly_name','')]
    if len(calendars) == 2: break
    time.sleep(1)
assert len(calendars) == 2, calendars
entity = next(s['entity_id'] for s in calendars if 'iCloud' in s['attributes']['friendly_name'])
path = '/api/calendars/' + entity + '?start=' + today.isoformat() + 'T00:00:00Z&end=' + (today+dt.timedelta(days=2)).isoformat()+'T00:00:00Z'
assert len(ha._request('GET',path)) == 2
try:
    rhs.HomeAssistant('http://127.0.0.1:8124', '')._request('POST', '/api/apple_calendar_sync/snapshot', payload, authenticated=False)
except rhs.UserError as e:
    assert '401' in str(e), e
else:
    raise AssertionError('unauthenticated publish accepted')
bad = dict(payload, calendars=[])
try: ha._request('POST','/api/apple_calendar_sync/snapshot',bad)
except rhs.UserError as e: assert '400' in str(e), e
else: raise AssertionError('empty snapshot accepted')
assert len(ha._request('GET',path)) == 2
payload['calendars'][0]['events'] = [payload['calendars'][0]['events'][0]]
payload['calendars'][0]['events'][0]['summary'] = 'Updated event'
ha._request('POST','/api/apple_calendar_sync/snapshot',payload)
events = ha._request('GET',path)
assert len(events) == 1 and events[0]['summary'] == 'Updated event', events
payload['calendars'] = payload['calendars'][1:]
ha._request('POST','/api/apple_calendar_sync/snapshot',payload)
for attempt in range(20):
    if ha.state(entity)['state'] == 'unavailable': break
    time.sleep(1)
assert ha.state(entity)['state'] == 'unavailable'
print('Receiver verified: one integration, two provider calendars, API events, atomic rejection, update, delete, removed calendar unavailable.')
