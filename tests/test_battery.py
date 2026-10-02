#!/usr/bin/env python3
"""Unit tests for the battery sensors.

Everything that decides what a sensor says is a pure function over recorded
macOS output, so none of this needs Bluetooth, a battery or a live Home
Assistant. The fixtures are real `system_profiler` and `pmset` output with the
addresses and serials replaced.

    /usr/bin/python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import reminders_ha_sync as rhs  # noqa: E402

rhs.LOG.addHandler(logging.NullHandler())
rhs.LOG.propagate = False


# Apple silicon: CurrentCapacity is already the percentage and MaxCapacity is
# pinned at 100. Trimmed to the keys the code reads.
APPLE_SILICON = {
    "BatteryInstalled": True,
    "CurrentCapacity": 83,
    "MaxCapacity": 100,
    "DesignCapacity": 6249,
    "NominalChargeCapacity": 5763,
    "AppleRawMaxCapacity": 5611,
    "CycleCount": 107,
    "Temperature": 3076,
    "IsCharging": False,
    "ExternalConnected": False,
    "FullyCharged": False,
    "AvgTimeToEmpty": 567,
    "AvgTimeToFull": 65535,
    "TimeRemaining": 65535,
}

# Intel reports both capacities in mAh, so the percentage has to be divided out.
INTEL = {
    "BatteryInstalled": True,
    "CurrentCapacity": 2500,
    "MaxCapacity": 5000,
    "DesignCapacity": 6000,
    "AppleRawMaxCapacity": 4800,
    "CycleCount": 42,
    "Temperature": 2950,
    "IsCharging": True,
    "ExternalConnected": True,
    "FullyCharged": False,
    "AvgTimeToEmpty": 65535,
    "AvgTimeToFull": 101,
    "TimeRemaining": 65535,
}

BLUETOOTH = {
    "SPBluetoothDataType": [
        {
            "controller_properties": {"controller_state": "attrib_on"},
            "device_connected": [
                {
                    "Аня’s AirPods Pro 3": {
                        "device_address": "F4:33:B7:00:00:01",
                        "device_batteryLevelLeft": "92%",
                        "device_batteryLevelRight": "97%",
                        "device_firmwareVersion": "8B41",
                        "device_minorType": "Headphones",
                    }
                },
                {
                    "Magic Mouse": {
                        "device_address": "AA:BB:CC:00:00:02",
                        "device_batteryLevelMain": "55%",
                        "device_minorType": "Mouse",
                    }
                },
                {
                    # No battery of any kind: not a device this publishes.
                    "Some Speaker": {
                        "device_address": "AA:BB:CC:00:00:03",
                        "device_minorType": "Speaker",
                    }
                },
            ],
            "device_not_connected": [
                {
                    "Old AirPods": {
                        "device_address": "2C:32:6A:00:00:04",
                        "device_batteryLevelCase": "52%",
                        "device_batteryLevelLeft": "100%",
                        "device_batteryLevelRight": "100%",
                        "device_minorType": "Headphones",
                    }
                },
                {"iPhone": {"device_address": "F0:D7:93:00:00:05"}},
            ],
        }
    ]
}

ACCPS = """Now drawing from 'Battery Power'
 -InternalBattery-0 (id=21954659)\t79%; discharging; 3:14 remaining present: true
 - (id=39261131)\t52%; discharging present: true
 - (id=39261132)\t100%; charging; 0:00 remaining present: true
 - (id=39261130)\t92%; discharging present: true
"""


def find(readings, unique_id):
    for reading in readings:
        if reading.unique_id == unique_id:
            return reading
    raise AssertionError("no reading %r in %r" % (unique_id, [r.unique_id for r in readings]))


class PercentTest(unittest.TestCase):
    def test_strips_the_sign(self):
        self.assertEqual(rhs.parse_percent("62%"), 62)
        self.assertEqual(rhs.parse_percent("100%"), 100)
        self.assertEqual(rhs.parse_percent(" 7 % "), 7)

    def test_plain_numbers_work_too(self):
        self.assertEqual(rhs.parse_percent(62), 62)
        self.assertEqual(rhs.parse_percent("62.4"), 62)

    def test_nonsense_is_no_reading(self):
        for value in (None, "", "unknown", "-5%", "120%", {}):
            self.assertIsNone(rhs.parse_percent(value), value)


class SmartBatteryTest(unittest.TestCase):
    def test_apple_silicon_reports_the_percentage_directly(self):
        self.assertEqual(rhs.smart_battery_percent(APPLE_SILICON), 83)

    def test_intel_capacities_are_divided_out(self):
        # 2500 of 5000 mAh. Taking CurrentCapacity at face value would publish
        # 2500%.
        self.assertEqual(rhs.smart_battery_percent(INTEL), 50)

    def test_missing_capacity_is_no_reading(self):
        self.assertIsNone(rhs.smart_battery_percent({}))

    def test_health_uses_the_nominal_capacity(self):
        # 5763 / 6249, which is the figure System Settings shows -- not the
        # slightly lower one AppleRawMaxCapacity gives.
        self.assertEqual(rhs.smart_battery_health(APPLE_SILICON), 92)

    def test_health_falls_back_to_the_raw_capacity(self):
        entry = dict(APPLE_SILICON)
        del entry["NominalChargeCapacity"]
        self.assertEqual(rhs.smart_battery_health(entry), 90)

    def test_health_without_a_design_capacity(self):
        self.assertIsNone(rhs.smart_battery_health({"NominalChargeCapacity": 100}))

    def test_minutes_to_empty_while_discharging(self):
        self.assertEqual(rhs.smart_battery_minutes(APPLE_SILICON), 567)

    def test_minutes_to_full_while_charging(self):
        self.assertEqual(rhs.smart_battery_minutes(INTEL), 101)

    def test_no_estimate_is_no_reading(self):
        entry = dict(APPLE_SILICON, AvgTimeToEmpty=rhs.NO_ESTIMATE)
        self.assertIsNone(rhs.smart_battery_minutes(entry))


class MacReadingsTest(unittest.TestCase):
    def test_the_whole_set(self):
        readings = rhs.mac_readings(APPLE_SILICON)
        self.assertEqual(
            [reading.unique_id for reading in readings],
            [
                "battery_level",
                "battery_charging",
                "ac_connected",
                "battery_full",
                "battery_health",
                "battery_cycles",
                "battery_temperature",
                "battery_time_remaining",
            ],
        )

    def test_level_is_a_battery_sensor(self):
        level = find(rhs.mac_readings(APPLE_SILICON), "battery_level")
        self.assertEqual(level.state, 83)
        self.assertEqual(level.device_class, "battery")
        self.assertEqual(level.unit, "%")
        self.assertEqual(level.kind, "sensor")

    def test_charging_is_a_binary_sensor(self):
        charging = find(rhs.mac_readings(INTEL), "battery_charging")
        self.assertIs(charging.state, True)
        self.assertEqual(charging.kind, "binary_sensor")
        self.assertEqual(charging.device_class, "battery_charging")

    def test_temperature_is_hundredths_of_a_degree(self):
        temperature = find(rhs.mac_readings(APPLE_SILICON), "battery_temperature")
        self.assertEqual(temperature.state, 30.8)

    def test_diagnostics_are_marked_as_such(self):
        readings = {r.unique_id: r for r in rhs.mac_readings(APPLE_SILICON)}
        self.assertFalse(readings["battery_level"].diagnostic)
        self.assertTrue(readings["battery_cycles"].diagnostic)

    def test_a_missing_value_becomes_unavailable_not_zero(self):
        # A cycle count of 0 and no cycle count at all must not look the same.
        readings = find(rhs.mac_readings({"CycleCount": None}), "battery_cycles")
        self.assertEqual(readings.state, rhs.UNAVAILABLE)


class BluetoothTest(unittest.TestCase):
    def setUp(self):
        self.devices = {d.name: d for d in rhs.parse_bluetooth(BLUETOOTH)}

    def test_only_devices_that_report_a_battery(self):
        self.assertEqual(
            sorted(self.devices),
            ["Magic Mouse", "Old AirPods", "Аня’s AirPods Pro 3"],
        )

    def test_airpods_get_a_sensor_per_cell(self):
        device = self.devices["Аня’s AirPods Pro 3"]
        self.assertEqual(
            [r.unique_id for r in device.readings],
            [
                "left_battery_level",
                "left_battery_charging",
                "right_battery_level",
                "right_battery_charging",
            ],
        )
        self.assertEqual(find(device.readings, "left_battery_level").state, 92)

    def test_a_single_cell_device_is_just_battery(self):
        device = self.devices["Magic Mouse"]
        self.assertEqual(
            [r.unique_id for r in device.readings],
            ["battery_level", "battery_charging"],
        )

    def test_the_key_is_the_address(self):
        self.assertEqual(self.devices["Magic Mouse"].key, "bt-aabbcc000002")

    def test_disconnected_devices_are_kept_but_marked(self):
        self.assertTrue(self.devices["Аня’s AirPods Pro 3"].available)
        self.assertFalse(self.devices["Old AirPods"].available)

    def test_a_disconnected_device_publishes_nothing_current(self):
        # macOS keeps the last reading forever. Publishing 52% for a case that
        # has been in a drawer for a month is worse than saying nothing.
        device = self.devices["Old AirPods"]
        self.assertEqual(find(device.readings, "case_battery_level").state, 52)
        published = {r.unique_id: r.state for r in device.to_publish()}
        self.assertEqual(set(published.values()), {rhs.UNAVAILABLE})

    def test_to_publish_does_not_mutate_the_readings(self):
        device = self.devices["Old AirPods"]
        device.to_publish()
        self.assertEqual(find(device.readings, "case_battery_level").state, 52)

    def test_junk_input_is_survivable(self):
        self.assertEqual(rhs.parse_bluetooth({}), [])
        self.assertEqual(rhs.parse_bluetooth([]), [])
        self.assertEqual(rhs.parse_bluetooth({"SPBluetoothDataType": [None]}), [])


class AccessoryChargingTest(unittest.TestCase):
    def test_percentages_map_to_charging(self):
        charging = rhs.parse_accessory_charging(ACCPS)
        self.assertEqual(charging, {52: False, 100: True, 92: False})

    def test_the_macs_own_battery_is_not_an_accessory(self):
        # 79% is InternalBattery. Letting it in would attribute the Mac's
        # charging state to any accessory that happened to sit at 79%.
        self.assertNotIn(79, rhs.parse_accessory_charging(ACCPS))

    def test_a_repeated_percentage_is_ambiguous(self):
        text = ACCPS + " - (id=1)\t92%; charging present: true\n"
        self.assertIsNone(rhs.parse_accessory_charging(text)[92])

    def test_empty_input(self):
        self.assertEqual(rhs.parse_accessory_charging(""), {})


class AttributeChargingTest(unittest.TestCase):
    def setUp(self):
        self.devices = rhs.parse_bluetooth(BLUETOOTH)
        rhs.attribute_charging(self.devices, rhs.parse_accessory_charging(ACCPS))
        self.by_name = {d.name: d for d in self.devices}

    def test_a_matched_percentage_sets_the_flag(self):
        device = self.by_name["Аня’s AirPods Pro 3"]
        self.assertIs(find(device.readings, "left_battery_charging").state, False)

    def test_a_percentage_pmset_never_mentions_stays_unknown(self):
        # The right earbud is at 97%, which pmset does not list at all. macOS
        # simply does not say, and a guess would be wrong half the time.
        device = self.by_name["Аня’s AirPods Pro 3"]
        self.assertEqual(
            find(device.readings, "right_battery_charging").state, rhs.UNAVAILABLE
        )

    def test_an_ambiguous_percentage_stays_unknown(self):
        device = self.by_name["Old AirPods"]
        # Both earbuds read 100%, and only one pmset row does.
        self.assertIs(find(device.readings, "left_battery_charging").state, True)
        charging = rhs.parse_accessory_charging(
            ACCPS + " - (id=9)\t100%; discharging present: true\n"
        )
        devices = rhs.parse_bluetooth(BLUETOOTH)
        rhs.attribute_charging(devices, charging)
        device = {d.name: d for d in devices}["Old AirPods"]
        self.assertEqual(
            find(device.readings, "left_battery_charging").state, rhs.UNAVAILABLE
        )


class PayloadTest(unittest.TestCase):
    def test_registration_carries_the_metadata(self):
        reading = find(rhs.mac_readings(APPLE_SILICON), "battery_level")
        self.assertEqual(
            rhs.register_payload(reading),
            {
                "type": "sensor",
                "unique_id": "battery_level",
                "name": "Battery Level",
                "state": 83,
                "device_class": "battery",
                "unit_of_measurement": "%",
                "state_class": "measurement",
            },
        )

    def test_diagnostics_get_an_entity_category(self):
        reading = find(rhs.mac_readings(APPLE_SILICON), "battery_cycles")
        self.assertEqual(rhs.register_payload(reading)["entity_category"], "diagnostic")

    def test_updates_are_only_the_state(self):
        reading = find(rhs.mac_readings(APPLE_SILICON), "battery_level")
        self.assertEqual(
            rhs.update_payload(reading),
            {"type": "sensor", "unique_id": "battery_level", "state": 83},
        )

    def test_stale_sensors_are_blanked(self):
        # The case only appears while the lid is open. Once registered it has
        # to keep being updated, or it freezes at whatever it last read.
        known = {
            "left_battery_level": "sensor",
            "case_battery_level": "sensor",
            "case_battery_charging": "binary_sensor",
        }
        readings = [find(rhs.mac_readings(APPLE_SILICON), "battery_level")]
        self.assertEqual(
            rhs.stale_payloads(known, readings),
            [
                {
                    "type": "binary_sensor",
                    "unique_id": "case_battery_charging",
                    "state": rhs.UNAVAILABLE,
                },
                {
                    "type": "sensor",
                    "unique_id": "case_battery_level",
                    "state": rhs.UNAVAILABLE,
                },
                {
                    "type": "sensor",
                    "unique_id": "left_battery_level",
                    "state": rhs.UNAVAILABLE,
                },
            ],
        )


class WebhookGoneTest(unittest.TestCase):
    """How `HomeAssistant.webhook` decides a registration no longer exists."""

    def client(self, answer) -> rhs.HomeAssistant:
        ha = rhs.HomeAssistant("http://ha.invalid:8123", "t")

        def request(method, path, body=None, authenticated=True):
            if isinstance(answer, Exception):
                raise answer
            return answer

        ha._request = request
        return ha

    def test_an_empty_body_means_the_registration_is_gone(self):
        # A webhook id Home Assistant does not know answers 200 with nothing in
        # it -- no 404, no error. Miss that and publishing silently no-ops.
        with self.assertRaises(rhs.MobileAppGone):
            self.client(None).webhook("hook", {"type": "update_sensor_states"})

    def test_a_result_object_is_passed_through(self):
        answer = {"battery_level": {"success": True}}
        self.assertEqual(self.client(answer).webhook("hook", {"type": "x"}), answer)

    def test_410_still_means_gone(self):
        gone = rhs.UserError("Home Assistant returned 410 for POST /api/webhook/x: ")
        with self.assertRaises(rhs.MobileAppGone):
            self.client(gone).webhook("hook", {"type": "x"})

    def test_any_other_error_is_left_alone(self):
        boom = rhs.UserError("Home Assistant returned 500 for POST /api/webhook/x: ")
        with self.assertRaises(rhs.UserError):
            self.client(boom).webhook("hook", {"type": "x"})


class FakeHa:
    """Enough of HomeAssistant to drive the publishing path."""

    def __init__(self, forget=(), gone_once=False, gone_always=False):
        self.registrations = []
        self.registered = []
        self.updates = []
        self.forget = set(forget)
        self.gone_once = gone_once
        self.gone_always = gone_always

    def register_mobile_app(self, device):
        self.registrations.append(device.name)
        return {"webhook_id": "hook-%d" % len(self.registrations)}

    def webhook(self, webhook_id, payload):
        if self.gone_always:
            raise rhs.MobileAppGone(webhook_id)
        if self.gone_once:
            self.gone_once = False
            raise rhs.MobileAppGone(webhook_id)
        if payload["type"] == "register_sensor":
            self.registered.append(payload["data"]["unique_id"])
            return {"success": True}
        self.updates.append([row["unique_id"] for row in payload["data"]])
        result = {}
        for row in payload["data"]:
            unique_id = row["unique_id"]
            if unique_id in self.forget:
                self.forget.discard(unique_id)
                result[unique_id] = {
                    "success": False,
                    "error": {"code": "not_registered", "message": "gone"},
                }
            else:
                result[unique_id] = {"success": True}
        return result


def mouse() -> rhs.BatteryDevice:
    return {d.name: d for d in rhs.parse_bluetooth(BLUETOOTH)}["Magic Mouse"]


class PublishDeviceTest(unittest.TestCase):
    def test_first_run_registers_everything(self):
        ha, device = FakeHa(), mouse()
        entry = {"webhook_id": "hook-1", "sensors": {}}
        rhs.publish_device(ha, entry, device, device.readings)
        self.assertEqual(ha.registered, ["battery_level", "battery_charging"])
        self.assertEqual(
            entry["sensors"],
            {"battery_level": "sensor", "battery_charging": "binary_sensor"},
        )

    def test_second_run_only_updates(self):
        ha, device = FakeHa(), mouse()
        entry = {
            "webhook_id": "hook-1",
            "sensors": {"battery_level": "sensor", "battery_charging": "binary_sensor"},
        }
        rhs.publish_device(ha, entry, device, device.readings)
        self.assertEqual(ha.registered, [])
        self.assertEqual(ha.updates, [["battery_level", "battery_charging"]])

    def test_a_forgotten_sensor_is_registered_again(self):
        # A deleted entity, or a restored backup. Home Assistant answers
        # not_registered per sensor rather than failing the call.
        ha, device = FakeHa(forget={"battery_level"}), mouse()
        entry = {
            "webhook_id": "hook-1",
            "sensors": {"battery_level": "sensor", "battery_charging": "binary_sensor"},
        }
        rhs.publish_device(ha, entry, device, device.readings)
        self.assertEqual(ha.registered, ["battery_level"])
        self.assertEqual(ha.updates[-1], ["battery_level"])

    def test_stale_sensors_ride_along_with_the_update(self):
        ha, device = FakeHa(), mouse()
        entry = {
            "webhook_id": "hook-1",
            "sensors": {
                "battery_level": "sensor",
                "battery_charging": "binary_sensor",
                "case_battery_level": "sensor",
            },
        }
        rhs.publish_device(ha, entry, device, device.readings)
        self.assertIn("case_battery_level", ha.updates[0])


class PublishBatteriesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "batteries.json")
        self.ha = FakeHa()

        devices = rhs.parse_bluetooth(BLUETOOTH)
        original_collect, original_ha = rhs.collect_battery_devices, rhs.HomeAssistant
        rhs.collect_battery_devices = lambda exclude=None: [
            device
            for device in devices
            if rhs.normalize_name(device.name) not in (exclude or set())
        ]
        rhs.HomeAssistant = lambda *a, **k: self.ha

        def restore():
            rhs.collect_battery_devices = original_collect
            rhs.HomeAssistant = original_ha

        self.addCleanup(restore)

    def config(self, **battery) -> rhs.Config:
        raw = {
            "home_assistant": {"url": "http://ha.invalid:8123", "token": "t"},
            "features": {"reminders": False, "battery": True},
            "battery": dict({"state_file": self.state}, **battery),
        }
        return rhs.Config(raw, "<test>")

    def stored(self) -> dict:
        with open(self.state, encoding="utf-8") as fh:
            return json.load(fh)

    def test_connected_devices_are_registered(self):
        self.assertEqual(rhs.publish_batteries(self.config()), 0)
        self.assertEqual(sorted(self.ha.registrations), ["Magic Mouse", "Аня’s AirPods Pro 3"])

    def test_an_absent_device_is_not_registered_at_all(self):
        # Otherwise every accessory ever paired with this Mac becomes a card of
        # permanently unavailable sensors.
        rhs.publish_batteries(self.config())
        self.assertNotIn("Old AirPods", self.ha.registrations)
        self.assertEqual(len(self.stored()["devices"]), 2)

    def test_an_absent_device_that_is_registered_still_reports(self):
        rhs.publish_batteries(self.config())
        # Pretend it was connected on an earlier run.
        store = rhs.BatteryStore(self.state)
        store.remember("bt-2c326a000004", "hook-9", "Old AirPods")
        store.save()

        self.ha.updates = []
        rhs.publish_batteries(self.config())
        blanked = [batch for batch in self.ha.updates if "case_battery_level" in batch]
        self.assertTrue(blanked, "the absent device was never updated")

    def test_excluded_devices_are_left_alone(self):
        rhs.publish_batteries(self.config(exclude=["Magic Mouse"]))
        self.assertEqual(self.ha.registrations, ["Аня’s AirPods Pro 3"])

    def test_a_deleted_registration_is_recreated(self):
        rhs.publish_batteries(self.config())
        # The AirPods are the first device published, so they are the one the
        # fake reports as gone.
        airpods = "bt-f433b7000001"
        before = self.stored()["devices"][airpods]["webhook_id"]

        self.ha.gone_once = True
        self.assertEqual(rhs.publish_batteries(self.config()), 0)
        after = self.stored()["devices"][airpods]["webhook_id"]
        self.assertNotEqual(before, after)

    def test_a_registration_that_dies_again_is_reported_not_raised(self):
        # Re-registering is one retry, not a loop, and the failure must stay
        # inside this device -- the ones after it still get their run, which is
        # what both names showing up twice proves.
        self.ha.gone_always = True
        self.assertEqual(rhs.publish_batteries(self.config()), 1)
        self.assertEqual(
            self.ha.registrations,
            ["Аня’s AirPods Pro 3"] * 2 + ["Magic Mouse"] * 2,
        )

    def test_a_dry_run_writes_nothing(self):
        with contextlib.redirect_stdout(io.StringIO()):
            rhs.publish_batteries(self.config(), dry_run=True)
        self.assertFalse(os.path.exists(self.state))
        self.assertEqual(self.ha.registrations, [])

    def test_the_publish_time_is_recorded(self):
        rhs.publish_batteries(self.config())
        self.assertIsNotNone(self.stored()["last_publish"])


class BatteryStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "nested", "batteries.json")

    def test_round_trip(self):
        store = rhs.BatteryStore(self.path)
        store.remember("mac-1", "hook", "MacBook")
        store.save()
        self.assertEqual(rhs.BatteryStore(self.path).registration("mac-1")["webhook_id"], "hook")

    def test_webhook_ids_are_not_world_readable(self):
        # A webhook id is a credential: it writes states into someone's home.
        store = rhs.BatteryStore(self.path)
        store.remember("mac-1", "hook", "MacBook")
        store.save()
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_a_future_version_is_ignored_rather_than_trusted(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"version": 99, "devices": {"mac-1": {"webhook_id": "x"}}}, fh)
        self.assertIsNone(rhs.BatteryStore(self.path).registration("mac-1"))

    def test_unreadable_state_is_survivable(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(rhs.BatteryStore(self.path).devices, {})

    def test_an_entry_without_a_webhook_is_not_a_registration(self):
        store = rhs.BatteryStore(self.path)
        store.devices["mac-1"] = {"name": "half-written"}
        self.assertIsNone(store.registration("mac-1"))


class FeaturesConfigTest(unittest.TestCase):
    def config(self, **overrides) -> rhs.Config:
        raw = {"home_assistant": {"url": "http://ha.invalid:8123", "token": "t"}}
        raw.update(overrides)
        return rhs.Config(raw, "<test>")

    def test_a_config_without_features_keeps_its_old_meaning(self):
        # Everything written before batteries existed. An upgrade must not
        # start registering devices in someone's Home Assistant.
        config = self.config()
        self.assertTrue(config.sync_reminders)
        self.assertFalse(config.sync_battery)

    def test_features_switch_the_halves(self):
        config = self.config(features={"reminders": False, "battery": True})
        self.assertFalse(config.sync_reminders)
        self.assertTrue(config.sync_battery)

    def test_a_typo_is_reported_rather_than_ignored(self):
        with self.assertRaises(rhs.UserError) as caught:
            self.config(features={"batteries": True})
        self.assertIn("batteries", str(caught.exception))

    def test_features_must_be_an_object(self):
        with self.assertRaises(rhs.UserError):
            self.config(features=["battery"])

    def test_battery_defaults(self):
        config = self.config()
        self.assertEqual(config.battery_interval, 300)
        self.assertTrue(config.battery_state_file.endswith("batteries.json"))
        self.assertEqual(config.battery_exclude, set())

    def test_battery_exclusions_are_folded_like_list_names(self):
        config = self.config(battery={"exclude": ["Магическая Мышь"]})
        self.assertIn(rhs.normalize_name("МАГИЧЕСКАЯ МЫШЬ"), config.battery_exclude)

    def test_battery_must_be_an_object(self):
        with self.assertRaises(rhs.UserError):
            self.config(battery=["nope"])


class ParseFeaturesTest(unittest.TestCase):
    def test_both(self):
        self.assertEqual(rhs.parse_features("reminders,battery"), (True, True, False))

    def test_one(self):
        self.assertEqual(rhs.parse_features("battery"), (False, True, False))

    def test_none_turns_everything_off(self):
        self.assertEqual(rhs.parse_features("none"), (False, False, False))

    def test_whitespace_and_case(self):
        self.assertEqual(rhs.parse_features(" Battery , Reminders "), (True, True, False))

    def test_a_typo_is_reported(self):
        with self.assertRaises(rhs.UserError):
            rhs.parse_features("batteries")


class ChecklistRenderTest(unittest.TestCase):
    OPTIONS = (("Reminders sync", "to-do lists"), ("Battery sensors", "levels"))

    def test_ticks_and_cursor(self):
        lines = rhs.render_checklist(self.OPTIONS, [True, False], 1)
        self.assertTrue(lines[0].startswith("  [x] Reminders sync"))
        self.assertTrue(lines[1].startswith("> [ ] Battery sensors"))

    def test_labels_line_up(self):
        lines = rhs.render_checklist(self.OPTIONS, [True, True], 0)
        self.assertEqual(lines[0].index("to-do lists"), lines[1].index("levels"))


class ChecklistKeysTest(unittest.TestCase):
    def test_space_toggles_what_is_under_the_cursor(self):
        chosen = [True, False]
        cursor, accepted = rhs.apply_checklist_key(b" ", 1, chosen)
        self.assertEqual((cursor, accepted), (1, False))
        self.assertEqual(chosen, [True, True])

    def test_arrows_move_and_wrap_around(self):
        chosen = [True, True]
        self.assertEqual(rhs.apply_checklist_key(b"\x1b[B", 1, chosen)[0], 0)
        self.assertEqual(rhs.apply_checklist_key(b"\x1b[A", 0, chosen)[0], 1)

    def test_vi_keys_work_too(self):
        chosen = [True, True]
        self.assertEqual(rhs.apply_checklist_key(b"j", 0, chosen)[0], 1)
        self.assertEqual(rhs.apply_checklist_key(b"k", 1, chosen)[0], 0)

    def test_enter_accepts(self):
        self.assertTrue(rhs.apply_checklist_key(b"\r", 0, [True])[1])

    def test_ctrl_c_still_interrupts(self):
        # cbreak leaves signals on, but the byte arrives here too.
        with self.assertRaises(KeyboardInterrupt):
            rhs.apply_checklist_key(b"\x03", 0, [True])

    def test_anything_else_is_ignored(self):
        chosen = [True]
        self.assertEqual(rhs.apply_checklist_key(b"z", 0, chosen), (0, False))
        self.assertEqual(chosen, [True])


class ChecklistFallbackTest(unittest.TestCase):
    """The numbered path, which is what a run without a usable terminal gets."""

    OPTIONS = ChecklistRenderTest.OPTIONS

    def answer(self, replies, selected=(True, False)):
        queue = list(replies)
        original = rhs.ask
        rhs.ask = lambda *args, **kwargs: queue.pop(0)
        self.addCleanup(lambda: setattr(rhs, "ask", original))
        with contextlib.redirect_stdout(io.StringIO()):
            return rhs.checklist_by_number("What should run?", self.OPTIONS, list(selected))

    def test_a_number_toggles_that_row(self):
        self.assertEqual(self.answer(["2", ""]), [True, True])

    def test_enter_accepts_what_is_shown(self):
        self.assertEqual(self.answer([""]), [True, False])

    def test_several_at_once(self):
        self.assertEqual(self.answer(["1,2", ""]), [False, True])

    def test_junk_and_out_of_range_are_ignored(self):
        self.assertEqual(self.answer(["x, 9, 1", ""]), [False, False])


if __name__ == "__main__":
    unittest.main()
