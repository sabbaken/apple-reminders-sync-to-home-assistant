"""Authenticated, atomic calendar snapshot receiver."""
import asyncio
import copy
from datetime import datetime, timezone

from homeassistant.components.http import HomeAssistantView, KEY_HASS_USER
from homeassistant.const import Platform
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .const import DOMAIN, SIGNAL
from .model import validate_snapshot


async def async_setup(hass, config):
    hass.http.register_view(CalendarSnapshotView(hass))
    return True


async def async_setup_entry(hass, entry):
    store = Store(hass, 1, DOMAIN + "." + entry.entry_id)
    snapshots = await store.async_load() or {}
    hass.data[DOMAIN] = {"store": store, "snapshots": snapshots, "lock": asyncio.Lock()}
    await hass.config_entries.async_forward_entry_setups(entry, [Platform.CALENDAR])
    return True


async def async_unload_entry(hass, entry):
    unloaded = await hass.config_entries.async_unload_platforms(entry, [Platform.CALENDAR])
    if unloaded:
        hass.data.pop(DOMAIN, None)
    return unloaded


class CalendarSnapshotView(HomeAssistantView):
    url = "/api/apple_calendar_sync/snapshot"
    name = "api:apple_calendar_sync:snapshot"
    requires_auth = True

    def __init__(self, hass):
        self.hass = hass

    async def post(self, request):
        if not request[KEY_HASS_USER].is_admin:
            return self.json_message("Administrator token required", status_code=403)
        data = self.hass.data.get(DOMAIN)
        if data is None:
            return self.json_message("Add the Apple Calendar Sync integration first", status_code=503)
        try:
            payload = validate_snapshot(await request.json())
        except (ValueError, TypeError, KeyError) as error:
            return self.json_message(str(error), status_code=400)
        async with data["lock"]:
            # Save first. A failed disk write never replaces the last good snapshot.
            snapshots = copy.deepcopy(data["snapshots"])
            snapshots[payload["source_id"]] = dict(payload, received_at=datetime.now(timezone.utc).isoformat())
            await data["store"].async_save(snapshots)
            data["snapshots"] = snapshots
            async_dispatcher_send(self.hass, SIGNAL)
        return self.json({"calendars": len(payload["calendars"]),
                          "events": sum(len(c["events"]) for c in payload["calendars"])})


async def async_remove_entry(hass, entry):
    """Delete cached personal events when the integration is removed."""
    await Store(hass, 1, DOMAIN + "." + entry.entry_id).async_remove()
