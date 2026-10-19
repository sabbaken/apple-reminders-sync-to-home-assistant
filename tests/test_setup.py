"""Setup credential handling, with no live HA or macOS access."""
import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, call, patch

import reminders_ha_sync as rhs


class SetupTokenTest(unittest.TestCase):
    def run_setup(self, answers=("",), addresses=None, failures=(), **options):
        args = argparse.Namespace(
            features="battery", yes=options.get("yes", False),
            url=options.get("url"), token=options.get("token"),
            reminders_binary=None, interval=600, battery_interval=300,
            no_sync=True, no_install=False,
        )
        previous = options.get("previous", {
            "url": "http://ha.invalid:8123", "token": "saved-token",
        })
        clients = []
        for failure in list(failures) + [None]:
            client = Mock()
            client.ping.side_effect = failure
            client.is_admin.return_value = True
            clients.append(client)
        addresses = iter(addresses) if addresses is not None else None

        def ask(question, default=""):
            if question == "Home Assistant address" and addresses is not None:
                return next(addresses)
            return default

        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            if previous:
                with open(path, "w") as stream:
                    json.dump({"home_assistant": previous}, stream)
            with contextlib.ExitStack() as stack:
                stack.enter_context(contextlib.redirect_stdout(output))
                stack.enter_context(patch.dict(os.environ, options.get("env", {}), clear=True))
                prompt = stack.enter_context(patch.object(rhs, "ask_secret", side_effect=answers))
                question = stack.enter_context(patch.object(rhs, "ask", side_effect=ask))
                ha = stack.enter_context(patch.object(rhs, "HomeAssistant", side_effect=clients))
                stack.enter_context(patch.object(rhs, "collect_battery_devices", return_value=[]))
                result = rhs.cmd_setup(path, args)
            with open(path) as stream:
                written = json.load(stream)
        self.assertEqual(result, 0)
        for credential in ("saved-token", "new-token", "env-token", "argument-token"):
            self.assertNotIn(credential, output.getvalue())
        return ha.call_args_list, written["home_assistant"], prompt, question

    def test_enter_keeps_saved_token(self):
        calls, saved, prompt, _ = self.run_setup()
        self.assertEqual(calls, [call("http://ha.invalid:8123", "saved-token")])
        self.assertEqual(saved["token"], "saved-token")
        self.assertIn("Enter to keep saved token", prompt.call_args.args[0])

    def test_can_replace_saved_token(self):
        calls, saved, _, _ = self.run_setup(answers=("new-token",))
        self.assertEqual(calls[0].args[1], "new-token")
        self.assertEqual(saved["token"], "new-token")

    def test_explicit_credentials_take_precedence(self):
        for options, expected in [
            ({"env": {"RHS_HA_TOKEN": "env-token"}}, "env-token"),
            ({"token": "argument-token", "env": {"RHS_HA_TOKEN": "env-token"}}, "argument-token"),
        ]:
            with self.subTest(options=options):
                calls, saved, prompt, _ = self.run_setup(answers=(), **options)
                self.assertEqual(calls[0].args[1], expected)
                self.assertEqual(saved["token"], expected)
                prompt.assert_not_called()

    def test_yes_reuses_saved_address_and_token_without_prompting(self):
        calls, _, prompt, question = self.run_setup(answers=(), yes=True)
        self.assertEqual(calls, [call("http://ha.invalid:8123", "saved-token")])
        prompt.assert_not_called()
        question.assert_not_called()

    def test_rejected_token_requires_replacement(self):
        calls, saved, prompt, _ = self.run_setup(
            answers=("", "new-token"),
            failures=(rhs.UserError("Home Assistant rejected the token (401)"),),
        )
        self.assertEqual([entry.args[1] for entry in calls], ["saved-token", "new-token"])
        self.assertEqual(saved["token"], "new-token")
        self.assertEqual(prompt.call_args.args, ("Token",))

    def test_new_address_requires_its_own_token(self):
        calls, _, prompt, _ = self.run_setup(
            url="http://other.invalid:8123", answers=("new-token",),
        )
        self.assertEqual(calls, [call("http://other.invalid:8123", "new-token")])
        self.assertEqual(prompt.call_args.args, ("Token",))

    def test_retry_at_new_address_does_not_forward_saved_token(self):
        calls, _, _, _ = self.run_setup(
            answers=("", "new-token"),
            addresses=("http://ha.invalid:8123", "http://other.invalid:8123"),
            failures=(rhs.UserError("Connection refused"),),
        )
        self.assertEqual(calls, [call("http://ha.invalid:8123", "saved-token"),
                                call("http://other.invalid:8123", "new-token")])

    def test_trailing_slash_does_not_require_a_new_token(self):
        calls, _, _, _ = self.run_setup(url="http://ha.invalid:8123/")
        self.assertEqual(calls[0].args[1], "saved-token")

    def test_first_setup_requires_a_token(self):
        calls, saved, prompt, _ = self.run_setup(previous={}, answers=("new-token",))
        self.assertEqual(calls[0].args[1], "new-token")
        self.assertEqual(saved["token"], "new-token")
        self.assertEqual(prompt.call_args.args, ("Token",))


if __name__ == "__main__":
    unittest.main()
