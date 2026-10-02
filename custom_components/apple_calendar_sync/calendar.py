"""Read-only native calendar entities, one for each EventKit calendar."""
import json
from datetime import timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SIGNAL
from .model import timestamp

SCAN_INTERVAL = timedelta(seconds=60)


async def async_setup_entry(hass, entry, async_add_entities):
    known = set()

    @callback
    def discover():
        entities = []
        for source, snapshot in hass.data[DOMAIN]["snapshots"].items():
            for calendar in snapshot["calendars"]:
                key = (source, calendar["id"])
                if key not in known:
                    known.add(key)
                    entities.append(AppleCalendar(source, calendar["id"]))
        if entities:
            async_add_entities(entities)

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL, discover))
    discover()


class AppleCalendar(CalendarEntity):
    _attr_should_poll = True
    _attr_icon = "mdi:calendar"
    _attr_supported_features = 0

    def __init__(self, source, calendar_id):
        self.source = source
        self.calendar_id = calendar_id
        # JSON avoids delimiter collisions and duplicate names across providers.
        self._attr_unique_id = json.dumps([source, calendar_id])
        self._event = None

    def snapshot(self):
        return self.hass.data[DOMAIN]["snapshots"].get(self.source, {})

    def calendar(self):
        return next((c for c in self.snapshot().get("calendars", [])
                     if c["id"] == self.calendar_id), None)

    @property
    def name(self):
        calendar = self.calendar()
        return "%s (%s)" % (calendar["name"], calendar["source"]) if calendar else self.calendar_id

    @property
    def available(self):
        snapshot = self.snapshot()
        received = dt_util.parse_datetime(snapshot.get("received_at", ""))
        return (self.calendar() is not None and received is not None
                and dt_util.utcnow() - received < timedelta(minutes=30)
                and timestamp(snapshot["range_end"]) > dt_util.utcnow())

    def events(self):
        calendar = self.calendar()
        return [CalendarEvent(start=timestamp(e["start"]), end=timestamp(e["end"]),
                              summary=e["summary"], uid=e["uid"],
                              description=e.get("description"), location=e.get("location"))
                for e in (calendar or {}).get("events", [])]

    @property
    def event(self):
        return self._event

    async def async_update(self):
        now = dt_util.now()
        events = sorted(self.events(), key=lambda e: e.start_datetime_local)
        self._event = next((e for e in events if e.end_datetime_local > now), None)

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL, self.updated))
        await self.async_update()

    @callback
    def updated(self):
        self.async_schedule_update_ha_state(force_refresh=True)

    async def async_get_events(self, hass, start_date, end_date):
        return sorted((e for e in self.events()
                       if e.end_datetime_local > start_date and e.start_datetime_local < end_date),
                      key=lambda e: e.start_datetime_local)
