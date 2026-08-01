#!/usr/bin/env python3
"""Unit tests for the merge engine and the normalizers.

These are the parts that decide whether data survives, and they are pure
functions, so they get tested without Reminders access or a live Home Assistant.

    /usr/bin/python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import logging
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import reminders_ha_sync as rhs  # noqa: E402

# The engine logs a warning for input it deliberately rejects; without a handler
# Python's last-resort one would scatter those across the test output.
rhs.LOG.addHandler(logging.NullHandler())
rhs.LOG.propagate = False


def make_config(**overrides) -> rhs.Config:
    raw = {
        "home_assistant": {"url": "http://ha.invalid:8123", "token": "t"},
        "lists": [{"reminders": "L", "ha": "todo.l"}],
    }
    raw.update(overrides)
    return rhs.Config(raw, "<test>")


def item(id: str, title: str, **kwargs) -> rhs.Item:
    return rhs.Item(id=id, title=title, **kwargs)


def link(r: str, h: str, **last) -> dict:
    snapshot = {"title": None, "notes": None, "due": None, "completed": False}
    snapshot.update(last)
    return {"r": r, "h": h, "last": snapshot}


def state(links=(), last_sync="2026-07-01T00:00:00+02:00", **kwargs) -> dict:
    base = {
        "last_sync": last_sync,
        "links": list(links),
        "r_tombstones": [],
        "h_tombstones": [],
    }
    base.update(kwargs)
    return base


def writes_for(plan: rhs.Plan, label: str) -> rhs.LinkWrite:
    for write in plan.link_writes:
        if write.label == label:
            return write
    raise AssertionError("no LinkWrite labelled %r" % label)


class NormalizeTest(unittest.TestCase):
    def test_notes_empty_is_none(self):
        self.assertIsNone(rhs.normalize_notes(""))
        self.assertIsNone(rhs.normalize_notes("   "))
        self.assertIsNone(rhs.normalize_notes(None))
        self.assertEqual(rhs.normalize_notes(" hi "), "hi")

    def test_title_is_stripped(self):
        # Home Assistant strips summaries on write; not matching that would make
        # a padded title look like a change on every single sync.
        self.assertEqual(rhs.normalize_title("  buy milk "), "buy milk")
        self.assertEqual(rhs.normalize_title(None), "")

    def test_date_only_passes_through(self):
        self.assertEqual(rhs.normalize_due("2026-08-02"), "2026-08-02")

    def test_trailing_z_is_parsed(self):
        # reminders-cli emits UTC with a Z, which Python 3.9 cannot read directly.
        parsed = rhs.parse_iso("2026-08-01T12:30:00Z")
        self.assertEqual(parsed.utcoffset(), dt.timedelta(0))

    def test_local_midnight_becomes_all_day(self):
        midnight_utc = (
            dt.datetime(2026, 8, 2, 0, 0)
            .astimezone()
            .astimezone(dt.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        self.assertEqual(rhs.normalize_due(midnight_utc), "2026-08-02")

    def test_timed_due_is_local_naive(self):
        aware = dt.datetime(2026, 8, 1, 14, 30).astimezone()
        self.assertEqual(rhs.normalize_due(aware.isoformat()), "2026-08-01T14:30:00")

    def test_unparseable_due_is_dropped(self):
        self.assertIsNone(rhs.normalize_due("next tuesday-ish"))

    def test_due_payload_forms(self):
        self.assertEqual(rhs.due_payload("2026-08-02"), {"due_date": "2026-08-02"})
        self.assertEqual(rhs.due_payload(None), {"due_date": None})
        payload = rhs.due_payload("2026-08-01T14:30:00")
        # Mutually exclusive with due_date, and offset-qualified so a timezone
        # mismatch between the Mac and Home Assistant cannot shift it.
        self.assertNotIn("due_date", payload)
        self.assertTrue(payload["due_datetime"].startswith("2026-08-01T14:30:00"))
        self.assertRegex(payload["due_datetime"], r"[+-]\d{2}:\d{2}$")


class SeedingTest(unittest.TestCase):
    def test_open_items_are_created_on_both_sides(self):
        plan = rhs.plan_pair(
            [item("r1", "from reminders")],
            [item("h1", "from ha")],
            state(last_sync=None),
            make_config(),
        )
        self.assertEqual([i.title for _, i in plan.ha_creates], ["from reminders"])
        self.assertEqual([i.title for _, i in plan.r_creates], ["from ha"])

    def test_first_sync_ignores_already_completed_items(self):
        # Reminders keeps completed items forever; importing them all would dump
        # years of history into Home Assistant.
        plan = rhs.plan_pair(
            [item("r1", "done ages ago", completed=True, completed_at="2020-01-01T00:00:00Z")],
            [item("h1", "also done", completed=True, completed_at="2020-01-01T00:00:00Z")],
            state(last_sync=None),
            make_config(),
        )
        self.assertEqual(plan.ha_creates, [])
        self.assertEqual(plan.r_creates, [])
        self.assertEqual(plan.r_tombstones, {"r1"})
        self.assertEqual(plan.h_tombstones, {"h1"})

    def test_recently_completed_item_is_imported(self):
        plan = rhs.plan_pair(
            [item("r1", "just done", completed=True, completed_at="2026-07-02T10:00:00Z")],
            [],
            state(last_sync="2026-07-01T00:00:00+02:00"),
            make_config(),
        )
        self.assertEqual([i.title for _, i in plan.ha_creates], ["just done"])
        self.assertEqual(plan.r_tombstones, set())

    def test_stale_completion_is_tombstoned(self):
        plan = rhs.plan_pair(
            [item("r1", "old", completed=True, completed_at="2026-06-01T10:00:00Z")],
            [],
            state(last_sync="2026-07-01T00:00:00+02:00"),
            make_config(),
        )
        self.assertEqual(plan.ha_creates, [])
        self.assertEqual(plan.r_tombstones, {"r1"})

    def test_same_title_items_are_adopted_not_duplicated(self):
        plan = rhs.plan_pair(
            [item("r1", "buy milk")],
            [item("h1", "buy milk")],
            state(last_sync=None),
            make_config(),
        )
        self.assertEqual(plan.ha_creates, [])
        self.assertEqual(plan.r_creates, [])
        self.assertEqual(len(plan.link_writes), 1)
        self.assertEqual(plan.link_writes[0].link, {"r": "r1", "h": "h1", "last": {
            "title": "buy milk", "notes": None, "due": None, "completed": False}})
        self.assertFalse(plan.link_writes[0].has_writes)

    def test_adoption_can_be_turned_off(self):
        plan = rhs.plan_pair(
            [item("r1", "buy milk")],
            [item("h1", "buy milk")],
            state(last_sync=None),
            make_config(match_by_title=False),
        )
        self.assertEqual(len(plan.ha_creates), 1)
        self.assertEqual(len(plan.r_creates), 1)

    def test_adoption_merges_differing_fields_by_policy(self):
        plan = rhs.plan_pair(
            [item("r1", "task", notes="from reminders")],
            [item("h1", "task", notes="from ha")],
            state(last_sync=None),
            make_config(),
        )
        write = writes_for(plan, "task")
        self.assertEqual(write.ha_fields, {"description": "from reminders"})
        self.assertEqual(write.link["last"]["notes"], "from reminders")


class MergeTest(unittest.TestCase):
    def test_title_changed_in_reminders_goes_to_ha(self):
        plan = rhs.plan_pair(
            [item("r1", "new title")],
            [item("h1", "old title")],
            state([link("r1", "h1", title="old title")]),
            make_config(),
        )
        write = writes_for(plan, "new title")
        self.assertEqual(write.ha_fields, {"rename": "new title"})
        self.assertEqual(write.r_update.get("title"), None)
        self.assertEqual(write.link["last"]["title"], "new title")

    def test_title_changed_in_ha_goes_to_reminders(self):
        plan = rhs.plan_pair(
            [item("r1", "old title")],
            [item("h1", "new title")],
            state([link("r1", "h1", title="old title")]),
            make_config(),
        )
        write = writes_for(plan, "old title")
        self.assertEqual(write.ha_fields, {})
        self.assertEqual(write.r_update["title"], "new title")
        self.assertEqual(write.link["last"]["title"], "new title")

    def test_completion_propagates_from_reminders(self):
        plan = rhs.plan_pair(
            [item("r1", "task", completed=True, completed_at="2026-07-02T10:00:00Z")],
            [item("h1", "task")],
            state([link("r1", "h1", title="task")]),
            make_config(),
        )
        self.assertEqual(writes_for(plan, "task").ha_fields, {"status": "completed"})

    def test_uncompletion_propagates_from_ha(self):
        plan = rhs.plan_pair(
            [item("r1", "task", completed=True)],
            [item("h1", "task", completed=False)],
            state([link("r1", "h1", title="task", completed=True)]),
            make_config(),
        )
        write = writes_for(plan, "task")
        self.assertEqual(write.r_update["completed"], False)
        self.assertEqual(write.r_update["was_completed"], True)

    def test_notes_cleared_in_ha(self):
        plan = rhs.plan_pair(
            [item("r1", "task", notes="text")],
            [item("h1", "task", notes=None)],
            state([link("r1", "h1", title="task", notes="text")]),
            make_config(),
        )
        write = writes_for(plan, "task")
        # The key must be present even though the value is None -- that is what
        # tells the executor to clear rather than to leave the notes alone.
        self.assertIn("notes", write.r_update)
        self.assertIsNone(write.r_update["notes"])

    def test_notes_cleared_in_reminders(self):
        plan = rhs.plan_pair(
            [item("r1", "task", notes=None)],
            [item("h1", "task", notes="text")],
            state([link("r1", "h1", title="task", notes="text")]),
            make_config(),
        )
        self.assertEqual(writes_for(plan, "task").ha_fields, {"description": ""})

    def test_identical_change_on_both_sides_is_not_a_conflict(self):
        plan = rhs.plan_pair(
            [item("r1", "same new")],
            [item("h1", "same new")],
            state([link("r1", "h1", title="old")]),
            make_config(),
        )
        write = writes_for(plan, "same new")
        self.assertEqual(plan.conflicts, [])
        self.assertFalse(write.has_writes)
        self.assertEqual(write.link["last"]["title"], "same new")

    def test_nothing_changed_produces_no_writes(self):
        plan = rhs.plan_pair(
            [item("r1", "task", notes="n", due="2026-08-02")],
            [item("h1", "task", notes="n", due="2026-08-02")],
            state([link("r1", "h1", title="task", notes="n", due="2026-08-02")]),
            make_config(),
        )
        self.assertTrue(plan.is_empty())


class ConflictTest(unittest.TestCase):
    def base_plan(self, **config):
        return rhs.plan_pair(
            [item("r1", "reminders wins")],
            [item("h1", "ha wins")],
            state([link("r1", "h1", title="original")]),
            make_config(**config),
        )

    def test_reminders_wins_by_default(self):
        plan = self.base_plan()
        write = writes_for(plan, "reminders wins")
        self.assertEqual(len(plan.conflicts), 1)
        self.assertEqual(write.ha_fields, {"rename": "reminders wins"})
        self.assertEqual(write.link["last"]["title"], "reminders wins")

    def test_ha_can_win(self):
        plan = self.base_plan(conflict_winner="ha")
        write = writes_for(plan, "reminders wins")
        self.assertEqual(write.r_update["title"], "ha wins")
        self.assertEqual(write.link["last"]["title"], "ha wins")

    def test_manual_leaves_both_alone(self):
        plan = self.base_plan(conflict_winner="manual")
        write = writes_for(plan, "reminders wins")
        self.assertFalse(write.has_writes)
        # The snapshot keeps the old value, so the conflict is reported again
        # next run rather than quietly resolving itself.
        self.assertEqual(write.link["last"]["title"], "original")


class DueFromHaTest(unittest.TestCase):
    """reminders-cli cannot write a due date to an existing reminder."""

    def plan_with(self, **config):
        return rhs.plan_pair(
            [item("r1", "task", due="2026-08-02")],
            [item("h1", "task", due="2026-08-09")],
            state([link("r1", "h1", title="task", due="2026-08-02")]),
            make_config(**config),
        )

    def test_revert_pushes_the_reminder_date_back(self):
        plan = self.plan_with()
        write = writes_for(plan, "task")
        self.assertEqual(write.ha_fields, {"due_date": "2026-08-02"})
        self.assertEqual(write.link["last"]["due"], "2026-08-02")
        self.assertEqual(len(plan.warnings), 1)

    def test_ignore_accepts_the_divergence_once(self):
        plan = self.plan_with(due_from_ha="ignore")
        write = writes_for(plan, "task")
        self.assertEqual(write.ha_fields, {})
        # Recording HA's value stops the warning repeating every run.
        self.assertEqual(write.link["last"]["due"], "2026-08-09")

    def test_recreate_rebuilds_the_reminder(self):
        plan = self.plan_with(due_from_ha="recreate")
        self.assertEqual(len(plan.r_recreates), 1)
        old_link, desired = plan.r_recreates[0]
        self.assertEqual(desired.due, "2026-08-09")
        self.assertEqual(desired.title, "task")
        # The stale link must not be persisted; the recreation supplies a new id.
        self.assertTrue(writes_for(plan, "task").superseded)
        # ...and the carried snapshot is the old one, in case the delete fails.
        self.assertEqual(old_link["last"]["due"], "2026-08-02")

    def test_recreate_declines_on_completed_reminders(self):
        plan = rhs.plan_pair(
            [item("r1", "task", due="2026-08-02", completed=True)],
            [item("h1", "task", due="2026-08-09", completed=True)],
            state([link("r1", "h1", title="task", due="2026-08-02", completed=True)]),
            make_config(due_from_ha="recreate"),
        )
        self.assertEqual(plan.r_recreates, [])
        self.assertEqual(writes_for(plan, "task").ha_fields, {"due_date": "2026-08-02"})

    def test_due_set_in_reminders_still_reaches_ha(self):
        plan = rhs.plan_pair(
            [item("r1", "task", due="2026-08-01T14:30:00")],
            [item("h1", "task")],
            state([link("r1", "h1", title="task")]),
            make_config(),
        )
        fields = writes_for(plan, "task").ha_fields
        self.assertIn("due_datetime", fields)
        self.assertEqual(plan.warnings, [])


class DeletionTest(unittest.TestCase):
    def test_deleted_in_reminders_removes_from_ha(self):
        plan = rhs.plan_pair(
            [],
            [item("h1", "task")],
            state([link("r1", "h1", title="task")]),
            make_config(),
        )
        self.assertEqual(plan.ha_remove_uids, ["h1"])
        self.assertEqual(plan.r_creates, [], "must not resurrect the deletion")
        # The link travels with the removal so a failed remove can be retried.
        self.assertEqual(plan.ha_removes[0]["r"], "r1")

    def test_deleted_in_ha_removes_from_reminders(self):
        plan = rhs.plan_pair(
            [item("r1", "task")],
            [],
            state([link("r1", "h1", title="task")]),
            make_config(),
        )
        self.assertEqual(plan.r_removes, ["r1"])
        self.assertEqual(plan.ha_creates, [], "must not resurrect the deletion")

    def test_deleted_on_both_sides_drops_the_link(self):
        plan = rhs.plan_pair([], [], state([link("r1", "h1", title="task")]), make_config())
        self.assertTrue(plan.is_empty())
        self.assertEqual(plan.link_writes, [])

    def test_delete_count_counts_both_directions(self):
        plan = rhs.plan_pair(
            [item("r1", "a")],
            [item("h2", "b")],
            state([link("r1", "hx", title="a"), link("rx", "h2", title="b")]),
            make_config(),
        )
        self.assertEqual(plan.delete_count, 2)


class TombstoneTest(unittest.TestCase):
    def test_tombstoned_item_is_left_alone(self):
        plan = rhs.plan_pair(
            [item("r1", "old", completed=True, completed_at="2020-01-01T00:00:00Z")],
            [],
            state(r_tombstones=["r1"]),
            make_config(),
        )
        self.assertEqual(plan.ha_creates, [])
        self.assertEqual(plan.r_tombstones, {"r1"})

    def test_tombstone_is_dropped_when_the_item_disappears(self):
        plan = rhs.plan_pair([], [], state(r_tombstones=["r1"]), make_config())
        self.assertEqual(plan.r_tombstones, set())

    def test_reopening_a_tombstoned_item_brings_it_back(self):
        plan = rhs.plan_pair(
            [item("r1", "revived", completed=False)],
            [],
            state(r_tombstones=["r1"]),
            make_config(),
        )
        self.assertEqual([i.title for _, i in plan.ha_creates], ["revived"])
        self.assertEqual(plan.r_tombstones, set())


class FakeReminders:
    def __init__(self, names, sources=1):
        self.names = list(names)
        self.sources = sources
        self.created = []

    def lists(self):
        return list(self.names)

    def new_list(self, name, source=None):
        if self.sources > 1 and not source:
            raise rhs.UserError("Multiple sources were found")
        self.created.append(name)
        self.names.append(name)


class FakeHa:
    """Stands in for Home Assistant when testing how lists get paired.

    `slugs` models the one behaviour that matters here and cannot be guessed
    from the name alone: Home Assistant derives a list's internal key by
    transliterating, so two differently-spelled names can collide. Tests that
    care pass the mapping in explicitly.
    """

    def __init__(self, entities, slugs=None):
        self.entities = dict(entities)
        self.slugs = dict(slugs or {})
        self.created = []

    def _key(self, name):
        return self.slugs.get(name, rhs.normalize_name(name))

    def todo_entities(self):
        return dict(self.entities)

    def create_local_todo(self, name):
        taken = {self._key(existing) for existing in self.entities.values()}
        if self._key(name) in taken:
            raise rhs.UserError("already_configured")
        entity_id = "todo.made_%d" % len(self.created)
        self.created.append(name)
        self.entities[entity_id] = name
        return entity_id


class PairingTest(unittest.TestCase):
    """Auto mode is what keeps the config down to a URL and a token."""

    def test_matches_on_name(self):
        pairing = rhs.resolve_pairs(
            make_config(lists="auto"),
            FakeReminders(["Покупки"]),
            FakeHa({"todo.pokupki": "Покупки"}),
        )
        self.assertEqual(pairing.pairs, [("Покупки", "todo.pokupki")])
        self.assertEqual(pairing.problems, [])

    def test_match_ignores_case_and_padding(self):
        # Names are compared with casefold, which is the fold that works for
        # Cyrillic; plain .lower() would be enough for ASCII only.
        pairing = rhs.resolve_pairs(
            make_config(lists="auto"),
            FakeReminders(["  ПОКУПКИ  "]),
            FakeHa({"todo.p": "покупки"}),
        )
        self.assertEqual(pairing.pairs, [("  ПОКУПКИ  ", "todo.p")])

    def test_creates_missing_ha_list(self):
        ha = FakeHa({})
        pairing = rhs.resolve_pairs(make_config(lists="auto"), FakeReminders(["Личное"]), ha)
        self.assertEqual(ha.created, ["Личное"])
        self.assertEqual(pairing.pairs, [("Личное", "todo.made_0")])

    def test_creates_missing_reminders_list(self):
        reminders = FakeReminders([])
        pairing = rhs.resolve_pairs(
            make_config(lists="auto"), reminders, FakeHa({"todo.groceries": "Groceries"})
        )
        self.assertEqual(reminders.created, ["Groceries"])
        self.assertEqual(pairing.pairs, [("Groceries", "todo.groceries")])

    def test_create_missing_can_be_limited_to_one_side(self):
        reminders = FakeReminders(["Личное"])
        ha = FakeHa({"todo.groceries": "Groceries"})
        pairing = rhs.resolve_pairs(
            make_config(lists="auto", create_missing="ha"), reminders, ha
        )
        self.assertEqual(ha.created, ["Личное"])
        self.assertEqual(reminders.created, [])
        self.assertEqual([p[0] for p in pairing.pairs], ["Личное"])

    def test_create_missing_none_creates_nothing(self):
        reminders = FakeReminders(["Личное"])
        ha = FakeHa({"todo.groceries": "Groceries"})
        pairing = rhs.resolve_pairs(
            make_config(lists="auto", create_missing="none"), reminders, ha
        )
        self.assertEqual((ha.created, reminders.created, pairing.pairs), ([], [], []))

    def test_create_false_reports_without_touching_anything(self):
        ha = FakeHa({})
        pairing = rhs.resolve_pairs(
            make_config(lists="auto"), FakeReminders(["Личное"]), ha, create=False
        )
        self.assertEqual(ha.created, [])
        self.assertEqual(pairing.pairs, [])
        self.assertIn("would create in Home Assistant", [n for _, _, n in pairing.plan])

    def test_exclude_by_reminders_name(self):
        pairing = rhs.resolve_pairs(
            make_config(lists="auto", exclude=["Movies"]),
            FakeReminders(["Movies", "Личное"]),
            FakeHa({"todo.movies": "Movies", "todo.lichnoe": "Личное"}),
        )
        self.assertEqual(pairing.pairs, [("Личное", "todo.lichnoe")])

    def test_exclude_by_entity_id(self):
        reminders = FakeReminders([])
        pairing = rhs.resolve_pairs(
            make_config(lists="auto", exclude=["todo.shopping_list"]),
            reminders,
            FakeHa({"todo.shopping_list": "Shopping List"}),
        )
        self.assertEqual(pairing.pairs, [])
        self.assertEqual(reminders.created, [], "must not create an excluded list")

    def test_excluded_list_does_not_return_via_the_other_direction(self):
        # "Movies" is excluded, so the Home Assistant list of the same name must
        # not be treated as a stray that needs a Reminders list creating.
        reminders = FakeReminders(["Movies"])
        pairing = rhs.resolve_pairs(
            make_config(lists="auto", exclude=["Movies"]),
            reminders,
            FakeHa({"todo.movies": "Movies"}),
        )
        self.assertEqual(pairing.pairs, [])
        self.assertEqual(reminders.created, [])

    def test_ambiguous_name_is_reported_not_guessed(self):
        pairing = rhs.resolve_pairs(
            make_config(lists="auto"),
            FakeReminders(["Shopping"]),
            FakeHa({"todo.a": "Shopping", "todo.b": "Shopping"}),
        )
        self.assertEqual(pairing.pairs, [])
        self.assertEqual(len(pairing.problems), 1)
        self.assertIn("explicitly", pairing.problems[0])

    def test_transliteration_collision_surfaces_as_a_problem(self):
        # Home Assistant slugifies by transliterating, so "Тест" and "Test"
        # fight over the same internal key.
        ha = FakeHa({"todo.test": "Test"}, slugs={"Тест": "test", "Test": "test"})
        pairing = rhs.resolve_pairs(
            make_config(lists="auto"), FakeReminders(["Тест", "Test"]), ha
        )
        self.assertEqual(pairing.pairs, [("Test", "todo.test")])
        self.assertEqual(ha.created, [], "the colliding list must not be created")
        self.assertEqual(len(pairing.problems), 1)

    def test_multiple_accounts_needs_a_source(self):
        pairing = rhs.resolve_pairs(
            make_config(lists="auto"),
            FakeReminders([], sources=2),
            FakeHa({"todo.groceries": "Groceries"}),
        )
        self.assertEqual(pairing.pairs, [])
        self.assertEqual(len(pairing.problems), 1)

    def test_explicit_mode_needs_both_sides_to_exist(self):
        pairing = rhs.resolve_pairs(
            make_config(lists=[{"reminders": "Личное", "ha": "todo.nope"}]),
            FakeReminders(["Личное"]),
            FakeHa({"todo.other": "Other"}),
        )
        self.assertEqual(pairing.pairs, [])
        self.assertEqual(len(pairing.problems), 1)

    def test_explicit_mode_never_creates(self):
        reminders = FakeReminders(["Личное"])
        ha = FakeHa({"todo.lichnoe": "Личное"})
        rhs.resolve_pairs(
            make_config(lists=[{"reminders": "Личное", "ha": "todo.lichnoe"}]),
            reminders,
            ha,
        )
        self.assertEqual((reminders.created, ha.created), ([], []))


class FakeProc:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class RemindersAccessTest(unittest.TestCase):
    """A refused read must never pass for a Mac with nothing on it.

    reminders-cli has three ways of saying the same thing and only one of them
    looks like an error, which is how a permission problem spent an afternoon
    masquerading as an empty list of lists.
    """

    def reminders(self) -> rhs.Reminders:
        return rhs.Reminders(binary="/nowhere/reminders")

    def run_returning(self, proc: FakeProc):
        """Swap out subprocess.run for one canned result, restored after."""
        real = rhs.subprocess.run
        rhs.subprocess.run = lambda *args, **kwargs: proc
        self.addCleanup(setattr, rhs.subprocess, "run", real)

    def test_no_lists_is_an_access_error(self):
        reminders = self.reminders()
        reminders._run_json = lambda args: []
        with self.assertRaises(rhs.RemindersAccessError):
            reminders.lists()

    def test_real_lists_come_back_unchanged(self):
        reminders = self.reminders()
        reminders._run_json = lambda args: ["Покупки", "Movies"]
        self.assertEqual(reminders.lists(), ["Покупки", "Movies"])

    def test_the_hint_names_the_signature_check(self):
        # The one thing that separates "works in a terminal, not under
        # launchd" from every other cause, so it has to be in the message.
        hint = self.reminders().access_hint()
        self.assertIn("codesign --verify", hint)
        self.assertIn("codesign --force --sign -", hint)

    def test_signature_check_holds_its_tongue_about_a_missing_binary(self):
        ok, detail = rhs.signature_check("/nowhere/reminders")
        self.assertTrue(ok)
        self.assertNotIn("codesign --force", detail)

    def test_missing_list_source_is_an_access_error(self):
        self.run_returning(
            FakeProc(1, "", "No existing list sources were found, please create a list in Reminders.app")
        )
        with self.assertRaises(rhs.RemindersAccessError):
            self.reminders().new_list("Movies")

    def test_ungranted_access_is_an_access_error(self):
        self.run_returning(FakeProc(1, "", "error: failed to grant reminders access"))
        with self.assertRaises(rhs.RemindersAccessError):
            self.reminders().lists()

    def test_an_ordinary_failure_stays_an_ordinary_failure(self):
        self.run_returning(FakeProc(1, "", "no reminder at index 4"))
        with self.assertRaises(rhs.UserError) as caught:
            self.reminders().lists()
        self.assertNotIsInstance(caught.exception, rhs.RemindersAccessError)


class LegacyLaunchAgentTest(unittest.TestCase):
    """Renaming the label must not leave the previous agent behind.

    An orphan stays loaded on its own schedule and syncs the same pairs out of
    the same state file, which is a worse failure than never renaming it.
    """

    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.home = home.name
        self.saved_home = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        self.addCleanup(self.restore_home)
        os.makedirs(os.path.join(self.home, "Library", "LaunchAgents"))

        # launchctl is not something a unit test should be reaching for.
        self.commands = []
        real = rhs.subprocess.run

        def record(args, **kwargs):
            self.commands.append(list(args))
            return FakeProc(0)

        rhs.subprocess.run = record
        self.addCleanup(setattr, rhs.subprocess, "run", real)

    def restore_home(self):
        if self.saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.saved_home

    def legacy_path(self) -> str:
        return rhs.launch_agent_path(rhs.LEGACY_LAUNCH_LABELS[0])

    def uninstall(self) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(rhs.cmd_uninstall(), 0)
        return out.getvalue()

    def test_uninstall_removes_a_legacy_agent(self):
        path = self.legacy_path()
        open(path, "w").close()
        self.assertIn(path, self.uninstall())
        self.assertFalse(os.path.exists(path))

    def test_install_drops_a_legacy_agent(self):
        path = self.legacy_path()
        open(path, "w").close()
        rhs.drop_legacy_launch_agents()
        self.assertFalse(os.path.exists(path))
        self.assertIn(
            [
                "launchctl",
                "bootout",
                "gui/%d/%s" % (os.getuid(), rhs.LEGACY_LAUNCH_LABELS[0]),
            ],
            self.commands,
        )

    def test_nothing_installed_is_not_a_failure(self):
        self.assertIn("nothing to remove", self.uninstall())

    def test_the_current_label_is_not_in_the_legacy_list(self):
        # Listing it there would have install boot out what it just wrote.
        self.assertNotIn(rhs.LAUNCH_LABEL, rhs.LEGACY_LAUNCH_LABELS)


class LogRoutingTest(unittest.TestCase):
    """The LaunchAgent redirects stderr into the log file, so a stream handler
    on top of the file handler writes every line twice."""

    def isolate_log(self) -> None:
        saved, level = list(rhs.LOG.handlers), rhs.LOG.level
        rhs.LOG.handlers = []

        def restore():
            for handler in rhs.LOG.handlers:
                handler.close()
            rhs.LOG.handlers = saved
            rhs.LOG.setLevel(level)

        self.addCleanup(restore)

    def test_same_file_seen_through_a_redirect(self):
        with tempfile.NamedTemporaryFile() as fh:
            self.assertTrue(rhs.is_same_file(fh.fileno(), fh.name))

    def test_different_files_are_not_confused(self):
        with tempfile.NamedTemporaryFile() as one, tempfile.NamedTemporaryFile() as two:
            self.assertFalse(rhs.is_same_file(one.fileno(), two.name))

    def test_a_path_that_is_not_there_is_not_the_same_file(self):
        with tempfile.NamedTemporaryFile() as fh:
            self.assertFalse(rhs.is_same_file(fh.fileno(), fh.name + ".gone"))

    def test_stderr_on_the_log_file_gets_one_handler(self):
        self.isolate_log()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sync.log")
            saved_fd = os.dup(2)
            try:
                with open(path, "a") as fh:
                    os.dup2(fh.fileno(), 2)
                rhs.setup_logging("info", path)
            finally:
                os.dup2(saved_fd, 2)
                os.close(saved_fd)
            self.assertEqual(
                [type(h).__name__ for h in rhs.LOG.handlers], ["RotatingFileHandler"]
            )

    def test_stderr_elsewhere_still_gets_the_stream_handler(self):
        self.isolate_log()
        with tempfile.TemporaryDirectory() as tmp:
            rhs.setup_logging("info", os.path.join(tmp, "sync.log"))
            self.assertEqual(
                [type(h).__name__ for h in rhs.LOG.handlers],
                ["RotatingFileHandler", "StreamHandler"],
            )


class ConfigTest(unittest.TestCase):
    def test_auto_is_the_default(self):
        config = rhs.Config(
            {"home_assistant": {"url": "http://ha.local:8123", "token": "t"}}, "<test>"
        )
        self.assertEqual(config.list_mode, "auto")
        self.assertEqual(config.create_missing, "both")

    def test_rejects_non_todo_entity(self):
        with self.assertRaises(rhs.UserError):
            make_config(lists=[{"reminders": "L", "ha": "sensor.nope"}])

    def test_rejects_unknown_list_mode(self):
        with self.assertRaises(rhs.UserError):
            make_config(lists="everything")

    def test_rejects_unknown_create_missing(self):
        with self.assertRaises(rhs.UserError):
            make_config(create_missing="sometimes")

    def test_rejects_url_without_scheme(self):
        with self.assertRaises(rhs.UserError):
            make_config(home_assistant={"url": "ha.local:8123", "token": "t"})

    def test_rejects_empty_token(self):
        with self.assertRaises(rhs.UserError):
            make_config(home_assistant={"url": "http://ha.local:8123", "token": ""})

    def test_rejects_unknown_conflict_winner(self):
        with self.assertRaises(rhs.UserError):
            make_config(conflict_winner="coin flip")

    def test_token_can_come_from_the_environment(self):
        os.environ["RHS_HA_TOKEN"] = "from-env"
        try:
            config = make_config(home_assistant={"url": "http://ha.local:8123", "token": ""})
            self.assertEqual(config.ha_token, "from-env")
        finally:
            del os.environ["RHS_HA_TOKEN"]


class CliFormattingTest(unittest.TestCase):
    def test_all_day_stays_date_only(self):
        # NSDataDetector decides all-day vs timed from whether a time is
        # present, so this must not grow a 00:00.
        self.assertEqual(rhs.format_due_for_cli("2026-08-02"), "2026-08-02")

    def test_timed_uses_a_space_and_drops_seconds(self):
        self.assertEqual(
            rhs.format_due_for_cli("2026-08-01T14:30:00"), "2026-08-01 14:30"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
