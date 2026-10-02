"""One integration receives all calendars, from one or more Macs."""
from homeassistant import config_entries
from .const import DOMAIN


class AppleCalendarConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="Apple Calendar Sync", data={})
        return self.async_show_form(step_id="user")
