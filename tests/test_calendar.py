"""Calendar snapshot contract and safe publication, without Calendar access."""
import copy
import datetime as dt
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch, Mock

import reminders_ha_sync as rhs

spec = importlib.util.spec_from_file_location(
    "calendar_model", Path(__file__).resolve().parents[1] / "custom_components/apple_calendar_sync/model.py")
model = importlib.util.module_from_spec(spec)
spec.loader.exec_module(model)


def snapshot():
    return {"version": 1, "source_id": "mac-1", "range_start": "2026-01-01T00:00:00Z",
            "range_end": "2027-01-01T00:00:00Z", "calendars": [
                {"id": "icloud-1", "name": "Личное", "source": "iCloud", "events": [
                    {"uid": "event@occurrence1", "summary": "Праздник", "start": "2026-10-02",
                     "end": "2026-10-03"},
                    {"uid": "event@occurrence2", "summary": "Call", "start": "2026-10-02T13:00:00+02:00",
                     "end": "2026-10-02T14:00:00+02:00", "location": "Office", "description": "Notes"}]},
                {"id": "google-1", "name": "Личное", "source": "Google", "events": []}]}


def config(**calendar):
    return rhs.Config({"home_assistant": {"url": "http://ha.invalid", "token": "t"},
                       "features": {"reminders": False, "calendar": True}, "calendar": calendar}, "test")


class SnapshotTest(unittest.TestCase):
    def test_all_day_and_timed_with_unicode_and_duplicate_names(self):
        self.assertEqual(model.validate_snapshot(snapshot()), snapshot())
        self.assertIs(type(model.timestamp("2026-10-02")), dt.date)
        self.assertIs(type(model.timestamp("2026-10-02T13:00:00Z")), dt.datetime)

    def test_zero_duration_google_event_is_valid(self):
        data = snapshot()
        data["calendars"][0]["events"][1]["end"] = data["calendars"][0]["events"][1]["start"]
        self.assertEqual(model.validate_snapshot(data), data)

    def test_bad_snapshots_do_not_mutate_input(self):
        invalid = [None, {}, dict(snapshot(), version=2), dict(snapshot(), calendars=[])]
        for data in invalid:
            before = copy.deepcopy(data)
            with self.subTest(data=data), self.assertRaises(ValueError):
                model.validate_snapshot(data)
            self.assertEqual(data, before)

    def test_duplicate_ids_are_rejected(self):
        data = snapshot()
        data["calendars"][1]["id"] = "icloud-1"
        with self.assertRaises(ValueError):
            model.validate_snapshot(data)

    def test_invalid_events_are_rejected_before_replacement(self):
        for changes in [ {"start": "2026-10-02T13:00:00"}, {"end": "2026-10-01"},
                         {"end": "2026-10-03T00:00:00Z"}, {"summary": 3}, {"location": None} ]:
            data = snapshot()
            data["calendars"][0]["events"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                model.validate_snapshot(data)

    def test_duplicate_occurrences_rejected(self):
        data = snapshot()
        data["calendars"][0]["events"].append(data["calendars"][0]["events"][0])
        with self.assertRaises(ValueError):
            model.validate_snapshot(data)


class CalendarConfigTest(unittest.TestCase):
    def test_legacy_config_does_not_enable_calendar(self):
        c = rhs.Config({"home_assistant": {"url": "http://ha.invalid", "token": "t"}}, "test")
        self.assertTrue(c.sync_reminders)
        self.assertFalse(c.sync_calendar)

    def test_calendar_only_and_feature_parser(self):
        self.assertFalse(config().sync_reminders)
        self.assertTrue(config().sync_calendar)
        self.assertEqual(rhs.parse_features("calendar"), (False, False, True))
        self.assertEqual(rhs.parse_features("reminders,battery,calendar"), (True, True, True))

    def test_bad_ranges_fail_in_config(self):
        for options in [{"interval": 0}, {"past_days": -1}, {"past_days": "365"},
                        {"future_days": 0}, {"future_days": True}, {"future_days": 1461}]:
            with self.subTest(options=options), self.assertRaises(rhs.UserError):
                config(**options)


class CalendarPublishTest(unittest.TestCase):
    @patch.object(rhs, "HomeAssistant")
    @patch.object(rhs, "collect_calendars", return_value=snapshot())
    def test_dry_run_does_not_contact_ha(self, collect, ha):
        self.assertEqual(rhs.publish_calendars(config(), dry_run=True), 0)
        ha.assert_not_called()

    @patch.object(rhs, "HomeAssistant")
    @patch.object(rhs, "collect_calendars", return_value=snapshot())
    def test_one_request_for_all_calendars(self, collect, ha):
        rhs.publish_calendars(config())
        ha.return_value._request.assert_called_once_with("POST", "/api/apple_calendar_sync/snapshot", snapshot())

    @patch.object(rhs, "calendar_binary", return_value="/helper")
    @patch.object(rhs.subprocess, "run")
    def test_denied_access_and_empty_reads_do_not_publish(self, run, binary):
        for result in [Mock(returncode=2, stderr="Access denied"),
                       Mock(returncode=0, stdout='{"version":1,"calendars":[]}'),
                       Mock(returncode=0, stdout='not json')]:
            run.return_value = result
            with self.subTest(result=result), self.assertRaises(rhs.UserError):
                rhs.collect_calendars(config())

    @patch.object(rhs, "calendar_binary", return_value="/helper")
    @patch.object(rhs.subprocess, "run")
    def test_timeout_becomes_user_error(self, run, binary):
        run.side_effect = subprocess.TimeoutExpired("helper", 120)
        with self.assertRaises(rhs.UserError):
            rhs.collect_calendars(config())

    @patch.object(rhs, "calendar_binary", return_value="/helper")
    @patch.object(rhs.subprocess, "run")
    def test_source_id_override(self, run, binary):
        import json
        run.return_value = Mock(returncode=0, stdout=json.dumps(snapshot()))
        self.assertEqual(rhs.collect_calendars(config(source_id="my-mac"))["source_id"], "my-mac")

    @patch.object(rhs, "remove_launch_agent", return_value=False)
    @patch.object(rhs, "drop_legacy_launch_agents")
    @patch.object(rhs, "install_launch_agent", return_value="/tmp/agent.plist")
    @patch.object(rhs, "publish_calendars", return_value=0)
    def test_calendar_installs_independent_agent(self, publish, install, legacy, remove):
        rhs.cmd_install(config(), 600)
        publish.assert_called_once()
        self.assertEqual(install.call_args.args[0], rhs.CALENDAR_LAUNCH_LABEL)
        self.assertEqual(install.call_args.args[1][-1], "calendar")
        self.assertEqual(install.call_args.args[2], 300)
        self.assertIn(rhs.LAUNCH_LABEL, [call.args[0] for call in remove.call_args_list])


if __name__ == "__main__":
    unittest.main()
