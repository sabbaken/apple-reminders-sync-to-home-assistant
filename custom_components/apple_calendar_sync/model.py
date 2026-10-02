"""Validate entire snapshots before changing stored calendar data (stdlib only)."""
from datetime import date, datetime


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("event dates must be strings")
    if len(value) == 10:
        return date.fromisoformat(value)
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timed events must include a UTC offset")
    return result


def text(row, key, required=True):
    value = row.get(key)
    if not isinstance(value, str) or (required and not value):
        raise ValueError("%s must be a non-empty string" % key)
    return value


def validate_snapshot(payload):
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("unsupported calendar snapshot version")
    text(payload, "source_id")
    lower, upper = timestamp(payload.get("range_start")), timestamp(payload.get("range_end"))
    if not isinstance(lower, datetime) or not isinstance(upper, datetime) or lower >= upper:
        raise ValueError("invalid snapshot range")
    calendars = payload.get("calendars")
    if not isinstance(calendars, list) or not calendars:
        raise ValueError("refusing an empty calendar snapshot")
    ids = set()
    for calendar in calendars:
        if not isinstance(calendar, dict):
            raise ValueError("invalid calendar")
        key = text(calendar, "id")
        if key in ids:
            raise ValueError("duplicate calendar id")
        ids.add(key)
        text(calendar, "name")
        text(calendar, "source", required=False)
        events = calendar.get("events")
        if not isinstance(events, list):
            raise ValueError("events must be an array")
        uids = set()
        for event in events:
            if not isinstance(event, dict):
                raise ValueError("invalid event")
            uid = text(event, "uid")
            if uid in uids:
                raise ValueError("duplicate event occurrence")
            uids.add(uid)
            text(event, "summary", required=False)
            start, end = timestamp(event.get("start")), timestamp(event.get("end"))
            if type(start) is not type(end) or start > end:
                raise ValueError("event start and end must have the same type and non-negative duration")
            for field in ("description", "location"):
                if field in event:
                    text(event, field, required=False)
    return payload
